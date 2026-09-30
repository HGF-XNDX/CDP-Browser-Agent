from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import logging
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4
from urllib.request import urlopen

from playwright.async_api import Browser, Page, async_playwright

from .dom import BrowserDOM


DATA_DIR_NAME = "data"
ANALYSIS_DIR_NAME = "analysis"

SAVE_PAGE_SCRIPT = r"""() => ({
    title: document.title || "",
    url: location.href,
    text: String(document.body?.innerText || document.documentElement?.innerText || "").replace(/\r/g, "").trim()
})"""


def safe_resource_name(value: str, fallback: str = "downloaded_resource") -> str:
    text = unquote(str(value or "")).strip().lower()
    text = re.sub(r"[\s.-]+", "_", text)
    text = "".join(char for char in text if char.isalnum() or char == "_")
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:100] or fallback


def infer_resource_name(url: str, filename: str | None = None) -> str:
    parsed = urlparse(url or "")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    hostname = (parsed.hostname or "").lower()
    candidate = Path(filename or (parts[-1] if parts else "")).stem
    generic_names = {"train", "test", "validation", "data", "dataset", "downloaded_resource", "link"}
    if candidate and safe_resource_name(candidate) not in generic_names:
        return safe_resource_name(candidate)
    return safe_resource_name((hostname.split(".")[0] if hostname else "") or "downloaded_resource")


def safe_artifact_name(value: str, fallback: str = "downloaded_resource") -> str:
    name = "".join(char for char in str(value) if char.isalnum() or char in "._- ").strip(". ")[:180]
    if not name:
        return fallback
    if Path(name).stem.upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        name = "file_" + name
    return name


def artifact_target(directory: Path, name: str) -> Path:
    target = directory / name
    if target.exists():
        target = directory / f"{target.stem}_{uuid4().hex[:10]}{target.suffix}"
    return target


def find_chrome_executable() -> str:
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        str(Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return ""


def cdp_port_from_url(cdp_url: str) -> int:
    parsed = urlparse(cdp_url or "")
    if parsed.port:
        return int(parsed.port)
    return 443 if parsed.scheme == "https" else 80


def cdp_host_from_url(cdp_url: str) -> str:
    parsed = urlparse(cdp_url or "")
    return parsed.hostname or "127.0.0.1"


def cdp_version_url(cdp_url: str) -> str:
    return cdp_url.rstrip("/") + "/json/version"


async def is_cdp_available(cdp_url: str, timeout: float = 0.8) -> bool:
    def probe() -> bool:
        try:
            with urlopen(cdp_version_url(cdp_url), timeout=timeout) as response:
                return response.status == 200
        except Exception:
            return False

    return await asyncio.to_thread(probe)


async def wait_for_cdp(cdp_url: str, timeout_ms: int) -> bool:
    deadline = time.monotonic() + max(timeout_ms, 1000) / 1000
    while time.monotonic() < deadline:
        if await is_cdp_available(cdp_url):
            return True
        await asyncio.sleep(0.25)
    return False


def viewport_mode(browser_config: dict) -> str:
    viewport = browser_config.get("viewport")
    if isinstance(viewport, str):
        value = viewport.strip().lower()
        if value in {"auto", "fullscreen", "maximized", "maximize"}:
            return value
    if isinstance(viewport, dict):
        width = str(viewport.get("width", "")).strip().lower()
        height = str(viewport.get("height", "")).strip().lower()
        if "fullscreen" in {width, height}:
            return "fullscreen"
        if "maximized" in {width, height} or "maximize" in {width, height}:
            return "maximized"
        if width == "auto" or height == "auto":
            return "auto"
    return "fixed"


def resolve_viewport(browser_config: dict) -> dict | None:
    if viewport_mode(browser_config) in {"auto", "fullscreen", "maximized", "maximize"}:
        return None
    viewport = browser_config.get("viewport") or {"width": 1280, "height": 900}
    if not isinstance(viewport, dict):
        return {"width": 1280, "height": 900}
    try:
        return {"width": int(viewport.get("width", 1280)), "height": int(viewport.get("height", 900))}
    except (TypeError, ValueError):
        return {"width": 1280, "height": 900}


def launch_cdp_browser_process(browser_config: dict, cdp_url: str) -> subprocess.Popen:
    executable = browser_config.get("executable_path") or browser_config.get("chrome_path") or find_chrome_executable()
    if not executable:
        raise RuntimeError("Chrome/Edge executable was not found. Set browser.executable_path in config.json.")

    port = int(browser_config.get("remote_debugging_port") or cdp_port_from_url(cdp_url) or 9222)
    host = browser_config.get("remote_debugging_address") or cdp_host_from_url(cdp_url)
    user_data_dir = (
        browser_config.get("user_data_dir")
        or browser_config.get("userDataDir")
        or str(Path(tempfile.gettempdir()) / "cdpagent-memory-chrome-profile")
    )
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)

    command = [
        str(executable),
        f"--remote-debugging-port={port}",
        f"--remote-debugging-address={host}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if viewport_mode(browser_config) in {"fullscreen", "maximized", "maximize"}:
        command.append("--start-maximized")
    start_url = browser_config.get("start_url")
    if start_url:
        command.append(start_url)
    command.extend(str(arg) for arg in browser_config.get("cdp_launch_args", []) if arg)

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)


