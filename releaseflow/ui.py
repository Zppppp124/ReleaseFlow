from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable

import customtkinter as ctk
from PIL import Image, ImageTk

from .config import (
    APP_NAME,
    DEFAULT_MIRRORS,
    Settings,
    app_data_dir,
    effective_github_token,
    load_settings,
    resource_path,
    save_settings,
)
from .downloader import (
    DownloadJob,
    DownloadManager,
    build_candidate_urls,
    mirror_label,
)
from .github_api import (
    Asset,
    GitHubClient,
    GitHubError,
    Release,
    RepoRef,
    format_bytes,
    parse_repo_url,
)
from .icons import make_icon, make_line_icon


COLORS = {
    "bg": "#0d131c",
    "surface": "#121b26",
    "surface2": "#172331",
    "surface3": "#1d2b3a",
    "line": "#263849",
    "line_soft": "#1d2b38",
    "text": "#f1f5f8",
    "text2": "#a6b5c3",
    "text3": "#728494",
    "accent": "#4fd1b5",
    "accent_hover": "#68e1c7",
    "accent_soft": "#244d49",
    "accent_dark": "#153d39",
    "blue": "#64a9ff",
    "warning": "#efbb67",
    "danger": "#f17d86",
    "success": "#61d49a",
}

FONT_FAMILY = "Microsoft YaHei UI"
MONO_FAMILY = "Cascadia Mono"


def ui_font(size: int, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)


