"""分批入账闸门测试夹具：每个用例隔离内存台账与暂存目录。"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_STAGING = _BACKEND_ROOT / "var" / "test-staging"
os.environ["BOREHOLE_STAGING_DIR"] = str(_STAGING)
sys.path.insert(0, str(_BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.routers.borehole_import import gate  # noqa: E402
from app.services.borehole import BoreholeService  # noqa: E402
from app.store import store  # noqa: E402

HEADER = "钻孔编号,勘探区,孔口坐标,现场确认,历史别名,设计孔深,终孔深度,开孔日期,终孔日期"


@pytest.fixture
def client():
    if _STAGING.exists():
        shutil.rmtree(_STAGING)
    _STAGING.mkdir(parents=True, exist_ok=True)
    store.reset()
    BoreholeService().migrate_legacy_aliases()
    gate._migrated = True
    with TestClient(app) as test_client:
        yield test_client
    if _STAGING.exists():
        shutil.rmtree(_STAGING)


def csv_file(content: str, name: str = "钻孔清单.csv"):
    return {"file": (name, content.encode("utf-8"), "text/csv")}


def ledger_codes() -> list[str]:
    return [str(row["钻孔编号"]) for row in store.rows("borehole")]


def ledger_by_code(code: str) -> dict:
    return next(row for row in store.rows("borehole") if row["钻孔编号"] == code)