class BrowserController:
    def __init__(
        self,
        browser: Browser,
        page: Page,
        focus_page: bool = True,
        downloads_path: str | None = None,
        observation_empty_retries: int = 3,
        observation_empty_retry_ms: int = 350,
        new_page_adoption_timeout_ms: int = 3000,
        organize_downloads_by_resource: bool = True,
    ):
        self.browser = browser
        self.page = page
        self.context = page.context
        self.connected_over_cdp = False
        self._cdp_process: subprocess.Popen | None = None
        self._close_cdp_process = False
        self.focus_page = focus_page
        self.downloads_path = str(Path(downloads_path or "downloads/browser-agent").expanduser().resolve())
        self.organize_downloads_by_resource = bool(organize_downloads_by_resource)
        self.active_resource_name: str | None = None
        self.observation_empty_retries = max(0, int(observation_empty_retries))
        self.observation_empty_retry_ms = max(0, int(observation_empty_retry_ms))
        self.new_page_adoption_timeout_ms = max(0, int(new_page_adoption_timeout_ms))
        self.observed_text_by_url: dict[str, str] = {}
        self.observed_ranges_by_url: dict[str, list[dict]] = {}
        self._page_ids: dict[Page, str] = {}
        self._next_page_id = 1
        self.page_id(self.page)
        self.dom = BrowserDOM()

    @classmethod
    async def launch(cls, config: dict) -> "BrowserController":
        playwright = await async_playwright().start()
        browser = None
        cdp_process = None
        try:
            browser_config = config.get("browser", {})
            mode = str(browser_config.get("connection") or browser_config.get("mode") or "launch").lower()
            viewport = resolve_viewport(browser_config)
            is_maximized = viewport_mode(browser_config) in {"fullscreen", "maximized", "maximize"}
            focus_page = bool(browser_config.get("focus_page", True))
            downloads_path = browser_config.get("downloads_path") or browser_config.get("download_dir")
            if downloads_path:
                Path(downloads_path).mkdir(parents=True, exist_ok=True)
            context_options = {"accept_downloads": True}
            storage = browser_config.get("storage_state") or browser_config.get("auth_state_path")
            if storage and Path(storage).is_file():
                context_options["storage_state"] = str(Path(storage).resolve())
            if viewport is None:
                context_options["no_viewport"] = True
            else:
                context_options["viewport"] = viewport
            # NOTE: downloads_path is a launch() argument, NOT a new_context() kwarg.
            # It is applied on chromium.launch(...) below (launch mode only).

            if mode == "cdp":
                cdp_url = browser_config.get("cdp_url") or browser_config.get("cdpUrl") or "http://127.0.0.1:9222"
                cdp_process = None
                if not await is_cdp_available(cdp_url):
                    if bool(browser_config.get("auto_start_cdp", True)):
                        logging.getLogger(__name__).info("Starting CDP browser for %s", cdp_url)
                        cdp_process = launch_cdp_browser_process(browser_config, cdp_url)
                        timeout_ms = int(browser_config.get("cdp_startup_timeout_ms", 10000))
                        if not await wait_for_cdp(cdp_url, timeout_ms):
                            raise RuntimeError(f"Started browser but CDP endpoint did not become available: {cdp_url}")
                    else:
                        raise RuntimeError(f"CDP endpoint is not available: {cdp_url}")
                browser = await playwright.chromium.connect_over_cdp(cdp_url)
                context = browser.contexts[0] if browser.contexts else await browser.new_context(**context_options)
                page = next((candidate for candidate in context.pages if candidate.url.startswith(("http://", "https://"))), None)
                if page is None:
                    page = context.pages[0] if context.pages else await context.new_page()
                if viewport is not None:
                    try:
                        await page.set_viewport_size(viewport)
                    except Exception:
                        pass
                start_url = browser_config.get("start_url")
                if start_url and not page.url.startswith(("http://", "https://")):
                    await page.goto(start_url, wait_until="domcontentloaded")
                controller = cls(
                    browser,
                    page,
                    focus_page=focus_page,
                    downloads_path=downloads_path,
                    observation_empty_retries=int(browser_config.get("observation_empty_retries", 3)),
                    observation_empty_retry_ms=int(browser_config.get("observation_empty_retry_ms", 350)),
                    new_page_adoption_timeout_ms=int(browser_config.get("new_page_adoption_timeout_ms", 3000)),
                    organize_downloads_by_resource=bool(browser_config.get("organize_downloads_by_resource", True)),
                )
                controller.connected_over_cdp = True
                controller._cdp_process = cdp_process
                controller._close_cdp_process = bool(browser_config.get("close_auto_started_cdp", False))
            else:
                launch_kwargs = {
                    "headless": bool(browser_config.get("headless", False)),
                    "slow_mo": int(browser_config.get("slow_mo", 300)),
                    "args": ["--start-maximized"] if is_maximized else None,
                }
                if browser_config.get("proxy"):
                    launch_kwargs["proxy"] = browser_config["proxy"]
                if downloads_path:
                    launch_kwargs["downloads_path"] = str(downloads_path)
                browser = await playwright.chromium.launch(**launch_kwargs)
                context = await browser.new_context(**context_options)
                page = await context.new_page()
                start_url = browser_config.get("start_url") or "about:blank"
                try:
                    await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    # Some sites insert a session challenge/redirect beyond navigation
                    # timeout even though the page continues loading successfully.
                    # Keep the browser session alive so the agent can observe/recover.
                    try:
                        await page.wait_for_timeout(2000)
                    except Exception:
                        pass
                controller = cls(
                    browser,
                    page,
                    focus_page=focus_page,
                    downloads_path=downloads_path,
                    observation_empty_retries=int(browser_config.get("observation_empty_retries", 3)),
                    observation_empty_retry_ms=int(browser_config.get("observation_empty_retry_ms", 350)),
                    new_page_adoption_timeout_ms=int(browser_config.get("new_page_adoption_timeout_ms", 3000)),
                    organize_downloads_by_resource=bool(browser_config.get("organize_downloads_by_resource", True)),
                )

            controller._playwright = playwright
            controller.auth_state_path = browser_config.get("auth_state_path")
            await controller.bring_page_to_front()
            return controller
        except BaseException:
            if browser is not None and not str(config.get("browser", {}).get("connection", "launch")).lower() == "cdp":
                await browser.close()
            await playwright.stop()
            if cdp_process is not None and cdp_process.poll() is None:
                cdp_process.terminate()
            raise

    async def bring_page_to_front(self) -> None:
        if not self.focus_page:
            return
        try:
            await self.page.bring_to_front()
        except Exception:
            pass

    def browser_pages(self) -> list[Page]:
        return [
            page
            for context in self.browser.contexts
            for page in context.pages
            if not page.is_closed()
        ]

    def page_id(self, page: Page) -> str:
        if page not in self._page_ids:
            self._page_ids[page] = f"page_{self._next_page_id}"
            self._next_page_id += 1
        return self._page_ids[page]

    def resolve_page_target(self, action: dict) -> Page | None:
        pages = self.browser_pages()
        page_id = str(action.get("page_id") or "").strip()
        if page_id:
            return next((page for page in pages if self.page_id(page) == page_id), None)
        try:
            page_index = int(action.get("page_index"))
        except (TypeError, ValueError):
            return None
        return pages[page_index] if 0 <= page_index < len(pages) else None

    async def adopt_new_page_if_any(self, known_pages: set[Page], timeout_ms: int | None = None) -> Page | None:
        deadline = time.monotonic() + max(
            0,
            self.new_page_adoption_timeout_ms if timeout_ms is None else int(timeout_ms),
        ) / 1000
        new_pages: list[Page] = []
        while True:
            new_pages = [page for page in self.browser_pages() if page not in known_pages]
            if new_pages or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.1)
        if not new_pages:
            return None
        self.page = new_pages[-1]
        self.context = self.page.context
        self.page_id(self.page)
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        await self.bring_page_to_front()
        return self.page

    async def page_context(self) -> dict:
        pages = []
        active_index = None
        active_page_id = None
        for index, page in enumerate(self.browser_pages()):
            if page == self.page:
                active_index = index
                active_page_id = self.page_id(page)
            try:
                title = await page.title()
            except Exception:
                title = ""
            pages.append(
                {
                    "pageId": self.page_id(page),
                    "index": index,
                    "title": (title or "")[:180],
                    "url": (page.url or "")[:700],
                    "active": page == self.page,
                    "currentTask": page == self.page,
                    "closed": page.is_closed(),
                }
            )
        return {
            "totalPages": len(pages),
            "activePageIndex": active_index,
            "taskPageIndex": active_index,
            "activePageId": active_page_id,
            "taskPageId": active_page_id,
            "connectedOverCdp": bool(self.connected_over_cdp),
            "pages": pages[:30],
        }

    def merge_observed_text(self, url: str, text: str) -> str:
        previous = self.observed_text_by_url.get(url, "")
        normalized = " ".join((text or "").split())
        if not normalized:
            return previous
        if normalized[:160] not in previous:
            combined = f"{previous}\n--- viewport ---\n{normalized}" if previous else normalized
            self.observed_text_by_url[url] = combined[-40000:]
        return self.observed_text_by_url.get(url, "")

    def merge_observed_ranges(self, url: str, current_range: dict) -> list[dict]:
        ranges = [*self.observed_ranges_by_url.get(url, []), current_range]
        clean_ranges = [
            {"start": int(item.get("start", 0)), "end": int(item.get("end", 0))}
            for item in ranges
            if item and int(item.get("end", 0)) >= int(item.get("start", 0))
        ]
        clean_ranges.sort(key=lambda item: item["start"])
        merged: list[dict] = []
        for item in clean_ranges:
            if not merged or item["start"] > merged[-1]["end"] + 80:
                merged.append(dict(item))
            else:
                merged[-1]["end"] = max(merged[-1]["end"], item["end"])
        self.observed_ranges_by_url[url] = merged[-20:]
        return self.observed_ranges_by_url[url]

    @staticmethod
    def is_transient_empty_observation(observation: dict) -> bool:
        url = str(observation.get("url") or "")
        if not url.startswith(("http://", "https://")):
            return False
        return not (
            observation.get("elements")
            or str(observation.get("viewportText") or "").strip()
            or str(observation.get("pageTextPreview") or "").strip()
            or str(observation.get("semanticTree") or "").strip()
        )

    async def evaluate_observation(self) -> dict:
        try:
            return await self.dom.observe(self.page)
        except Exception as exc:
            return {
                "url": self.page.url,
                "title": "Browser Error Page",
                "observationError": str(exc)[:1000],
                "visibleText": f"Browser Error: {exc}",
                "viewportText": f"Browser Error: {exc}",
                "pageTextPreview": "",
                "observedText": "",
                "observedTextLength": 0,
                "scroll": {"canScrollDown": False, "viewportHeight": 800, "observedCoverage": 1},
                "viewport": {"width": 0, "height": 0},
                "elements": [],
            }

    async def observe(self) -> dict:
        await self.bring_page_to_front()
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=1500)
        except Exception:
            pass
        observation = await self.evaluate_observation()
        initial_empty = self.is_transient_empty_observation(observation)
        empty_retries = 0
        while (
            self.is_transient_empty_observation(observation)
            and empty_retries < self.observation_empty_retries
        ):
            empty_retries += 1
            await self.page.wait_for_timeout(self.observation_empty_retry_ms)
            observation = await self.evaluate_observation()
        if initial_empty:
            observation["captureMeta"] = {
                "initialEmpty": True,
                "emptyRetries": empty_retries,
                "stabilizedAfterRetry": not self.is_transient_empty_observation(observation),
            }
        observed_text = self.merge_observed_text(observation.get("url", ""), observation.get("viewportText", ""))
        scroll = observation.get("scroll") or {}
        current_range = scroll.get("currentRange") or {}
        observed_ranges = self.merge_observed_ranges(observation.get("url", ""), current_range)
        scroll_height = int(scroll.get("scrollHeight") or 0)
        observed_pixels = sum(max(0, int(item.get("end", 0)) - int(item.get("start", 0))) for item in observed_ranges)
        if scroll_height > 0:
            scroll["observedRanges"] = observed_ranges
            scroll["observedCoverage"] = min(1, observed_pixels / scroll_height)
            observation["scroll"] = scroll
        observation["observedText"] = observed_text
        observation["observedTextLength"] = len(observed_text)
        observation["visibleText"] = "\n\n--- Observed text ---\n".join(
            part
            for part in [
                observation.get("viewportText", ""),
                observation.get("pageTextPreview", ""),
                observed_text,
            ]
            if part
        )[:22000]
        return observation

    async def capture_screenshot_for_vision(self) -> dict | None:
        await self.bring_page_to_front()
        try:
            image = await self.page.screenshot(type="jpeg", quality=65, full_page=False)
        except Exception:
            return None
        data_url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
        return {
            "dataUrl": data_url,
            "mimeType": "image/jpeg",
            "scope": "current_viewport",
        }

    def resource_directories(
        self,
        url: str = "",
        filename: str | None = None,
        resource_name: str | None = None,
    ) -> tuple[Path, Path, str]:
        root = Path(self.downloads_path).resolve()
        if not self.organize_downloads_by_resource:
            root.mkdir(parents=True, exist_ok=True)
            return root, root, ""
        if not self.active_resource_name:
            self.active_resource_name = safe_resource_name(resource_name) if resource_name else infer_resource_name(url, filename)
        resource_root = root / self.active_resource_name
        data_dir = resource_root / DATA_DIR_NAME
        analysis_dir = resource_root / ANALYSIS_DIR_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        analysis_dir.mkdir(parents=True, exist_ok=True)
        return data_dir, analysis_dir, self.active_resource_name

    def artifact_directories(self) -> tuple[Path, Path]:
        if self.organize_downloads_by_resource and self.active_resource_name:
            resource_root = Path(self.downloads_path).resolve() / self.active_resource_name
            return resource_root / DATA_DIR_NAME, resource_root / ANALYSIS_DIR_NAME
        root = Path(self.downloads_path).resolve()
        return root, root

    async def download_url(self, url: str, filename: str | None = None, resource_name: str | None = None) -> dict:
        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("download only supports http/https URLs")
        safe_name = filename or Path(parsed.path).name or "downloaded_resource"
        safe_name = safe_artifact_name(safe_name)
        target_dir, analysis_dir, resolved_resource_name = self.resource_directories(url, safe_name, resource_name)
        target_path = artifact_target(target_dir, safe_name)

        # BrowserContext.request shares the browser cookie jar, including HttpOnly cookies.
        response = await self.context.request.get(url, timeout=30000)
        try:
            if not response.ok:
                return {"ok": False, "errorType": "download_http_error",
                        "message": f"Download returned HTTP {response.status}", "url": url,
                        "http_status": response.status, "retry_after": response.headers.get('retry-after')}
            data = await response.body()
        finally:
            await response.dispose()
        if not data:
            return {"ok": False, "errorType": "download_empty", "message": "Empty response", "url": url}
        # A session/challenge page is not a successful binary file download.
        head = data[:1024].lstrip().lower()
        looks_html = head.startswith(b"<!doctype html") or head.startswith(b"<html")
        target_suffix = Path(safe_name).suffix.lower()
        if looks_html and target_suffix not in {".html", ".htm", ".txt", ""}:
            return {
                "ok": False,
                "message": "download_returned_html_not_file: 该链接返回的是网页而非文件，请改用 save_page 保存可见正文",
                "errorType": "download_html_mismatch",
                "bytes": len(data),
                "url": url,
            }
        target_path.write_bytes(data)
        return {
            "ok": True,
            "message": "downloaded",
            "path": str(target_path),
            "bytes": len(data),
            "url": url,
            "resourceName": resolved_resource_name,
            "dataDir": str(target_dir),
            "analysisDir": str(analysis_dir),
        }

    async def _save_playwright_download(
        self,
        download,
        source_url: str,
        resource_name: str | None = None,
    ) -> dict:
        suggested = download.suggested_filename or Path(urlparse(download.url or "").path).name or "downloaded_resource"
        safe_name = safe_artifact_name(suggested)
        target_dir, analysis_dir, resolved_resource_name = self.resource_directories(
            source_url or download.url or self.page.url,
            safe_name,
            resource_name,
        )
        target_path = artifact_target(target_dir, safe_name)
        await download.save_as(str(target_path))
        size = target_path.stat().st_size if target_path.exists() else 0
        return {
            "ok": bool(size > 0),
            "message": "downloaded" if size > 0 else "download_empty",
            "path": str(target_path) if size > 0 else "",
            "bytes": size,
            "url": download.url or source_url or self.page.url,
            "resourceName": resolved_resource_name,
            "dataDir": str(target_dir),
            "analysisDir": str(analysis_dir),
        }

    async def save_current_page(self, filename: str | None = None, resource_name: str | None = None) -> dict:
        page_data = await self.page.evaluate(SAVE_PAGE_SCRIPT)
        text = (page_data.get("text") or "").strip()
        if not text:
            return {"ok": False, "message": "page_text_empty", "errorType": "save_page_error"}
        title = page_data.get("title") or "saved_page"
        safe_name = filename or f"{title}.txt"
        safe_name = safe_artifact_name(safe_name, "saved_page.txt")
        if Path(safe_name).suffix.lower() != ".txt":
            safe_name = f"{Path(safe_name).stem}.txt"
        target_dir, analysis_dir, resolved_resource_name = self.resource_directories(
            page_data.get("url") or self.page.url,
            safe_name,
            resource_name,
        )
        target_path = artifact_target(target_dir, safe_name)
        content = "\n".join(
            [
                f"Title: {title}",
                f"Source URL: {page_data.get('url') or self.page.url}",
                "",
                text,
            ]
        )
        target_path.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "message": "saved_page",
            "path": str(target_path),
            "bytes": target_path.stat().st_size,
            "url": page_data.get("url") or self.page.url,
            "resourceName": resolved_resource_name,
            "dataDir": str(target_dir),
            "analysisDir": str(analysis_dir),
            "analysis": {
                "kind": "saved_page_text",
                "summary": f"Saved current page readable text, about {len(text)} characters.",
                "title": title,
            },
        }

    async def execute(self, action: dict) -> dict:
        await self.bring_page_to_front()
        name = action.get("action")
        if name == "download":
            return await self.download_url(action.get("url", ""), action.get("filename"), action.get("resource_name"))
        if name == "save_page":
            return await self.save_current_page(action.get("filename"), action.get("resource_name"))
        if name == "navigate":
            await self.page.goto(action["url"], wait_until="domcontentloaded", timeout=20000)
            await self.page.wait_for_timeout(700)
            await self.bring_page_to_front()
            return {"ok": True, "message": "navigating", "url": self.page.url, "pageId": self.page_id(self.page)}

        if name == "open_tab":
            page = await self.context.new_page()
            self.page = page
            self.context = page.context
            page_id = self.page_id(page)
            await self.bring_page_to_front()
            await page.goto(action["url"], wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(700)
            await self.bring_page_to_front()
            return {"ok": True, "message": "opened_tab", "url": page.url, "pageId": page_id}

        if name == "switch_tab":
            previous_page_id = self.page_id(self.page)
            page = self.resolve_page_target(action)
            if page is None:
                return {"ok": False, "message": "page_not_found", "errorType": "tab_error"}
            self.page = page
            self.context = page.context
            await self.bring_page_to_front()
            return {
                "ok": True,
                "message": "switched_tab",
                "url": page.url,
                "pageId": self.page_id(page),
                "previousPageId": previous_page_id,
            }

        if name == "back":
            await self.page.go_back(wait_until="domcontentloaded", timeout=10000)
            await self.page.wait_for_timeout(700)
            await self.bring_page_to_front()
            return {"ok": True, "message": "back"}

        # Listen before the input action. Both clicks and Enter can produce downloads/popups.
        source_page = self.page
        source_page_url = source_page.url
        downloads = []
        popups = []
        def on_download(download):
            downloads.append(download)
        def on_popup(popup):
            popups.append(popup)
        source_page.on("download", on_download)
        source_page.on("popup", on_popup)
        try:
            result = await self.dom.execute(source_page, action)
            if name in {"click", "press"}:
                await source_page.wait_for_timeout(700)
                if downloads:
                    return await self._save_playwright_download(downloads[0], source_page_url, action.get("resource_name"))
                if popups and not popups[-1].is_closed():
                    self.page = popups[-1]
                    self.context = self.page.context
                    self.dom.invalidate()
                    try:
                        await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    result.update(message="opened_new_page", url=self.page.url, adoptedNewPage=True)
                await self.bring_page_to_front()
            return result
        finally:
            source_page.remove_listener("download", on_download)
            source_page.remove_listener("popup", on_popup)

    async def save_session(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.context.storage_state(path=str(path), indexed_db=True)
        if getattr(self, "auth_state_path", None):
            target = Path(self.auth_state_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        return {"browser_state_file": str(path), "last_page_url": self.page.url}

    async def close(self) -> None:
        if not self.connected_over_cdp:
            try:
                await self.browser.close()
            except Exception:
                pass
        try:
            await self._playwright.stop()
        except Exception:
            pass
        if self._cdp_process and self._close_cdp_process and self._cdp_process.poll() is None:
            try:
                self._cdp_process.terminate()
            except Exception:
                pass


def pretty_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
