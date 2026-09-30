"""钻孔编录接口：维护钻孔，覆盖开始钻进、登记终孔、执行封孔等动作。

分批入账闸门也挂在这里：
- POST /api/borehole/imports            上传清单：预检通过才整批落库，否则整批退暂存
- GET  /api/borehole/imports            导入批次台账（按指纹可查）
- GET  /api/borehole/imports/{id}       单批次明细
- GET  /api/borehole/imports/{id}/staging-file  下载暂存原件
- GET  /api/borehole/todos              编录待办清单
- POST /api/borehole/alias-migrations   早期孔号历史别名迁移
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.schemas import (
    ActionResult,
    AliasMigrationPayload,
    EntryPayload,
    ImportUploadPayload,
    PageResult,
)
from app.services.borehole import BoreholeService
from app.services.borehole_import import BoreholeImportService

router = APIRouter(prefix="/api/borehole", tags=["钻孔编录"])

service = BoreholeService()
import_service = BoreholeImportService()

LIST_FIELDS = ["钻孔编号", "勘探区", "孔口坐标", "设计孔深", "终孔深度", "开孔日期", "终孔日期", "钻孔状态"]
STATUSES = ["待施工", "钻进中", "已终孔", "已封孔", "已废弃"]


# ===================== 分批入账闸门 =====================

@router.post("/imports")
def submit_import(payload: ImportUploadPayload) -> dict[str, Any]:
    """上传钻孔清单：先按勘探区与孔口坐标做冲突预检，全部通过才整批落库。

    - 任一孔号命中存量（含历史别名）或坐标重合：整批退回暂存文件，不写入任何台账行；
    - 同一文件指纹已处理：按幂等回放原结果，不生成第二份台账；
    - 解析中断：返回失败行与暂存批次，携带 resume_batch_id 重新上传即从失败行续传。
    """
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="上传内容为空，请提供钻孔清单 CSV 原文")
    try:
        if payload.resume_batch_id is not None:
            return import_service.resume(payload.resume_batch_id, payload.content)
        return import_service.submit(payload.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/imports")
def list_imports(
    fingerprint: str | None = Query(default=None, description="按文件指纹精确查询"),
) -> dict[str, Any]:
    """导入批次台账：每次上传都登记一条，可按指纹核对幂等情况。"""
    batches = import_service.list_batches(digest=fingerprint)
    return {"total": len(batches), "items": batches}


@router.get("/imports/{batch_id}")
def get_import(batch_id: int) -> dict[str, Any]:
    batch = import_service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"导入批次 {batch_id} 不存在")
    return batch


@router.get("/imports/{batch_id}/staging-file")
def download_staging_file(batch_id: int) -> FileResponse:
    """下载预检退档 / 解析中断批次的暂存原件，供现场核对后重新上传。"""
    path = import_service.staging_file(batch_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 没有可取回的暂存文件")
    return FileResponse(path, media_type="text/csv", filename=path.split("/")[-1])


@router.get("/todos")
def list_todos(pending_only: bool = Query(default=True)) -> dict[str, Any]:
    """编录待办清单：整批入账后按新孔逐条回写。"""
    items = import_service.list_todos(pending_only=pending_only)
    return {"total": len(items), "items": items}


@router.post("/alias-migrations", response_model=ActionResult)
def migrate_aliases(payload: AliasMigrationPayload) -> ActionResult:
    """早期孔号迁移：补齐历史别名；坐标冲突时以现场确认坐标覆盖为准。"""
    items = [item.model_dump() for item in payload.items]
    try:
        result = import_service.migrate_aliases(items)
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=result["ok"], message=result["message"], entry=result)


# ===================== 钻孔编录台账 =====================

@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按钻孔编号检索"),
    status: str | None = Query(default=None, description="待施工、钻进中、已终孔、已封孔、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按钻孔编号与状态过滤钻孔编录列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条钻孔明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"钻孔 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条钻孔，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="钻孔已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条钻孔执行开始钻进、登记终孔、执行封孔；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出钻孔编录清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "borehole", "total": total, "items": items}
