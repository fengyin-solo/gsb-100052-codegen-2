"""钻孔编录「分批入账闸门」业务规则。

设计目标（对应需求）：
1. 上传清单先做冲突预检（勘探区 + 孔口坐标 + 孔号/历史别名），本轮全部通过才允许落库；
2. 任一孔号命中存量记录（含历史别名）→ 整批退回暂存文件，绝不只导入一半；
3. 同一文件按 SHA256 指纹幂等，只生效一次，不会生成第二份台账；
4. 解析中断后从失败行继续（断点续传），且必须重新读取本文件的坐标，不用旧坐标顶替；
5. 早期孔号通过别名迁移补齐历史别名，坐标冲突时以现场确认坐标为准。

落库在内存里一次完成：先预检、后整体 append，过程中不修改存量表，因此任何冲突都不会
留下半批数据。预检失败与解析中断的原件会写入暂存目录，供下载核对与断点续传。
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.store import store

MODULE = "borehole"
BATCH_TABLE = "borehole_import_batch"
TODO_TABLE = "borehole_todo"
ALIAS_TABLE = "borehole_alias"

REQUIRED_FIELDS = ["钻孔编号", "勘探区", "孔口坐标"]
# 这些字段在落库时按标准列写入钻孔编录台账，其余列原样保留也无妨，但模板里用这些。
OPTIONAL_FIELDS = ["设计孔深", "终孔深度", "开孔日期", "终孔日期", "钻孔状态"]
ALL_COLUMNS = REQUIRED_FIELDS + OPTIONAL_FIELDS

STATUS_PENDING_IMPORT = "待施工"

# 批次状态
BATCH_COMMITTED = "已入账"
BATCH_REJECTED = "冲突退暂存"
BATCH_PARSE_FAILED = "解析中断"

# 坐标里的数字片段（支持负号与小数），用于把同一孔口坐标的不同写法对齐。
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
# 兜底键：抽不出两个数字时，去掉所有空白与分隔符，按纯文本比较。
_NON_WORD_RE = re.compile(r"[\s,，;；/|、:：()（）\[\]【】]+")


def fingerprint(content: str) -> str:
    """对文件原文做 SHA256，作为幂等指纹；空行与表头都参与，避免改一行还撞指纹。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def normalize_area(value: Any) -> str:
    return str(value or "").strip()


def normalize_code(value: Any) -> str:
    """孔号归一：去空白；后续比对孔号与历史别名时都先过这里。"""
    return str(value or "").strip()


def coordinate_key(value: Any) -> str:
    """把孔口坐标归一成稳定的比较键。

    优先抽前两个数字（X/Y），保留 6 位小数，规避 "100, 200" 与 "100.0/200.00"
    这类书写差异；抽不出两个数字时，去掉分隔符按文本兜底。
    """
    text = str(value or "").strip()
    nums = _NUM_RE.findall(text)
    if len(nums) >= 2:
        x, y = (round(float(nums[0]), 6), round(float(nums[1]), 6))
        return f"xy:{x:f},{y:f}"
    return "txt:" + _NON_WORD_RE.sub("", text)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _next_code() -> int:
    return store.next_id(BATCH_TABLE)


