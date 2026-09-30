"""钻孔编录业务规则：状态流转、字段校验、孔号身份索引与历史别名迁移。"""
from __future__ import annotations

from typing import Any

from app.seed import LEGACY_BOREHOLE_ALIASES
from app.store import store

MODULE = "borehole"
REQUIRED_FIELDS = ["钻孔编号", "勘探区", "孔口坐标"]
STATUS_ORDER = ["待施工", "钻进中", "已终孔", "已封孔", "已废弃"]
ACTION_RULES = {"开始钻进": "钻进中", "登记终孔": "已终孔", "执行封孔": "已封孔"}
NEGATIVE_ACTIONS = []


class BoreholeService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("钻孔编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ("设计孔深", "终孔深度", "开孔日期", "终孔日期", "钻孔状态"):
            if str(values.get(field) or "").strip():
                entry[field] = values.get(field)
        entry["历史别名"] = []
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"钻孔 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于钻孔编录可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"钻孔已{action}"

    # ------------------------------------------------------------------
    # 孔号身份索引：现行孔号 + 历史别名都指向同一条存量记录
    # ------------------------------------------------------------------
    def _identity_index(self) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for row in store.rows(MODULE):
            code = str(row.get("钻孔编号", "")).strip()
            if code:
                index[code] = row
            for alias in row.get("历史别名", []) or []:
                alias = str(alias).strip()
                if alias:
                    index[alias] = row
        return index

    def migrate_legacy_aliases(self) -> dict[str, int]:
        """把早期孔号作为历史别名补到存量记录上；幂等，重复执行不产生重复别名。

        返回补齐的记录数与别名条数，供启动与导入闸门调用。
        """
        migrated_rows = 0
        migrated_aliases = 0
        for row in store.rows(MODULE):
            code = str(row.get("钻孔编号", "")).strip()
            aliases = row.setdefault("历史别名", [])
            touched = False
            for legacy in LEGACY_BOREHOLE_ALIASES.get(code, []):
                if legacy not in aliases:
                    aliases.append(legacy)
                    migrated_aliases += 1
                    touched = True
            if touched:
                migrated_rows += 1
        return {"rows": migrated_rows, "aliases": migrated_aliases}