def fit_text(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


class IconCache:
    def __init__(self, master: tk.Misc) -> None:
        self.master = master
        self.cache: dict[tuple[str, int, str], ctk.CTkImage] = {}

    def get(self, name: str, size: int = 22) -> ctk.CTkImage:
        key = (name, size, COLORS["text2"])
        if key not in self.cache:
            image = self._draw(name, size)
            self.cache[key] = ctk.CTkImage(
                light_image=image, dark_image=image, size=(size, size)
            )
        return self.cache[key]

    def _draw(self, name: str, size: int) -> Image.Image:
        return make_line_icon(name, size, COLORS["text2"])


class ReleaseFlowApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.settings = load_settings()
        self.icons = IconCache(self)
        self.event_queue: queue.Queue = queue.Queue()
        self.ui_queue: queue.Queue = queue.Queue()
        self.download_manager = DownloadManager(self.event_queue)
        self.releases: list[Release] = []
        self.repo: RepoRef | None = None
        self.selected_release: Release | None = None
        self.release_rows: dict[int, dict] = {}
        self.asset_rows: dict[int, dict] = {}
        self.asset_vars: dict[int, tk.BooleanVar] = {}
        self.job_cards: dict[int, dict] = {}
        self.worker: threading.Thread | None = None
        self.next_release_page = 2
        self.has_more_releases = False
        self.load_more_btn: ctk.CTkButton | None = None
        self.cancel_fetch = threading.Event()
        self.source_results: dict[str, object] = {}
        self.closed = False
        self.current_view = "workspace"
        self._image_refs: list[ImageTk.PhotoImage] = []
        self._setup_window()
        self._build_ui()
        self._show_view("workspace")
        self.after(120, self._poll_events)
        self.bind("<Control-v>", self._paste_repo)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_window(self) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        self.title(APP_NAME)
        self.geometry("1180x760")
        self.minsize(980, 650)
        self.configure(fg_color=COLORS["bg"])
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        try:
            icon_image = make_icon(64)
            icon_path = resource_path("releaseflow_icon.png")
            if icon_path.parent.exists():
                icon_image.save(icon_path)
            self._icon_photo = ImageTk.PhotoImage(icon_image)
            self.iconphoto(True, self._icon_photo)
        except (OSError, tk.TclError):
            pass

    def _build_ui(self) -> None:
        self.root_frame = ctk.CTkFrame(self, fg_color=COLORS["bg"], corner_radius=0)
        self.root_frame.grid(row=0, column=0, sticky="nsew")
        self.root_frame.grid_columnconfigure(0, weight=1)
        self.root_frame.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_workspace()
        self._build_downloads()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(
            self.root_frame,
            height=76,
            fg_color=COLORS["surface"],
            corner_radius=0,
            border_width=0,
        )
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        header.grid_propagate(False)
        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.grid(row=0, column=0, padx=(24, 18), pady=14, sticky="w")
        mark = ctk.CTkLabel(
            brand,
            text="RF",
            width=38,
            height=38,
            corner_radius=9,
            fg_color=COLORS["accent_dark"],
            text_color=COLORS["accent"],
            font=ui_font(14, "bold"),
        )
        mark.pack(side="left")
        brand_text = ctk.CTkFrame(brand, fg_color="transparent")
        brand_text.pack(side="left", padx=(11, 0))
        ctk.CTkLabel(
            brand_text,
            text="ReleaseFlow",
            font=ui_font(17, "bold"),
            text_color=COLORS["text"],
            anchor="w",
            height=20,
        ).pack(anchor="w")
        ctk.CTkLabel(
            brand_text,
            text="GitHub Release 下载器",
            font=ui_font(11),
            text_color=COLORS["text3"],
            anchor="w",
            height=17,
        ).pack(anchor="w")

        repo_bar = ctk.CTkFrame(
            header,
            fg_color=COLORS["surface2"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["line"],
            height=42,
        )
        repo_bar.grid(row=0, column=1, padx=(0, 18), pady=16, sticky="ew")
        repo_bar.grid_columnconfigure(0, weight=1)
        repo_bar.grid_columnconfigure(1, weight=0)
        repo_bar.grid_columnconfigure(2, weight=0)
        repo_bar.grid_columnconfigure(3, weight=0)
        repo_bar.grid_columnconfigure(4, weight=0)
        self.repo_entry = ctk.CTkEntry(
            repo_bar,
            placeholder_text="粘贴 GitHub 项目地址，例如 github.com/owner/repo",
            placeholder_text_color=COLORS["text3"],
            text_color=COLORS["text"],
            fg_color="transparent",
            border_width=0,
            height=38,
            font=ui_font(12),
        )
        self.repo_entry.grid(row=0, column=0, padx=(13, 6), sticky="ew")
        self.icon_button(
            repo_bar,
            "search",
            "解析 Releases",
            self.start_fetch,
            width=40,
            fg=COLORS["accent_dark"],
        ).grid(row=0, column=2, padx=(0, 5), pady=5)
        self.icon_button(
            repo_bar,
            "copy",
            "从剪贴板粘贴",
            self.paste_and_fetch,
            width=40,
        ).grid(row=0, column=3, padx=(0, 5), pady=5)

        nav = ctk.CTkFrame(header, fg_color="transparent")
        nav.grid(row=0, column=2, padx=(0, 20), pady=16, sticky="e")
        self.workspace_nav = ctk.CTkButton(
            nav,
            text=f"浏览",
            command=lambda: self._show_view("workspace"),
            width=74,
            height=38,
            corner_radius=7,
            fg_color=COLORS["surface3"],
            hover_color=COLORS["line"],
            text_color=COLORS["text"],
            font=ui_font(12, "bold"),
        )
        self.workspace_nav.pack(side="left", padx=3)
        self.downloads_nav = ctk.CTkButton(
            nav,
            text="下载  0",
            command=lambda: self._show_view("downloads"),
            width=88,
            height=38,
            corner_radius=7,
            fg_color="transparent",
            hover_color=COLORS["line"],
            text_color=COLORS["text2"],
            font=ui_font(12),
        )
        self.downloads_nav.pack(side="left", padx=3)
        self.icon_button(nav, "folder", "打开下载目录", self.open_download_dir, width=38).pack(
            side="left", padx=(9, 3)
        )
        self.icon_button(nav, "terminal", "设置", self.open_settings, width=38).pack(
            side="left", padx=3
        )

    def _build_workspace(self) -> None:
        self.workspace = ctk.CTkFrame(
            self.root_frame, fg_color=COLORS["bg"], corner_radius=0
        )
        self.workspace.grid(row=1, column=0, sticky="nsew")
        self.workspace.grid_columnconfigure(0, weight=0, minsize=452)
        self.workspace.grid_columnconfigure(1, weight=1, minsize=500)
        self.workspace.grid_rowconfigure(1, weight=1)
        self._build_workspace_title()
        self._build_release_panel()
        self._build_asset_panel()

    def _build_workspace_title(self) -> None:
        top = ctk.CTkFrame(self.workspace, fg_color="transparent", height=62)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=24)
        top.grid_columnconfigure(0, weight=1)
        left = ctk.CTkFrame(top, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", pady=13)
        ctk.CTkLabel(
            left,
            text="项目发布",
            font=ui_font(20, "bold"),
            text_color=COLORS["text"],
            anchor="w",
            height=26,
        ).pack(anchor="w")
        self.repo_meta_label = ctk.CTkLabel(
            left,
            text="输入项目地址后加载 Releases",
            font=ui_font(11),
            text_color=COLORS["text3"],
            anchor="w",
            height=18,
        )
        self.repo_meta_label.pack(anchor="w")
        right = ctk.CTkFrame(top, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e", pady=13)
        self.precheck = ctk.CTkCheckBox(
            right,
            text="包含预发布版本",
            variable=tk.BooleanVar(value=True),
            onvalue=True,
            offvalue=False,
            width=130,
            height=25,
            checkbox_width=18,
            checkbox_height=18,
            corner_radius=4,
            border_width=1,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            border_color=COLORS["line"],
            checkmark_color=COLORS["bg"],
            text_color=COLORS["text2"],
            font=ui_font(11),
            command=self.start_fetch if self.repo else None,
        )
        self.precheck.pack(side="left", padx=10)
        self.fetch_count_label = ctk.CTkLabel(
            right,
            text="",
            font=ui_font(11),
            text_color=COLORS["text3"],
            width=80,
            anchor="e",
        )
        self.fetch_count_label.pack(side="left", padx=3)

    def _build_release_panel(self) -> None:
        panel = ctk.CTkFrame(
            self.workspace,
            fg_color=COLORS["surface"],
            corner_radius=0,
            border_width=1,
            border_color=COLORS["line_soft"],
        )
        panel.grid(row=1, column=0, sticky="nsew", padx=(22, 7), pady=(0, 20))
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)
        panel_head = ctk.CTkFrame(panel, fg_color="transparent", height=48)
        panel_head.grid(row=0, column=0, sticky="ew", padx=14)
        panel_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            panel_head,
            text="版本",
            font=ui_font(11, "bold"),
            text_color=COLORS["text3"],
            anchor="w",
        ).grid(row=0, column=0, sticky="w", pady=14)
        self.release_state = ctk.CTkLabel(
            panel_head,
            text="等待加载",
            font=ui_font(10),
            text_color=COLORS["text3"],
            anchor="e",
        )
        self.release_state.grid(row=0, column=1, sticky="e", pady=14)
        self.release_scroll = ctk.CTkScrollableFrame(
            panel,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color=COLORS["line"],
            scrollbar_button_hover_color=COLORS["text3"],
        )
        self.release_scroll.grid(row=1, column=0, sticky="nsew", padx=(4, 2), pady=(0, 8))
        self.release_scroll.grid_columnconfigure(0, weight=1)
        self.release_empty = self._empty_state(
            self.release_scroll,
            "search",
            "还没有加载版本",
            "粘贴项目地址并解析后，Releases 会显示在这里。",
        )
        self.release_empty.grid(row=0, column=0, sticky="ew", pady=(30, 0))

    def _build_asset_panel(self) -> None:
        panel = ctk.CTkFrame(
            self.workspace,
            fg_color=COLORS["surface"],
            corner_radius=0,
            border_width=1,
            border_color=COLORS["line_soft"],
        )
        panel.grid(row=1, column=1, sticky="nsew", padx=(7, 22), pady=(0, 20))
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)
        head = ctk.CTkFrame(panel, fg_color="transparent", height=94)
        head.grid(row=0, column=0, sticky="ew", padx=20)
        head.grid_columnconfigure(0, weight=1)
        self.asset_title = ctk.CTkLabel(
            head,
            text="选择一个版本",
            font=ui_font(18, "bold"),
            text_color=COLORS["text"],
            anchor="w",
            height=26,
        )
        self.asset_title.grid(row=0, column=0, sticky="w", pady=(17, 0))
        self.asset_subtitle = ctk.CTkLabel(
            head,
            text="Release 资产会显示在这里",
            font=ui_font(10),
            text_color=COLORS["text3"],
            anchor="w",
            height=17,
        )
        self.asset_subtitle.grid(row=1, column=0, sticky="w")
        self.asset_actions = ctk.CTkFrame(head, fg_color="transparent")
        self.asset_actions.grid(row=0, column=1, rowspan=2, sticky="e")
        self.download_selected_btn = ctk.CTkButton(
            self.asset_actions,
            text="下载所选",
            command=self.download_selected,
            height=34,
            width=114,
            corner_radius=7,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["bg"],
            font=ui_font(11, "bold"),
            state="disabled",
        )
        self.download_selected_btn.pack(side="left", padx=(0, 7))
        self.release_page_btn = ctk.CTkButton(
            self.asset_actions,
            text="页面",
            image=self.icons.get("external"),
            compound="left",
            command=self.open_release_page,
            height=34,
            width=76,
            corner_radius=7,
            fg_color="transparent",
            hover_color=COLORS["surface3"],
            border_width=1,
            border_color=COLORS["line"],
            text_color=COLORS["text2"],
            font=ui_font(11),
            state="disabled",
        )
        self.release_page_btn.pack(side="left")
        divider = ctk.CTkFrame(
            panel, height=1, fg_color=COLORS["line_soft"], corner_radius=0
        )
        divider.grid(row=1, column=0, sticky="ew", padx=20)
        content = ctk.CTkFrame(panel, fg_color="transparent")
        content.grid(row=2, column=0, sticky="nsew", padx=20, pady=(8, 16))
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(1, weight=1)
        column_head = ctk.CTkFrame(content, fg_color="transparent", height=31)
        column_head.grid(row=0, column=0, sticky="ew")
        column_head.grid_columnconfigure(1, weight=1)
        self.select_all_var = tk.BooleanVar(value=False)
        self.select_all_checkbox = ctk.CTkCheckBox(
            column_head,
            text="",
            width=22,
            checkbox_width=18,
            checkbox_height=18,
            corner_radius=4,
            border_width=1,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            border_color=COLORS["line"],
            checkmark_color=COLORS["bg"],
            variable=self.select_all_var,
            command=self._toggle_all_assets,
        )
        self.select_all_checkbox.grid(row=0, column=0, padx=(0, 9), sticky="w")
        ctk.CTkLabel(
            column_head,
            text="资产文件",
            font=ui_font(10, "bold"),
            text_color=COLORS["text3"],
            anchor="w",
        ).grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(
            column_head,
            text="大小",
            width=72,
            font=ui_font(10, "bold"),
            text_color=COLORS["text3"],
            anchor="e",
        ).grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.asset_scroll = ctk.CTkScrollableFrame(
            content,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color=COLORS["line"],
            scrollbar_button_hover_color=COLORS["text3"],
        )
        self.asset_scroll.grid(row=1, column=0, sticky="nsew")
        self.asset_scroll.grid_columnconfigure(0, weight=1)
        self.asset_empty = self._empty_state(
            self.asset_scroll,
            "info",
            "尚未选择版本",
            "从左侧版本列表中选择一个 Release。",
        )
        self.asset_empty.grid(row=0, column=0, sticky="ew", pady=(35, 0))

    def _build_downloads(self) -> None:
        self.downloads_view = ctk.CTkFrame(
            self.root_frame, fg_color=COLORS["bg"], corner_radius=0
        )
        self.downloads_view.grid(row=1, column=0, sticky="nsew")
        self.downloads_view.grid_columnconfigure(0, weight=1)
        self.downloads_view.grid_rowconfigure(1, weight=1)
        top = ctk.CTkFrame(self.downloads_view, fg_color="transparent", height=72)
        top.grid(row=0, column=0, sticky="ew", padx=24)
        top.grid_columnconfigure(0, weight=1)
        title = ctk.CTkFrame(top, fg_color="transparent")
        title.grid(row=0, column=0, sticky="w", pady=14)
        ctk.CTkLabel(
            title,
            text="下载队列",
            font=ui_font(20, "bold"),
            text_color=COLORS["text"],
            anchor="w",
            height=25,
        ).pack(anchor="w")
        self.queue_meta = ctk.CTkLabel(
            title,
            text="0 个任务",
            font=ui_font(11),
            text_color=COLORS["text3"],
            anchor="w",
            height=18,
        )
        self.queue_meta.pack(anchor="w")
        actions = ctk.CTkFrame(top, fg_color="transparent")
        actions.grid(row=0, column=1, sticky="e", pady=14)
        self.speedcheck_btn = ctk.CTkButton(
            actions,
            text="测试线路",
            image=self.icons.get("refresh"),
            compound="left",
            command=self.start_speed_test,
            width=104,
            height=34,
            corner_radius=7,
            fg_color="transparent",
            hover_color=COLORS["surface3"],
            border_width=1,
            border_color=COLORS["line"],
            text_color=COLORS["text2"],
            font=ui_font(11),
        )
        self.speedcheck_btn.pack(side="left", padx=5)
        ctk.CTkButton(
            actions,
            text="打开目录",
            image=self.icons.get("folder"),
            compound="left",
            command=self.open_download_dir,
            width=104,
            height=34,
            corner_radius=7,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["bg"],
            font=ui_font(11, "bold"),
        ).pack(side="left", padx=5)
        body = ctk.CTkFrame(
            self.downloads_view,
            fg_color=COLORS["surface"],
            corner_radius=0,
            border_width=1,
            border_color=COLORS["line_soft"],
        )
        body.grid(row=1, column=0, sticky="nsew", padx=22, pady=(0, 20))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)
        summary = ctk.CTkFrame(body, fg_color="transparent", height=64)
        summary.grid(row=0, column=0, sticky="ew", padx=18)
        summary.grid_columnconfigure(0, weight=1)
        self.queue_summary = ctk.CTkLabel(
            summary,
            text="还没有下载任务",
            font=ui_font(11),
            text_color=COLORS["text2"],
            anchor="w",
        )
        self.queue_summary.grid(row=0, column=0, sticky="w", pady=16)
        self.global_speed = ctk.CTkLabel(
            summary,
            text="0 B/s",
            font=ctk.CTkFont(family=MONO_FAMILY, size=13, weight="bold"),
            text_color=COLORS["accent"],
            anchor="e",
        )
        self.global_speed.grid(row=0, column=1, sticky="e", pady=16)
        self.queue_scroll = ctk.CTkScrollableFrame(
            body,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color=COLORS["line"],
            scrollbar_button_hover_color=COLORS["text3"],
        )
        self.queue_scroll.grid(row=1, column=0, sticky="nsew", padx=(8, 4), pady=(0, 10))
        self.queue_scroll.grid_columnconfigure(0, weight=1)
        self.queue_empty = self._empty_state(
            self.queue_scroll,
            "download",
            "队列是空的",
            "选择一个 Release 资产后点击“下载所选”。",
        )
        self.queue_empty.grid(row=0, column=0, sticky="ew", pady=(50, 0))

    def _empty_state(
        self,
        parent: ctk.CTkBaseClass,
        icon: str,
        title: str,
        subtitle: str,
    ) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        icon_label = ctk.CTkLabel(
            frame,
            text="",
            image=self.icons.get(icon, 32),
            width=55,
            height=55,
            fg_color=COLORS["surface2"],
            corner_radius=12,
        )
        icon_label.pack(pady=(0, 12))
        ctk.CTkLabel(
            frame,
            text=title,
            font=ui_font(13, "bold"),
            text_color=COLORS["text2"],
            height=23,
        ).pack()
        ctk.CTkLabel(
            frame,
            text=subtitle,
            font=ui_font(10),
            text_color=COLORS["text3"],
            wraplength=310,
            justify="center",
        ).pack(pady=(3, 0))
        return frame

    def icon_button(
        self,
        parent: ctk.CTkBaseClass,
        icon: str,
        tooltip: str,
        command: Callable[[], None],
        width: int = 38,
        fg: str = "transparent",
    ) -> ctk.CTkButton:
        button = ctk.CTkButton(
            parent,
            text="",
            image=self.icons.get(icon, 20),
            command=command,
            width=width,
            height=34,
            corner_radius=7,
            fg_color=fg,
            hover_color=COLORS["line"],
            border_width=0,
        )
        Tooltip(button, tooltip)
        return button

    def _show_view(self, view: str) -> None:
        self.current_view = view
        if view == "downloads":
            self.workspace.grid_remove()
            self.downloads_view.grid()
            self.workspace_nav.configure(fg_color="transparent", text_color=COLORS["text2"])
            self.downloads_nav.configure(
                fg_color=COLORS["surface3"], text_color=COLORS["text"]
            )
        else:
            self.downloads_view.grid_remove()
            self.workspace.grid()
            self.workspace_nav.configure(
                fg_color=COLORS["surface3"], text_color=COLORS["text"]
            )
            self.downloads_nav.configure(fg_color="transparent", text_color=COLORS["text2"])

    def _paste_repo(self, _event: tk.Event | None = None) -> str:
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return "break"
        if text:
            self.repo_entry.delete(0, "end")
            self.repo_entry.insert(0, text)
            self.repo_entry.focus_set()
        return "break"

    def paste_and_fetch(self) -> None:
        self._paste_repo()
        if self.repo_entry.get().strip():
            self.start_fetch()

    def start_fetch(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_fetch.set()
            return
        raw = self.repo_entry.get().strip()
        if not raw:
            try:
                raw = self.clipboard_get().strip()
                self.repo_entry.insert(0, raw)
            except tk.TclError:
                pass
        try:
            repo = parse_repo_url(raw)
        except ValueError as exc:
            messagebox.showwarning("地址不可用", str(exc), parent=self)
            return
        self.repo = repo
        self.show_fetching(repo)
        self.cancel_fetch.clear()
        include_pre = bool(self.precheck.get())
        token = effective_github_token(self.settings)
        proxies = self.settings.proxies
        self.worker = threading.Thread(
            target=self._fetch_worker,
            args=(
                repo,
                include_pre,
                token,
                proxies,
                self.settings.allow_insecure_tls,
            ),
            daemon=True,
        )
        self.after(40, self._poll_ui_events)
        self.worker.start()

    def _fetch_worker(
        self,
        repo: RepoRef,
        include_pre: bool,
        token: str,
        proxies: str,
        allow_insecure_tls: bool,
    ) -> None:
        try:
            client = GitHubClient(
                token=token,
                proxies=proxies,
                allow_insecure_tls=allow_insecure_tls,
            )
            releases, has_more = client.fetch_release_page(repo, 1, per_page=10)
            if not include_pre:
                releases = [item for item in releases if not item.prerelease]
            if not self.cancel_fetch.is_set():
                self._send_ui(
                    lambda: self._load_releases(repo, releases, has_more, True)
                )
        except GitHubError as exc:
            self._send_ui(lambda message=str(exc): self._fetch_failed(message))
        except Exception as exc:
            self._send_ui(
                lambda message=str(exc): self._fetch_failed(f"加载失败：{message}")
            )

    def _send_ui(self, callback: Callable[[], None]) -> None:
        if not self.closed:
            self.ui_queue.put(callback)

    def _poll_ui_events(self) -> None:
        if self.closed:
            return
        try:
            while True:
                callback = self.ui_queue.get_nowait()
                try:
                    callback()
                except (tk.TclError, RuntimeError):
                    pass
        except queue.Empty:
            pass
        self.after(50, self._poll_ui_events)

    def show_fetching(self, repo: RepoRef) -> None:
        self.clear_release_rows()
        self.release_state.configure(text="正在加载", text_color=COLORS["accent"])
        self.repo_meta_label.configure(text=f"{repo.owner}/{repo.name}  ·  正在请求 GitHub")
        self.fetch_count_label.configure(text="...")
        loading = ctk.CTkLabel(
            self.release_scroll,
            text="正在读取 Releases，请稍候",
            font=ui_font(11),
            text_color=COLORS["text2"],
            height=70,
        )
        loading.grid(row=0, column=0, sticky="ew", pady=(24, 0))
        self.release_rows[-1] = {"widget": loading, "selected": False}
        self._clear_assets()

    def _clear_assets(self) -> None:
        for child in self.asset_scroll.winfo_children():
            child.destroy()
        self.asset_rows.clear()
        self.asset_vars.clear()
        self.selected_release = None
        self.asset_title.configure(text="选择一个版本")
        self.asset_subtitle.configure(text="Release 资产会显示在这里")
        self.download_selected_btn.configure(state="disabled", text="下载所选")
        self.release_page_btn.configure(state="disabled")
        self.select_all_var.set(False)
        self.asset_empty = self._empty_state(
            self.asset_scroll,
            "info",
            "尚未选择版本",
            "从左侧版本列表中选择一个 Release。",
        )
        self.asset_empty.grid(row=0, column=0, sticky="ew", pady=(35, 0))

    def clear_release_rows(self) -> None:
        for child in self.release_scroll.winfo_children():
            child.destroy()
        self.release_rows.clear()
        self.release_empty = None
        self.load_more_btn = None

    def _load_releases(
        self,
        repo: RepoRef,
        releases: list[Release],
        has_more: bool = False,
        reset: bool = False,
    ) -> None:
        self.repo = repo
        if reset:
            self.releases = []
            self.clear_release_rows()
            self.next_release_page = 2
        known = {item.id for item in self.releases}
        self.releases.extend(item for item in releases if item.id not in known)
        self.has_more_releases = has_more
        if not reset:
            self.next_release_page += 1
        releases = self.releases
        if not releases:
            self.release_empty = self._empty_state(
                self.release_scroll,
                "info",
                "没有可用的 Release",
                "项目可能只提供源码，或尚未发布带资产的版本。",
            )
            self.release_empty.grid(row=0, column=0, sticky="ew", pady=(20, 0))
            self.release_state.configure(text="没有版本", text_color=COLORS["text3"])
            self.repo_meta_label.configure(
                text=f"{repo.owner}/{repo.name}  ·  这个项目还没有公开 Release"
            )
            self.fetch_count_label.configure(text="0 个版本")
            return
        asset_count = sum(len(release.assets) for release in releases)
        self.release_state.configure(
            text=f"{len(releases)} 个版本", text_color=COLORS["success"]
        )
        self.repo_meta_label.configure(
            text=f"{repo.owner}/{repo.name}  ·  {asset_count} 个资产文件"
        )
        self.fetch_count_label.configure(text=f"{len(releases)} 个版本")
        for index, release in enumerate(releases):
            if release.id not in self.release_rows:
                self._add_release_row(release, index)
        if has_more:
            self.load_more_btn = ctk.CTkButton(
                self.release_scroll,
                text="加载更多版本",
                command=self.load_more_releases,
                height=30,
                corner_radius=6,
                fg_color="transparent",
                hover_color=COLORS["surface3"],
                border_width=1,
                border_color=COLORS["line"],
                text_color=COLORS["text2"],
                font=ui_font(10),
            )
            self.load_more_btn.grid(
                row=len(releases), column=0, sticky="ew", padx=(4, 5), pady=8
            )
        if reset or not self.selected_release:
            self.after(30, lambda: self._select_release(releases[0]))

    def load_more_releases(self) -> None:
        if not self.repo or (self.worker and self.worker.is_alive()):
            return
        if self.load_more_btn:
            self.load_more_btn.configure(text="正在加载...", state="disabled")
        repo = self.repo
        page = self.next_release_page
        token = effective_github_token(self.settings)
        proxies = self.settings.proxies
        include_pre = bool(self.precheck.get())
        insecure = self.settings.allow_insecure_tls

        def work() -> None:
            try:
                releases, has_more = GitHubClient(
                    token=token,
                    proxies=proxies,
                    allow_insecure_tls=insecure,
                ).fetch_release_page(repo, page, per_page=10)
                if not include_pre:
                    releases = [item for item in releases if not item.prerelease]
                self._send_ui(
                    lambda: self._load_releases(repo, releases, has_more, False)
                )
            except Exception as exc:
                self._send_ui(lambda message=str(exc): self._load_more_failed(message))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _load_more_failed(self, message: str) -> None:
        if self.load_more_btn:
            self.load_more_btn.configure(text="加载更多版本", state="normal")
        messagebox.showwarning("加载失败", message, parent=self)

    def _add_release_row(self, release: Release, index: int) -> None:
        row = ctk.CTkFrame(
            self.release_scroll,
            fg_color="transparent",
            corner_radius=7,
            border_width=1,
            border_color=COLORS["surface"],
            height=74,
        )
        row.grid(row=index, column=0, sticky="ew", padx=(4, 5), pady=3)
        row.grid_columnconfigure(0, weight=1)
        row.grid_propagate(False)
        top = ctk.CTkFrame(row, fg_color="transparent", height=27)
        top.grid(row=0, column=0, sticky="ew", padx=11, pady=(8, 0))
        top.grid_columnconfigure(1, weight=1)
        tag = ctk.CTkLabel(
            top,
            text=fit_text(release.tag_name or release.display_name, 18),
            font=ui_font(12, "bold"),
            text_color=COLORS["text"],
            anchor="w",
            height=22,
        )
        tag.grid(row=0, column=0, sticky="w")
        if release.prerelease:
            badge = ctk.CTkLabel(
                top,
                text="预发布",
                font=ui_font(9, "bold"),
                text_color=COLORS["warning"],
                fg_color="#3c2e1c",
                corner_radius=4,
                height=19,
                width=47,
            )
            badge.grid(row=0, column=1, sticky="w", padx=(7, 0))
        date = ctk.CTkLabel(
            top,
            text=release.published_display,
            font=ui_font(9),
            text_color=COLORS["text3"],
            anchor="e",
            height=20,
        )
        date.grid(row=0, column=2, sticky="e")
        meta = ctk.CTkLabel(
            row,
            text=f"{fit_text(release.display_name, 38)}  ·  {len(release.assets)} 个文件",
            font=ui_font(9),
            text_color=COLORS["text3"],
            anchor="w",
            height=18,
        )
        meta.grid(row=1, column=0, sticky="ew", padx=11, pady=(1, 8))
        widgets = (row, top, tag, date, meta)
        for widget in widgets:
            widget.bind("<Button-1>", lambda _event, r=release: self._select_release(r))
        row.bind("<Enter>", lambda _event, r=row, x=release: self._hover_release(r, x))
        row.bind("<Leave>", lambda _event, r=row, x=release: self._leave_release(r, x))
        self.release_rows[release.id] = {
            "frame": row,
            "tag": tag,
            "date": date,
            "meta": meta,
        }

    def _hover_release(self, row: ctk.CTkFrame, release: Release) -> None:
        selected = self.selected_release and self.selected_release.id == release.id
        if not selected:
            row.configure(fg_color=COLORS["surface2"], border_color=COLORS["line"])

    def _leave_release(self, row: ctk.CTkFrame, release: Release) -> None:
        selected = self.selected_release and self.selected_release.id == release.id
        if not selected:
            row.configure(fg_color="transparent", border_color=COLORS["surface"])

    def _select_release(self, release: Release) -> None:
        self.selected_release = release
        for rel_id, row in self.release_rows.items():
            if rel_id < 0:
                continue
            selected = rel_id == release.id
            row["frame"].configure(
                fg_color=COLORS["surface2"] if selected else "transparent",
                border_color=COLORS["accent_dark"] if selected else COLORS["surface"],
            )
            row["tag"].configure(text_color=COLORS["accent"] if selected else COLORS["text"])
        self._show_assets(release)

    def _show_assets(self, release: Release) -> None:
        for child in self.asset_scroll.winfo_children():
            child.destroy()
        self.asset_rows.clear()
        self.asset_vars.clear()
        self.asset_title.configure(text=fit_text(release.display_name, 44))
        info = f"{release.tag_name}  ·  {release.published_display}"
        if release.prerelease:
            info += "  ·  预发布"
        self.asset_subtitle.configure(text=info)
        self.release_page_btn.configure(
            state="normal" if release.html_url else "disabled"
        )
        self.select_all_var.set(False)
        if not release.assets:
            self.download_selected_btn.configure(state="disabled")
            empty = self._empty_state(
                self.asset_scroll,
                "info",
                "这个版本没有附件",
                "该 Release 可能只包含说明，或者资产尚未上传。",
            )
            empty.grid(row=0, column=0, sticky="ew", pady=(30, 0))
            return
        for index, asset in enumerate(release.assets):
            self._add_asset_row(asset, index)
        self.download_selected_btn.configure(state="normal")

    def _add_asset_row(self, asset: Asset, index: int) -> None:
        row = ctk.CTkFrame(
            self.asset_scroll,
            fg_color=COLORS["surface2"],
            corner_radius=7,
            border_width=1,
            border_color=COLORS["line_soft"],
            height=57,
            cursor="hand2",
        )
        row.grid(row=index, column=0, sticky="ew", padx=(1, 5), pady=3)
        row.grid_columnconfigure(1, weight=1)
        row.grid_propagate(False)
        var = tk.BooleanVar(value=False)
        self.asset_vars[asset.id] = var
        checkbox = ctk.CTkCheckBox(
            row,
            text="",
            variable=var,
            command=self._refresh_selected_button,
            width=22,
            checkbox_width=18,
            checkbox_height=18,
            corner_radius=4,
            border_width=1,
            fg_color=COLORS["accent_soft"],
            hover_color=COLORS["accent_hover"],
            border_color=COLORS["text3"],
            checkmark_color=COLORS["accent"],
        )
        checkbox.grid(row=0, column=0, rowspan=2, padx=(10, 6), sticky="w")
        name = ctk.CTkLabel(
            row,
            text=fit_text(asset.name, 54),
            font=ui_font(11, "bold"),
            text_color=COLORS["text"],
            anchor="w",
            height=22,
        )
        name.grid(row=0, column=1, sticky="ew", pady=(6, 0))
        meta = ctk.CTkLabel(
            row,
            text=f"{asset.content_type or '文件'}  ·  下载 {asset.download_count:,} 次",
            font=ui_font(9),
            text_color=COLORS["text3"],
            anchor="w",
            height=17,
        )
        meta.grid(row=1, column=1, sticky="ew", pady=(0, 6))
        link_button = self.icon_button(
            row,
            "copy",
            "复制下载链接",
            lambda a=asset: self.copy_asset_link(a),
            width=28,
        )
        link_button.grid(row=0, column=2, rowspan=2, padx=(4, 2), sticky="e")
        size = ctk.CTkLabel(
            row,
            text=format_bytes(asset.size),
            font=ctk.CTkFont(family=MONO_FAMILY, size=10),
            text_color=COLORS["text2"],
            anchor="e",
            width=65,
        )
        size.grid(row=0, column=3, rowspan=2, padx=(5, 12), sticky="e")
        self.asset_rows[asset.id] = {
            "frame": row,
            "name": name,
            "size": size,
            "var": var,
        }
        for widget in (row, name, meta, size):
            widget.configure(cursor="hand2")
            widget.bind(
                "<Button-1>",
                lambda _event, v=var: self._toggle_asset_var(v),
            )

    def _toggle_asset_var(self, var: tk.BooleanVar) -> None:
        var.set(not var.get())
        self._refresh_selected_button()

    def _refresh_selected_button(self) -> None:
        count = sum(1 for var in self.asset_vars.values() if var.get())
        self.download_selected_btn.configure(
            state="normal" if self.selected_release and self.selected_release.assets else "disabled",
            text=f"下载  {count}" if count else "下载所选",
        )

    def _toggle_all_assets(self) -> None:
        value = bool(self.select_all_var.get())
        for var in self.asset_vars.values():
            var.set(value)
        self._refresh_selected_button()

    def copy_asset_link(self, asset: Asset) -> None:
        self.clipboard_clear()
        self.clipboard_append(asset.browser_url)
        self.update_idletasks()

    def open_release_page(self) -> None:
        if self.selected_release and self.selected_release.html_url:
            open_url(self.selected_release.html_url)

    def download_selected(self) -> None:
        release = self.selected_release
        if not release:
            return
        assets = [
            asset
            for asset in release.assets
            if self.asset_vars.get(asset.id) and self.asset_vars[asset.id].get()
        ]
        if not assets:
            for asset in release.assets:
                if asset.id in self.asset_vars:
                    self.asset_vars[asset.id].set(True)
            assets = list(release.assets)
        target_dir = Path(self.settings.download_dir)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("目录不可用", str(exc), parent=self)
            return
        for asset in assets:
            if not asset.browser_url:
                continue
            existing = next(
                (
                    job
                    for job in self.download_manager.jobs
                    if job.asset.id == asset.id and not job.stopped
                ),
                None,
            )
            if existing and existing.active:
                continue
            job = DownloadJob(
                asset=asset,
                repo=self.repo.canonical if self.repo else "",
                target_dir=target_dir,
                threads=self.settings.threads,
                mirror_mode=self.settings.mirror_mode,
                selected_mirror=self.settings.selected_mirror,
                allow_insecure_tls=self.settings.allow_insecure_tls,
            )
            candidates = build_candidate_urls(
                asset.browser_url,
                self.settings.mirror_mode,
                self.settings.selected_mirror,
            )
            if self.settings.mirror_mode == "mirror":
                candidates = [
                    (label, url) for label, url in candidates if label != "直连"
                ]
            self.download_manager.add_job(job)
            self._add_job_card(job)
            self.download_manager.start(job, candidates, self.settings.proxies)
        for var in self.asset_vars.values():
            var.set(False)
        self.select_all_var.set(False)
        self._refresh_selected_button()
        self._update_queue_ui()
        self._show_view("downloads")

    def _add_job_card(self, job: DownloadJob) -> None:
        card = ctk.CTkFrame(
            self.queue_scroll,
            fg_color=COLORS["surface2"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["line_soft"],
            height=100,
        )
        card.grid(
            row=len(self.job_cards),
            column=0,
            sticky="ew",
            padx=(3, 6),
            pady=4,
        )
        card.grid_columnconfigure(0, weight=1)
        card.grid_propagate(False)
        top = ctk.CTkFrame(card, fg_color="transparent", height=30)
        top.grid(row=0, column=0, sticky="ew", padx=13, pady=(7, 0))
        top.grid_columnconfigure(0, weight=1)
        name = ctk.CTkLabel(
            top,
            text=fit_text(job.filename, 74),
            font=ui_font(11, "bold"),
            text_color=COLORS["text"],
            anchor="w",
            height=21,
        )
        name.grid(row=0, column=0, sticky="w")
        status = ctk.CTkLabel(
            top,
            text="等待",
            font=ui_font(10),
            text_color=COLORS["accent"],
            anchor="e",
            width=90,
        )
        status.grid(row=0, column=1, sticky="e", padx=5)
        controls = ctk.CTkFrame(top, fg_color="transparent")
        controls.grid(row=0, column=2, sticky="e")
        pause_btn = self.icon_button(
            controls, "pause", "暂停 / 继续", lambda: self.toggle_pause(job), width=28
        )
        pause_btn.pack(side="left", padx=1)
        self.icon_button(
            controls, "stop", "取消任务", lambda: self.cancel_job(job), width=28
        ).pack(side="left", padx=1)
        progress = ctk.CTkProgressBar(
            card,
            height=5,
            corner_radius=2,
            fg_color=COLORS["surface3"],
            progress_color=COLORS["accent"],
        )
        progress.grid(row=1, column=0, sticky="ew", padx=13, pady=(9, 7))
        progress.set(0)
        meta = ctk.CTkFrame(card, fg_color="transparent", height=23)
        meta.grid(row=2, column=0, sticky="ew", padx=13, pady=(0, 7))
        meta.grid_columnconfigure(0, weight=1)
        info = ctk.CTkLabel(
            meta,
            text=f"{format_bytes(job.asset.size)}  ·  等待连接",
            font=ctk.CTkFont(family=MONO_FAMILY, size=9),
            text_color=COLORS["text3"],
            anchor="w",
            height=20,
        )
        info.grid(row=0, column=0, sticky="w")
        speed = ctk.CTkLabel(
            meta,
            text="0 B/s",
            font=ctk.CTkFont(family=MONO_FAMILY, size=9),
            text_color=COLORS["text2"],
            anchor="e",
            width=120,
            height=20,
        )
        speed.grid(row=0, column=1, sticky="e")
        self.job_cards[job.asset.id] = {
            "frame": card,
            "name": name,
            "status": status,
            "progress": progress,
            "info": info,
            "speed": speed,
            "pause_btn": pause_btn,
        }
        self.queue_empty.grid_remove()
        self._layout_job_cards()

    def _layout_job_cards(self) -> None:
        for index, job in enumerate(self.download_manager.jobs):
            card_data = self.job_cards.get(job.asset.id)
            if card_data:
                card_data["frame"].grid_configure(row=index)

    def toggle_pause(self, job: DownloadJob) -> None:
        if job.pause_event.is_set():
            self.download_manager.resume(job)
        elif job.active:
            self.download_manager.pause(job)
        elif job.status in {"失败", "已取消", "已暂停"}:
            candidates = build_candidate_urls(
                job.asset.browser_url,
                job.mirror_mode,
                job.selected_mirror,
            )
            if job.mirror_mode == "mirror":
                candidates = [
                    (label, url) for label, url in candidates if label != "直连"
                ]
            self.download_manager.start(job, candidates, self.settings.proxies)

    def cancel_job(self, job: DownloadJob) -> None:
        self.download_manager.cancel(job)
        if not job.active:
            job.stopped = True
            card = self.job_cards.get(job.asset.id)
            if card:
                card["status"].configure(text="已取消", text_color=COLORS["text3"])
                card["frame"].configure(border_color=COLORS["line_soft"])
        self._update_queue_ui()

    def start_speed_test(self) -> None:
        assets = [
            asset
            for release in self.releases
            for asset in release.assets
            if asset.browser_url
        ]
        if not assets:
            messagebox.showinfo("没有可测试资产", "先加载一个包含附件文件的 Release。", parent=self)
            return
        asset = max(assets, key=lambda item: item.size)
        candidates = build_candidate_urls(asset.browser_url, "auto")
        self.speedcheck_btn.configure(state="disabled", text="测试中")
        self.source_results.clear()
        for widget in self.queue_scroll.winfo_children():
            if widget is not self.queue_empty:
                widget.destroy()
        self.job_cards.clear()
        row = ctk.CTkFrame(
            self.queue_scroll,
            fg_color=COLORS["surface2"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["line_soft"],
            height=120,
        )
        row.grid(row=0, column=0, sticky="ew", padx=(3, 6), pady=4)
        row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            row,
            text=f"线路测速  ·  {fit_text(asset.name, 46)}",
            font=ui_font(11, "bold"),
            text_color=COLORS["text"],
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=13, pady=(10, 3))
        lines = []
        for label, _url in candidates:
            label_widget = ctk.CTkLabel(
                row,
                text=f"{label:<10}  等待测速",
                font=ctk.CTkFont(family=MONO_FAMILY, size=10),
                text_color=COLORS["text3"],
                anchor="w",
            )
            label_widget.grid(
                row=1 + len(lines), column=0, sticky="ew", padx=13, pady=1
            )
            lines.append((label, label_widget))
        cancel_event = threading.Event()

        def report(index: int, info: object) -> None:
            if self.closed:
                return

            def apply() -> None:
                label, widget = lines[index]
                if getattr(info, "ok", False):
                    speed = getattr(info, "speed", 0.0)
                    latency = getattr(info, "latency", 0.0)
                    widget.configure(
                        text=f"{label:<10}  {format_bytes(speed)}/s    延迟 {latency:.2f}s",
                        text_color=COLORS["success"],
                    )
                else:
                    widget.configure(
                        text=f"{label:<10}  不可用  {getattr(info, 'error', '')}",
                        text_color=COLORS["danger"],
                    )

            self.after(0, apply)

        def work() -> None:
            for index, (label, url) in enumerate(candidates):
                from .downloader import test_source

                info = test_source(
                    url,
                    label,
                    self.settings.proxies,
                    self.settings.allow_insecure_tls,
                    cancel_event=cancel_event,
                )
                report(index, info)
            self.after(
                0,
                lambda: self.speedcheck_btn.configure(
                    state="normal", text="测试线路"
                ),
            )

        threading.Thread(target=work, daemon=True).start()

    def _poll_events(self) -> None:
        if self.closed:
            return
        changed = False
        while True:
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break
            changed = True
            self._handle_event(event)
        if changed:
            self._update_queue_ui()
        self.after(160, self._poll_events)

    def _handle_event(self, event: dict) -> None:
        job = event.get("job")
        if not isinstance(job, DownloadJob):
            return
        card = self.job_cards.get(job.asset.id)
        if not card:
            return
        kind = event.get("kind")
        ratio = job.downloaded / job.total if job.total else 0
        if kind == "progress":
            card["progress"].set(min(1, max(0, ratio)))
            card["status"].configure(
                text=job.status,
                text_color=COLORS["accent"] if job.status != "已暂停" else COLORS["warning"],
            )
            source = f"  ·  {job.source}" if job.source else ""
            card["info"].configure(
                text=f"{format_bytes(job.downloaded)} / {format_bytes(job.total)}  ·  {self.settings.threads} 线程{source}"
            )
            card["speed"].configure(text=f"{format_bytes(job.speed)}/s")
        elif kind == "source":
            job.source = str(event.get("source") or job.source)
            card["info"].configure(
                text=f"{format_bytes(job.downloaded)} / {format_bytes(job.total)}  ·  线路 {job.source}"
            )
        elif kind == "done":
            card["progress"].set(1)
            card["status"].configure(text="已完成", text_color=COLORS["success"])
            card["speed"].configure(text="完成", text_color=COLORS["success"])
            card["info"].configure(
                text=f"{format_bytes(job.downloaded)}  ·  {job.source}  ·  {job.final_path}"
            )
            card["pause_btn"].configure(image=self.icons.get("refresh"))
        elif kind == "cancelled":
            card["status"].configure(text="已取消", text_color=COLORS["text3"])
            card["speed"].configure(text="—", text_color=COLORS["text3"])
        elif kind == "failed":
            card["status"].configure(text="失败", text_color=COLORS["danger"])
            card["speed"].configure(text="—", text_color=COLORS["text3"])
            card["info"].configure(
                text=fit_text(str(event.get("error") or job.error), 110),
                text_color=COLORS["danger"],
            )

    def _update_queue_ui(self) -> None:
        jobs = self.download_manager.jobs
        active = sum(1 for job in jobs if job.active)
        done = sum(1 for job in jobs if job.status == "已完成")
        total_speed = sum(job.speed for job in jobs if job.active)
        self.downloads_nav.configure(
            text=f"下载  {len(jobs)}" if jobs else "下载  0"
        )
        self.queue_meta.configure(
            text=f"{len(jobs)} 个任务  ·  {active} 个正在进行  ·  {done} 个已完成"
            if jobs
            else "0 个任务"
        )
        self.queue_summary.configure(
            text=f"进行中 {active}  ·  已完成 {done}"
            if jobs
            else "还没有下载任务"
        )
        self.global_speed.configure(text=f"{format_bytes(total_speed)}/s")

    def _fetch_failed(self, message: str) -> None:
        self.clear_release_rows()
        self.release_empty = self._empty_state(
            self.release_scroll,
            "warning",
            "加载失败",
            message,
        )
        self.release_empty.grid(row=0, column=0, sticky="ew", pady=(28, 0))
        self.release_state.configure(text="加载失败", text_color=COLORS["danger"])
        self.repo_meta_label.configure(text=message[:100])
        self.fetch_count_label.configure(text="")
        self.release_page_btn.configure(state="disabled")
        self.download_selected_btn.configure(state="disabled", text="下载所选")
        if "额度" in message:
            self._show_rate_limit_action()

    def _show_rate_limit_action(self) -> None:
        for child in self.asset_scroll.winfo_children():
            child.destroy()
        self.asset_rows.clear()
        self.asset_vars.clear()
        self.asset_title.configure(text="GitHub API 额度已用完")
        self.asset_subtitle.configure(
            text="未登录请求共享公网 IP 的 60 次/小时额度，稍后重试或添加 Token"
        )
        frame = ctk.CTkFrame(
            self.asset_scroll,
            fg_color=COLORS["surface2"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["line"],
        )
        frame.grid(row=0, column=0, sticky="ew", padx=6, pady=20)
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            frame,
            text="这次不是仓库本身的问题。GitHub 对未登录 API 请求按出口 IP 限速，"
            "当前地址的额度已经用尽。",
            font=ui_font(10),
            text_color=COLORS["text2"],
            wraplength=500,
            justify="left",
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        ctk.CTkLabel(
            frame,
            text="添加一个只读 Token 后，限制会提高到 5000 次/小时。Token 仅保存在本机。",
            font=ui_font(10),
            text_color=COLORS["text3"],
            wraplength=500,
            justify="left",
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 13))
        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 15))
        ctk.CTkButton(
            buttons,
            text="获取 Token",
            image=self.icons.get("external"),
            compound="left",
            command=lambda: open_url(
                "https://github.com/settings/tokens/new?scopes=&description=ReleaseFlow"
            ),
            width=104,
            height=32,
            corner_radius=6,
            fg_color="transparent",
            hover_color=COLORS["surface3"],
            border_width=1,
            border_color=COLORS["line"],
            text_color=COLORS["text2"],
            font=ui_font(10),
        ).pack(side="left", padx=(0, 7))
        ctk.CTkButton(
            buttons,
            text="填写 Token",
            command=lambda: SettingsDialog(
                self, self.settings, self._save_token_settings, token_only=True
            ),
            width=94,
            height=32,
            corner_radius=6,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["bg"],
            font=ui_font(10, "bold"),
        ).pack(side="left")

    def _save_token_settings(self, settings: Settings) -> None:
        self._save_settings(settings)
        if self.repo:
            self.start_fetch()

    def open_download_dir(self) -> None:
        path = Path(self.settings.download_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self)

    def open_settings(self) -> None:
        SettingsDialog(self, self.settings, self._save_settings)

    def _save_settings(self, settings: Settings) -> None:
        self.settings = settings
        try:
            save_settings(settings)
        except OSError as exc:
            messagebox.showwarning("设置未保存", str(exc), parent=self)

    def _on_close(self) -> None:
        active = [job for job in self.download_manager.jobs if job.active]
        if active:
            answer = messagebox.askyesno(
                "退出 ReleaseFlow",
                "仍有下载任务在进行。退出会暂停任务，已下载的数据会保留。\n确定退出吗？",
                parent=self,
            )
            if not answer:
                return
            for job in active:
                job.cancel_event.set()
        for job in self.download_manager.jobs:
            job.cancel_event.set()
            job.pause_event.clear()
        self.closed = True
        self.cancel_fetch.set()
        try:
            save_settings(self.settings)
        except OSError:
            pass
        self.destroy()