def _staging_dir() -> Path:
    path = Path(settings.staging_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_staging(batch_id: int, digest: str, content: str) -> str:
    """把退档/中断的原件落到暂存文件，返回相对可读的文件名（同时给出绝对路径）。"""
    directory = _staging_dir()
    name = f"borehole_batch_{batch_id}_{digest[:12]}.csv"
    target = directory / name
    target.write_text(content, encoding="utf-8")
    return str(target)


def _alias_index() -> dict[str, str]:
    """历史别名 -> 当前孔号（每个别名唯一，重复别名由迁移接口负责拦下）。"""
    index: dict[str, str] = {}
    for row in store.rows(ALIAS_TABLE):
        for alias in row.get("aliases", []) or []:
            index[normalize_code(alias)] = row["钻孔编号"]
    return index


def _borehole_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in store.rows(MODULE):
        index[normalize_code(row.get("钻孔编号"))] = row
    return index


def _split_lines(content: str) -> list[str]:
    """按物理行拆分，保留行号语义（第 1 行是表头）。统一换行符。"""
    return content.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def parse_manifest(content: str) -> tuple[list[dict[str, str]], dict[str, Any] | None, int]:
    """解析上传清单。

    返回 (rows, error, failed_line)：
    - 成功：rows 为字段字典，error=None；
    - 解析中断：rows 是失败行之前已成功解析的行（续传时直接复用），error 说明原因，
      failed_line 为出错的物理行号（1 起）。
    """
    lines = _split_lines(content)
    if not any(line.strip() for line in lines):
        return [], {"reason": "文件为空，没有可解析的钻孔行"}, 1

    reader = csv.reader(io.StringIO(content.replace("\r\n", "\n").replace("\r", "\n")))
    parsed_rows: list[dict[str, str]] = []
    header: list[str] | None = None

    for physical_line, raw in enumerate(reader, start=1):
        # csv.reader 会把末尾换行产出一个空 [] 行，忽略纯空行。
        if not raw or all(not str(cell).strip() for cell in raw):
            continue
        cells = [str(cell).strip() for cell in raw]
        if header is None:
            header = cells
            missing = [name for name in REQUIRED_FIELDS if name not in header]
            if missing:
                return parsed_rows, {
                    "reason": f"表头缺少必填列：{'、'.join(missing)}（模板列：{'、'.join(ALL_COLUMNS)}）",
                }, physical_line
            continue

        name_to_index = {name: idx for idx, name in enumerate(header)}
        row: dict[str, str] = {}
        for name in ALL_COLUMNS:
            idx = name_to_index.get(name)
            row[name] = cells[idx] if idx is not None and idx < len(cells) else ""

        missing = [name for name in REQUIRED_FIELDS if not row[name]]
        if missing:
            return parsed_rows, {
                "reason": f"第 {physical_line} 行缺少必填字段：{'、'.join(missing)}",
            }, physical_line

        # 坐标可解析性：含数字就必须能抽出至少两个，避免 "100" 这种半截坐标悄悄落库。
        coord_text = row["孔口坐标"]
        if _NUM_RE.search(coord_text) and len(_NUM_RE.findall(coord_text)) < 2:
            return parsed_rows, {
                "reason": f"第 {physical_line} 行孔口坐标不完整（需要 X、Y 两个坐标值）：{coord_text}",
            }, physical_line

        parsed_rows.append(row)

    if header is None or not parsed_rows:
        return [], {"reason": "只有表头，没有任何钻孔数据行"}, 1
    return parsed_rows, None, 0


def preflight(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """对整批行做冲突预检；返回冲突明细列表（空列表表示整批通过）。

    命中规则：
    - 孔号与存量钻孔（含历史别名）相同：整批退；
    - 同一勘探区内，孔口坐标与存量钻孔重合（疑似同一孔）：冲突待现场确认；
    - 本批内部孔号重复；
    - 本批内部同勘探区同坐标但孔号不同（一坐标多孔）。
    """
    conflicts: list[dict[str, Any]] = []
    existing = _borehole_index()
    alias_index = _alias_index()

    # 存量索引：勘探区 -> [(坐标键, 孔号)]
    coord_lookup: dict[str, list[tuple[str, str]]] = {}
    for ex in existing.values():
        area = normalize_area(ex.get("勘探区"))
        coord_lookup.setdefault(area, []).append(
            (coordinate_key(ex.get("孔口坐标")), normalize_code(ex.get("钻孔编号")))
        )

    seen_codes: dict[str, int] = {}
    seen_coords: dict[tuple[str, str], str] = {}

    for index, row in enumerate(rows, start=1):
        code = normalize_code(row["钻孔编号"])
        area = normalize_area(row["勘探区"])
        key = coordinate_key(row["孔口坐标"])
        location = {"行号": index, "钻孔编号": code, "勘探区": area, "孔口坐标": row["孔口坐标"]}

        if code in existing:
            conflicts.append({**location, "冲突类型": "孔号已存在",
                              "说明": f"孔号 {code} 已在钻孔编录台账中，整批退回暂存，不允许重复入账"})
            continue
        if code in alias_index:
            conflicts.append({**location, "冲突类型": "命中历史别名",
                              "说明": f"孔号 {code} 是 {alias_index[code]} 的历史别名，请走别名迁移补齐而非新建"})
            continue

        for ex_key, ex_code in coord_lookup.get(area, []):
            if ex_key == key:
                conflicts.append({**location, "冲突类型": "坐标与存量重合",
                                  "说明": f"勘探区「{area}」内坐标与存量钻孔 {ex_code} 重合，需现场确认孔口坐标"})
                break

        prev_line = seen_codes.get(code)
        if prev_line is not None:
            conflicts.append({**location, "冲突类型": "本批孔号重复",
                              "说明": f"孔号 {code} 在本批第 {prev_line} 行已出现"})
        else:
            seen_codes[code] = index

        prev_code = seen_coords.get((area, key))
        if prev_code is not None and prev_code != code:
            conflicts.append({**location, "冲突类型": "本批坐标重复",
                              "说明": f"该坐标在本批已被孔号 {prev_code} 占用，同一勘探区内不能一坐标多孔"})
        else:
            seen_coords[(area, key)] = code

    return conflicts


def _build_entry(row: dict[str, str], *, source_batch_id: int) -> dict[str, Any]:
    """构造一条与手工登记口径一致的钻孔编录台账行。"""
    entry: dict[str, Any] = {"id": store.next_id(MODULE)}
    for name in ALL_COLUMNS:
        entry[name] = row.get(name) or None
    entry["status"] = STATUS_PENDING_IMPORT
    entry["pending"] = True
    entry["abnormal"] = False
    entry["来源批次"] = source_batch_id
    return entry


def _build_todo(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": store.next_id(TODO_TABLE),
        "钻孔id": entry["id"],
        "钻孔编号": entry["钻孔编号"],
        "勘探区": entry["勘探区"],
        "待办状态": "待编录",
        "pending": True,
        "来源批次": entry["来源批次"],
        "创建时间": _now(),
    }


class BoreholeImportService:
    """分批入账闸门：预检—暂存—幂等—续传—落库回写。"""

    # ----- 批次台账查询 -----
    def list_batches(self, *, digest: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(BATCH_TABLE)
        if digest:
            rows = [row for row in rows if row.get("指纹") == digest]
        return rows

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        return store.find(BATCH_TABLE, batch_id)

    def find_committed(self, digest: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_TABLE):
            if row.get("指纹") == digest and row.get("批次状态") == BATCH_COMMITTED:
                return row
        return None

    def find_active_staging(self, digest: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_TABLE):
            if row.get("指纹") != digest:
                continue
            if row.get("批次状态") in (BATCH_REJECTED, BATCH_PARSE_FAILED):
                return row
        return None

    # ----- 暂存文件 -----
    def staging_file(self, batch_id: int) -> str | None:
        batch = self.get_batch(batch_id)
        if batch is None:
            return None
        path = batch.get("暂存文件")
        return path if path and Path(path).exists() else None

    # ----- 核心：上传 + 预检 + （通过时）整批落库 -----
    def submit(self, content: str) -> dict[str, Any]:
        digest = fingerprint(content)

        # 幂等闸门：同一指纹已入账，直接回放原结果，绝不生成第二份台账。
        committed = self.find_committed(digest)
        if committed is not None:
            return self._result(committed, reused=True)

        # 同一文件仍在暂存（退档或上次解析中断）：直接返回原批次，不重复造暂存文件；
        # 真正「从失败行接着走」由 resume 完成（续传的是修正后的文件）。
        staged = self.find_active_staging(digest)
        if staged is not None:
            return self._result(staged, reused=True)

        rows, parse_error, failed_line = parse_manifest(content)
        batch_id = _next_code()

        if parse_error is not None:
            return self._register_parse_failure(batch_id, digest, content, rows, parse_error, failed_line)

        conflicts = preflight(rows)
        if conflicts:
            return self._register_rejected(batch_id, digest, content, rows, conflicts)

        return self._commit(batch_id, digest, content, rows)

    def resume(self, parent_batch_id: int, content: str) -> dict[str, Any]:
        """断点续传：以父批次失败行为起点解析修正文件，随后仍走整批预检与整批落库。"""
        parent = self.get_batch(parent_batch_id)
        if parent is None:
            raise ValueError(f"批次 {parent_batch_id} 不存在，无法从失败行续传")
        if parent.get("批次状态") != BATCH_PARSE_FAILED:
            raise ValueError(f"批次 {parent_batch_id} 不是解析中断状态，无需续传")

        digest = fingerprint(content)
        if self.find_committed(digest) is not None:
            return self._result(self.find_committed(digest), reused=True)

        failed_line = int(parent.get("失败行", 0) or 0)
        prefix_rows = list(parent.get("已解析行", []) or [])

        # 关键：坐标必须从「本次上传文件」的失败行重新读取，禁止沿用任何旧坐标。
        rows, parse_error, new_failed_line, error_line_absolute = self._parse_from_line(
            content, failed_line, prefix_rows
        )

        batch_id = _next_code()
        if parse_error is not None:
            batch = self._register_parse_failure(
                batch_id, digest, content, rows, parse_error, error_line_absolute,
                parent_batch_id=parent_batch_id, resume_offset=failed_line,
            )
            self._mark_superseded(parent, batch_id)
            return batch

        conflicts = preflight(rows)
        if conflicts:
            batch = self._register_rejected(batch_id, digest, content, rows, conflicts,
                                            parent_batch_id=parent_batch_id)
            self._mark_superseded(parent, batch_id)
            return batch

        batch = self._commit(batch_id, digest, content, rows, parent_batch_id=parent_batch_id)
        self._mark_superseded(parent, batch_id)
        return batch

    def _parse_from_line(
        self, content: str, failed_line: int, prefix_rows: list[dict[str, str]]
    ) -> tuple[list[dict[str, str]], dict[str, Any] | None, int, int]:
        """续传解析：复用失败行之前的行，从失败行开始重新读取新文件。

        新文件结构：第 1 行仍是表头，第 2 行起为「从原失败行开始」的数据。
        返回 (rows, error, new_failed_line, absolute_failed_line)。
        """
        lines = _split_lines(content)
        if len(lines) < 2:
            return prefix_rows, {"reason": "续传文件缺少表头或数据行，无法接着解析"}, 2, failed_line

        reader = csv.reader(io.StringIO(content.replace("\r\n", "\n").replace("\r", "\n")))
        all_iter = list(reader)
        if not all_iter:
            return prefix_rows, {"reason": "续传文件为空"}, 1, failed_line

        header = [str(c).strip() for c in all_iter[0]]
        missing = [name for name in REQUIRED_FIELDS if name not in header]
        if missing:
            return prefix_rows, {
                "reason": f"续传表头缺少必填列：{'、'.join(missing)}",
            }, 1, failed_line

        name_to_index = {name: idx for idx, name in enumerate(header)}
        rows = list(prefix_rows)

        for relative_line, raw in enumerate(all_iter[1:], start=2):
            if not raw or all(not str(cell).strip() for cell in raw):
                continue
            cells = [str(cell).strip() for cell in raw]
            row: dict[str, str] = {}
            for name in ALL_COLUMNS:
                idx = name_to_index.get(name)
                row[name] = cells[idx] if idx is not None and idx < len(cells) else ""

            row_missing = [name for name in REQUIRED_FIELDS if not row[name]]
            if row_missing:
                absolute = failed_line + (relative_line - 2)
                return rows, {
                    "reason": f"续传数据第 {relative_line} 行（原文件第 {absolute} 行）缺少必填字段："
                              f"{'、'.join(row_missing)}",
                }, relative_line, absolute

            coord_text = row["孔口坐标"]
            if _NUM_RE.search(coord_text) and len(_NUM_RE.findall(coord_text)) < 2:
                absolute = failed_line + (relative_line - 2)
                return rows, {
                    "reason": f"续传数据第 {relative_line} 行（原文件第 {absolute} 行）孔口坐标不完整：{coord_text}",
                }, relative_line, absolute

            rows.append(row)

        if len(all_iter) == 1:
            return prefix_rows, {"reason": "续传文件只有表头，没有从失败行接续的数据"}, 2, failed_line
        return rows, None, 0, 0

    # ----- 批次落库 -----
    def _register_rejected(
        self, batch_id: int, digest: str, content: str,
        rows: list[dict[str, str]], conflicts: list[dict[str, Any]],
        *, parent_batch_id: int | None = None,
    ) -> dict[str, Any]:
        path = _write_staging(batch_id, digest, content)
        batch = {
            "id": batch_id,
            "指纹": digest,
            "批次状态": BATCH_REJECTED,
            "总行数": len(rows),
            "入账数": 0,
            "冲突": conflicts,
            "暂存文件": path,
            "已解析行": rows,
            "失败行": 0,
            "父批次": parent_batch_id,
            "创建时间": _now(),
        }
        store.rows(BATCH_TABLE).append(batch)
        return self._result(batch)

    def _register_parse_failure(
        self, batch_id: int, digest: str, content: str,
        prefix_rows: list[dict[str, str]], error: dict[str, Any], failed_line: int,
        *, parent_batch_id: int | None = None, resume_offset: int | None = None,
    ) -> dict[str, Any]:
        path = _write_staging(batch_id, digest, content)
        batch = {
            "id": batch_id,
            "指纹": digest,
            "批次状态": BATCH_PARSE_FAILED,
            "总行数": len(prefix_rows),
            "入账数": 0,
            "错误": error,
            "失败行": failed_line,
            "续传起始行": resume_offset,
            "暂存文件": path,
            "已解析行": prefix_rows,
            "冲突": [],
            "父批次": parent_batch_id,
            "创建时间": _now(),
        }
        store.rows(BATCH_TABLE).append(batch)
        return self._result(batch)

    def _commit(
        self, batch_id: int, digest: str, content: str, rows: list[dict[str, str]],
        *, parent_batch_id: int | None = None,
    ) -> dict[str, Any]:
        # 整批落库：先在局部构造好全部台账行与待办，再一次性 append；
        # 前面已 preflight 通过，这里不再写一半。
        new_entries = [_build_entry(row, source_batch_id=batch_id) for row in rows]
        new_todos = [_build_todo(entry) for entry in new_entries]

        ledger = store.rows(MODULE)
        ledger.extend(new_entries)
        store.rows(TODO_TABLE).extend(new_todos)

        batch = {
            "id": batch_id,
            "指纹": digest,
            "批次状态": BATCH_COMMITTED,
            "总行数": len(rows),
            "入账数": len(new_entries),
            "钻孔id": [entry["id"] for entry in new_entries],
            "待办id": [todo["id"] for todo in new_todos],
            "暂存文件": None,
            "已解析行": rows,
            "冲突": [],
            "失败行": 0,
            "父批次": parent_batch_id,
            "创建时间": _now(),
            "入账时间": _now(),
        }
        store.rows(BATCH_TABLE).append(batch)
        return self._result(batch)

    def _mark_superseded(self, parent: dict[str, Any], child_id: int) -> None:
        parent["已被批次续作"] = child_id

    def _result(self, batch: dict[str, Any], *, reused: bool = False) -> dict[str, Any]:
        status = batch["批次状态"]
        if status == BATCH_COMMITTED:
            message = "整批预检通过，已入账并回写钻孔编录台账与编录待办清单"
        elif status == BATCH_REJECTED:
            message = (
                f"预检发现 {len(batch.get('冲突', []))} 处冲突，整批已退回暂存文件，未写入任何台账行"
            )
        else:
            message = (
                f"解析在第 {batch.get('失败行')} 行中断，已暂存；可从该行续传，"
                "坐标以续传文件为准"
            )
        if reused:
            message = "同一文件指纹已处理过，按幂等只回放原结果：" + message
        return {"ok": status == BATCH_COMMITTED, "reused": reused, "message": message, "batch": batch}

    # ----- 编录待办 -----
    def list_todos(self, *, pending_only: bool = True) -> list[dict[str, Any]]:
        rows = store.rows(TODO_TABLE)
        if pending_only:
            rows = [row for row in rows if row.get("pending")]
        return rows

    # ----- 历史别名迁移 -----
    def migrate_aliases(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """为早期孔号记录补齐历史别名；坐标冲突时以现场确认坐标为准。

        每项形如：{"钻孔编号": "BORE-0009", "历史别名": ["ZK9","旧9"],
                   "现场确认坐标": "X,Y"}（坐标可选，提供即覆盖）。
        迁移不经过分批闸门，它是对存量记录的订正；但仍做整体校验，任一不通过就整批不动。
        """
        if not items:
            raise ValueError("没有提供任何待迁移的孔号")

        normalized: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        existing = _borehole_index()
        alias_index = _alias_index()

        for idx, item in enumerate(items, start=1):
            code = normalize_code(item.get("钻孔编号"))
            aliases = [normalize_code(a) for a in (item.get("历史别名") or []) if normalize_code(a)]
            confirmed = str(item.get("现场确认坐标") or "").strip()
            base = {"序号": idx, "钻孔编号": code}

            if not code:
                errors.append({**base, "问题": "缺少钻孔编号"})
                continue
            if code not in existing:
                errors.append({**base, "问题": "台账中找不到该钻孔，无法迁移"})
                continue
            if not aliases:
                errors.append({**base, "问题": "未提供历史别名"})
                continue

            # 别名不能占用别的孔（含别的孔已有别名），避免把两孔指到一起。
            occupied = [a for a in aliases if a in existing and a != code]
            alias_hit = [a for a in aliases if a in alias_index and alias_index[a] != code]
            if occupied:
                errors.append({**base, "问题": f"别名 {('、'.join(occupied))} 已是其他在档孔号"})
            if alias_hit:
                errors.append({**base, "问题": f"别名 {('、'.join(alias_hit))} 已挂在其他孔号下"})

            normalized.append({**base, "aliases": aliases, "confirmed": confirmed or None,
                               "entry": existing[code]})

        if errors:
            return {"ok": False, "message": "别名迁移预检未通过，整批未改动任何存量记录",
                    "errors": errors, "migrated": []}

        migrated: list[dict[str, Any]] = []
        # 先校验全部、再统一落变更。
        for data in normalized:
            entry = data["entry"]
            code = data["钻孔编号"]
            record = next((row for row in store.rows(ALIAS_TABLE) if row["钻孔编号"] == code), None)
            if record is None:
                record = {"id": store.next_id(ALIAS_TABLE), "钻孔编号": code, "aliases": [],
                          "坐标更新记录": []}
                store.rows(ALIAS_TABLE).append(record)

            added: list[str] = []
            for alias in data["aliases"]:
                if alias not in record["aliases"]:
                    record["aliases"].append(alias)
                    added.append(alias)

            coord_changed = False
            if data["confirmed"]:
                old_coord = entry.get("孔口坐标")
                if coordinate_key(old_coord) != coordinate_key(data["confirmed"]):
                    entry["孔口坐标"] = data["confirmed"]
                    entry["坐标来源"] = "现场确认"
                    coord_changed = True
                    record["坐标更新记录"].append(
                        {"旧坐标": old_coord, "新坐标": data["confirmed"], "确认时间": _now()}
                    )

            migrated.append({"钻孔编号": code, "新增别名": added,
                             "坐标已按现场确认更新": coord_changed})

        return {"ok": True, "message": f"已为 {len(migrated)} 个早期孔号补齐历史别名",
                "errors": [], "migrated": migrated}
