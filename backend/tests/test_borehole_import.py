"""分批入账闸门业务规则测试。"""
from __future__ import annotations

from conftest import HEADER, csv_file, ledger_by_code, ledger_codes


def test_clean_batch_lands_and_syncs_todo(client):
    content = "\n".join([
        HEADER,
        "ZK101,北山矿区,X=4452200.000, Y=38512400.000,否,,300,,,",
        "ZK102,南坡矿区,4452250.5 38512450.8,否,,260,,,",
    ])
    response = client.post("/api/borehole/imports", files=csv_file(content))
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["batch"]["status"] == "landed"
    assert data["batch"]["landed_count"] == 2
    assert set(data["batch"]["landed_ids"]) <= {row["id"] for row in client.get("/api/borehole").json()["items"]}

    todos = client.get("/api/borehole/todos").json()
    assert todos["total"] == 2
    todo_codes = {item["钻孔编号"] for item in todos["items"]}
    assert todo_codes == {"ZK101", "ZK102"}
    assert all(item["status"] == "待编录" for item in todos["items"])


def test_existing_code_rejects_entire_batch_without_half_import(client):
    content = "\n".join([
        HEADER,
        "ZK101,北山矿区,X=4452200.000, Y=38512400.000,否,,300,,,",
        "ZK001,北山矿区,X=4452999.000, Y=38512999.000,否,,300,,,",
    ])
    data = client.post("/api/borehole/imports", files=csv_file(content)).json()
    assert data["ok"] is False
    assert data["batch"]["status"] == "conflict_rejected"
    conflict_types = {item["type"] for item in data["batch"]["conflicts"]}
    assert "CODE_EXISTS" in conflict_types

    # 命中存量孔号：同行的新孔 ZK101 也不许落库，台账与待办都不能出现半批
    assert "ZK101" not in ledger_codes()
    assert client.get("/api/borehole/todos").json()["total"] == 0
    # 整批退至暂存文件，批次可查
    batch_no = data["batch"]["batch_no"]
    detail = client.get(f"/api/borehole/imports/{batch_no}").json()
    assert len(detail["parsed_rows"]) == 2
    assert len(detail["conflicts"]) >= 1


def test_same_file_fingerprint_is_idempotent(client):
    content = "\n".join([
        HEADER,
        "ZK201,北山矿区,X=4452300.000, Y=38512500.000,否,,200,,,",
    ])
    first = client.post("/api/borehole/imports", files=csv_file(content, name="a.csv")).json()
    assert first["ok"] is True
    landed_after_first = len(ledger_codes())

    second = client.post("/api/borehole/imports", files=csv_file(content, name="a-copy.csv")).json()
    assert second["idempotent"] is True
    assert second["ok"] is False
    assert second["batch"]["batch_no"] == first["batch"]["batch_no"]
    # 不得生成第二份台账：钻孔数与导入批次数都不增长
    assert len(ledger_codes()) == landed_after_first
    assert len(client.get("/api/borehole/imports").json()) == 1


def test_duplicate_file_of_staging_batch_also_idempotent(client):
    content = "\n".join([
        HEADER,
        "ZK001,北山矿区,X=4452999.000, Y=38512999.000,否,,300,,,",
    ])
    first = client.post("/api/borehole/imports", files=csv_file(content)).json()
    assert first["batch"]["status"] == "conflict_rejected"
    second = client.post("/api/borehole/imports", files=csv_file(content)).json()
    assert second["idempotent"] is True
    assert second["batch"]["batch_no"] == first["batch"]["batch_no"]
    assert len(client.get("/api/borehole/imports").json()) == 1


def test_parse_interrupts_and_resumes_from_failed_line(client):
    content = "\n".join([
        HEADER,
        "ZK301,北山矿区,X=4452310.000, Y=38512510.000,否,,200,,,",
        "ZK302,北山矿区,坐标缺失,否,,200,,,",
        "ZK303,北山矿区,X=4452330.000, Y=38512530.000,否,,200,,,",
    ])
    first = client.post("/api/borehole/imports", files=csv_file(content)).json()
    assert first["batch"]["status"] == "parsing_failed"
    assert first["batch"]["failed_line"] == 3
    assert first["batch"]["parsed_rows"] == 1
    # 中断时没有任何行落库
    assert "ZK301" not in ledger_codes()

    batch_no = first["batch"]["batch_no"]
    fixed = content.replace("ZK302,北山矿区,坐标缺失,", "ZK302,北山矿区,X=4452320.000, Y=38512520.000,")
    resumed = client.post(
        f"/api/borehole/imports/{batch_no}/resume",
        json={"content": fixed},
    ).json()
    assert resumed["ok"] is True
    assert resumed["resumed_from_line"] == 3
    assert resumed["batch"]["status"] == "landed"
    assert {ledger_by_code(code)["钻孔编号"] for code in ("ZK301", "ZK302", "ZK303")} == {"ZK301", "ZK302", "ZK303"}
    assert client.get("/api/borehole/todos").json()["total"] == 3


