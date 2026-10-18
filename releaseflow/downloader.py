from __future__ import annotations

import hashlib
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

import requests

from .config import DEFAULT_MIRRORS, USER_AGENT
from .github_api import Asset, parse_proxies


class DownloadError(RuntimeError):
    pass


class DownloadCancelled(RuntimeError):
    pass


@dataclass
class SourceResult:
    prefix: str
    ok: bool
    latency: float = 0.0
    speed: float = 0.0
    status: int | None = None
    error: str = ""
    supports_range: bool = False
    total: int = 0


@dataclass
class DownloadJob:
    asset: Asset
    repo: str
    target_dir: Path
    threads: int = 8
    mirror_mode: str = "auto"
    selected_mirror: str = DEFAULT_MIRRORS[0]
    allow_insecure_tls: bool = False
    range_supported: bool = False
    total: int = 0
    downloaded: int = 0
    speed: float = 0.0
    status: str = "等待"
    error: str = ""
    source: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    started_at: float = 0.0
    finished_at: float = 0.0
    stopped: bool = False

    @property
    def filename(self) -> str:
        return self.asset.name

    @property
    def final_path(self) -> Path:
        return self.target_dir / self.filename

    @property
    def part_path(self) -> Path:
        return self.target_dir / f".{self.filename}.releaseflow.part"

    @property
    def active(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    @property
    def resumable(self) -> bool:
        if self.range_supported:
            return self.part_path.exists() or any(
                self.part_path.with_name(f"{self.part_path.name}.{i}.seg").exists()
                for i in range(32)
            )
        return self.part_path.exists()


def build_candidate_urls(
    original_url: str,
    mirror_mode: str = "auto",
    selected_mirror: str = DEFAULT_MIRRORS[0],
    mirrors: Iterable[str] | None = None,
) -> list[tuple[str, str]]:
    source = (original_url or "").strip()
    if not source:
        return []
    if mirror_mode == "direct":
        return [("直连", source)]
    if mirror_mode == "mirror" and selected_mirror:
        return [(mirror_label(selected_mirror), selected_mirror.rstrip("/") + "/" + source)]
    result: list[tuple[str, str]] = []
    for mirror in mirrors or DEFAULT_MIRRORS:
        result.append((mirror_label(mirror), mirror.rstrip("/") + "/" + source))
    result.append(("直连", source))
    return result


def mirror_label(url: str) -> str:
    host = (urlparse(url).hostname or url).lower()
    labels = {
        "ghfast.top": "Fast",
        "gh-proxy.com": "Proxy",
        "ghproxy.net": "Mirror",
    }
    return labels.get(host, host.removeprefix("www."))


def make_session(proxies_text: str = "", allow_insecure_tls: bool = False) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/octet-stream",
            "Accept-Encoding": "identity",
        }
    )
    proxies = parse_proxies(proxies_text)
    if proxies:
        session.proxies.update(proxies)
    if allow_insecure_tls:
        session.verify = False
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except (ImportError, AttributeError):
            pass
    return session


def test_source(
    url: str,
    label: str,
    proxies_text: str = "",
    allow_insecure_tls: bool = False,
    timeout: float = 12.0,
    cancel_event: threading.Event | None = None,
) -> SourceResult:
    session = make_session(proxies_text, allow_insecure_tls)
    started = time.monotonic()
    try:
        response = session.get(
            url,
            headers={"Range": "bytes=0-65535"},
            stream=True,
            timeout=(8, timeout),
            allow_redirects=True,
        )
        elapsed = time.monotonic() - started
        try:
            if response.status_code not in {200, 206} or cancel_event and cancel_event.is_set():
                return SourceResult(
                    label,
                    False,
                    elapsed,
                    status=response.status_code,
                    error=f"HTTP {response.status_code}",
                )
            data = bytearray()
            sample_started = time.monotonic()
            for chunk in response.iter_content(16384):
                if cancel_event and cancel_event.is_set():
                    break
                data.extend(chunk)
                if len(data) >= 65536 or time.monotonic() - sample_started > 2.5:
                    break
            sample_elapsed = max(0.01, time.monotonic() - sample_started)
            content_range = response.headers.get("Content-Range", "")
            match = re.search(r"/(\d+)$", content_range)
            total = int(match.group(1)) if match else 0
            if not total:
                length = response.headers.get("Content-Length", "")
                if response.status_code == 200 and length.isdigit():
                    total = int(length)
            return SourceResult(
                label,
                True,
                elapsed,
                len(data) / sample_elapsed,
                response.status_code,
                supports_range=response.status_code == 206,
                total=total,
            )
        finally:
            response.close()
    except requests.RequestException as exc:
        return SourceResult(
            label,
            False,
            time.monotonic() - started,
            error=type(exc).__name__,
        )
    finally:
        session.close()


