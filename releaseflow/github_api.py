from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote, urlparse

import requests

from .config import USER_AGENT


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str
    canonical: str


@dataclass(frozen=True)
class Asset:
    id: int
    name: str
    size: int
    download_count: int
    content_type: str
    browser_url: str
    api_url: str


@dataclass(frozen=True)
class Release:
    id: int
    tag_name: str
    name: str
    published_at: str
    created_at: str
    prerelease: bool
    draft: bool
    body: str
    html_url: str
    assets: tuple[Asset, ...]

    @property
    def display_name(self) -> str:
        return self.name.strip() or self.tag_name

    @property
    def published_display(self) -> str:
        value = self.published_at or self.created_at
        if not value:
            return "未标注日期"
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone().strftime("%Y-%m-%d")
        except ValueError:
            return value[:10]


_REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_UNSAFE_PATH = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def parse_repo_url(value: str) -> RepoRef:
    text = value.strip()
    if not text:
        raise ValueError("请粘贴 GitHub 项目地址")
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise ValueError("项目地址格式不正确") from exc
    host = (parsed.hostname or "").lower()
    if host not in {"github.com", "www.github.com"}:
        raise ValueError("目前只支持 github.com 项目地址")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError("地址中缺少仓库名，例如 github.com/owner/repo")
    owner, name = parts[0], parts[1]
    if name.endswith(".git"):
        name = name[:-4]
    if (
        not owner
        or not name
        or not _REPO_PART.fullmatch(owner)
        or not _REPO_PART.fullmatch(name)
    ):
        raise ValueError("GitHub 所有者或仓库名格式不正确")
    return RepoRef(owner, name, f"https://github.com/{owner}/{name}")


def sanitize_filename(name: str, fallback: str = "download.bin") -> str:
    cleaned = _UNSAFE_PATH.sub("_", str(name or "")).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned or cleaned in {".", ".."}:
        cleaned = fallback
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    stem = cleaned.split(".", 1)[0].upper()
    if stem in reserved:
        cleaned = "_" + cleaned
    return cleaned[:240]


def format_bytes(value: int | float) -> str:
    size = float(max(0, value))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class GitHubClient:
    def __init__(
        self,
        token: str = "",
        proxies: str = "",
        allow_insecure_tls: bool = False,
        session: requests.Session | None = None,
    ) -> None:
        self.token = token.strip()
        self.proxies = parse_proxies(proxies)
        self.session = session or requests.Session()
        if allow_insecure_tls:
            self.session.verify = False
            _disable_session_warnings()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        if self.token:
            self.session.headers.update(
                {
                    "Authorization": f"Bearer {self.token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
            )

    def _request(self, url: str, timeout: tuple[float, float] = (12, 35)) -> requests.Response:
        try:
            response = self.session.get(
                url,
                timeout=timeout,
                allow_redirects=True,
                proxies=self.proxies,
            )
        except requests.RequestException as exc:
            raise GitHubError(
                "无法连接 GitHub API，请检查网络或代理设置"
            ) from exc
        if response.status_code == 404:
            raise GitHubError("仓库不存在，或 Releases 未公开", 404)
        if response.status_code in {401, 403}:
            remaining = response.headers.get("X-RateLimit-Remaining")
            if remaining == "0":
                reset = response.headers.get("X-RateLimit-Reset", "")
                hint = ""
                if reset.isdigit():
                    when = datetime.fromtimestamp(int(reset), tz=timezone.utc)
                    hint = f"（额度重置于 {when.astimezone().strftime('%H:%M')}）"
                raise GitHubError(f"GitHub API 访问额度已用完{hint}", response.status_code)
            raise GitHubError("GitHub 拒绝了请求，请检查 Token", response.status_code)
        if not response.ok:
            raise GitHubError(
                f"GitHub API 返回 HTTP {response.status_code}",
                response.status_code,
            )
        return response

    def fetch_releases(
        self,
        repo: RepoRef,
        per_page: int = 10,
        include_prerelease: bool = True,
        max_pages: int = 5,
        cancel_event: threading.Event | None = None,
    ) -> list[Release]:
        releases: list[Release] = []
        seen: set[int] = set()
        for page in range(1, max_pages + 1):
            if cancel_event and cancel_event.is_set():
                break
            url = (
                f"https://api.github.com/repos/{quote(repo.owner)}/"
                f"{quote(repo.name)}/releases?per_page={per_page}&page={page}"
            )
            response = self._request(url)
            payload = response.json()
            if not isinstance(payload, list):
                raise GitHubError("GitHub API 返回了无法识别的数据")
            for item in payload:
                release = parse_release(item)
                if release.id in seen:
                    continue
                seen.add(release.id)
                if release.draft:
                    continue
                if not include_prerelease and release.prerelease:
                    continue
                releases.append(release)
            links = response.links
            if "next" not in links or len(payload) < per_page:
                break
            time.sleep(0.05)
        return releases

    def fetch_release_page(
        self,
        repo: RepoRef,
        page: int,
        per_page: int = 10,
    ) -> tuple[list[Release], bool]:
        url = (
            f"https://api.github.com/repos/{quote(repo.owner)}/"
            f"{quote(repo.name)}/releases?per_page={per_page}&page={page}"
        )
        response = self._request(url)
        payload = response.json()
        if not isinstance(payload, list):
            raise GitHubError("GitHub API 返回了无法识别的数据")
        releases = [
            parse_release(item)
            for item in payload
            if isinstance(item, dict) and not item.get("draft")
        ]
        return releases, bool(response.links.get("next")) or len(payload) >= per_page


def parse_proxies(value: str) -> dict[str, str] | None:
    text = (value or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text
    return {"http": text, "https": text}


def _disable_session_warnings() -> None:
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except (ImportError, AttributeError):
        pass


def parse_release(item: dict[str, Any]) -> Release:
    assets: list[Asset] = []
    for asset in item.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name = sanitize_filename(str(asset.get("name") or ""), f"asset-{asset.get('id', '')}")
        assets.append(
            Asset(
                id=int(asset.get("id") or 0),
                name=name,
                size=int(asset.get("size") or 0),
                download_count=int(asset.get("download_count") or 0),
                content_type=str(asset.get("content_type") or ""),
                browser_url=str(asset.get("browser_download_url") or ""),
                api_url=str(asset.get("url") or ""),
            )
        )
    return Release(
        id=int(item.get("id") or 0),
        tag_name=str(item.get("tag_name") or ""),
        name=str(item.get("name") or ""),
        published_at=str(item.get("published_at") or ""),
        created_at=str(item.get("created_at") or ""),
        prerelease=bool(item.get("prerelease")),
        draft=bool(item.get("draft")),
        body=str(item.get("body") or ""),
        html_url=str(item.get("html_url") or ""),
        assets=tuple(assets),
    )


