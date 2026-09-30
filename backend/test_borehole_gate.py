"""分批入账闸门端到端测试：用 FastAPI TestClient 直接打接口。

覆盖需求：
1. 冲突预检（勘探区 + 孔口坐标）全部通过才整批落库；
2. 任一孔号命中存量 → 整批退暂存，绝不只导入一半；
3. 同文件指纹幂等，只生效一次，不生成第二份台账；
4. 解析中断从失败行续传，且坐标以续传文件为准（不用旧坐标顶替）；
5. 早期孔号迁移补历史别名，坐标冲突以现场确认坐标为准；
6. 落库结果同步回写台账与编录待办；
7. 暂存文件可下载。
"""
from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path

# 在导入应用前把暂存目录指到临时目录，避免污染真实 var/staging。
TMP_STAGING = tempfile.mkdtemp(prefix="borehole_staging_")
os.environ.setdefault("BOREHOLE_STAGING_DIR", TMP_STAGING)

from starlette.testclient import TestClient  # noqa: E402

import app.config as config_module  # noqa: E402
import app.store as store_module  # noqa: E402

# config 是 frozen dataclass，用 object.__setattr__ 临时改暂存目录。
object.__setattr__(config_module.settings, "staging_dir", TMP_STAGING)

from app.main import app  # noqa: E402

client = TestClient(app)