def test_resume_uses_current_coordinates_not_old_ones(client):
    # 第 3 行先是坏坐标；修好时给出与第一版完全不同的坐标，落库必须采用修正值
    content = "\n".join([
        HEADER,
        "ZK401,北山矿区,X=4452400.000, Y=38512600.000,否,,200,,,",
        "ZK402,北山矿区,坏坐标,否,,200,,,",
    ])
    first = client.post("/api/borehole/imports", files=csv_file(content)).json()
    batch_no = first["batch"]["batch_no"]

    fixed = "\n".join([
        HEADER,
        "ZK401,北山矿区,X=4452411.111, Y=38512611.222,否,,200,,,",
        "ZK402,北山矿区,X=4452422.333, Y=38512622.444,否,,200,,,",
    ])
    resumed = client.post(f"/api/borehole/imports/{batch_no}/resume", json={"content": fixed}).json()
    assert resumed["ok"] is True
    assert ledger_by_code("ZK401")["孔口坐标"] == "X=4452411.111, Y=38512611.222"
    assert ledger_by_code("ZK402")["孔口坐标"] == "X=4452422.333, Y=38512622.444"


def test_legacy_alias_match_with_same_coordinates_passes(client):
    # BSZK1 是 ZK001 的历史别名，且勘探区/坐标与存量一致：视作同一孔，整批通过
    content = "\n".join([
        HEADER,
        "BSZK1,北山矿区,X=4452100.500, Y=38512300.250,否,,,,,,",
        "ZK501,北山矿区,X=4452500.000, Y=38512700.000,否,,200,,,",
    ])
    data = client.post("/api/borehole/imports", files=csv_file(content)).json()
    assert data["ok"] is True, data["batch"]["conflicts"]
    # 别名命中不新建台账，只有真正的新孔 ZK501 入库并产生一条待办
    assert "ZK501" in ledger_codes()
    assert len(ledger_codes()) == 4
    todos = client.get("/api/borehole/todos").json()["items"]
    assert [item["钻孔编号"] for item in todos] == ["ZK501"]


def test_alias_coordinate_conflict_requires_field_confirmation(client):
    content = "\n".join([
        HEADER,
        "BSZK1,北山矿区,X=4452100.999, Y=38512300.999,否,,,,,,",
    ])
    rejected = client.post("/api/borehole/imports", files=csv_file(content)).json()
    assert rejected["batch"]["status"] == "conflict_rejected"
    assert rejected["batch"]["conflicts"][0]["type"] == "ALIAS_COORD_CONFLICT"
    # 存量坐标未被改写
    assert ledger_by_code("ZK001")["孔口坐标"] == "X=4452100.500, Y=38512300.250"

    # 同一暂存批次勾选「现场确认」后整批重试：现场坐标为准覆盖旧坐标
    batch_no = rejected["batch"]["batch_no"]
    confirmed = content.replace(",否,", ",是,")
    resumed = client.post(f"/api/borehole/imports/{batch_no}/resume", json={"content": confirmed}).json()
    assert resumed["ok"] is True
    assert ledger_by_code("ZK001")["孔口坐标"] == "X=4452100.999, Y=38512300.999"
    assert ledger_by_code("ZK001").get("坐标来源") == "现场确认"
    assert "BSZK1" in ledger_by_code("ZK001")["历史别名"]


def test_coordinate_collision_with_different_code_rejects_batch(client):
    content = "\n".join([
        HEADER,
        "ZK601,北山矿区,X=4452100.500, Y=38512300.250,否,,,,,,",
    ])
    data = client.post("/api/borehole/imports", files=csv_file(content)).json()
    assert data["batch"]["status"] == "conflict_rejected"
    assert data["batch"]["conflicts"][0]["type"] == "COORD_EXISTS"
    assert "ZK601" not in ledger_codes()


def test_intra_file_duplicates_reject_batch(client):
    content = "\n".join([
        HEADER,
        "ZK701,北山矿区,X=4452700.000, Y=38512800.000,否,,,,,,",
        "ZK701,南坡矿区,X=4452701.000, Y=38512801.000,否,,,,,,",
        "ZK702,南坡矿区,X=4452700.000, Y=38512800.000,否,,,,,,",
    ])
    data = client.post("/api/borehole/imports", files=csv_file(content)).json()
    types = {item["type"] for item in data["batch"]["conflicts"]}
    assert types == {"FILE_DUP_CODE", "FILE_DUP_COORD"}


def test_declared_alias_owned_by_other_borehole_rejects_batch(client):
    # 新孔 ZK711 声明的历史别名 BSZK1 已被存量 ZK001 占用：整批退回
    content = "\n".join([
        HEADER,
        "ZK711,北山矿区,X=4452711.000, Y=38512811.000,否,BSZK1,,,,,",
    ])
    data = client.post("/api/borehole/imports", files=csv_file(content)).json()
    assert data["batch"]["status"] == "conflict_rejected"
    assert data["batch"]["conflicts"][0]["type"] == "ALIAS_EXISTS"
    assert "ZK711" not in ledger_codes()


