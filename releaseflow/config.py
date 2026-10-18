from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


APP_NAME = "ReleaseFlow"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 ReleaseFlow/1.0"
)

DEFAULT_MIRRORS = [
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
]


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / ".local" / "share")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_download_dir() -> Path:
    downloads = Path.home() / "Downloads"
    return downloads if downloads.exists() else Path.home()


@dataclass
class Settings:
    download_dir: str = ""
    threads: int = 8
    mirror_mode: str = "auto"
    selected_mirror: str = "https://ghfast.top/"
    allow_insecure_tls: bool = False
    proxies: str = ""
    github_token: str = ""
    token_from_env: bool = True

    def __post_init__(self) -> None:
        if not self.download_dir:
            self.download_dir = str(default_download_dir())
        self.threads = min(32, max(1, int(self.threads or 8)))
        self.mirror_mode = (
            self.mirror_mode
            if self.mirror_mode in {"auto", "mirror", "direct"}
            else "auto"
        )
        if self.selected_mirror not in DEFAULT_MIRRORS:
            self.selected_mirror = DEFAULT_MIRRORS[0]


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def load_settings() -> Settings:
    path = settings_path()
    if not path.exists():
        return Settings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        allowed = {field.name for field in fields(Settings)}
        return Settings(**{key: value for key, value in raw.items() if key in allowed})
    except (OSError, ValueError, TypeError):
        return Settings()


def effective_github_token(settings: Settings) -> str:
    """Prefer the saved token, then standard environment variables."""
    if settings.github_token.strip():
        return settings.github_token.strip()
    if not settings.token_from_env:
        return ""
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def save_settings(settings: Settings) -> None:
    path = settings_path()
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp, path)


def resource_path(relative: str) -> Path:
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return bundle_dir / relative


def to_json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return value
