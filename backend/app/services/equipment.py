"""勘探设备业务规则：仪器台账、校准计划清单、领用待办共用一套状态机。

设计约定：
- 仪器以「仪器编号」为唯一身份，同型号设备不得共用编号；所有联动都按编号回写。
- 台账（equipment）是主表，校准计划清单（equipment_calibration）与领用待办
  （equipment_loan）是派生表，任何动作都在同一把锁、同一个快照事务里回写，
  任一步失败整体回滚，不允许只改一张表。
- 每条台账行带 version，每次成功流转 +1；侧栏统计与详情都必须读同一版本号。
- 动作前置状态在 ACTION_RULES 里显式声明，不允许跳步（例如未领用不能归还）。
- 并发提交按 entry_id 串行化，重复请求用幂等键短路，防止写到错误设备。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import date
from typing import Any, Iterator

from app.store import store

MODULE = "equipment"
CALIBRATION_MODULE = "equipment_calibration"
LOAN_MODULE = "equipment_loan"

REQUIRED_FIELDS = ["仪器编号", "仪器名称", "型号规格"]

# 状态序列：存量未知状态统一迁入「待核验」，核验合格后才能进入在库可用。
STATUS_PENDING_CHECK = "待核验"
STATUS_AVAILABLE = "在库可用"
STATUS_BORROWED = "借出使用"
STATUS_REPAIRING = "维修中"
STATUS_DECOMMISSIONED = "已停用"
STATUS_ORDER = [
    STATUS_PENDING_CHECK,
    STATUS_AVAILABLE,
    STATUS_BORROWED,
    STATUS_REPAIRING,
    STATUS_DECOMMISSIONED,
]
KNOWN_STATUSES = frozenset(STATUS_ORDER)

# 旧版数据里出现过、需要在迁移时归一化的状态别名。
STATUS_ALIASES = {
    "待检定": STATUS_PENDING_CHECK,
    "已报废": STATUS_DECOMMISSIONED,
}

# 校准计划清单状态
CAL_PENDING = "待校准"
CAL_PASSED = "校准合格"
CAL_SUSPENDED = "暂停校准"
CAL_TERMINATED = "计划终止"

# 领用待办状态
LOAN_OPEN = "借出未还"
LOAN_RETURNING = "检修中挂账"
LOAN_CLOSED = "已归还"

# 动作 → (目标状态, 允许的前置状态)。前置状态不匹配一律拒绝，不做隐式跳转。
ACTION_RULES: dict[str, tuple[str, tuple[str, ...]]] = {
    "检定通过": (STATUS_AVAILABLE, (STATUS_PENDING_CHECK,)),
    "领用": (STATUS_BORROWED, (STATUS_AVAILABLE,)),
    "检修": (STATUS_REPAIRING, (STATUS_AVAILABLE,)),
    "修竣入库": (STATUS_AVAILABLE, (STATUS_REPAIRING,)),
    "归还": (STATUS_AVAILABLE, (STATUS_BORROWED,)),
    "停用": (STATUS_DECOMMISSIONED, (
        STATUS_PENDING_CHECK,
        STATUS_AVAILABLE,
        STATUS_REPAIRING,
        STATUS_BORROWED,
    )),
}

# 旧版客户端动作名 → 规范动作名
ACTION_ALIASES = {
    "借出使用": "领用",
    "报修仪器": "检修",
}


class EquipmentError(Exception):
    """业务校验失败：携带可读原因，路由层转成失败结果。"""


class EquipmentService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._migrated = False
        # 幂等结果缓存：key=(entry_id, idem_key) → ActionResult 快照
        self._idempotency: dict[tuple[int, str], dict[str, Any]] = {}

    # ------------------------------------------------------------------ 迁移

    def _ensure_migrated(self) -> None:
        """存量状态不明的仪器迁入待核验，并补齐校准计划与历史领用待办。"""
        if self._migrated:
            return
        with self._lock:
            if self._migrated:
                return
            rows = store.rows(MODULE)
            calibrations = store.rows(CALIBRATION_MODULE)
            loans = store.rows(LOAN_MODULE)
            for row in rows:
                raw_status = str(row.get("status") or "")
                status = STATUS_ALIASES.get(raw_status, raw_status)
                if status not in KNOWN_STATUSES:
                    status = STATUS_PENDING_CHECK
                row["status"] = status
                row.setdefault("abnormal", status == STATUS_REPAIRING)
                row["仪器状态"] = status
                # pending 语义：除停用外都还挂在待处理看板上
                row["pending"] = status != STATUS_DECOMMISSIONED
                row["version"] = int(row.get("version") or 1)
                serial = str(row.get("仪器编号") or "").strip()
                # 存量台账里的「使用人员」是示例占位值：只有借出态保留为历史经办人，
                # 其余节点一律清空，避免归还后旧阶段残留。
                if status != STATUS_BORROWED:
                    row["使用人员"] = None
                # 示例数据里日期/有效期字段也是占位文本，清掉后由检定动作重新写回。
                for field in ("检定日期", "有效期至"):
                    value = str(row.get(field) or "")
                    if "样例" in value:
                        row[field] = None
                if not any(
                    str(item.get("仪器编号") or "").strip() == serial
                    for item in calibrations
                ):
                    calibrations.append(self._new_calibration(row, status))
                if status == STATUS_BORROWED and not any(
                    str(item.get("仪器编号") or "").strip() == serial
                    and item.get("状态") == LOAN_OPEN
                    for item in loans
                ):
                    loans.append(self._new_loan(row))
            self._migrated = True

    @staticmethod
    def _new_calibration(row: dict[str, Any], status: str) -> dict[str, Any]:
        cal_status = {
            STATUS_AVAILABLE: CAL_PASSED,
            STATUS_BORROWED: CAL_PASSED,
            STATUS_REPAIRING: CAL_SUSPENDED,
            STATUS_DECOMMISSIONED: CAL_TERMINATED,
        }.get(status, CAL_PENDING)
        return {
            "id": 0,
            "仪器编号": str(row.get("仪器编号") or "").strip(),
            "仪器名称": row.get("仪器名称"),
            "型号规格": row.get("型号规格"),
            "检定日期": row.get("检定日期") if cal_status == CAL_PASSED else None,
            "有效期至": row.get("有效期至") if cal_status == CAL_PASSED else None,
            "状态": cal_status,
            "version": int(row.get("version") or 1),
        }

    @staticmethod
    def _new_loan(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": 0,
            "仪器编号": str(row.get("仪器编号") or "").strip(),
            "仪器名称": row.get("仪器名称"),
            "型号规格": row.get("型号规格"),
            "经办人": row.get("使用人员"),
            "借出日期": date.today().isoformat(),
            "归还日期": None,
            "状态": LOAN_OPEN,
            "version": int(row.get("version") or 1),
        }

    # ------------------------------------------------------------------ 事务

    @contextmanager
    def _transaction(self, tables: list[str]) -> Iterator[None]:
        """对涉及到的表做快照，中途抛错则整体回滚（未成功不留残数据）。"""
        snapshots = {name: [dict(row) for row in store.rows(name)] for name in tables}
        try:
            yield
        except Exception:
            for name, snapshot in snapshots.items():
                table = store.rows(name)
                table.clear()
                table.extend(snapshot)
            raise

    # ------------------------------------------------------------------ 查询

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        self._ensure_migrated()
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("仪器编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [dict(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        self._ensure_migrated()
        row = store.find(MODULE, entry_id)
        return dict(row) if row else None

    def list_calibrations(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_migrated()
        rows = store.rows(CALIBRATION_MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("仪器编号", ""))]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        return [dict(row) for row in rows]

    def list_loans(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_migrated()
        rows = store.rows(LOAN_MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("仪器编号", ""))]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        """侧栏卡片：与台账读取同一份数据并带上当前版本水位。"""
        self._ensure_migrated()
        rows = store.rows(MODULE)
        loans = store.rows(LOAN_MODULE)
        return {
            "revision": max((int(r.get("version") or 0) for r in rows), default=0),
            "total": len(rows),
            "available": sum(1 for r in rows if r.get("status") == STATUS_AVAILABLE),
            "borrowed": sum(1 for r in rows if r.get("status") == STATUS_BORROWED),
            "repairing": sum(1 for r in rows if r.get("status") == STATUS_REPAIRING),
            "pending_check": sum(1 for r in rows if r.get("status") == STATUS_PENDING_CHECK),
            "open_loans": sum(1 for r in loans if r.get("状态") != LOAN_CLOSED),
        }

    # ------------------------------------------------------------------ 写入

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        self._ensure_migrated()
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        serial = str(values["仪器编号"]).strip()
        with self._lock:
            # 同型号设备也以仪器编号为唯一依据，重复编号直接拒绝。
            if any(str(row.get("仪器编号") or "").strip() == serial for row in store.rows(MODULE)):
                raise EquipmentError(f"仪器编号 {serial} 已存在，同型号设备也不得共用编号")
            with self._transaction([MODULE, CALIBRATION_MODULE]):
                rows = store.rows(MODULE)
                entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
                for field in ("仪器编号", "仪器名称", "型号规格", "精度指标", "检定日期", "有效期至"):
                    if values.get(field) is not None:
                        entry[field] = values.get(field)
                entry["使用人员"] = None
                entry["status"] = STATUS_PENDING_CHECK
                entry["仪器状态"] = STATUS_PENDING_CHECK
                entry["pending"] = True
                entry["abnormal"] = False
                entry["version"] = 1
                rows.append(entry)

                cal_rows = store.rows(CALIBRATION_MODULE)
                calibration = self._new_calibration(entry, STATUS_PENDING_CHECK)
                calibration["id"] = max((int(r.get("id", 0)) for r in cal_rows), default=0) + 1
                cal_rows.append(calibration)
            return dict(entry), []

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        operator: str | None = None,
        expected_version: int | None = None,
        idem_key: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """执行状态流转。成功返回新台账快照；失败抛 EquipmentError（事务已回滚）。"""
        self._ensure_migrated()
        action = str(action or "").strip()
        action = ACTION_ALIASES.get(action, action)
        if action not in ACTION_RULES:
            raise EquipmentError(f"动作「{action}」不属于勘探设备可执行范围")

        operator = (operator or "").strip() or None
        idem_key = (idem_key or "").strip() or None
        cache_key: tuple[int, str] | None = (entry_id, idem_key) if idem_key else None

        with self._lock:
            # 重复提交幂等：同一设备 + 同一幂等键直接回放上次结果，不再执行。
            if cache_key is not None and cache_key in self._idempotency:
                cached = self._idempotency[cache_key]
                return dict(cached["entry"]), cached["message"]

            entry = store.find(MODULE, entry_id)
            if entry is None:
                raise EquipmentError(f"勘探仪器 {entry_id} 不存在或已归档")

            current_version = int(entry.get("version") or 0)
            if expected_version is not None and int(expected_version) != current_version:
                raise EquipmentError(
                    f"仪器状态已被其他人更新（当前版本 {current_version}），请刷新后重试"
                )

            target, allowed = ACTION_RULES[action]
            if not allowed:
                raise EquipmentError(f"动作「{action}」需要先满足前置流程，不能从当前节点发起")
            current = str(entry.get("status") or "")
            if current not in allowed:
                raise EquipmentError(
                    f"仪器 {entry.get('仪器编号')} 当前为「{current}」，不允许执行「{action}」"
                )
            if action in ("领用",) and not operator:
                raise EquipmentError("领用必须登记经办人")

            tables = [MODULE, CALIBRATION_MODULE, LOAN_MODULE]
            try:
                with self._transaction(tables):
                    self._apply_action(entry, action, target, operator)
                    new_version = current_version + 1
                    entry["version"] = new_version
                    entry["status"] = target
                    entry["仪器状态"] = target
                    entry["pending"] = target != STATUS_DECOMMISSIONED
                    entry["abnormal"] = target == STATUS_REPAIRING
                    self._sync_projections(entry, action, target, new_version)
            except EquipmentError:
                if cache_key is not None:
                    self._idempotency.pop(cache_key, None)
                raise

            snapshot = dict(entry)
            message = f"勘探仪器已{action}"
            if cache_key is not None:
                self._idempotency[cache_key] = {"entry": dict(snapshot), "message": message}
            return snapshot, message

    # -------------------------------------------------------------- 状态回写

    def _apply_action(
        self,
        entry: dict[str, Any],
        action: str,
        target: str,
        operator: str | None,
    ) -> None:
        serial = str(entry.get("仪器编号") or "").strip()
        if action == "检定通过":
            today = date.today().isoformat()
            entry["检定日期"] = today
            entry["使用人员"] = None
        elif action == "领用":
            # 经办人写进台账与新建的领用待办；历史待办的经办人永不覆盖。
            entry["使用人员"] = operator
        elif action == "检修":
            entry["使用人员"] = None
        elif action == "归还":
            # 旧经办人留在已关闭的待办里，台账只清空当前持有人。
            entry["使用人员"] = None
        elif action == "停用":
            # 借出未还也允许停用（如丢失、强制退役）：待办被强制结清，
            # 但经办人字段保留，历史领用关系仍可追溯。
            entry["使用人员"] = None

    def _sync_projections(
        self,
        entry: dict[str, Any],
        action: str,
        target: str,
        new_version: int,
    ) -> None:
        serial = str(entry.get("仪器编号") or "").strip()

        # ---- 校准计划清单：始终与台账一一对应，按编号回写同一条 ----
        calibration = next(
            (row for row in store.rows(CALIBRATION_MODULE)
             if str(row.get("仪器编号") or "").strip() == serial),
            None,
        )
        if calibration is None:
            calibration = self._new_calibration(entry, target)
            calibration["id"] = max(
                (int(r.get("id", 0)) for r in store.rows(CALIBRATION_MODULE)), default=0
            ) + 1
            store.rows(CALIBRATION_MODULE).append(calibration)
        cal_target = {
            STATUS_PENDING_CHECK: CAL_PENDING,
            STATUS_AVAILABLE: CAL_PASSED,
            STATUS_BORROWED: CAL_PASSED,
            STATUS_REPAIRING: CAL_SUSPENDED,
            STATUS_DECOMMISSIONED: CAL_TERMINATED,
        }[target]
        calibration["状态"] = cal_target
        calibration["型号规格"] = entry.get("型号规格")
        calibration["仪器名称"] = entry.get("仪器名称")
        if cal_target == CAL_PASSED:
            today = date.today().isoformat()
            calibration["检定日期"] = entry.get("检定日期") or calibration.get("检定日期") or today
            if entry.get("有效期至"):
                calibration["有效期至"] = entry["有效期至"]
            entry["检定日期"] = calibration["检定日期"]
            entry["有效期至"] = calibration.get("有效期至")
        calibration["version"] = new_version

        # ---- 领用待办：只追加、不改历史经办人 ----
        loans = store.rows(LOAN_MODULE)
        open_loan = next(
            (row for row in loans
             if str(row.get("仪器编号") or "").strip() == serial
             and row.get("状态") != LOAN_CLOSED),
            None,
        )
        if action == "领用":
            loan = self._new_loan(entry)
            loan["id"] = max((int(r.get("id", 0)) for r in loans), default=0) + 1
            loan["经办人"] = entry.get("使用人员")
            loan["version"] = new_version
            loans.append(loan)
        elif action == "检修" and open_loan is not None:
            # 台账不允许借出直接检修；保留该分支仅作防御，不改变经办人。
            open_loan["状态"] = LOAN_RETURNING
            open_loan["version"] = new_version
        elif action == "归还":
            if open_loan is None:
                raise EquipmentError(
                    f"仪器 {serial} 没有未结清的领用待办，归还属于跳步操作"
                )
            open_loan["状态"] = LOAN_CLOSED
            open_loan["归还日期"] = date.today().isoformat()
            # 经办人字段刻意不动：历史领用关系维持此前经办人。
            open_loan["version"] = new_version
        elif action == "停用":
            for row in loans:
                if (
                    str(row.get("仪器编号") or "").strip() == serial
                    and row.get("状态") != LOAN_CLOSED
                ):
                    row["状态"] = LOAN_CLOSED
                    row["归还日期"] = date.today().isoformat()
                    row["version"] = new_version


service = EquipmentService()