def test_alias_area_mismatch_even_confirmed_reports_conflict(client):
    # 历史别名对上但勘探区不同：即使勾选现场确认也不能静默并入，要求现场核对
    content = "\n".join([
        HEADER,
        "BSZK1,南坡矿区,X=4452100.500, Y=38512300.250,是,,,,,,",
    ])
    data = client.post("/api/borehole/imports", files=csv_file(content)).json()
    assert data["batch"]["status"] == "conflict_rejected"
    assert data["batch"]["conflicts"][0]["type"] == "ALIAS_COORD_CONFLICT"
    # 勘探区与孔口坐标都不得被改动
    assert ledger_by_code("ZK001")["勘探区"] == "北山矿区"
    assert ledger_by_code("ZK001")["孔口坐标"] == "X=4452100.500, Y=38512300.250"


def test_repair_single_failed_line_then_resume(client):
    content = "\n".join([
        HEADER,
        "ZK801,北山矿区,,否,,200,,,",
        "ZK802,北山矿区,X=4452802.000, Y=38512902.000,否,,200,,,",
    ])
    first = client.post("/api/borehole/imports", files=csv_file(content)).json()
    assert first["batch"]["failed_line"] == 2

    batch_no = first["batch"]["batch_no"]
    resumed = client.post(
        f"/api/borehole/imports/{batch_no}/resume",
        json={"line": 2, "values": {"孔口坐标": "X=4452801.000, Y=38512901.000"}},
    ).json()
    assert resumed["ok"] is True
    assert ledger_by_code("ZK801")["孔口坐标"] == "X=4452801.000, Y=38512901.000"


def test_staging_file_survives_gate_rebuild(client):
    content = "\n".join([
        HEADER,
        "ZK001,北山矿区,X=4452999.000, Y=38512999.000,否,,,,,,",
    ])
    first = client.post("/api/borehole/imports", files=csv_file(content)).json()
    assert first["batch"]["status"] == "conflict_rejected"
    batch_no = first["batch"]["batch_no"]

    # 模拟服务重启：重建闸门实例，未完成批次从暂存文件接回，且仍可续解析
    from app.routers import borehole_import as import_router
    import_router.gate = type(import_router.gate)()
    batches = client.get("/api/borehole/imports").json()
    assert any(item["batch_no"] == batch_no for item in batches)

    fixed = content.replace("ZK001", "BSZK1").replace("X=4452999.000, Y=38512999.000", "X=4452100.500, Y=38512300.250")
    resumed = client.post(f"/api/borehole/imports/{batch_no}/resume", json={"content": fixed}).json()
    assert resumed["ok"] is True


def test_complete_todo_is_idempotent(client):
    content = "\n".join([HEADER, "ZK901,北山矿区,X=4452901.000, Y=38512001.000,否,,200,,,"])
    client.post("/api/borehole/imports", files=csv_file(content)).json()
    todo_id = client.get("/api/borehole/todos").json()["items"][0]["id"]

    first = client.post(f"/api/borehole/todos/{todo_id}/actions", json={"values": {"action": "完成编录"}}).json()
    assert first["ok"] is True
    assert first["entry"]["status"] == "已完成"
    second = client.post(f"/api/borehole/todos/{todo_id}/actions", json={"values": {"action": "完成编录"}}).json()
    assert "已完成" in second["message"]


def test_excel_style_csv_with_quoted_coordinates_and_confirm_flag(client):
    # Excel 另存形态：坐标含逗号已加引号、行尾逗号，「现场确认=是」必须落在正确列上
    content = "\n".join([
        HEADER,
        'BSZK1,北山矿区,"X=4452100.999, Y=38512300.999",是,,200,,,',
    ])
    data = client.post("/api/borehole/imports", files=csv_file(content)).json()
    assert data["ok"] is True, data["batch"]["conflicts"]
    assert ledger_by_code("ZK001")["孔口坐标"] == "X=4452100.999, Y=38512300.999"
    assert ledger_by_code("ZK001").get("坐标来源") == "现场确认"


def test_empty_file_rejected(client):
    response = client.post("/api/borehole/imports", files=csv_file("   \n "))
    assert response.status_code == 400


def test_template_endpoint(client):
    data = client.get("/api/borehole/imports/template").json()
    assert data["required"] == ["钻孔编号", "勘探区", "孔口坐标"]
    assert "现场确认" in data["columns"]
    # 模板 CSV 的坐标列已加引号，按模板填完即可顺利过闸
    landed = client.post("/api/borehole/imports", files=csv_file(data["csv"], name="模板.csv")).json()
    assert landed["ok"] is True, landed["batch"]