class Tooltip:
    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _event: tk.Event | None = None) -> None:
        if self.window or not self.text:
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self.window,
            text=self.text,
            background=COLORS["surface3"],
            foreground=COLORS["text"],
            font=(FONT_FAMILY, 9),
            padx=8,
            pady=4,
            relief="flat",
        )
        label.pack()

    def hide(self, _event: tk.Event | None = None) -> None:
        if self.window:
            self.window.destroy()
            self.window = None


class SettingsDialog(ctk.CTkToplevel):
    def __init__(
        self,
        parent: ReleaseFlowApp,
        settings: Settings,
        on_save: Callable[[Settings], None],
        token_only: bool = False,
    ) -> None:
        super().__init__(parent)
        self.parent = parent
        self.settings = settings
        self.on_save = on_save
        self.token_only = token_only
        self.title("填写 GitHub Token" if token_only else "设置")
        self.geometry("570x340" if token_only else "570x560")
        self.minsize(520, 340 if token_only else 520)
        self.configure(fg_color=COLORS["bg"])
        self.transient(parent)
        self.grab_set()
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build()
        self.after(50, self.lift)

    def _build(self) -> None:
        body = ctk.CTkFrame(
            self, fg_color=COLORS["surface"], corner_radius=0, border_width=1,
            border_color=COLORS["line_soft"]
        )
        body.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)
        body.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            body,
            text="GitHub Token" if self.token_only else "下载设置",
            font=ui_font(18, "bold"),
            text_color=COLORS["text"],
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=20, pady=(17, 3))
        ctk.CTkLabel(
            body,
            text=(
                "用于提高 Release API 额度，只保存在本机。"
                if self.token_only
                else "保存后应用于新建的下载任务。"
            ),
            font=ui_font(10),
            text_color=COLORS["text3"],
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 12))
        if self.token_only:
            self._build_token_only(body)
            return
        fields = ctk.CTkFrame(body, fg_color="transparent")
        fields.grid(row=2, column=0, sticky="ew", padx=20)
        fields.grid_columnconfigure(1, weight=1)
        row = 0
        self._label(fields, row, "下载目录")
        dir_box = ctk.CTkFrame(fields, fg_color="transparent")
        dir_box.grid(row=row, column=1, sticky="ew", pady=5)
        dir_box.grid_columnconfigure(0, weight=1)
        self.dir_entry = ctk.CTkEntry(
            dir_box,
            fg_color=COLORS["surface2"],
            border_color=COLORS["line"],
            text_color=COLORS["text"],
            height=34,
            corner_radius=6,
        )
        self.dir_entry.grid(row=0, column=0, sticky="ew")
        self.dir_entry.insert(0, self.settings.download_dir)
        parent_app = self.parent
        ctk.CTkButton(
            dir_box,
            text="",
            image=parent_app.icons.get("folder"),
            command=self._choose_dir,
            width=36,
            height=34,
            corner_radius=6,
            fg_color=COLORS["surface3"],
            hover_color=COLORS["line"],
        ).grid(row=0, column=1, padx=(5, 0))
        row += 1
        self._label(fields, row, "下载线程")
        self.threads_var = tk.StringVar(value=str(self.settings.threads))
        ctk.CTkOptionMenu(
            fields,
            values=["1", "2", "4", "6", "8", "12", "16", "24", "32"],
            variable=self.threads_var,
            width=100,
            height=34,
            corner_radius=6,
            fg_color=COLORS["surface2"],
            button_color=COLORS["surface3"],
            button_hover_color=COLORS["line"],
            dropdown_fg_color=COLORS["surface2"],
            dropdown_hover_color=COLORS["surface3"],
            text_color=COLORS["text"],
            font=ui_font(11),
        ).grid(row=row, column=1, sticky="w", pady=5)
        row += 1
        self._label(fields, row, "下载线路")
        self.mode_var = tk.StringVar(value=self.settings.mirror_mode)
        mode_box = ctk.CTkFrame(fields, fg_color="transparent")
        mode_box.grid(row=row, column=1, sticky="w", pady=5)
        for value, text in (
            ("auto", "自动"),
            ("mirror", "指定镜像"),
            ("direct", "GitHub 直连"),
        ):
            ctk.CTkRadioButton(
                mode_box,
                text=text,
                variable=self.mode_var,
                value=value,
                font=ui_font(10),
                text_color=COLORS["text2"],
                fg_color=COLORS["accent"],
                hover_color=COLORS["accent_hover"],
                radiobutton_width=16,
                radiobutton_height=16,
                border_width_unchecked=1,
            ).pack(side="left", padx=(0, 9))
        row += 1
        self._label(fields, row, "指定镜像")
        self.mirror_var = tk.StringVar(
            value=mirror_label(self.settings.selected_mirror)
        )
        self.mirror_map = {mirror_label(item): item for item in DEFAULT_MIRRORS}
        ctk.CTkOptionMenu(
            fields,
            values=list(self.mirror_map.keys()),
            variable=self.mirror_var,
            width=160,
            height=34,
            corner_radius=6,
            fg_color=COLORS["surface2"],
            button_color=COLORS["surface3"],
            button_hover_color=COLORS["line"],
            dropdown_fg_color=COLORS["surface2"],
            dropdown_hover_color=COLORS["surface3"],
            text_color=COLORS["text"],
            font=ui_font(11),
        ).grid(row=row, column=1, sticky="w", pady=5)
        row += 1
        self._label(fields, row, "HTTP 代理")
        self.proxy_entry = ctk.CTkEntry(
            fields,
            placeholder_text="可选，如 http://127.0.0.1:7890",
            placeholder_text_color=COLORS["text3"],
            fg_color=COLORS["surface2"],
            border_color=COLORS["line"],
            text_color=COLORS["text"],
            height=34,
            corner_radius=6,
        )
        self.proxy_entry.grid(row=row, column=1, sticky="ew", pady=5)
        self.proxy_entry.insert(0, self.settings.proxies)
        row += 1
        self._label(fields, row, "GitHub Token")
        self.token_entry = ctk.CTkEntry(
            fields,
            placeholder_text="可选，提高 API 额度；仅保存在本机",
            placeholder_text_color=COLORS["text3"],
            fg_color=COLORS["surface2"],
            border_color=COLORS["line"],
            text_color=COLORS["text"],
            height=34,
            corner_radius=6,
            show="*",
        )
        self.token_entry.grid(row=row, column=1, sticky="ew", pady=5)
        self.token_entry.insert(0, self.settings.github_token)
        row += 1
        self.insecure_var = tk.BooleanVar(value=self.settings.allow_insecure_tls)
        insecure = ctk.CTkCheckBox(
            fields,
            text="允许不安全 TLS（仅在证书错误时临时使用）",
            variable=self.insecure_var,
            font=ui_font(10),
            text_color=COLORS["text3"],
            checkbox_width=17,
            checkbox_height=17,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            border_color=COLORS["line"],
            corner_radius=4,
        )
        insecure.grid(row=row, column=1, sticky="w", pady=(10, 3))
        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=20, pady=14)
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            footer,
            text="镜像可用性会随服务状态变化，自动模式会在失败时回退直连。",
            font=ui_font(9),
            text_color=COLORS["text3"],
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            footer,
            text="取消",
            command=self.destroy,
            width=76,
            height=34,
            corner_radius=6,
            fg_color="transparent",
            hover_color=COLORS["surface3"],
            border_width=1,
            border_color=COLORS["line"],
            text_color=COLORS["text2"],
            font=ui_font(11),
        ).grid(row=0, column=1, padx=6)
        ctk.CTkButton(
            footer,
            text="保存设置",
            command=self._save,
            width=94,
            height=34,
            corner_radius=6,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["bg"],
            font=ui_font(11, "bold"),
        ).grid(row=0, column=2, padx=(6, 0))

    def _build_token_only(self, body: ctk.CTkFrame) -> None:
        fields = ctk.CTkFrame(body, fg_color="transparent")
        fields.grid(row=2, column=0, sticky="ew", padx=20)
        fields.grid_columnconfigure(1, weight=1)
        self._label(fields, 0, "GitHub Token")
        self.token_entry = ctk.CTkEntry(
            fields,
            placeholder_text="粘贴只读 Token（不需要勾选任何权限）",
            placeholder_text_color=COLORS["text3"],
            fg_color=COLORS["surface2"],
            border_color=COLORS["line"],
            text_color=COLORS["text"],
            height=34,
            corner_radius=6,
            show="*",
        )
        self.token_entry.grid(row=0, column=1, sticky="ew", pady=5)
        self.token_entry.insert(0, self.settings.github_token)
        self._label(fields, 1, "")
        ctk.CTkLabel(
            fields,
            text="创建 Token 时保持权限为空即可读取公开 Releases；Token 仅写入本机配置。",
            font=ui_font(9),
            text_color=COLORS["text3"],
            wraplength=390,
            justify="left",
            anchor="w",
        ).grid(row=1, column=1, sticky="w", pady=(2, 4))
        self.dir_entry = _EntryValue(self.settings.download_dir)
        self.proxy_entry = _EntryValue(self.settings.proxies)
        self.threads_var = tk.StringVar(value=str(self.settings.threads))
        self.mode_var = tk.StringVar(value=self.settings.mirror_mode)
        self.mirror_var = tk.StringVar(
            value=mirror_label(self.settings.selected_mirror)
        )
        self.mirror_map = {mirror_label(item): item for item in DEFAULT_MIRRORS}
        self.insecure_var = tk.BooleanVar(value=self.settings.allow_insecure_tls)
        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=20, pady=14)
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            footer,
            text="取消",
            command=self.destroy,
            width=76,
            height=34,
            corner_radius=6,
            fg_color="transparent",
            hover_color=COLORS["surface3"],
            border_width=1,
            border_color=COLORS["line"],
            text_color=COLORS["text2"],
            font=ui_font(11),
        ).grid(row=0, column=1, padx=6)
        ctk.CTkButton(
            footer,
            text="保存并重试",
            command=self._save,
            width=106,
            height=34,
            corner_radius=6,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["bg"],
            font=ui_font(11, "bold"),
        ).grid(row=0, column=2, padx=(6, 0))

    def _label(self, parent: ctk.CTkFrame, row: int, text: str) -> None:
        ctk.CTkLabel(
            parent,
            text=text,
            font=ui_font(10, "bold"),
            text_color=COLORS["text3"],
            anchor="w",
            width=88,
        ).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 10))

    def _choose_dir(self) -> None:
        selected = filedialog.askdirectory(
            parent=self,
            initialdir=self.dir_entry.get() or str(Path.home()),
        )
        if selected:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, selected)

    def _save(self) -> None:
        settings = Settings(
            download_dir=self.dir_entry.get().strip(),
            threads=int(self.threads_var.get()),
            mirror_mode=self.mode_var.get(),
            selected_mirror=self.mirror_map.get(
                self.mirror_var.get(), DEFAULT_MIRRORS[0]
            ),
            allow_insecure_tls=bool(self.insecure_var.get()),
            proxies=self.proxy_entry.get().strip(),
            github_token=self.token_entry.get().strip(),
            token_from_env=True,
        )
        self.on_save(settings)
        self.destroy()


class _EntryValue:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


def open_url(url: str) -> None:
    import webbrowser

    webbrowser.open(url)
