from __future__ import annotations

import hashlib
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from releaseflow.downloader import (
    DownloadJob,
    MultiPartDownloader,
    build_candidate_urls,
    test_source,
)
from releaseflow.github_api import (
    Asset,
    GitHubClient,
    GitHubError,
    format_bytes,
    parse_release,
    parse_repo_url,
    sanitize_filename,
)


class RangeHandler(BaseHTTPRequestHandler):
    payload = bytes(range(256)) * 16384
    seen_ranges: list[str] = []

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/blob")
            self.end_headers()
            return
        if self.path not in {"/blob", "/redirected"}:
            self.send_response(404)
            self.end_headers()
            return
        range_header = self.headers.get("Range", "")
        self.seen_ranges.append(range_header)
        if range_header.startswith("bytes="):
            raw = range_header.removeprefix("bytes=")
            start_text, _, end_text = raw.partition("-")
            start = int(start_text or 0)
            end = int(end_text) if end_text else len(self.payload) - 1
            end = min(end, len(self.payload) - 1)
            body = self.payload[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.payload)}")
        else:
            body = self.payload
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)


class FakeResponse:
    def __init__(self, status_code: int, payload: object, headers=None, links=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.links = links or {}
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.headers: dict[str, str] = {}

    def get(self, *_args, **_kwargs):
        return next(self.responses)


def make_asset(size: int, name: str = "archive.zip") -> Asset:
    return Asset(1, name, size, 10, "application/zip", "https://github.com/a/b/x", "")


class CoreTests(unittest.TestCase):
    def test_repo_url_variants(self) -> None:
        expected = ("owner", "repo")
        for value in (
            "https://github.com/owner/repo",
            "http://www.github.com/owner/repo/",
            "github.com/owner/repo/releases/tag/v1",
            "https://github.com/owner/repo.git",
            "  github.com/owner/repo?tab=releases  ",
        ):
            repo = parse_repo_url(value)
            self.assertEqual((repo.owner, repo.name), expected)

    def test_repo_url_rejects_non_github(self) -> None:
        with self.assertRaises(ValueError):
            parse_repo_url("https://gitlab.com/owner/repo")
        with self.assertRaises(ValueError):
            parse_repo_url("https://github.com/onlyowner")

    def test_filename_safety(self) -> None:
        self.assertEqual(sanitize_filename("a:b?.zip"), "a_b_.zip")
        self.assertEqual(sanitize_filename("CON"), "_CON")
        self.assertEqual(sanitize_filename("..."), "download.bin")

    def test_release_parser(self) -> None:
        release = parse_release(
            {
                "id": 7,
                "tag_name": "v1.0",
                "name": "",
                "published_at": "2026-10-18T10:00:00Z",
                "prerelease": True,
                "assets": [
                    {
                        "id": 3,
                        "name": "setup.exe",
                        "size": 100,
                        "browser_download_url": "https://example/x",
                    }
                ],
            }
        )
        self.assertEqual(release.display_name, "v1.0")
        self.assertTrue(release.prerelease)
        self.assertEqual(release.assets[0].name, "setup.exe")

    def test_format_bytes(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1536), "1.5 KB")

    def test_candidates(self) -> None:
        direct = build_candidate_urls("https://github.com/a/b", "direct")
        self.assertEqual(direct, [("直连", "https://github.com/a/b")])
        auto = build_candidate_urls("https://github.com/a/b", "auto")
        self.assertEqual(auto[0][0], "Fast")
        self.assertEqual(auto[-1][0], "直连")

    def test_pagination_and_token(self) -> None:
        items = [
            {
                "id": 1,
                "tag_name": "v1",
                "published_at": "2026-01-01T00:00:00Z",
                "assets": [],
            },
            {
                "id": 2,
                "tag_name": "v2",
                "published_at": "2026-02-01T00:00:00Z",
                "assets": [],
            },
        ]
        session = FakeSession(
            [
                FakeResponse(200, items, links={"next": {"url": "next"}}),
                FakeResponse(200, [dict(items[1], id=3)], links={}),
            ]
        )
        with patch("releaseflow.github_api.requests.Session", return_value=session):
            client = GitHubClient(token="secret")
        self.assertEqual(session.headers["Authorization"], "Bearer secret")
        releases = client.fetch_releases(
            parse_repo_url("github.com/a/b"), per_page=2, max_pages=2
        )
        self.assertEqual([item.id for item in releases], [1, 2, 3])

    def test_single_release_page(self) -> None:
        items = [
            {
                "id": 11,
                "tag_name": "v2",
                "published_at": "2026-02-01T00:00:00Z",
                "assets": [],
            },
            {
                "id": 12,
                "tag_name": "draft",
                "draft": True,
                "assets": [],
            },
        ]
        session = FakeSession(
            [FakeResponse(200, items, links={"next": {"url": "next"}})]
        )
        with patch("releaseflow.github_api.requests.Session", return_value=session):
            client = GitHubClient()
        releases, has_more = client.fetch_release_page(
            parse_repo_url("github.com/a/b"), 1, per_page=2
        )
        self.assertEqual([item.id for item in releases], [11])
        self.assertTrue(has_more)

    def test_settings_roundtrip(self) -> None:
        from releaseflow import config

        with TemporaryDirectory() as temp:
            config_path = Path(temp) / "settings.json"
            config_path.write_text(
                json.dumps(
                    {
                        "download_dir": temp,
                        "threads": 6,
                        "mirror_mode": "direct",
                        "github_token": "tok",
                        "token_from_env": False,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(config, "settings_path", return_value=config_path):
                loaded = config.load_settings()
            self.assertIsInstance(loaded, config.Settings)
            self.assertEqual(loaded.threads, 6)
            self.assertEqual(config.effective_github_token(loaded), "tok")

    def test_rate_limit_error(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    403,
                    {},
                    headers={"X-RateLimit-Remaining": "0"},
                )
            ]
        )
        with patch("releaseflow.github_api.requests.Session", return_value=session):
            client = GitHubClient()
        with self.assertRaises(GitHubError) as context:
            client.fetch_releases(parse_repo_url("github.com/a/b"))
        self.assertEqual(context.exception.status, 403)

    def test_test_source_and_redirect(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            result = test_source(base + "/blob", "local")
            self.assertTrue(result.ok)
            self.assertTrue(result.supports_range)
            self.assertEqual(result.total, len(RangeHandler.payload))
            redirected = test_source(base + "/redirect", "local")
            self.assertTrue(redirected.ok)
        finally:
            server.shutdown()
            server.server_close()

    def test_parallel_download_and_resume(self) -> None:
        RangeHandler.seen_ranges = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as temp:
                target = Path(temp)
                asset = make_asset(len(RangeHandler.payload))
                job = DownloadJob(
                    asset=asset,
                    repo="https://github.com/a/b",
                    target_dir=target,
                    threads=4,
                )
                url = f"http://127.0.0.1:{server.server_port}/blob"
                downloader = MultiPartDownloader(job)
                path = downloader.download([("local", url)])
                self.assertEqual(path.read_bytes(), RangeHandler.payload)
                self.assertEqual(job.downloaded, len(RangeHandler.payload))
                self.assertEqual(len(RangeHandler.seen_ranges), 5)
        finally:
            server.shutdown()
            server.server_close()

    def test_single_stream_no_range(self) -> None:
        with TemporaryDirectory() as temp:
            asset = make_asset(100)
            job = DownloadJob(
                asset=asset,
                repo="https://github.com/a/b",
                target_dir=Path(temp),
            )
            job.part_path.write_bytes(b"x" * 10)
            downloader = MultiPartDownloader(job)
            with patch(
                "releaseflow.downloader.test_source",
                return_value=__import__(
                    "releaseflow.downloader", fromlist=["SourceResult"]
                ).SourceResult("local", True, total=100),
            ), patch.object(downloader, "_single_stream") as single:
                downloader._prepare(0)
                self.assertEqual(len(downloader._parts(100, 100)), 1)


if __name__ == "__main__":
    unittest.main()