def rank_sources(
    candidates: list[tuple[str, str]],
    proxies_text: str = "",
    cancel_event: threading.Event | None = None,
) -> list[SourceResult]:
    results: list[SourceResult] = []
    for label, url in candidates:
        if cancel_event and cancel_event.is_set():
            break
        results.append(test_source(url, label, proxies_text, cancel_event=cancel_event))
    usable = [item for item in results if item.ok]
    usable.sort(
        key=lambda item: (
            -item.speed,
            item.latency,
            not item.supports_range,
        )
    )
    return usable + [item for item in results if not item.ok]


class MultiPartDownloader:
    def __init__(
        self,
        job: DownloadJob,
        proxies_text: str = "",
        event_queue: queue.Queue | None = None,
    ) -> None:
        self.job = job
        self.proxies_text = proxies_text
        self.events = event_queue or queue.Queue()
        self.session = make_session(proxies_text, job.allow_insecure_tls)
        self.segment_paths: list[Path] = []
        self.segment_sizes: list[int] = []
        self.segment_ranges: list[tuple[int, int]] = []
        self._lock = threading.Lock()
        self._last_emit = 0.0
        self._last_bytes = 0
        self._last_time = time.monotonic()

    def _emit(self, kind: str, **payload) -> None:
        try:
            self.events.put_nowait({"kind": kind, "job": self.job, **payload})
        except queue.Full:
            pass

    def _update_speed(self, downloaded: int, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            self.job.downloaded = downloaded
            if force or now - self._last_emit >= 0.2:
                elapsed = max(0.05, now - self._last_time)
                current_speed = max(0.0, (downloaded - self._last_bytes) / elapsed)
                if self.job.speed:
                    current_speed = self.job.speed * 0.65 + current_speed * 0.35
                self.job.speed = current_speed
                self._last_bytes = downloaded
                self._last_time = now
                self._last_emit = now
                self._emit("progress")

    def _parts(self, total: int, wanted: int) -> list[tuple[int, int]]:
        if total <= 0:
            return [(0, -1)]
        count = max(1, min(wanted, 32))
        if total < 4 * 1024 * 1024:
            count = 1
        chunk = total // count
        parts: list[tuple[int, int]] = []
        for index in range(count):
            start = index * chunk
            end = total - 1 if index == count - 1 else (index + 1) * chunk - 1
            if start <= end:
                parts.append((start, end))
        return parts

    def _segment_path(self, index: int) -> Path:
        return self.job.part_path.with_name(
            f"{self.job.part_path.name}.{index}.seg"
        )

    def _prepare(self, total: int) -> list[tuple[int, int]]:
        if not self.job.range_supported:
            self.segment_ranges = []
            self.segment_sizes = []
            self.segment_paths = [self.job.part_path]
            return [(0, -1)]
        self.job.part_path.parent.mkdir(parents=True, exist_ok=True)
        parts = self._parts(total, self.job.threads)
        self.segment_ranges = parts
        self.segment_sizes = [end - start + 1 for start, end in parts]
        self.segment_paths = []
        existing: list[int] = []
        for index, (start, end) in enumerate(parts):
            path = self._segment_path(index)
            self.segment_paths.append(path)
            if not path.exists():
                path.touch()
            size = path.stat().st_size
            if end >= 0:
                wanted = end - start + 1
                if size > wanted:
                    with path.open("r+b") as handle:
                        handle.truncate(wanted)
                    size = wanted
            existing.append(size)
        if any(existing):
            self.job.status = "继续下载"
        return parts

    def _download_segment(
        self,
        index: int,
        start: int,
        end: int,
        url: str,
        initial_size: int,
    ) -> None:
        path = self.segment_paths[index]
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.stat().st_size if path.exists() else 0
        part_start = start
        if existing > 0:
            start += existing
        if end >= 0 and start > end:
            path.touch()
            return
        headers = {"Range": f"bytes={start}-{end if end >= 0 else ''}"}
        attempts = 0
        while attempts < 5:
            if self.job.cancel_event.is_set():
                raise DownloadCancelled()
            if self.job.pause_event.is_set():
                time.sleep(0.2)
                continue
            try:
                with requests.Session() as session:
                    session.headers.update(self.session.headers)
                    session.proxies.update(self.session.proxies)
                    session.verify = self.session.verify
                    with session.get(
                        url,
                        headers=headers,
                        stream=True,
                        timeout=(12, 60),
                        allow_redirects=True,
                    ) as response:
                        if response.status_code not in {200, 206}:
                            raise DownloadError(f"分片 {index + 1}: HTTP {response.status_code}")
                        if response.status_code == 200 and index > 0:
                            raise DownloadError("线路不支持分片下载")
                        with path.open("ab") as handle:
                            for chunk in response.iter_content(256 * 1024):
                                if self.job.cancel_event.is_set():
                                    raise DownloadCancelled()
                                if self.job.pause_event.is_set():
                                    self.job.status = "已暂停"
                                    while self.job.pause_event.is_set():
                                        if self.job.cancel_event.is_set():
                                            raise DownloadCancelled()
                                        time.sleep(0.15)
                                    self.job.status = "下载中"
                                if chunk:
                                    handle.write(chunk)
                                    with self._lock:
                                        self.job.downloaded += len(chunk)
                                        current = self.job.downloaded
                                    self._update_speed(current)
                return
            except DownloadCancelled:
                raise
            except (requests.RequestException, DownloadError, OSError) as exc:
                attempts += 1
                if attempts >= 5:
                    raise DownloadError(f"分片 {index + 1} 下载失败: {exc}") from exc
                self.job.status = f"重试 {attempts}/5"
                time.sleep(min(10, 1.5 * attempts))
                current_size = path.stat().st_size if path.exists() else 0
                start = part_start + current_size
                headers = {"Range": f"bytes={start}-{end if end >= 0 else ''}"}

    def download(self, candidates: list[tuple[str, str]]) -> Path:
        job = self.job
        if job.final_path.exists() and verify_size(job.final_path, job.asset.size):
            job.total = job.asset.size or job.final_path.stat().st_size
            job.downloaded = job.total
            job.status = "已完成"
            job.source = "本地文件"
            job.speed = 0
            self._emit("done", path=str(job.final_path))
            return job.final_path
        job.status = "连接中"
        job.started_at = time.monotonic()
        job.target_dir.mkdir(parents=True, exist_ok=True)
        chosen: tuple[str, str] | None = None
        response_info: SourceResult | None = None
        errors: list[str] = []
        for label, url in candidates:
            if job.cancel_event.is_set():
                raise DownloadCancelled()
            info = test_source(
                url,
                label,
                self.proxies_text,
                self.job.allow_insecure_tls,
                timeout=15,
                cancel_event=job.cancel_event,
            )
            if info.ok:
                chosen, response_info = (label, url), info
                break
            errors.append(f"{label}: {info.error or info.status}")
        if not chosen or not response_info:
            raise DownloadError("所有下载线路均不可用：" + "；".join(errors))
        job.source = chosen[0]
        job.range_supported = response_info.supports_range
        if response_info.total:
            job.total = response_info.total
        elif job.asset.size:
            job.total = job.asset.size
        job.status = "下载中"
        job.downloaded = 0
        parts = self._prepare(job.total if job.range_supported else 0)
        self._emit("source", source=chosen[0])
        try:
            if not job.range_supported or len(parts) == 1:
                self._single_stream(chosen[1])
            else:
                self._parallel(chosen[1], parts)
            self._finalize(parts)
            job.status = "已完成"
            job.finished_at = time.monotonic()
            job.speed = 0
            self._emit("done", path=str(job.final_path))
            return job.final_path
        except DownloadCancelled:
            job.status = "已取消"
            job.speed = 0
            self._emit("cancelled")
            raise
        except Exception as exc:
            job.status = "失败"
            job.error = str(exc)
            job.speed = 0
            self._emit("failed", error=str(exc))
            raise
        finally:
            self.session.close()

    def _single_stream(self, url: str) -> None:
        job = self.job
        self.segment_paths = [job.part_path]
        existing = job.part_path.stat().st_size if job.part_path.exists() else 0
        headers = {"Range": f"bytes={existing}-"} if existing else {}
        for attempt in range(1, 6):
            if job.cancel_event.is_set():
                raise DownloadCancelled()
            try:
                with self.session.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(12, 60),
                    allow_redirects=True,
                ) as response:
                    if response.status_code not in {200, 206}:
                        raise DownloadError(f"HTTP {response.status_code}")
                    if existing and response.status_code != 206:
                        existing = 0
                        job.part_path.unlink(missing_ok=True)
                    mode = "ab" if existing else "wb"
                    with job.part_path.open(mode) as handle:
                        for chunk in response.iter_content(512 * 1024):
                            if job.cancel_event.is_set():
                                raise DownloadCancelled()
                            while job.pause_event.is_set():
                                job.status = "已暂停"
                                if job.cancel_event.is_set():
                                    raise DownloadCancelled()
                                time.sleep(0.15)
                            if job.status == "已暂停":
                                job.status = "下载中"
                            if chunk:
                                handle.write(chunk)
                                job.downloaded += len(chunk)
                                self._update_speed(job.downloaded)
                self._finalize([job.part_path])
                return
            except DownloadCancelled:
                raise
            except (requests.RequestException, DownloadError, OSError) as exc:
                if attempt == 5:
                    raise DownloadError(f"下载失败: {exc}") from exc
                existing = job.part_path.stat().st_size if job.part_path.exists() else 0
                job.downloaded = existing
                headers = {"Range": f"bytes={existing}-"} if existing else {}
                job.status = f"重试 {attempt}/5"
                time.sleep(min(8, attempt))

    def _parallel(self, url: str, parts: list[tuple[int, int]]) -> None:
        job = self.job
        errors: list[Exception] = []
        lock = threading.Lock()
        threads: list[threading.Thread] = []

        def worker(index: int, start: int, end: int) -> None:
            try:
                self._download_segment(index, start, end, url, 0)
            except DownloadCancelled:
                pass
            except Exception as exc:
                with lock:
                    errors.append(exc)
                    job.cancel_event.set()

        for index, (start, end) in enumerate(parts):
            thread = threading.Thread(
                target=worker,
                args=(index, start, end),
                daemon=True,
                name=f"part-{index + 1}",
            )
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        if job.cancel_event.is_set() and not errors:
            raise DownloadCancelled()
        if errors:
            raise DownloadError(str(errors[0]))

    def _combine_parts(self, parts: list[tuple[int, int]]) -> None:
        job = self.job
        base_paths = [self._segment_path(index) for index in range(len(parts))]
        for index, path in enumerate(base_paths):
            wanted = parts[index][1] - parts[index][0] + 1
            if not path.exists() or path.stat().st_size != wanted:
                raise DownloadError(f"分片 {index + 1} 不完整，请继续下载重试")
        with job.part_path.open("wb") as output:
            for path in base_paths:
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        output.write(chunk)

    def _finalize(self, parts: list[Path] | list[tuple[int, int]]) -> None:
        job = self.job
        is_ranges = bool(parts) and isinstance(parts[0], tuple)
        if len(parts) == 1:
            data_path = (
                self._segment_path(0) if is_ranges else parts[0]
            )
        else:
            data_path = job.part_path
            if not job.range_supported:
                raise DownloadError("不完整的分片状态")
            self._combine_parts(self.segment_ranges)
        if data_path == job.final_path:
            return
        if job.final_path.exists():
            job.final_path.unlink()
        os.replace(data_path, job.final_path)
        if is_ranges:
            for index in range(len(parts)):
                self._segment_path(index).unlink(missing_ok=True)
        job.downloaded = job.final_path.stat().st_size
        self._update_speed(job.downloaded, force=True)


