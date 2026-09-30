"""勘探设备接口：仪器台账、校准计划清单、领用待办三组只读视图 + 统一动作入口。

所有状态流转都走 POST /{entry_id}/actions，动作名、经办人、期望版本、幂等键
都在请求体里；业务校验失败返回 ok=false 与可读原因，不产生部分写入。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.equipment import (
    STATUS_ORDER,
    EquipmentError,
    service,
)

router = APIRouter(prefix="/api/equipment", tags=["勘探设备"])

LEDGER_FIELDS = ["仪器编号", "仪器名称", "型号规格", "精度指标", "检定日期", "有效期至", "使用人员", "仪器状态"]
CALIBRATION_FIELDS = ["仪器编号", "仪器名称", "型号规格", "检定日期", "有效期至", "状态"]
LOAN_FIELDS = ["仪器编号", "仪器名称", "型号规格", "经办人", "借出日期", "归还日期", "状态"]
STATUSES = STATUS_ORDER


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按仪器编号检索"),
    status: str | None = Query(default=None, description="待核验、在库可用、借出使用、维修中、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按仪器编号与状态过滤设备台账；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/stats")
def equipment_stats() -> dict[str, Any]:
    """侧栏统计卡片：revision 是台账当前版本水位，用于和详情比对。"""
    return service.stats()


@router.get("/calibration")
def list_calibrations(
    keyword: str | None = Query(default=None, description="按仪器编号检索"),
    status: str | None = Query(default=None, description="待校准、校准合格、暂停校准、计划终止"),
) -> dict[str, Any]:
    """校准计划清单：与台账按仪器编号一一对应，只读当前版本。"""
    items = service.list_calibrations(keyword=keyword, status=status)
    return {"module": "equipment_calibration", "total": len(items), "items": items}


@router.get("/loans")
def list_loans(
    keyword: str | None = Query(default=None, description="按仪器编号检索"),
    status: str | None = Query(default=None, description="借出未还、检修中挂账、已归还"),
) -> dict[str, Any]:
    """领用待办：只追加不覆盖，历史经办人永久保留。"""
    items = service.list_loans(keyword=keyword, status=status)
    return {"module": "equipment_loan", "total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出设备台账：返回全量当前版本数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "equipment", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单台仪器明细（含 version）；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"勘探仪器 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一台勘探仪器，仪器编号全局唯一；缺字段或重复编号都说明原因。"""
    try:
        entry, missing = service.create_entry(payload.values)
    except EquipmentError as exc:
        return ActionResult(ok=False, message=str(exc))
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="勘探仪器已登记，待核验", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """统一动作入口：校验前置状态 → 事务回写台账/校准计划/领用待办 → 版本 +1。

    请求体 values 支持：
    - action: 检定通过 / 领用 / 检修 / 修竣入库 / 归还 / 停用
    - operator: 经办人（领用必填，仅写入新建待办，不改历史待办）
    - version: 前端持有的台账版本，不一致直接拒绝并发提交
    - idem_key: 幂等键，重复提交回放首次结果
    """
    values = payload.values or {}
    action = str(values.get("action") or "").strip()

    def _as_int(name: str) -> int | None:
        raw = values.get(name)
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise EquipmentError(f"{name} 必须是整数版本号")

    try:
        expected_version = _as_int("version")
        entry, message = service.run_action(
            entry_id,
            action,
            operator=str(values.get("operator") or ""),
            expected_version=expected_version,
            idem_key=str(values.get("idem_key") or "") or None,
        )
    except EquipmentError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=message, entry=entry)
