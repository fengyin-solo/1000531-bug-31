"""勘探设备接口：台账、校准计划、领用待办共用同一状态机版本。

读取口径：
- GET /api/equipment/workspace 返回侧栏（待办/校准清单/统计）与台账列表同版本载荷。
- GET /api/equipment/{id}/detail 返回单台仪器的台账+计划+待办聚合详情。
写入口径：
- POST /api/equipment/{id}/actions 走状态机，支持 request_id 幂等与 expected_version 乐观锁。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.equipment import ACTIONS, EquipmentError, equipment_service

router = APIRouter(prefix="/api/equipment", tags=["勘探设备"])

LIST_FIELDS = ["仪器编号", "仪器名称", "型号规格", "精度指标", "检定日期", "有效期至", "使用人员", "仪器状态"]
STATUSES = ["待核验", "在库可用", "借出使用", "维修中", "已停用"]


@router.get("/workspace")
def workspace(
    keyword: str | None = Query(default=None, description="按仪器编号检索"),
    status: str | None = Query(default=None, description="待核验、在库可用、借出使用、维修中、已停用"),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """侧栏与台账列表的唯一读口径，保证两边读取同一个台账版本。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    return equipment_service.workspace(keyword=keyword, status=status, page=page, size=size)


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
    items, total = equipment_service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出设备台账清单：返回当前过滤条件下的全量数据。"""
    items, total = equipment_service.list_entries(page=1, size=10000)
    return {"module": "equipment", "total": total, "items": items}


@router.get("/{entry_id}/detail")
def get_detail(entry_id: int) -> dict[str, Any]:
    """单台仪器聚合详情：台账、校准计划、领用待办在同一快照下读取。"""
    detail = equipment_service.get_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"勘探仪器 {entry_id} 不存在或已归档")
    return detail


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单台仪器台账明细；不存在时给出可读的错误说明。"""
    entry = equipment_service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"勘探仪器 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一台仪器：仪器编号全局唯一，新建后落在待核验节点并生成首次校准计划。"""
    entry, errors = equipment_service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, code="bad_payload", message="；".join(errors))
    return ActionResult(ok=True, message="勘探仪器已登记，等待检定", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行领用、检修、归还、停用等状态机动作；重复提交幂等，版本冲突/跳步会被拒绝。"""
    action = str(payload.values.get("action") or "").strip()
    if not action:
        return ActionResult(ok=False, code="bad_action", message="缺少 action 参数")
    if action not in ACTIONS:
        return ActionResult(ok=False, code="bad_action", message=f"动作「{action}」不属于勘探设备可执行范围")
    try:
        result = equipment_service.run_action(
            entry_id,
            action,
            payload.values,
            request_id=payload.request_id,
            expected_version=payload.expected_version,
        )
    except EquipmentError as exc:
        return ActionResult(ok=False, code=exc.code, message=exc.message)
    return ActionResult(**result)
