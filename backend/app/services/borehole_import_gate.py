"""钻孔编录分批入账闸门。

上传的钻孔清单必须先过闸：
1. 逐行解析，遇到第一个坏行立即中断，批次退到暂存文件，修好后从失败行继续解析；
2. 全部行解析完才做冲突预检（勘探区 + 孔口坐标 + 孔号身份/历史别名）；
3. 任一冲突都整批退回暂存文件，不允许只落一半；只有本轮全部通过才一次性落库，
   落库结果同步写回钻孔编录台账与编录待办清单；
4. 同一文件按字节指纹幂等，重复上传只返回首次结果，绝不生成第二份台账；
5. 历史别名命中存量记录时视为同一孔；坐标不一致以「现场确认」的孔口坐标为准。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.borehole import BoreholeService
from app.store import store

LEDGER_MODULE = "borehole"
TODO_MODULE = "borehole_todo"
IMPORT_MODULE = "borehole_import"

FIELD_CODE = "钻孔编号"
FIELD_AREA = "勘探区"
FIELD_COORD = "孔口坐标"
FIELD_CONFIRMED = "现场确认"
FIELD_ALIASES = "历史别名"
REQUIRED_COLUMNS = [FIELD_CODE, FIELD_AREA, FIELD_COORD]
# 列顺序即清单表头顺序：基础三段 + 现场确认 + 历史别名 + 孔深/日期
OPTIONAL_COLUMNS = [FIELD_CONFIRMED, FIELD_ALIASES, "设计孔深", "终孔深度", "开孔日期", "终孔日期"]
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

STATUS_PARSING = "parsing_failed"
STATUS_CONFLICT = "conflict_rejected"
STATUS_LANDED = "landed"
STATUS_LABELS = {
    STATUS_PARSING: "解析中断（暂存中）",
    STATUS_CONFLICT: "预检未过（整批暂存）",
    STATUS_LANDED: "已入账",
}

CONFIRMED_TOKENS = {"是", "true", "1", "y", "yes", "已确认", "现场确认", "确认"}
_ALIAS_SPLIT = re.compile(r"[；;、|/]")
_COORD_PAIR = re.compile(
    r"(?:X|E|东)\s*[=:：]?\s*(-?\d+(?:\.\d+)?).*?(?:Y|N|北)\s*[=:：]?\s*(-?\d+(?:\.\d+)?)"
)
_COORD_BARE = re.compile(r"(-?\d+(?:\.\d+)?)")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def parse_coordinate(value: Any) -> tuple[str, float, float] | None:
    """把各种写法的孔口坐标归一成 (X, Y)；无法成对解析时返回 None。

    接受：X=..,Y=..、E:.. N:..、"4452100.5, 38512300.2" 等，统一保留三位小数。
    """
    text = str(value or "").strip()
    if not text:
        return None
    match = _COORD_PAIR.search(text)
    if match:
        x_text, y_text = match.groups()
    else:
        # 无 X/Y 标记时退化为「前两个数字即坐标」，兼容 4452100.5 38512300.2 这类写法
        numbers = _COORD_BARE.findall(text)
        if len(numbers) < 2:
            return None
        x_text, y_text = numbers[0], numbers[1]
    try:
        x, y = float(x_text), float(y_text)
    except (TypeError, ValueError):
        return None
    return f"X={x:.3f}, Y={y:.3f}", round(x, 3), round(y, 3)


def _is_confirmed(value: Any) -> bool:
    return str(value or "").strip().lower() in CONFIRMED_TOKENS


def _split_aliases(value: Any) -> list[str]:
    aliases: list[str] = []
    for piece in _ALIAS_SPLIT.split(str(value or "")):
        piece = piece.strip()
        if piece and piece not in aliases:
            aliases.append(piece)
    return aliases


class BoreholeImportGate:
    """分批入账闸门：解析、暂存、预检、落库的规则全部收在这一层。"""

    def __init__(self, staging_dir: Path | None = None) -> None:
        self.staging_dir = staging_dir or settings.staging_dir
        self._lock = threading.RLock()
        self._migrated = False
        self._recover_from_disk()

    # ------------------------------------------------------------------
    # 初始化与暂存文件
    # ------------------------------------------------------------------
    def _ensure_migration(self) -> None:
        if not self._migrated:
            BoreholeService().migrate_legacy_aliases()
            self._migrated = True

    def _staging_path(self, batch_no: str) -> Path:
        return self.staging_dir / f"{batch_no}.json"

    def _recover_from_disk(self) -> None:
        """重启后把暂存文件里的未完成批次接回内存台账。"""
        if not self.staging_dir.exists():
            return
        known = {row.get("batch_no") for row in store.rows(IMPORT_MODULE)}
        for path in sorted(self.staging_dir.glob("*.json")):
            try:
                artifact = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            batch_no = artifact.get("batch_no")
            if not batch_no or batch_no in known or artifact.get("status") == STATUS_LANDED:
                continue
            store.rows(IMPORT_MODULE).append(self._registry_view(artifact))

    def _save_staging(self, artifact: dict[str, Any]) -> None:
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._staging_path(str(artifact["batch_no"])).write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _remove_staging(self, batch_no: str) -> None:
        try:
            self._staging_path(batch_no).unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _registry_view(artifact: dict[str, Any]) -> dict[str, Any]:
        """导入批次台账：暂存明细的列表视图。"""
        return {
            "id": artifact.get("id"),
            "batch_no": artifact["batch_no"],
            "filename": artifact.get("filename"),
            "fingerprint": artifact.get("fingerprint"),
            "status": artifact.get("status"),
            "status_label": STATUS_LABELS.get(artifact.get("status"), artifact.get("status", "")),
            "total_rows": artifact.get("total_rows", 0),
            "parsed_rows": len(artifact.get("parsed_rows", [])),
            "failed_line": artifact.get("failed_line"),
            "parse_error": artifact.get("parse_error"),
            "conflicts": artifact.get("conflicts", []),
            "conflict_count": len(artifact.get("conflicts", [])),
            "landed_count": len(artifact.get("landed_ids", [])),
            "landed_ids": artifact.get("landed_ids", []),
            "notices": artifact.get("notices", []),
            "created_at": artifact.get("created_at"),
            "updated_at": artifact.get("updated_at"),
        }

    def _upsert_registry(self, artifact: dict[str, Any]) -> None:
        view = self._registry_view(artifact)
        rows = store.rows(IMPORT_MODULE)
        for index, row in enumerate(rows):
            if row.get("batch_no") == artifact["batch_no"]:
                rows[index] = view
                return
        rows.append(view)

    # ------------------------------------------------------------------
    # 上传（指纹幂等 + 首次解析）
    # ------------------------------------------------------------------
    def upload(self, filename: str, raw: bytes) -> dict[str, Any]:
        with self._lock:
            self._ensure_migration()
            fingerprint = _sha256_bytes(raw)
            for existing in store.rows(IMPORT_MODULE):
                if existing.get("fingerprint") == fingerprint:
                    return {
                        "ok": False,
                        "idempotent": True,
                        "message": self._idempotent_message(existing),
                        "batch": existing,
                    }
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = ""
            artifact = self._new_artifact(filename, raw, text, fingerprint)
            if not text:
                artifact["failed_line"] = 1
                artifact["parse_error"] = "文件编码无法按 UTF-8 解析，请另存为 UTF-8 后修复重试"
                self._persist(artifact)
                return {"ok": False, "idempotent": False, "message": artifact["parse_error"], "batch": self._registry_view(artifact)}
            self._parse(artifact, resume=False)
            self._persist(artifact)
            return {
                "ok": artifact["status"] == STATUS_LANDED,
                "idempotent": False,
                "message": self._status_message(artifact),
                "batch": self._registry_view(artifact),
            }

    def _new_artifact(self, filename: str, raw: bytes, text: str, fingerprint: str) -> dict[str, Any]:
        batch_no = f"BH-IMP-{datetime.now().strftime('%Y%m%d%H%M%S')}-{fingerprint[:8]}"
        first_line = next((line for line in text.splitlines() if line.strip()), "")
        delimiter = "\t" if "\t" in first_line and first_line.count("\t") >= first_line.count(",") else ","
        return {
            "id": store.next_id(IMPORT_MODULE),
            "batch_no": batch_no,
            "filename": filename,
            "fingerprint": fingerprint,
            "content_fingerprint": _sha256_text(text),
            "delimiter": delimiter,
            "raw_text": text,
            "total_rows": 0,
            "parsed_rows": [],
            "failed_line": None,
            "parse_error": None,
            "status": STATUS_PARSING,
            "conflicts": [],
            "landed_ids": [],
            "notices": [],
            "created_at": _now(),
            "updated_at": _now(),
        }

    @staticmethod
    def _idempotent_message(existing: dict[str, Any]) -> str:
        if existing.get("status") == STATUS_LANDED:
            return f"文件指纹 {existing.get('fingerprint', '')[:12]} 已入账，重复上传不生成第二份台账"
        return f"文件指纹 {existing.get('fingerprint', '')[:12]} 已在暂存文件中，请修复失败行后继续解析，不重复建批"

    @staticmethod
    def _status_message(artifact: dict[str, Any]) -> str:
        if artifact["status"] == STATUS_LANDED:
            return f"本批 {len(artifact.get('landed_ids', []))} 个钻孔全部通过预检并已入账，台账与待办已同步"
        if artifact["status"] == STATUS_CONFLICT:
            return f"预检发现 {len(artifact['conflicts'])} 处冲突，整批退回暂存文件，未写入任何台账记录"
        return f"解析在第 {artifact['failed_line']} 行中断，整批暂存，修复后将从该行继续解析"

    # ------------------------------------------------------------------
    # 解析（可断点续走，坐标只信当前文件，不拿旧坐标顶替）
    # ------------------------------------------------------------------
    def _parse(self, artifact: dict[str, Any], *, resume: bool, full_reparse: bool = False) -> None:
        text = artifact["raw_text"]
        delimiter = artifact["delimiter"]
        lines = text.splitlines()
        if not resume or full_reparse:
            # 首次解析或整份替换：从表头后第一行走起，旧坐标不参与后续判定
            artifact["parsed_rows"] = []
            artifact["failed_line"] = None
            artifact["parse_error"] = None
            artifact["total_rows"] = 0
            start_line = 2
        else:
            start_line = artifact["failed_line"] or 2
            # 续解析时已暂存的行也要按当前暂存文本重新归一坐标，杜绝拿历史坐标顶替
            for row in artifact["parsed_rows"]:
                self._refresh_row_coordinate(row)
        for line_no in range(1, len(lines) + 1):
            if line_no == 1 or line_no < start_line:
                continue
            line = lines[line_no - 1]
            if not line.strip():
                continue
            try:
                values = next(csv.reader(io.StringIO(line), delimiter=delimiter))
            except csv.Error as exc:
                artifact["failed_line"] = line_no
                artifact["parse_error"] = f"第 {line_no} 行无法按列分隔解析：{exc}"
                artifact["status"] = STATUS_PARSING
                return
            parsed = self._parse_line(line_no, values, line)
            if isinstance(parsed, str):
                artifact["failed_line"] = line_no
                artifact["parse_error"] = parsed
                artifact["status"] = STATUS_PARSING
                return
            # 续解析时同一行可能已在暂存里，按行号覆盖而不是重复追加
            artifact["parsed_rows"] = [r for r in artifact["parsed_rows"] if r["source_line"] != line_no]
            artifact["parsed_rows"].append(parsed)
            artifact["failed_line"] = None
            artifact["parse_error"] = None
        artifact["total_rows"] = len(artifact["parsed_rows"])
        artifact["failed_line"] = None
        artifact["parse_error"] = None
        self._precheck_and_maybe_land(artifact)

    @staticmethod
    def _normalize_cells(cells: list[str], raw_line: str = "") -> list[str]:
        """兼容现场 Excel 直接另存的 CSV：

        1. 孔口坐标里的逗号常把一格劈成两格——前两格固定是孔号、勘探区，从第三格起
           尝试把相邻格用逗号拼回，直到能解析成成对坐标为止；
        2. Excel 另存时行尾逗号产生的空尾列可能被 csv 解析器吞掉，需要按原始行的
           尾逗号数补齐，否则「现场确认」等后段字段会错位。
        标准写法（坐标列已加引号或用分号/制表符分隔）不受影响。
        """
        coord_index = ALL_COLUMNS.index(FIELD_COORD)
        merged = False
        if len(cells) > coord_index:
            head, tail = cells[:coord_index], cells[coord_index:]
            if parse_coordinate(tail[0]) is None:
                for end in range(2, min(len(tail), 4) + 1):
                    joined = ",".join(tail[:end]).strip()
                    if parse_coordinate(joined) is not None:
                        cells = head + [joined] + tail[end:]
                        merged = True
                        break
        # csv 解析器会丢掉全部行尾空列：每个尾逗号对应一个被丢的空列；
        # 若做过坐标合并，合并掉的那个逗号也需要补回一个尾列名额。
        stripped_line = raw_line.rstrip("\r\n")
        trailing_commas = len(stripped_line) - len(stripped_line.rstrip(","))
        expected = len(cells) + trailing_commas + (1 if merged else 0)
        if len(cells) < expected:
            cells = cells + [""] * (expected - len(cells))
        return cells

    def _parse_line(self, line_no: int, values: list[str], raw_line: str = "") -> dict[str, Any] | str:
        cells = self._normalize_cells([item.strip() for item in values], raw_line)
        row: dict[str, Any] = {"source_line": line_no}
        for offset, column in enumerate(ALL_COLUMNS):
            row[column] = cells[offset] if offset < len(cells) else ""
        row[FIELD_CONFIRMED] = _is_confirmed(row.get(FIELD_CONFIRMED))
        row[FIELD_ALIASES] = _split_aliases(row.get(FIELD_ALIASES))
        for field in REQUIRED_COLUMNS:
            if not str(row.get(field) or "").strip():
                return f"第 {line_no} 行缺少必填字段「{field}」，请补全后从该行继续解析"
        coordinate = parse_coordinate(row.get(FIELD_COORD))
        if coordinate is None:
            return f"第 {line_no} 行孔口坐标「{row.get(FIELD_COORD)}」无法识别为成对坐标，请修正后从该行继续解析"
        row["坐标归一"], row["x"], row["y"] = coordinate
        return row

    @staticmethod
    def _refresh_row_coordinate(row: dict[str, Any]) -> None:
        coordinate = parse_coordinate(row.get(FIELD_COORD))
        if coordinate is None:
            return
        row["坐标归一"], row["x"], row["y"] = coordinate

    # ------------------------------------------------------------------
    # 冲突预检：勘探区 + 孔口坐标 + 孔号（含历史别名）
    # ------------------------------------------------------------------
    def _ledger_snapshot(self) -> tuple[dict[str, dict[str, Any]], dict[tuple[float, float], dict[str, Any]]]:
        identity: dict[str, dict[str, Any]] = {}
        coordinates: dict[tuple[float, float], dict[str, Any]] = {}
        for ledger_row in store.rows(LEDGER_MODULE):
            code = str(ledger_row.get(FIELD_CODE, "")).strip()
            if code:
                identity[code] = ledger_row
            for alias in ledger_row.get(FIELD_ALIASES, []) or []:
                identity[str(alias).strip()] = ledger_row
            coordinate = parse_coordinate(ledger_row.get(FIELD_COORD))
            if coordinate is not None:
                coordinates[(coordinate[1], coordinate[2])] = ledger_row
        return identity, coordinates

    def _precheck_and_maybe_land(self, artifact: dict[str, Any]) -> None:
        identity_index, coord_index = self._ledger_snapshot()
        conflicts: list[dict[str, Any]] = []
        seen_codes: dict[str, int] = {}
        seen_coords: dict[tuple[float, float], int] = {}
        decisions: list[dict[str, Any]] = []
        for row in artifact["parsed_rows"]:
            line_no = int(row["source_line"])
            code = str(row[FIELD_CODE]).strip()
            coord_key = (float(row["x"]), float(row["y"]))
            decision = {"source_line": line_no, "kind": "new", "target_id": None}

            earlier_code = seen_codes.get(code)
            if earlier_code:
                conflicts.append(self._conflict(line_no, code, "FILE_DUP_CODE",
                                                f"孔号 {code} 在本文件第 {earlier_code} 行已出现，孔号不得重复"))
            earlier_coord = seen_coords.get(coord_key)
            if earlier_coord:
                conflicts.append(self._conflict(line_no, code, "FILE_DUP_COORD",
                                                f"孔口坐标 {row['坐标归一']} 与本文件第 {earlier_coord} 行重复"))
            seen_codes[code] = line_no
            seen_coords[coord_key] = line_no

            matched = identity_index.get(code)
            if matched is not None:
                current_code = str(matched.get(FIELD_CODE, "")).strip()
                if code == current_code:
                    conflicts.append(self._conflict(
                        line_no, code, "CODE_EXISTS",
                        f"孔号 {code} 已命中存量台账记录（台账ID {matched.get('id')}），整批不得入账"))
                else:
                    # 命中的是历史别名：同一孔位，勘探区必须一致；坐标差异以现场确认口径裁决
                    matched_coord = parse_coordinate(matched.get(FIELD_COORD))
                    same_coord = matched_coord is not None and (matched_coord[1], matched_coord[2]) == coord_key
                    same_area = str(matched.get(FIELD_AREA, "")).strip() == str(row[FIELD_AREA]).strip()
                    if not same_area:
                        # 勘探区对不上不能静默并入，即使勾选了现场确认也要退回人工核对
                        conflicts.append(self._conflict(
                            line_no, code, "ALIAS_COORD_CONFLICT",
                            f"旧孔号 {code} 对应台账孔号 {current_code}（ID {matched.get('id')}）位于"
                            f"「{matched.get(FIELD_AREA)}」，与清单勘探区「{row[FIELD_AREA]}」不一致，请现场核对"))
                    elif same_coord:
                        decision["kind"] = "alias_match"
                        decision["target_id"] = matched.get("id")
                    elif row[FIELD_CONFIRMED]:
                        decision["kind"] = "alias_coord_override"
                        decision["target_id"] = matched.get("id")
                        artifact.setdefault("notices", []).append(
                            f"第 {line_no} 行以现场确认坐标为准，更新孔号 {current_code}（旧孔号 {code}）孔口坐标为 {row['坐标归一']}")
                    else:
                        conflicts.append(self._conflict(
                            line_no, code, "ALIAS_COORD_CONFLICT",
                            f"旧孔号 {code} 对应台账孔号 {current_code}（ID {matched.get('id')}），"
                            f"孔口坐标与存量不一致，需现场确认坐标后整批重试"))
            else:
                coord_owner = coord_index.get(coord_key)
                if coord_owner is not None:
                    conflicts.append(self._conflict(
                        line_no, code, "COORD_EXISTS",
                        f"孔口坐标 {row['坐标归一']} 与存量孔号 {coord_owner.get(FIELD_CODE)}"
                        f"（ID {coord_owner.get('id')}）重合，疑似一孔多号"))

            # 文件里声明的历史别名也要查身份
            for alias in row.get(FIELD_ALIASES, []):
                owner = identity_index.get(alias)
                if owner is not None and owner is not matched:
                    conflicts.append(self._conflict(
                        line_no, code, "ALIAS_EXISTS",
                        f"历史别名 {alias} 已被存量孔号 {owner.get(FIELD_CODE)}（ID {owner.get('id')}）占用"))
            decisions.append(decision)

        artifact["conflicts"] = conflicts
        if conflicts:
            # 任一冲突：整批退至暂存文件，不允许只导入一半
            artifact["status"] = STATUS_CONFLICT
            artifact["landed_ids"] = []
            return
        self._land(artifact, decisions)

    @staticmethod
    def _conflict(line_no: int, code: str, code_type: str, message: str) -> dict[str, Any]:
        return {"source_line": line_no, "孔号": code, "type": code_type, "message": message}

    # ------------------------------------------------------------------
    # 一次性落库：台账 + 待办同步回写
    # ------------------------------------------------------------------
    def _land(self, artifact: dict[str, Any], decisions: list[dict[str, Any]]) -> None:
        decision_by_line = {item["source_line"]: item for item in decisions}
        landed_ids: list[int] = []
        landed_todos: list[int] = []
        for row in artifact["parsed_rows"]:
            decision = decision_by_line[row["source_line"]]
            if decision["kind"] == "new":
                entry = {
                    "id": store.next_id(LEDGER_MODULE),
                    FIELD_CODE: str(row[FIELD_CODE]).strip(),
                    FIELD_AREA: str(row[FIELD_AREA]).strip(),
                    FIELD_COORD: row["坐标归一"],
                    "设计孔深": str(row.get("设计孔深") or "").strip(),
                    "终孔深度": str(row.get("终孔深度") or "").strip(),
                    "开孔日期": str(row.get("开孔日期") or "").strip(),
                    "终孔日期": str(row.get("终孔日期") or "").strip(),
                    FIELD_ALIASES: list(row.get(FIELD_ALIASES, [])),
                    "status": "待施工",
                    "pending": True,
                    "abnormal": False,
                    "导入批次": artifact["batch_no"],
                }
                store.rows(LEDGER_MODULE).append(entry)
                landed_ids.append(entry["id"])
                todo_id = self._create_todo(entry, artifact["batch_no"])
                landed_todos.append(todo_id)
            elif decision["kind"] == "alias_coord_override":
                target = store.find(LEDGER_MODULE, int(decision["target_id"]))
                if target is not None:
                    # 现场确认坐标为准；旧坐标绝不能顶替文件里的新坐标
                    target[FIELD_COORD] = row["坐标归一"]
                    target["坐标来源"] = "现场确认"
                    aliases = target.setdefault(FIELD_ALIASES, [])
                    if str(row[FIELD_CODE]).strip() not in aliases:
                        aliases.append(str(row[FIELD_CODE]).strip())
        artifact["status"] = STATUS_LANDED
        artifact["conflicts"] = []
        artifact["landed_ids"] = landed_ids
        artifact["landed_todos"] = landed_todos

    def _create_todo(self, entry: dict[str, Any], batch_no: str) -> int:
        # 同一孔只保留一条未完成待办，避免重复批次产生重复待办
        for todo in store.rows(TODO_MODULE):
            if todo.get("钻孔编号") == entry[FIELD_CODE] and todo.get("status") != "已完成":
                return int(todo["id"])
        todo = {
            "id": store.next_id(TODO_MODULE),
            "borehole_id": entry["id"],
            "钻孔编号": entry[FIELD_CODE],
            "勘探区": entry[FIELD_AREA],
            "孔口坐标": entry[FIELD_COORD],
            "批次号": batch_no,
            "status": "待编录",
            "pending": True,
            "来源": "分批入账",
            "created_at": _now(),
            "completed_at": None,
        }
        store.rows(TODO_MODULE).append(todo)
        return int(todo["id"])

    # ------------------------------------------------------------------
    # 修复与续解析
    # ------------------------------------------------------------------
    def repair_and_resume(self, batch_no: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_migration()
            artifact = self._load_artifact(batch_no)
            raw_content = str(payload.get("content") or "")
            full_reparse = bool(raw_content)
            if raw_content:
                # 整份替换：换文件内容后不拿任何旧坐标，全量重解析后重新过闸
                artifact["raw_text"] = raw_content
                artifact["content_fingerprint"] = _sha256_text(raw_content)
                artifact["notices"] = []
            line_no = payload.get("line") or payload.get("source_line")
            values = payload.get("values")
            if line_no and isinstance(values, dict):
                self._apply_row_repair(artifact, int(line_no), values)
            artifact["updated_at"] = _now()
            if artifact["status"] == STATUS_LANDED:
                return {"ok": True, "message": "该批次已入账，无需继续解析", "batch": self._registry_view(artifact)}
            resume_from = artifact.get("failed_line")
            self._parse(artifact, resume=True, full_reparse=full_reparse)
            artifact["updated_at"] = _now()
            self._persist(artifact)
            return {
                "ok": artifact["status"] == STATUS_LANDED,
                "resumed_from_line": resume_from,
                "message": self._status_message(artifact),
                "batch": self._registry_view(artifact),
            }

    def _apply_row_repair(self, artifact: dict[str, Any], line_no: int, values: dict[str, Any]) -> None:
        target = next((row for row in artifact["parsed_rows"] if row["source_line"] == line_no), None)
        lines = artifact["raw_text"].splitlines()
        if target is None:
            # 坏行此前没解析成功：保留原行其它字段，只覆盖被修复的列
            original: dict[str, Any] = {}
            if 1 <= line_no <= len(lines):
                original_cells = self._normalize_cells([
                    item.strip() for item in next(csv.reader(io.StringIO(lines[line_no - 1]), delimiter=artifact["delimiter"]))
                ])
                original = {column: (original_cells[offset] if offset < len(original_cells) else "")
                            for offset, column in enumerate(ALL_COLUMNS)}
            merged = {**original, **{key: val for key, val in values.items() if key in ALL_COLUMNS}}
            if 1 <= line_no <= len(lines):
                lines[line_no - 1] = self._rebuild_line(artifact, merged)
                artifact["raw_text"] = "\n".join(lines)
            return
        for column, value in values.items():
            if column in ALL_COLUMNS:
                target[column] = value
        target[FIELD_CONFIRMED] = _is_confirmed(target.get(FIELD_CONFIRMED))
        target[FIELD_ALIASES] = _split_aliases(target.get(FIELD_ALIASES))
        coordinate = parse_coordinate(target.get(FIELD_COORD))
        if coordinate is not None:
            target["坐标归一"], target["x"], target["y"] = coordinate
        lines = artifact["raw_text"].splitlines()
        if 1 <= line_no <= len(lines):
            lines[line_no - 1] = self._rebuild_line(artifact, target)
            artifact["raw_text"] = "\n".join(lines)

    @staticmethod
    def _rebuild_line(artifact: dict[str, Any], values: dict[str, Any]) -> str:
        delimiter = artifact["delimiter"]
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=delimiter, lineterminator="")
        writer.writerow([str(values.get(column, "")) for column in ALL_COLUMNS])
        return buffer.getvalue()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_batches(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(store.rows(IMPORT_MODULE))

    def get_batch(self, batch_no: str) -> dict[str, Any] | None:
        with self._lock:
            path = self._staging_path(batch_no)
            if path.exists():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return None
            for row in store.rows(IMPORT_MODULE):
                if row.get("batch_no") == batch_no:
                    return row
            return None

    def list_todos(self, *, status: str | None = None, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = list(store.rows(TODO_MODULE))
            if status:
                rows = [row for row in rows if row.get("status") == status]
            total = len(rows)
            start = max(page - 1, 0) * size
            return rows[start:start + size], total

    def complete_todo(self, todo_id: int) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            todo = store.find(TODO_MODULE, todo_id)
            if todo is None:
                return None, f"编录待办 {todo_id} 不存在"
            if todo.get("status") == "已完成":
                return todo, "该待办已完成，未重复操作"
            todo["status"] = "已完成"
            todo["pending"] = False
            todo["completed_at"] = _now()
            return todo, "编录待办已完成"

    def _load_artifact(self, batch_no: str) -> dict[str, Any]:
        path = self._staging_path(batch_no)
        if not path.exists():
            raise KeyError(batch_no)
        return json.loads(path.read_text(encoding="utf-8"))

    def _persist(self, artifact: dict[str, Any]) -> None:
        artifact["updated_at"] = _now()
        if artifact["status"] == STATUS_LANDED:
            self._remove_staging(str(artifact["batch_no"]))
        else:
            self._save_staging(artifact)
        self._upsert_registry(artifact)