HEADER = "钻孔编号,勘探区,孔口坐标,设计孔深,终孔深度,开孔日期,终孔日期,钻孔状态"
PASS_MARK = "✓"
FAIL_MARK = "✗"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {PASS_MARK if condition else FAIL_MARK} {name}" + (f" —— {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(f"{name}: {detail}")


def ledger_count() -> int:
    return client.get("/api/borehole?size=200").json()["total"]


def todo_count() -> int:
    return client.get("/api/borehole/todos").json()["total"]


def upload_csv(content: str, resume=None):
    body = {"content": content}
    if resume is not None:
        body["resume_batch_id"] = resume
    return client.post("/api/borehole/imports", json=body)


def import_service_get_superseded(batch_id: int):
    batch = store_module.store.find("borehole_import_batch", batch_id)
    return batch.get("已被批次续作") if batch else None


print("== 初始状态 ==")
before = ledger_count()
before_todos = todo_count()
check("种子台账有 3 条", before == 3, f"actual={before}")
check("初始编录待办为 0（种子未落待办）", before_todos == 0, f"actual={before_todos}")


print("== 场景1：整批预检通过 → 整批落库并回写待办 ==")
csv1 = "\n".join([
    HEADER,
    "ZK-100,北区,X=500.0/Y=300.0,120,,,2026-09-10,,",
    "ZK-101,北区,X=500/Y=310,80,,,,,",
    "ZK-102,南区,X=600/Y=400,60,,,,,",
])
r = upload_csv(csv1)
data = r.json()
check("HTTP 200 且 ok", r.status_code == 200 and data["ok"] is True, str(data.get("message")))
check("入账数 = 3", data["batch"]["入账数"] == 3, str(data["batch"].get("入账数")))
check("台账新增 3 条", ledger_count() == before + 3, f"{ledger_count()} vs {before+3}")
check("待办新增 3 条", todo_count() == before_todos + 3, f"{todo_count()}")
check("台账里能查到新孔 ZK-100",
      any(row["钻孔编号"] == "ZK-100" for row in client.get("/api/borehole?size=200").json()["items"]))
check("新孔状态为待施工且 pending",
      all(row["status"] == "待施工" and row["pending"] for row in client.get("/api/borehole?size=200").json()["items"] if row["钻孔编号"].startswith("ZK-1")))


print("== 场景2：同一文件重复上传 → 指纹幂等，只生效一次 ==")
r2 = upload_csv(csv1)
d2 = r2.json()
check("返回 reused=True", d2["reused"] is True, str(d2.get("message")))
check("已入账但不再新增台账", ledger_count() == before + 3 and d2["batch"]["批次状态"] == "已入账",
      f"count={ledger_count()}")
# 连传三次确认
upload_csv(csv1)
check("第三次上传台账仍不增加", ledger_count() == before + 3, f"count={ledger_count()}")
check("待办也不重复生成", todo_count() == before_todos + 3, f"todos={todo_count()}")


print("== 场景3：孔号命中存量（含历史别名）→ 整批退暂存，不导入一半 ==")
# 三行：第一行全新，第二行孔号撞到存量 ZK-100；整批都不得落库
csv_conflict_code = "\n".join([
    HEADER,
    "ZK-200,西区,X=700/Y=500,50,,,,,",
    "ZK-100,北区,X=500/Y=300,90,,,,,",  # 孔号已存在
    "ZK-201,西区,X=701/Y=501,50,,,,,",
])
r = upload_csv(csv_conflict_code)
d = r.json()
check("ok=False", d["ok"] is False, str(d.get("message")))
check("批次状态=冲突退暂存", d["batch"]["批次状态"] == "冲突退暂存", d["batch"].get("批次状态"))
check("入账数=0", d["batch"]["入账数"] == 0)
check("台账没有任何新增（ZK-200/201 也未落库）",
      ledger_count() == before + 3 and
      not any(row["钻孔编号"] in ("ZK-200", "ZK-201") for row in client.get("/api/borehole?size=200").json()["items"]),
      "出现半批导入")
check("冲突明细包含「孔号已存在」",
      any(c["冲突类型"] == "孔号已存在" for c in d["batch"]["冲突"]), str(d["batch"]["冲突"]))
check("暂存文件已生成且可下载",
      Path(d["batch"]["暂存文件"]).exists())
dl = client.get(f"/api/borehole/imports/{d['batch']['id']}/staging-file")
check("暂存文件下载 200 且内容为原件", dl.status_code == 200 and dl.text == csv_conflict_code)


print("== 场景4：同勘探区同坐标（坐标写法不同）→ 坐标冲突退暂存 ==")
csv_coord = "\n".join([
    HEADER,
    "ZK-300,北区,X=500.000/Y=300.000,40,,,,,",  # 与 ZK-100 同区同点（写法归一后相同）
])
r = upload_csv(csv_coord)
d = r.json()
check("坐标归一后识别为重合",
      (not d["ok"]) and any(c["冲突类型"] == "坐标与存量重合" for c in d["batch"]["冲突"]),
      str(d["batch"].get("冲突")))
check("整批未落库", ledger_count() == before + 3)


print("== 场景5：解析中断 → 从失败行续传，坐标用新文件 ==")
# 第2行有效；第3行缺坐标 → 在第3行中断
csv_bad = "\n".join([
    HEADER,
    "ZK-400,东区,X=800/Y=600,70,,,,,",
    "ZK-401,东区,,70,,,,,",  # 缺孔口坐标
    "ZK-402,东区,X=802/Y=602,70,,,,,",
])
r = upload_csv(csv_bad)
d = r.json()
check("解析中断状态", (not d["ok"]) and d["batch"]["批次状态"] == "解析中断", str(d.get("message")))
check("失败行=3", d["batch"]["失败行"] == 3, str(d["batch"].get("失败行")))
check("中断时未落库任何行（ZK-400 也不入）",
      not any(row["钻孔编号"] == "ZK-400" for row in client.get("/api/borehole?size=200").json()["items"]))
parent_id = d["batch"]["id"]

# 续传文件：表头 + 从原失败行（第3行）开始的修正数据。
# 关键：给失败行一个“正确的新坐标”，验证不会用任何旧坐标顶替（旧的本就是空，更不能凭空造）。
resume_csv = "\n".join([
    HEADER,
    "ZK-401,东区,X=801/Y=601,70,,,,,",  # 原第3行，修正
    "ZK-402,东区,X=802/Y=602,70,,,,,",  # 原第4行
])
rr = upload_csv(resume_csv, resume=parent_id)
dd = rr.json()
check("续传成功且整批入账", dd["ok"] is True, str(dd.get("message")))
# 续传批次 = 前缀1行(ZK-400) + 续传2行 = 3 行
check("续传入账数=3（前缀1 + 接续2）", dd["batch"]["入账数"] == 3, str(dd["batch"].get("入账数")))
items = client.get("/api/borehole?size=200").json()["items"]
z401 = next((row for row in items if row["钻孔编号"] == "ZK-401"), None)
check("ZK-401 坐标取自续传文件（801/601），非空、非旧值",
      z401 is not None and "801" in str(z401["孔口坐标"]) and "601" in str(z401["孔口坐标"]),
      str(z401 and z401["孔口坐标"]))
check("台账累计 +3", ledger_count() == before + 6, f"count={ledger_count()}")
check("父批次被标记为已被续作",
      import_service_get_superseded(parent_id) == dd["batch"]["id"])


print("== 场景6：历史别名迁移（别名补齐 + 现场确认坐标优先） ==")
mig = {"items": [
    {"钻孔编号": "BORE-0001", "历史别名": ["OLD-1", "旧1号"], "现场确认坐标": "X=900,Y=700"},
]}
r = client.post("/api/borehole/alias-migrations", json=mig)
d = r.json()
check("迁移成功", d["ok"] is True, str(d.get("message")))
b1 = client.get("/api/borehole/1").json()
check("BORE-0001 坐标被现场确认值覆盖", "900" in str(b1["孔口坐标"]) and "700" in str(b1["孔口坐标"]),
      str(b1.get("孔口坐标")))
check("记录了坐标来源=现场确认", b1.get("坐标来源") == "现场确认", str(b1.get("坐标来源")))

# 用迁移后的别名去导入 → 应命中历史别名并整批退档
csv_alias = "\n".join([
    HEADER,
    "OLD-1,钻孔编录样例1,X=900/Y=700,30,,,,,",
])
r = upload_csv(csv_alias)
d = r.json()
check("历史别名导入被拦（命中历史别名）",
      (not d["ok"]) and any(c["冲突类型"] == "命中历史别名" for c in d["batch"]["冲突"]),
      str(d["batch"].get("冲突")))
check("别名冲突同样零入账", ledger_count() == before + 6)

# 迁移预检失败 → 整批不动（别名占用别的孔）
mig_bad = {"items": [
    {"钻孔编号": "BORE-0002", "历史别名": ["OLD-1"]},  # OLD-1 已挂在 BORE-0001
]}
r = client.post("/api/borehole/alias-migrations", json=mig_bad)
d = r.json()
check("别名占用他孔时整批迁移被拒", d["ok"] is False, str(d.get("message")))


print("== 场景7：批次台账可按指纹查询、待办可列 ==")
digest = client.get("/api/borehole/imports").json()["items"][-1]["指纹"]
by_fp = client.get(f"/api/borehole/imports?fingerprint={digest}").json()
check("按指纹能检索到批次", by_fp["total"] >= 1)
check("待办清单只含 pending 项", all(t["pending"] for t in client.get("/api/borehole/todos").json()["items"]))


print()
if failures:
    print(f"FAIL：{len(failures)} 项未通过")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("全部断言通过 ✓")
