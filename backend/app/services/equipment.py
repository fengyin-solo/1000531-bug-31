"""勘探设备业务规则：仪器状态机、校准计划与领用待办的事务回写。

设计约束（对应修复要求）：
- 设备台账是唯一事实源，每次流转 +version；校准计划、领用待办只允许由状态机回写。
- 同型号仪器以「仪器编号」为唯一依据；历史领用关系（上次经办人）不在归还时被覆盖。
- 每次流转校验前置状态，三表在同一事务内提交，失败整体回滚。
- request_id 保证重复提交幂等；expected_version 做乐观锁，挡住并发/过期写入。
- 存量状态不明的仪器统一迁移到「待核验」节点，检定通过后才允许流转。
"""
from __future__ import annotations

import copy
import threading
import uuid
from datetime import date
from typing import Any

from app.store import store

MODULE = "equipment"
REQUIRED_FIELDS = ["仪器编号", "仪器名称", "型号规格"]

# 状态机节点。待检定/已报废是历史叫法，启动迁移时归一到待核验/已停用。
STATUS_PENDING = "待核验"
STATUS_AVAILABLE = "在库可用"
STATUS_BORROWED = "借出使用"
STATUS_REPAIR = "维修中"
STATUS_RETIRED = "已停用"
LEGACY_STATUS = {"待检定": STATUS_PENDING, "已报废": STATUS_RETIRED}
KNOWN_STATUSES = [STATUS_PENDING, STATUS_AVAILABLE, STATUS_BORROWED, STATUS_REPAIR, STATUS_RETIRED]

# 动作 -> 可执行的前置状态集合；目标状态由各动作处理器结合领用关系决定。
ACTION_INSPECT = "送检检定"
ACTION_BORROW = "领用仪器"
ACTION_REPAIR = "报修仪器"
ACTION_REPAIR_DONE = "检修完成"
ACTION_RETURN = "归还仪器"
ACTION_RETIRE = "停用仪器"
ACTION_PRECONDITIONS: dict[str, set[str]] = {
    ACTION_INSPECT: {STATUS_PENDING},
    ACTION_BORROW: {STATUS_AVAILABLE},
    ACTION_REPAIR: {STATUS_AVAILABLE, STATUS_BORROWED},
    ACTION_REPAIR_DONE: {STATUS_REPAIR},
    ACTION_RETURN: {STATUS_BORROWED},
    ACTION_RETIRE: {STATUS_AVAILABLE, STATUS_REPAIR},
}
ACTIONS = list(ACTION_PRECONDITIONS)

# 校准计划 / 领用待办状态
PLAN_PENDING = "待检定"
PLAN_PASSED = "检定合格"
PLAN_FAILED = "检定不合格"
PLAN_CANCELLED = "已取消"
TODO_OPEN = "待领用"
TODO_CLOSED = "已归还"
TODO_CANCELLED = "已关闭"

TODAY = date(2026, 9, 30).isoformat()