class DownloadManager:
    def __init__(self, event_queue: queue.Queue | None = None) -> None:
        self.events = event_queue or queue.Queue(maxsize=1000)
        self.jobs: list[DownloadJob] = []
        self.lock = threading.Lock()

    def add_job(self, job: DownloadJob) -> None:
        with self.lock:
            self.jobs.append(job)

    def start(
        self,
        job: DownloadJob,
        candidates: list[tuple[str, str]],
        proxies_text: str = "",
    ) -> None:
        if job.active:
            return
        job.cancel_event.clear()
        job.pause_event.clear()
        job.error = ""
        downloader = MultiPartDownloader(job, proxies_text, self.events)

        def run() -> None:
            try:
                downloader.download(candidates)
            except DownloadCancelled:
                pass
            except Exception as exc:
                job.status = "失败"
                job.error = str(exc)

        job.thread = threading.Thread(target=run, daemon=True, name=job.filename)
        job.thread.start()

    def cancel(self, job: DownloadJob) -> None:
        job.cancel_event.set()
        job.pause_event.clear()
        job.status = "正在取消"

    def pause(self, job: DownloadJob) -> None:
        if job.active:
            job.pause_event.set()
            job.status = "已暂停"

    def resume(self, job: DownloadJob) -> None:
        if job.active:
            job.pause_event.clear()
            job.status = "下载中"

    def abandon(self, job: DownloadJob) -> None:
        job.cancel_event.set()
        job.pause_event.clear()
        job.status = "正在取消"
        if job.thread and job.thread.is_alive():
            job.thread.join(timeout=1.5)
        job.stopped = True


def verify_size(path: Path, expected: int) -> bool:
    return expected <= 0 or (path.exists() and path.stat().st_size == expected)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
