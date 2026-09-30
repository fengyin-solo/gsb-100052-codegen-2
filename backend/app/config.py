"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# 钻孔清单预检失败 / 解析中断时，整批退回的暂存目录（项目根 backend/var/staging）。
_STAGING_DIR = Path(__file__).resolve().parent.parent / "var" / "staging"


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
    # 分批入账闸门的暂存文件目录：冲突退档、解析中断的原件都落到这里。
    staging_dir: str = str(_STAGING_DIR)


settings = Settings()