class EquipmentError(Exception):
    """业务校验失败：携带稳定 code，路由层据此返回可读说明。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _new_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _one_year_after(value: str) -> str:
    """检定有效期默认按一年滚动；日期非法时回落到固定基准日，避免脏数据写回。"""
    try:
        parsed = date.fromisoformat(value)
        return parsed.replace(year=parsed.year + 1).isoformat()
    except ValueError:
        return date(2027, 9, 30).isoformat()


class EquipmentService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._plans: list[dict[str, Any]] = []
        self._todos: list[dict[str, Any]] = []
        # request_id -> 首次请求的结果（成功或失败都登记，重复提交原样返回）
        self._idempotency: dict[str, dict[str, Any]] = {}
        self._migrated = False
        self.bootstrap()

    # ------------------------------------------------------------------ 迁移

    def reset(self) -> None:
        """测试隔离：恢复种子台账并清空派生表后重新迁移。"""
        with self._lock:
            store.reset_to_seed(MODULE)
            self._plans = []
            self._todos = []
            self._idempotency = {}
            self._migrated = False
            self.bootstrap()

    def bootstrap(self) -> None:
        """存量数据一次性迁移：状态归一、版本补齐、计划/待办按当前状态派生。"""
        with self._lock:
            if self._migrated:
                return
            ledger = store.rows(MODULE)
            seen_serials: set[str] = set()
            for entry in ledger:
                raw_status = str(entry.get("status") or "").strip()
                status = LEGACY_STATUS.get(raw_status, raw_status if raw_status in KNOWN_STATUSES else STATUS_PENDING)
                entry["status"] = status
                serial = str(entry.get("仪器编号") or "").strip()
                if serial:
                    seen_serials.add(serial)
                entry.setdefault("仪器编号", serial)
                entry["version"] = int(entry.get("version") or 1)
                if status == STATUS_BORROWED:
                    holder = str(entry.get("使用人员") or "").strip() or "历史经办人"
                    entry["使用人员"] = holder
                    entry["上次经办人"] = entry.get("上次经办人") or holder
                else:
                    entry["上次经办人"] = entry.get("上次经办人") or ""
                entry["仪器状态"] = status
                entry["pending"] = status in {STATUS_PENDING, STATUS_BORROWED, STATUS_REPAIR}
                entry["abnormal"] = status == STATUS_REPAIR

                plan_status = {
                    STATUS_PENDING: PLAN_PENDING,
                    STATUS_REPAIR: PLAN_PENDING,
                    STATUS_AVAILABLE: PLAN_PASSED,
                    STATUS_BORROWED: PLAN_PASSED,
                    STATUS_RETIRED: PLAN_CANCELLED,
                }[status]
                self._plans.append({
                    "id": _new_id(self._plans),
                    "仪器编号": serial,
                    "plan_status": plan_status,
                    "ledger_version": entry["version"],
                    "检定日期": entry.get("检定日期") or "",
                    "有效期至": entry.get("有效期至") or "",
                    "备注": "存量迁移派生" if plan_status == PLAN_PENDING else "存量迁移归档",
                })
                if status == STATUS_BORROWED:
                    self._todos.append({
                        "id": _new_id(self._todos),
                        "仪器编号": serial,
                        "todo_status": TODO_OPEN,
                        "经办人": entry.get("使用人员") or "历史经办人",
                        "ledger_version": entry["version"],
                        "领用日期": TODAY,
                        "归还日期": "",
                    })
            self._migrated = True

    # ------------------------------------------------------------------ 读取

    def _find_by_id(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def _find_by_serial(self, serial: str) -> list[dict[str, Any]]:
        return [row for row in store.rows(MODULE) if str(row.get("仪器编号") or "").strip() == serial]

    def _plans_for(self, serial: str) -> list[dict[str, Any]]:
        return [plan for plan in self._plans if plan["仪器编号"] == serial]

    def _todos_for(self, serial: str) -> list[dict[str, Any]]:
        return [todo for todo in self._todos if todo["仪器编号"] == serial]

    def _active_plan(self, serial: str) -> dict[str, Any] | None:
        plans = [p for p in self._plans_for(serial) if p["plan_status"] != PLAN_CANCELLED]
        return plans[-1] if plans else None

    def _open_todo(self, serial: str) -> dict[str, Any] | None:
        for todo in reversed(self._todos_for(serial)):
            if todo["todo_status"] == TODO_OPEN:
                return todo
        return None

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = [copy.deepcopy(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("仪器编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            entry = self._find_by_id(entry_id)
            return copy.deepcopy(entry) if entry else None

    def get_detail(self, entry_id: int) -> dict[str, Any] | None:
        """详情读口径：台账、计划、待办在同一快照下组装，版本必然一致。"""
        with self._lock:
            entry = self._find_by_id(entry_id)
            if entry is None:
                return None
            return self._detail(entry)

    def _stats(self) -> dict[str, int]:
        rows = store.rows(MODULE)
        return {
            "total": len(rows),
            "在库可用": sum(1 for r in rows if r["status"] == STATUS_AVAILABLE),
            "借出使用": sum(1 for r in rows if r["status"] == STATUS_BORROWED),
            "维修中": sum(1 for r in rows if r["status"] == STATUS_REPAIR),
            "待核验": sum(1 for r in rows if r["status"] == STATUS_PENDING),
            "已停用": sum(1 for r in rows if r["status"] == STATUS_RETIRED),
            "待校准": sum(1 for p in self._plans if p["plan_status"] == PLAN_PENDING),
            "待办未归还": sum(1 for t in self._todos if t["todo_status"] == TODO_OPEN),
        }

    def _detail(self, entry: dict[str, Any]) -> dict[str, Any]:
        serial = str(entry.get("仪器编号") or "")
        duplicate = len(self._find_by_serial(serial)) > 1
        allowed = [] if duplicate else [
            action for action, states in ACTION_PRECONDITIONS.items() if entry["status"] in states
        ]
        return {
            "equipment": copy.deepcopy(entry),
            "allowed_actions": allowed,
            "active_plan": copy.deepcopy(self._active_plan(serial)),
            "plans": copy.deepcopy(self._plans_for(serial)),
            "open_todo": copy.deepcopy(self._open_todo(serial)),
            "todos": copy.deepcopy(self._todos_for(serial)),
        }

    def workspace(
        self, *, keyword: str | None = None, status: str | None = None, page: int = 1, size: int = 20
    ) -> dict[str, Any]:
        """侧栏与详情的唯一读口径：同一份载荷、同一个台账版本。"""
        with self._lock:
            version = max((int(r.get("version", 1)) for r in store.rows(MODULE)), default=0)
            page_rows, total = self.list_entries(keyword=keyword, status=status, page=page, size=size)
            details = [self._detail(row) for row in page_rows]
            todos = [t for t in self._todos if t["todo_status"] == TODO_OPEN]
            plans = [p for p in self._plans if p["plan_status"] == PLAN_PENDING]
            return {
                "version": version,
                "page": page,
                "size": size,
                "total": total,
                "stats": self._stats(),
                "items": details,
                "todos": copy.deepcopy(sorted(todos, key=lambda t: t["id"], reverse=True)),
                "plans": copy.deepcopy(sorted(plans, key=lambda p: p["id"], reverse=True)),
                "statuses": list(KNOWN_STATUSES),
                "actions": list(ACTIONS),
            }

    # ------------------------------------------------------------------ 登记

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        serial = str(values["仪器编号"]).strip()
        with self._lock:
            if self._find_by_serial(serial):
                return None, [f"仪器编号「{serial}」已存在，同型号仪器以仪器编号为唯一依据"]
            rows = store.rows(MODULE)
            entry = {"id": _new_id(rows)}
            entry.update({field: str(values[field]).strip() for field in REQUIRED_FIELDS})
            for optional in ("精度指标", "检定日期", "有效期至"):
                if str(values.get(optional) or "").strip():
                    entry[optional] = str(values[optional]).strip()
            entry.update({
                "status": STATUS_PENDING,
                "仪器状态": STATUS_PENDING,
                "pending": True,
                "abnormal": False,
                "version": 1,
                "使用人员": "",
                "上次经办人": "",
            })
            rows.append(entry)
            self._plans.append({
                "id": _new_id(self._plans),
                "仪器编号": serial,
                "plan_status": PLAN_PENDING,
                "ledger_version": 1,
                "检定日期": entry.get("检定日期", ""),
                "有效期至": entry.get("有效期至", ""),
                "备注": "新登记首次检定",
            })
            return copy.deepcopy(entry), []

    # ---------------------------------------------------------- 事务与状态机

    def _tx(self, mutate) -> Any:
        """三表事务：先在快照外拿锁，深拷贝快照；异常时整体回滚。"""
        with self._lock:
            snapshot = (
                copy.deepcopy(store.rows(MODULE)),
                copy.deepcopy(self._plans),
                copy.deepcopy(self._todos),
            )
            try:
                result = mutate()
            except Exception:
                ledger, plans, todos = snapshot
                store.replace_rows(MODULE, ledger)
                self._plans[:] = plans
                self._todos[:] = todos
                raise
            return result

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        values = values or {}
        if not request_id:
            request_id = uuid.uuid4().hex
        with self._lock:
            cached = self._idempotency.get(request_id)
            if cached is not None:
                # request_id 只服务于同一次提交（同一设备、同一动作）；换了动作就拒绝，防止拿旧令牌写新操作。
                if cached.get("entry_id") != entry_id or cached.get("action") != action:
                    raise EquipmentError(
                        "idempotency_conflict",
                        f"请求编号 {request_id} 已用于另一笔操作，请重新发起",
                    )
                # 重复提交：成功结果原样回放；失败结论以同一 code 再抛一次，绝不再次写台账。
                replay = cached["result"]
                if not replay["ok"]:
                    raise EquipmentError(replay["code"], replay["message"])
                return copy.deepcopy(replay)
            try:
                result = self._tx(lambda: self._advance(entry_id, action, values, expected_version))
            except EquipmentError as exc:
                self._idempotency[request_id] = {
                    "entry_id": entry_id,
                    "action": action,
                    "result": {"ok": False, "code": exc.code, "message": exc.message},
                }
                raise
            payload = {"ok": True, "code": "ok", "message": result["message"], "entry": result["entry"]}
            self._idempotency[request_id] = {"entry_id": entry_id, "action": action, "result": payload}
            return copy.deepcopy(payload)

    def _advance(
        self, entry_id: int, action: str, values: dict[str, Any], expected_version: int | None
    ) -> dict[str, Any]:
        entry = self._find_by_id(entry_id)
        if entry is None:
            raise EquipmentError("not_found", f"勘探仪器 {entry_id} 不存在或已归档")
        if action not in ACTION_PRECONDITIONS:
            raise EquipmentError("bad_action", f"动作「{action}」不属于勘探设备可执行范围")

        serial = str(values.get("仪器编号") or "").strip()
        entry_serial = str(entry.get("仪器编号") or "").strip()
        if serial and serial != entry_serial:
            # 提交体里的编号与台账不一致，说明请求打到了错误设备（跳步/并发串台）。
            raise EquipmentError(
                "serial_mismatch",
                f"仪器编号「{serial}」与台账记录「{entry_serial}」不一致，禁止写入",
            )
        if len(self._find_by_serial(entry_serial)) > 1:
            raise EquipmentError("duplicate_serial", f"仪器编号「{entry_serial}」存在重复台账，须先人工去重再流转")
        if expected_version is not None and int(expected_version) != int(entry.get("version", 1)):
            raise EquipmentError(
                "version_conflict",
                f"台账已更新到第 {entry.get('version')} 版，请基于当前版本重新提交",
            )

        current = entry["status"]
        if current not in ACTION_PRECONDITIONS[action]:
            allowed = "、".join(sorted(ACTION_PRECONDITIONS[action]))
            raise EquipmentError(
                "illegal_transition",
                f"「{current}」状态不允许执行「{action}」，该动作仅允许前置状态：{allowed}",
            )

        handler = getattr(self, f"_do_{action}")
        message = handler(entry, values)
        entry["version"] = int(entry.get("version", 1)) + 1
        entry["仪器状态"] = entry["status"]
        entry["pending"] = entry["status"] in {STATUS_PENDING, STATUS_BORROWED, STATUS_REPAIR}
        entry["abnormal"] = entry["status"] == STATUS_REPAIR
        return {"message": message, "entry": copy.deepcopy(entry)}

    # ---------------------------------------------------------------- 动作实现

    def _bump_plan(self, entry: dict[str, Any], plan_status: str, *, note: str, inspect: bool = False) -> None:
        serial = str(entry["仪器编号"])
        plan = self._active_plan(serial)
        if plan is None:
            plan = {"id": _new_id(self._plans), "仪器编号": serial}
            self._plans.append(plan)
        if inspect:
            inspect_date = str(entry.get("检定日期") or TODAY)
            entry["检定日期"] = inspect_date
            entry["有效期至"] = _one_year_after(inspect_date)
        plan.update({
            "plan_status": plan_status,
            "ledger_version": int(entry["version"]) + 1,
            "检定日期": entry.get("检定日期", ""),
            "有效期至": entry.get("有效期至", ""),
            "备注": note,
        })

    def _do_送检检定(self, entry: dict[str, Any], values: dict[str, Any]) -> str:
        result = str(values.get("检定结论") or PLAN_PASSED).strip()
        if result not in {PLAN_PASSED, PLAN_FAILED}:
            raise EquipmentError("bad_payload", "检定结论只支持「检定合格 / 检定不合格」")
        inspect_date = str(values.get("检定日期") or "").strip()
        if inspect_date:
            entry["检定日期"] = inspect_date
        if result == PLAN_PASSED:
            entry["status"] = STATUS_AVAILABLE
            self._bump_plan(entry, PLAN_PASSED, note="检定合格入在库", inspect=True)
            return "检定合格，仪器已转入在库可用"
        entry["status"] = STATUS_REPAIR
        self._bump_plan(entry, PLAN_FAILED, note="检定不合格转检修", inspect=True)
        return "检定不合格，仪器已转入维修中"

    def _do_领用仪器(self, entry: dict[str, Any], values: dict[str, Any]) -> str:
        operator = str(values.get("经办人") or values.get("使用人员") or "").strip()
        if not operator:
            raise EquipmentError("bad_payload", "领用仪器必须登记经办人")
        if self._open_todo(str(entry["仪器编号"])):
            raise EquipmentError("illegal_transition", "该仪器存在未归还的领用待办，不能重复领用")
        entry["status"] = STATUS_BORROWED
        entry["使用人员"] = operator
        self._todos.append({
            "id": _new_id(self._todos),
            "仪器编号": str(entry["仪器编号"]),
            "todo_status": TODO_OPEN,
            "经办人": operator,
            "ledger_version": int(entry["version"]) + 1,
            "领用日期": TODAY,
            "归还日期": "",
        })
        plan = self._active_plan(str(entry["仪器编号"]))
        if plan:
            plan["ledger_version"] = int(entry["version"]) + 1
        return f"仪器已领用，经办人：{operator}"

    def _do_报修仪器(self, entry: dict[str, Any], values: dict[str, Any]) -> str:
        entry["status"] = STATUS_REPAIR
        # 借出期间报修：领用关系与经办人原样保留，待办不关闭。
        self._bump_plan(entry, PLAN_PENDING, note="报修后须重新检定")
        return "仪器已报修，进入维修中"

    def _do_检修完成(self, entry: dict[str, Any], values: dict[str, Any]) -> str:
        serial = str(entry["仪器编号"])
        open_todo = self._open_todo(serial)
        self._bump_plan(entry, PLAN_PENDING, note="检修完成待复检")
        if open_todo:
            # 带领用关系检修：仪器回到领用人手上，历史经办人不变。
            entry["status"] = STATUS_BORROWED
            entry["使用人员"] = open_todo["经办人"]
            return "检修完成，仪器回到领用人手上，待复检后方可归还"
        entry["status"] = STATUS_PENDING
        return "检修完成，仪器进入待核验节点"

    def _do_归还仪器(self, entry: dict[str, Any], values: dict[str, Any]) -> str:
        serial = str(entry["仪器编号"])
        open_todo = self._open_todo(serial)
        if open_todo is None:
            raise EquipmentError("illegal_transition", "该仪器没有未关闭的领用待办，不能归还")
        operator = str(values.get("经办人") or "").strip()
        if operator and operator != open_todo["经办人"]:
            raise EquipmentError(
                "serial_mismatch",
                f"归还经办人「{operator}」与领用经办人「{open_todo['经办人']}」不一致",
            )
        # 历史领用关系维持此前经办人：关闭待办留痕，清空当前使用人，保留上次经办人。
        entry["上次经办人"] = open_todo["经办人"]
        entry["使用人员"] = ""
        entry["status"] = STATUS_PENDING
        open_todo.update({
            "todo_status": TODO_CLOSED,
            "归还日期": str(values.get("归还日期") or TODAY),
            "ledger_version": int(entry["version"]) + 1,
        })
        self._bump_plan(entry, PLAN_PENDING, note="归还后须复检")
        return f"仪器已归还（原经办人：{open_todo['经办人']}），进入待核验节点"

    def _do_停用仪器(self, entry: dict[str, Any], values: dict[str, Any]) -> str:
        serial = str(entry["仪器编号"])
        entry["status"] = STATUS_RETIRED
        for plan in self._plans_for(serial):
            if plan["plan_status"] != PLAN_CANCELLED:
                plan.update({"plan_status": PLAN_CANCELLED, "ledger_version": int(entry["version"]) + 1})
        open_todo = self._open_todo(serial)
        if open_todo:
            open_todo.update({"todo_status": TODO_CANCELLED, "ledger_version": int(entry["version"]) + 1})
        entry["使用人员"] = ""
        return "仪器已停用，校准计划与未结待办已同步关闭"


equipment_service = EquipmentService()
