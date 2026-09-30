"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_staging_dir() -> Path:
    """暂存文件目录：可用环境变量覆盖，默认放在后端运行目录下（已纳入 .gitignore）。"""
    return Path(os.environ.get("BOREHOLE_STAGING_DIR", Path(__file__).resolve().parent.parent / "var" / "staging"))


@dataclass(frozen=True)
class Settings:
    app_name: str = "地质勘探数据管理平台"
    env: str = "local"
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 分批入账闸门的暂存文件目录：预检未过、解析中断的批次都落在这里
    staging_dir: Path = field(default_factory=_default_staging_dir)


settings = Settings()
