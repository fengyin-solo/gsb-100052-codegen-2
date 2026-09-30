"""钻孔编录分批入账闸门接口：上传清单、暂存批次、修复续解析、编录待办。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.borehole_import_gate import BoreholeImportGate

router = APIRouter(prefix="/api/borehole", tags=["钻孔编录-分批入账"])

gate = BoreholeImportGate()

IMPORT_TEMPLATE_COLUMNS = [
    "钻孔编号", "勘探区", "孔口坐标", "现场确认", "历史别名",
    "设计孔深", "终孔深度", "开孔日期", "终孔日期",
]


class RepairPayload(BaseModel):
    """修复暂存批次：可整份替换文件内容，也可只改某一行的若干字段。"""

    content: str | None = None
    line: int | None = None
    values: dict[str, Any] | None = None


@router.get("/imports/template")
def import_template() -> dict[str, Any]:
    """返回导入清单的表头约定与可直接另存的 CSV 样例（孔口坐标已加引号，避免逗号拆列）。"""
    sample_rows = [
        {
            "钻孔编号": "ZK101",
            "勘探区": "北山矿区",
            "孔口坐标": "X=4452200.000, Y=38512400.000",
            "现场确认": "否",
            "历史别名": "",
            "设计孔深": "300",
            "终孔深度": "",
            "开孔日期": "2026-10-01",
            "终孔日期": "",
        },
        {
            "钻孔编号": "BSZK1",
            "勘探区": "北山矿区",
            "孔口坐标": "X=4452100.500, Y=38512300.250",
            "现场确认": "是",
            "历史别名": "",
            "设计孔深": "",
            "终孔深度": "",
            "开孔日期": "",
            "终孔日期": "",
        },
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=IMPORT_TEMPLATE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(sample_rows)
    return {
        "module": "borehole",
        "columns": IMPORT_TEMPLATE_COLUMNS,
        "required": ["钻孔编号", "勘探区", "孔口坐标"],
        "csv": buffer.getvalue(),
        "sample": sample_rows,
    }


@router.post("/imports")
async def upload_import(file: UploadFile = File(..., description="钻孔清单 CSV/TSV（UTF-8）")) -> dict[str, Any]:
    """上传钻孔清单进闸：先解析与冲突预检，全部通过才落库；同一文件指纹只生效一次。"""
    raw = await file.read()
    if not raw.strip():
        raise HTTPException(status_code=400, detail="上传文件为空，请选择有效的钻孔清单")
    result = gate.upload(file.filename or "未命名清单.csv", raw)
    return result


@router.get("/imports", response_model=list[dict])
def list_imports() -> list[dict[str, Any]]:
    """导入批次台账：按时间倒序展示已入账与暂存中的批次。"""
    return sorted(gate.list_batches(), key=lambda row: int(row.get("id", 0)), reverse=True)


@router.get("/imports/{batch_no}")
def get_import(batch_no: str) -> dict[str, Any]:
    """查看暂存批次明细：含已解析行、失败行、冲突明细，便于现场逐行核对。"""
    artifact = gate.get_batch(batch_no)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"暂存批次 {batch_no} 不存在或已清理")
    return artifact


@router.post("/imports/{batch_no}/resume")
def resume_import(batch_no: str, payload: RepairPayload) -> dict[str, Any]:
    """修复失败行/冲突后继续解析：从失败那一行接着走，坐标只认当前文件。"""
    try:
        return gate.repair_and_resume(
            batch_no,
            {"content": payload.content, "line": payload.line, "values": payload.values},
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"暂存批次 {batch_no} 不存在或已清理，无法继续解析")


@router.get("/todos", response_model=PageResult[dict])
def list_todos(
    status: str | None = Query(default=None, description="待编录、已完成"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """编录待办清单：落库结果同步回写的待编录任务。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = gate.list_todos(status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/todos/{todo_id}/actions", response_model=ActionResult)
def run_todo_action(todo_id: int, payload: EntryPayload) -> ActionResult:
    """编录待办动作：目前支持完成编录，重复完成给出幂等提示。"""
    todo, message = gate.complete_todo(todo_id)
    if todo is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=todo)
