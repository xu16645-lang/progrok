import os
import sys
import time
import uuid
import random
import logging
import asyncio
from typing import Optional, Union
import argparse
from quart import Quart, request, jsonify
from camoufox.async_api import AsyncCamoufox
from patchright.async_api import async_playwright
from db_results import init_db, save_result, load_result, cleanup_old_results
from browser_configs import browser_config
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich import box



COLORS = {
    'MAGENTA': '\033[35m',
    'BLUE': '\033[34m',
    'GREEN': '\033[32m',
    'YELLOW': '\033[33m',
    'RED': '\033[31m',
    'RESET': '\033[0m',
}


class CustomLogger(logging.Logger):
    @staticmethod
    def format_message(level, color, message):
        timestamp = time.strftime('%H:%M:%S')
        return f"[{timestamp}] [{COLORS.get(color)}{level}{COLORS.get('RESET')}] -> {message}"

    def debug(self, message, *args, **kwargs):
        super().debug(self.format_message('DEBUG', 'MAGENTA', message), *args, **kwargs)

    def info(self, message, *args, **kwargs):
        super().info(self.format_message('INFO', 'BLUE', message), *args, **kwargs)

    def success(self, message, *args, **kwargs):
        super().info(self.format_message('SUCCESS', 'GREEN', message), *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        super().warning(self.format_message('WARNING', 'YELLOW', message), *args, **kwargs)

    def error(self, message, *args, **kwargs):
        super().error(self.format_message('ERROR', 'RED', message), *args, **kwargs)


logging.setLoggerClass(CustomLogger)
logger: CustomLogger = logging.getLogger("TurnstileAPIServer")  # type: ignore
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)


class TurnstileAPIServer:

    def __init__(self, headless: bool, useragent: Optional[str], debug: bool, browser_type: str, thread: int, proxy_support: bool, use_random_config: bool = False, browser_name: Optional[str] = None, browser_version: Optional[str] = None):
        self.app = Quart(__name__)
        self.debug = debug
        self.browser_type = browser_type
        self.headless = headless
        self.thread_count = max(1, int(thread or 1))
        self.proxy_support = proxy_support
        self.browser_pool = asyncio.Queue()
        self.use_random_config = use_random_config
        self.browser_name = browser_name
        self.browser_version = browser_version
        self.console = Console()

        # TURNSTILE_LAZY=0 (default) prewarms browser pool at startup.
        # TURNSTILE_LAZY=1 defers browser start until first solve.
        # TURNSTILE_IDLE_SEC (default 600) reclaims the pool after quiet period.
        # TURNSTILE_REUSE_PAGE=1 (default) reuses context/page per browser slot.
        lazy_raw = (os.getenv("TURNSTILE_LAZY", "0") or "0").strip().lower()
        self.lazy_browsers = lazy_raw not in ("0", "false", "no", "off")
        reuse_raw = (os.getenv("TURNSTILE_REUSE_PAGE", "1") or "1").strip().lower()
        self.reuse_pages = reuse_raw not in ("0", "false", "no", "off")
        try:
            self.idle_sec = float(os.getenv("TURNSTILE_IDLE_SEC", "600") or 600)
        except (TypeError, ValueError):
            self.idle_sec = 600.0
        if self.idle_sec < 0:
            self.idle_sec = 0.0
        self._pool_ready = False
        self._pool_lock: Optional[asyncio.Lock] = None
        self._owned_browsers: list = []
        self._playwright = None
        self._camoufox = None
        self._last_used = 0.0
        self._idle_task: Optional[asyncio.Task] = None
        self._in_flight = 0

        # Initialize useragent and sec_ch_ua attributes
        self.useragent = useragent
        self.sec_ch_ua = None


        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            if browser_name and browser_version:
                config = browser_config.get_browser_config(browser_name, browser_version)
                if config:
                    useragent, sec_ch_ua = config
                    self.useragent = useragent
                    self.sec_ch_ua = sec_ch_ua
            elif useragent:
                self.useragent = useragent
            else:
                browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                self.browser_name = browser
                self.browser_version = version
                self.useragent = useragent
                self.sec_ch_ua = sec_ch_ua

        self.browser_args = []
        if self.useragent:
            self.browser_args.append(f"--user-agent={self.useragent}")

        self._setup_routes()

    def display_welcome(self):
        """Displays welcome screen with logo."""
        self.console.clear()

        combined_text = Text()
        combined_text.append("\n📢 Channel: ", style="bold white")
        combined_text.append("https://t.me/D3_vin", style="cyan")
        combined_text.append("\n💬 Chat: ", style="bold white")
        combined_text.append("https://t.me/D3vin_chat", style="cyan")
        combined_text.append("\n📁 GitHub: ", style="bold white")
        combined_text.append("https://github.com/D3-vin", style="cyan")
        combined_text.append("\n📁 Version: ", style="bold white")
        combined_text.append("1.2a", style="green")
        combined_text.append("\n")

        info_panel = Panel(
            Align.left(combined_text),
            title="[bold blue]Turnstile Solver[/bold blue]",
            subtitle="[bold magenta]Dev by D3vin[/bold magenta]",
            box=box.ROUNDED,
            border_style="bright_blue",
            padding=(0, 1),
            width=50
        )

        self.console.print(info_panel)
        self.console.print()




    def _setup_routes(self) -> None:
        """Set up the application routes."""
        self.app.before_serving(self._startup)
        self.app.route('/turnstile', methods=['GET'])(self.process_turnstile)
        self.app.route('/result', methods=['GET'])(self.get_result)
        # YesCaptcha / CapSolver 兼容协议
        self.app.route('/createTask', methods=['POST'])(self.create_task)
        self.app.route('/getTaskResult', methods=['POST'])(self.get_task_result)
        # Memory/ops helpers
        self.app.route('/health', methods=['GET'])(self.health)
        self.app.route('/reclaim', methods=['POST', 'GET'])(self.reclaim)
        self.app.route('/')(self.index)


    async def _startup(self) -> None:
        """Boot HTTP + DB; optionally warm browsers (or wait for first task)."""
        self.display_welcome()
        self._pool_lock = asyncio.Lock()
        try:
            await init_db()
            # Periodic result cleanup (independent of browsers)
            asyncio.create_task(self._periodic_cleanup())

            if self.lazy_browsers:
                logger.info(
                    f"Lazy browser mode ON — pool starts on first captcha "
                    f"(thread={self.thread_count}, reuse_page={self.reuse_pages}, "
                    f"idle_reclaim={self.idle_sec:.0f}s)"
                )
                if self.idle_sec > 0:
                    self._idle_task = asyncio.create_task(self._idle_reaper())
            else:
                logger.info(
                    f"Starting browser prewarm (eager, thread={self.thread_count}, "
                    f"reuse_page={self.reuse_pages})"
                )
                await self._initialize_browser()
                self._pool_ready = True
                self._last_used = time.time()
                if self.idle_sec > 0:
                    self._idle_task = asyncio.create_task(self._idle_reaper())
        except Exception as e:
            logger.error(f"Failed to start turnstile solver: {str(e)}")
            raise

    async def _initialize_browser(self) -> None:
        """Initialize the browser and create the page pool."""
        # Drain any leftover entries before rebuilding.
        await self._drain_pool_discard()

        playwright = None
        camoufox = None

        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            playwright = await async_playwright().start()
            self._playwright = playwright
        elif self.browser_type == "camoufox":
            camoufox = AsyncCamoufox(headless=self.headless)
            self._camoufox = camoufox

        browser_configs = []
        for _ in range(self.thread_count):
            if self.browser_type in ['chromium', 'chrome', 'msedge']:
                if self.use_random_config:
                    browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                elif self.browser_name and self.browser_version:
                    config = browser_config.get_browser_config(self.browser_name, self.browser_version)
                    if config:
                        useragent, sec_ch_ua = config
                        browser = self.browser_name
                        version = self.browser_version
                    else:
                        browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                else:
                    browser = getattr(self, 'browser_name', 'custom')
                    version = getattr(self, 'browser_version', 'custom')
                    useragent = self.useragent
                    sec_ch_ua = getattr(self, 'sec_ch_ua', '')
            else:
                # Для camoufox и других браузеров используем значения по умолчанию
                browser = self.browser_type
                version = 'custom'
                useragent = self.useragent
                sec_ch_ua = getattr(self, 'sec_ch_ua', '')


            browser_configs.append({
                'browser_name': browser,
                'browser_version': version,
                'useragent': useragent,
                'sec_ch_ua': sec_ch_ua
            })

        owned = []
        for i in range(self.thread_count):
            config = browser_configs[i]

            browser_args = [
                "--window-position=0,0",
                "--force-device-scale-factor=1"
            ]
            if config['useragent']:
                browser_args.append(f"--user-agent={config['useragent']}")

            browser = None
            if self.browser_type in ['chromium', 'chrome', 'msedge'] and playwright:
                browser = await playwright.chromium.launch(
                    channel=self.browser_type,
                    headless=self.headless,
                    args=browser_args
                )
            elif self.browser_type == "camoufox" and camoufox:
                browser = await camoufox.start()

            if browser:
                owned.append((i + 1, browser, config))
                session = None
                if self.reuse_pages:
                    try:
                        session = await self._create_session(
                            browser, config, self._pick_proxy() if self.proxy_support else None, i + 1
                        )
                        if self.debug:
                            logger.info(f"Browser {i + 1}: page session prewarmed")
                    except Exception as e:
                        logger.warning(f"Browser {i + 1}: page prewarm failed: {e}")
                        session = None
                await self.browser_pool.put((i + 1, browser, config, session))

            if self.debug:
                logger.info(f"Browser {i + 1} initialized successfully with {config['browser_name']} {config['browser_version']}")

        self._owned_browsers = owned
        self._pool_ready = True
        self._last_used = time.time()
        logger.info(
            f"Browser pool initialized with {self.browser_pool.qsize()} browsers "
            f"(reuse_page={self.reuse_pages})"
        )

        if self.use_random_config:
            logger.info(f"Each browser in pool received random configuration")
        elif self.browser_name and self.browser_version:
            logger.info(f"All browsers using configuration: {self.browser_name} {self.browser_version}")
        else:
            logger.info("Using custom configuration")

        if self.debug:
            for i, config in enumerate(browser_configs):
                logger.debug(f"Browser {i+1} config: {config['browser_name']} {config['browser_version']}")
                logger.debug(f"Browser {i+1} User-Agent: {config['useragent']}")
                logger.debug(f"Browser {i+1} Sec-CH-UA: {config['sec_ch_ua']}")

    async def _drain_pool_discard(self) -> None:
        """Empty the asyncio queue; close reusable page sessions when present."""
        while True:
            try:
                item = self.browser_pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            except Exception:
                break
            try:
                _index, _browser, _config, session = self._unpack_pool_item(item)
                await self._close_session(session)
            except Exception:
                pass

    @staticmethod
    def _unpack_pool_item(item):
        """Normalize pool tuple to (index, browser, config, session)."""
        if not item:
            return None, None, None, None
        if len(item) >= 4:
            return item[0], item[1], item[2], item[3]
        if len(item) == 3:
            return item[0], item[1], item[2], None
        return None, None, None, None

    def _load_proxies(self) -> list:
        if not self.proxy_support:
            return []
        proxy_file_path = os.path.join(os.getcwd(), "proxies.txt")
        try:
            with open(proxy_file_path) as proxy_file:
                return [line.strip() for line in proxy_file if line.strip()]
        except FileNotFoundError:
            return []
        except Exception as e:
            logger.error(f"Error reading proxy file: {str(e)}")
            return []

    def _pick_proxy(self) -> Optional[str]:
        proxies = self._load_proxies()
        return random.choice(proxies) if proxies else None

    async def _session_alive(self, session: Optional[dict]) -> bool:
        if not session:
            return False
        page = session.get("page")
        context = session.get("context")
        if page is None or context is None:
            return False
        try:
            if page.is_closed():
                return False
        except Exception:
            return False
        return True

    async def _close_session(self, session: Optional[dict]) -> None:
        if not session:
            return
        page = session.get("page")
        context = session.get("context")
        session["page"] = None
        session["context"] = None
        if page is not None:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass

    async def _create_session(
        self,
        browser,
        browser_config: Optional[dict],
        proxy: Optional[str],
        index: int,
    ) -> dict:
        context_options = self._build_context_options(
            browser_config or {}, proxy if self.proxy_support else None
        )
        try:
            context = await browser.new_context(**context_options)
        except Exception as ctx_err:
            if self.debug:
                logger.warning(
                    f"Browser {index}: new_context failed ({ctx_err}); retry minimal options"
                )
            context = await browser.new_context(no_viewport=True)

        page = await context.new_page()
        try:
            await page.set_viewport_size({"width": 500, "height": 100})
        except Exception:
            pass

        await self._antishadow_inject(page)
        await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
        });

        window.chrome = {
            runtime: {},
            loadTimes: function() {},
            csi: function() {},
        };
        """)
        return {"context": context, "page": page, "proxy": proxy}

    async def _ensure_session(
        self,
        browser,
        browser_config: Optional[dict],
        session: Optional[dict],
        index: int,
    ) -> Optional[dict]:
        sticky_proxy = (session or {}).get("proxy") if session else None
        if self.reuse_pages and await self._session_alive(session):
            needs_proxy = self.proxy_support and bool(self._load_proxies())
            if needs_proxy and not sticky_proxy:
                await self._close_session(session)
            else:
                return session
        elif session:
            await self._close_session(session)

        proxy = sticky_proxy if sticky_proxy else (self._pick_proxy() if self.proxy_support else None)
        try:
            return await self._create_session(browser, browser_config, proxy, index)
        except Exception as e:
            logger.error(f"Browser {index}: failed to create page session: {e}")
            return None

    async def _close_maybe_async(self, obj, *method_names: str, label: str = "resource") -> bool:
        """Best-effort close helper for browser/driver objects."""
        if obj is None:
            return False
        for meth_name in method_names:
            meth = getattr(obj, meth_name, None)
            if meth is None:
                continue
            try:
                if meth_name == "__aexit__":
                    result = meth(None, None, None)
                else:
                    result = meth()
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(result, timeout=8.0)
                logger.debug(f"{label}: {meth_name} ok")
                return True
            except Exception as e:
                if self.debug:
                    logger.warning(f"{label}: {meth_name} failed: {e}")
        return False

    def _kill_process_tree(self, proc) -> None:
        """Force-kill a browser child process tree (Camoufox/Chromium leftovers)."""
        if proc is None:
            return
        pid = getattr(proc, "pid", None)
        if not pid:
            return
        try:
            import signal
            import os as _os

            # Prefer process group kill when available.
            try:
                pgid = _os.getpgid(int(pid))
                _os.killpg(pgid, signal.SIGTERM)
            except Exception:
                try:
                    _os.kill(int(pid), signal.SIGTERM)
                except Exception:
                    pass
            try:
                # Give it a moment, then hard kill.
                time.sleep(0.05)
                try:
                    pgid = _os.getpgid(int(pid))
                    _os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    try:
                        _os.kill(int(pid), signal.SIGKILL)
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception as e:
            if self.debug:
                logger.warning(f"kill process tree pid={pid} failed: {e}")

    async def _force_kill_browser(self, browser, index: int | None = None) -> None:
        """Hard cleanup for browsers that ignore close()/aclose()."""
        label = f"Browser {index}" if index is not None else "Browser"
        # Playwright-style browser process
        for attr in ("process", "_process"):
            proc = getattr(browser, attr, None)
            if proc is not None:
                self._kill_process_tree(proc)
                if self.debug:
                    logger.debug(f"{label}: force-killed via {attr}")
                return
        # Nested browser objects (some wrappers)
        for attr in ("browser", "_browser", "impl_obj", "_impl_obj"):
            nested = getattr(browser, attr, None)
            if nested is None or nested is browser:
                continue
            for nested_attr in ("process", "_process"):
                proc = getattr(nested, nested_attr, None)
                if proc is not None:
                    self._kill_process_tree(proc)
                    if self.debug:
                        logger.debug(f"{label}: force-killed nested {attr}.{nested_attr}")
                    return

    async def _shutdown_browsers(self) -> None:
        """Close every browser and release Playwright/Camoufox drivers."""
        items = list(self._owned_browsers or [])
        self._owned_browsers = []
        await self._drain_pool_discard()

        for index, browser, _config in items:
            closed = await self._close_maybe_async(
                browser, "close", "aclose", label=f"Browser {index}"
            )
            if not closed:
                await self._force_kill_browser(browser, index=index)
            else:
                # Even after close(), Camoufox occasionally leaves zombie children.
                # Best-effort hard cleanup when process handle is still visible.
                try:
                    await self._force_kill_browser(browser, index=index)
                except Exception:
                    pass
            if self.debug:
                logger.debug(f"Browser {index}: closed")

        if self._playwright is not None:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=8.0)
            except Exception as e:
                if self.debug:
                    logger.warning(f"Playwright stop failed: {e}")
            self._playwright = None

        if self._camoufox is not None:
            # AsyncCamoufox may expose aclose / __aexit__; best-effort.
            await self._close_maybe_async(
                self._camoufox, "aclose", "close", "__aexit__", label="Camoufox"
            )
            self._camoufox = None

        # Idle reclaim must not keep a stuck counter forever.
        # If a solve task crashed without finally, _in_flight could block all future reclaim.
        if self._in_flight != 0:
            logger.warning(
                f"Resetting leaked in-flight counter during reclaim: was {self._in_flight}"
            )
            self._in_flight = 0

        self._pool_ready = False
        # Keep last_used as historical activity; do not bump it here or reclaim loops thrash.
        logger.info("Browser pool reclaimed (idle / rebuild)")

    async def _ensure_pool(self) -> None:
        """Make sure the browser pool is warm before solving."""
        self._last_used = time.time()
        if self._pool_ready and self.browser_pool.qsize() > 0:
            return
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()
        async with self._pool_lock:
            self._last_used = time.time()
            if self._pool_ready and self.browser_pool.qsize() > 0:
                return
            # Rebuild if never ready, or all instances were dropped/disconnected.
            if self._pool_ready and self.browser_pool.empty() and self._in_flight > 0:
                # All browsers currently checked out — nothing to warm.
                return
            logger.info(
                f"Warming browser pool (thread={self.thread_count}, type={self.browser_type})"
            )
            if self._pool_ready or self._owned_browsers or self._playwright or self._camoufox:
                await self._shutdown_browsers()
            await self._initialize_browser()

    async def _idle_reaper(self) -> None:
        """Close browsers after TURNSTILE_IDLE_SEC with no captcha activity."""
        # Check more frequently than idle window so reclaim is timely.
        interval = 15.0 if self.idle_sec <= 60 else min(30.0, max(10.0, self.idle_sec / 6.0))
        stuck_since = 0.0
        while True:
            try:
                await asyncio.sleep(interval)
                if self.idle_sec <= 0:
                    continue
                # Nothing warm / owned → nothing to reclaim.
                if not self._pool_ready and not self._owned_browsers:
                    stuck_since = 0.0
                    continue

                idle_for = time.time() - (self._last_used or 0.0)
                if idle_for < self.idle_sec:
                    stuck_since = 0.0
                    continue

                # Guard against leaked in-flight counters: if we have been idle longer
                # than 2x idle window and still see in-flight > 0, force reclaim.
                if self._in_flight > 0:
                    if stuck_since <= 0:
                        stuck_since = time.time()
                    stuck_for = time.time() - stuck_since
                    if stuck_for < max(self.idle_sec * 2.0, 120.0):
                        if self.debug:
                            logger.debug(
                                f"Idle reaper waiting: in_flight={self._in_flight}, "
                                f"idle={idle_for:.0f}s, stuck={stuck_for:.0f}s"
                            )
                        continue
                    logger.warning(
                        f"Idle reaper force-reclaim: in_flight stuck at {self._in_flight} "
                        f"for {stuck_for:.0f}s (idle={idle_for:.0f}s)"
                    )
                else:
                    stuck_since = 0.0

                if self._pool_lock is None:
                    self._pool_lock = asyncio.Lock()
                async with self._pool_lock:
                    idle_for = time.time() - (self._last_used or 0.0)
                    if idle_for < self.idle_sec:
                        continue
                    if self._in_flight > 0 and idle_for < max(self.idle_sec * 2.0, 120.0):
                        continue
                    owned_n = len(self._owned_browsers or [])
                    qsize = self.browser_pool.qsize()
                    logger.info(
                        f"No captcha for {idle_for:.0f}s — reclaiming "
                        f"queue={qsize} owned={owned_n} in_flight={self._in_flight}"
                    )
                    await self._shutdown_browsers()
                    stuck_since = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Idle reaper error: {e}")

    async def _periodic_cleanup(self):
        """Periodic cleanup of old results every hour"""
        while True:
            try:
                await asyncio.sleep(3600)
                deleted_count = await cleanup_old_results(days_old=7)
                if deleted_count > 0:
                    logger.info(f"Cleaned up {deleted_count} old results")
            except Exception as e:
                logger.error(f"Error during periodic cleanup: {e}")

    async def _antishadow_inject(self, page):
        await page.add_init_script("""
          (function() {
            const originalAttachShadow = Element.prototype.attachShadow;
            Element.prototype.attachShadow = function(init) {
              const shadow = originalAttachShadow.call(this, init);
              if (init.mode === 'closed') {
                window.__lastClosedShadowRoot = shadow;
              }
              return shadow;
            };
          })();
        """)



    async def _optimized_route_handler(self, route):
        """Оптимизированный обработчик маршрутов для экономии ресурсов."""
        url = route.request.url
        resource_type = route.request.resource_type

        allowed_types = {'document', 'script', 'xhr', 'fetch'}

        allowed_domains = [
            'challenges.cloudflare.com',
            'static.cloudflareinsights.com',
            'cloudflare.com'
        ]

        if resource_type in allowed_types:
            await route.continue_()
        elif any(domain in url for domain in allowed_domains):
            await route.continue_()
        else:
            await route.abort()

    async def _block_rendering(self, page):
        """Блокировка рендеринга для экономии ресурсов"""
        await page.route("**/*", self._optimized_route_handler)

    async def _unblock_rendering(self, page):
        """Разблокировка рендеринга"""
        await page.unroute("**/*", self._optimized_route_handler)

    async def _find_turnstile_elements(self, page, index: int):
        """Умная проверка всех возможных Turnstile элементов"""
        selectors = [
            '.cf-turnstile',
            '[data-sitekey]',
            'iframe[src*="turnstile"]',
            'iframe[title*="widget"]',
            'div[id*="turnstile"]',
            'div[class*="turnstile"]'
        ]

        elements = []
        for selector in selectors:
            try:
                # Безопасная проверка count()
                try:
                    count = await page.locator(selector).count()
                except Exception:
                    # Если count() дает ошибку, пропускаем этот селектор
                    continue

                if count > 0:
                    elements.append((selector, count))
                    if self.debug:
                        logger.debug(f"Browser {index}: Found {count} elements with selector '{selector}'")
            except Exception as e:
                if self.debug:
                    logger.debug(f"Browser {index}: Selector '{selector}' failed: {str(e)}")
                continue

        return elements

    async def _find_and_click_checkbox(self, page, index: int):
        """Найти и кликнуть по чекбоксу Turnstile CAPTCHA внутри iframe"""
        try:
            # Пробуем разные селекторы iframe с защитой от ошибок
            iframe_selectors = [
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[src*="turnstile"]',
                'iframe[title*="widget"]'
            ]

            iframe_locator = None
            for selector in iframe_selectors:
                try:
                    test_locator = page.locator(selector).first
                    # Безопасная проверка count для iframe
                    try:
                        iframe_count = await test_locator.count()
                    except Exception:
                        iframe_count = 0

                    if iframe_count > 0:
                        iframe_locator = test_locator
                        if self.debug:
                            logger.debug(f"Browser {index}: Found Turnstile iframe with selector: {selector}")
                        break
                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Iframe selector '{selector}' failed: {str(e)}")
                    continue

            if iframe_locator:
                try:
                    # Получаем frame из iframe
                    iframe_element = await iframe_locator.element_handle()
                    frame = await iframe_element.content_frame()

                    if frame:
                        # Ищем чекбокс внутри iframe
                        checkbox_selectors = [
                            'input[type="checkbox"]',
                            '.cb-lb input[type="checkbox"]',
                            'label input[type="checkbox"]'
                        ]

                        for selector in checkbox_selectors:
                            try:
                                # Полностью избегаем locator.count() в iframe - используем альтернативный подход
                                try:
                                    # Пробуем кликнуть напрямую без count проверки
                                    checkbox = frame.locator(selector).first
                                    await checkbox.click(timeout=2000)
                                    if self.debug:
                                        logger.debug(f"Browser {index}: Successfully clicked checkbox in iframe with selector '{selector}'")
                                    return True
                                except Exception as click_e:
                                    # Если прямой клик не сработал, записываем в debug но не падаем
                                    if self.debug:
                                        logger.debug(f"Browser {index}: Direct checkbox click failed for '{selector}': {str(click_e)}")
                                    continue
                            except Exception as e:
                                if self.debug:
                                    logger.debug(f"Browser {index}: Iframe checkbox selector '{selector}' failed: {str(e)}")
                                continue

                        # Если нашли iframe, но не смогли кликнуть чекбокс, пробуем клик по iframe
                        try:
                            if self.debug:
                                logger.debug(f"Browser {index}: Trying to click iframe directly as fallback")
                            await iframe_locator.click(timeout=1000)
                            return True
                        except Exception as e:
                            if self.debug:
                                logger.debug(f"Browser {index}: Iframe direct click failed: {str(e)}")

                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Failed to access iframe content: {str(e)}")

        except Exception as e:
            if self.debug:
                logger.debug(f"Browser {index}: General iframe search failed: {str(e)}")

        return False

    async def _try_click_strategies(self, page, index: int):
        strategies = [
            ('checkbox_click', lambda: self._find_and_click_checkbox(page, index)),
            ('direct_widget', lambda: self._safe_click(page, '.cf-turnstile', index)),
            ('iframe_click', lambda: self._safe_click(page, 'iframe[src*="turnstile"]', index)),
            ('js_click', lambda: page.evaluate("document.querySelector('.cf-turnstile')?.click()")),
            ('sitekey_attr', lambda: self._safe_click(page, '[data-sitekey]', index)),
            ('any_turnstile', lambda: self._safe_click(page, '*[class*="turnstile"]', index)),
            ('xpath_click', lambda: self._safe_click(page, "//div[@class='cf-turnstile']", index))
        ]

        for strategy_name, strategy_func in strategies:
            try:
                result = await strategy_func()
                if result is True or result is None:  # None означает успех для большинства стратегий
                    if self.debug:
                        logger.debug(f"Browser {index}: Click strategy '{strategy_name}' succeeded")
                    return True
            except Exception as e:
                if self.debug:
                    logger.debug(f"Browser {index}: Click strategy '{strategy_name}' failed: {str(e)}")
                continue

        return False

    async def _safe_click(self, page, selector: str, index: int):
        """Полностью безопасный клик с максимальной защитой от ошибок"""
        try:
            # Пробуем кликнуть напрямую без count() проверки
            locator = page.locator(selector).first
            await locator.click(timeout=1000)
            return True
        except Exception as e:
            # Логируем ошибку только в debug режиме
            if self.debug and "Can't query n-th element" not in str(e):
                logger.debug(f"Browser {index}: Safe click failed for '{selector}': {str(e)}")
            return False

    async def _inject_captcha_directly(self, page, websiteKey: str, action: str = '', cdata: str = '', index: int = 0):
        """Inject CAPTCHA directly into the target website"""
        script = f"""
        // Remove any existing turnstile widgets first
        document.querySelectorAll('.cf-turnstile').forEach(el => el.remove());
        document.querySelectorAll('[data-sitekey]').forEach(el => el.remove());

        // Create turnstile widget directly on the page
        const captchaDiv = document.createElement('div');
        captchaDiv.className = 'cf-turnstile';
        captchaDiv.setAttribute('data-sitekey', '{websiteKey}');
        captchaDiv.setAttribute('data-callback', 'onTurnstileCallback');
        {f'captchaDiv.setAttribute("data-action", "{action}");' if action else ''}
        {f'captchaDiv.setAttribute("data-cdata", "{cdata}");' if cdata else ''}
        captchaDiv.style.position = 'fixed';
        captchaDiv.style.top = '20px';
        captchaDiv.style.left = '20px';
        captchaDiv.style.zIndex = '9999';
        captchaDiv.style.backgroundColor = 'white';
        captchaDiv.style.padding = '15px';
        captchaDiv.style.border = '2px solid #0f79af';
        captchaDiv.style.borderRadius = '8px';
        captchaDiv.style.boxShadow = '0 4px 12px rgba(0, 0, 0, 0.3)';

        // Add to body immediately
        document.body.appendChild(captchaDiv);

        // Load Turnstile script and render widget
        const loadTurnstile = () => {{
            const script = document.createElement('script');
            script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
            script.async = true;
            script.defer = true;
            script.onload = function() {{
                console.log('Turnstile script loaded');
                // Wait a bit for script to initialize
                setTimeout(() => {{
                    if (window.turnstile && window.turnstile.render) {{
                        try {{
                            window.turnstile.render(captchaDiv, {{
                                sitekey: '{websiteKey}',
                                {f'action: "{action}",' if action else ''}
                                {f'cdata: "{cdata}",' if cdata else ''}
                                callback: function(token) {{
                                    console.log('Turnstile solved with token:', token);
                                    // Create hidden input for token
                                    let tokenInput = document.querySelector('input[name="cf-turnstile-response"]');
                                    if (!tokenInput) {{
                                        tokenInput = document.createElement('input');
                                        tokenInput.type = 'hidden';
                                        tokenInput.name = 'cf-turnstile-response';
                                        document.body.appendChild(tokenInput);
                                    }}
                                    tokenInput.value = token;
                                }},
                                'error-callback': function(error) {{
                                    console.log('Turnstile error:', error);
                                }}
                            }});
                        }} catch (e) {{
                            console.log('Turnstile render error:', e);
                        }}
                    }} else {{
                        console.log('Turnstile API not available');
                    }}
                }}, 1000);
            }};
            script.onerror = function() {{
                console.log('Failed to load Turnstile script');
            }};
            document.head.appendChild(script);
        }};

        // Check if Turnstile is already loaded
        if (window.turnstile) {{
            console.log('Turnstile already loaded, rendering immediately');
            try {{
                window.turnstile.render(captchaDiv, {{
                    sitekey: '{websiteKey}',
                    {f'action: "{action}",' if action else ''}
                    {f'cdata: "{cdata}",' if cdata else ''}
                    callback: function(token) {{
                        console.log('Turnstile solved with token:', token);
                        let tokenInput = document.querySelector('input[name="cf-turnstile-response"]');
                        if (!tokenInput) {{
                            tokenInput = document.createElement('input');
                            tokenInput.type = 'hidden';
                            tokenInput.name = 'cf-turnstile-response';
                            document.body.appendChild(tokenInput);
                        }}
                        tokenInput.value = token;
                    }},
                    'error-callback': function(error) {{
                        console.log('Turnstile error:', error);
                    }}
                }});
            }} catch (e) {{
                console.log('Immediate render error:', e);
                loadTurnstile();
            }}
        }} else {{
            loadTurnstile();
        }}

        // Setup global callback
        window.onTurnstileCallback = function(token) {{
            console.log('Global turnstile callback executed:', token);
        }};
        """

        await page.evaluate(script)
        if self.debug:
            logger.debug(f"Browser {index}: Injected CAPTCHA directly into website with sitekey: {websiteKey}")

    def _build_context_options(self, browser_config: dict, proxy: Optional[str] = None) -> dict:
        """Build browser context options with Camoufox-safe defaults."""
        context_options: dict = {}

        # Camoufox + newer Playwright rejects default viewport.isMobile scheme.
        # Always disable default viewport and set size after page creation.
        context_options["no_viewport"] = True

        useragent = (browser_config or {}).get("useragent")
        if useragent:
            context_options["user_agent"] = useragent

        sec_ch_ua = (browser_config or {}).get("sec_ch_ua")
        if sec_ch_ua and str(sec_ch_ua).strip():
            context_options["extra_http_headers"] = {"sec-ch-ua": str(sec_ch_ua).strip()}

        if proxy:
            if "@" in proxy:
                scheme_part, auth_part = proxy.split("://", 1)
                auth, address = auth_part.split("@", 1)
                username, password = auth.split(":", 1)
                ip, port = address.split(":", 1)
                context_options["proxy"] = {
                    "server": f"{scheme_part}://{ip}:{port}",
                    "username": username,
                    "password": password,
                }
            else:
                parts = proxy.split(":")
                if len(parts) == 5:
                    proxy_scheme, proxy_ip, proxy_port, proxy_user, proxy_pass = parts
                    context_options["proxy"] = {
                        "server": f"{proxy_scheme}://{proxy_ip}:{proxy_port}",
                        "username": proxy_user,
                        "password": proxy_pass,
                    }
                elif len(parts) == 3:
                    context_options["proxy"] = {"server": proxy}
                else:
                    raise ValueError(f"Invalid proxy format: {proxy}")

        return context_options

    async def _solve_turnstile(self, task_id: str, url: str, sitekey: str, action: Optional[str] = None, cdata: Optional[str] = None):
        """Solve the Turnstile challenge."""
        start_time = time.time()
        index = None
        browser = None
        browser_config = None
        session = None
        acquired = False
        solved_ok = False

        # Mark in-flight before warm-up so the idle reaper cannot reclaim mid-acquire.
        # Always pair with the outer finally decrement — never leave this sticky.
        self._in_flight += 1
        try:
            try:
                await self._ensure_pool()
                self._last_used = time.time()
                raw = await self.browser_pool.get()
                index, browser, browser_config, session = self._unpack_pool_item(raw)
                acquired = True
                self._last_used = time.time()
            except Exception as e:
                logger.error(f"Failed to acquire browser from pool: {e}")
                await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0, "error": str(e)})
                return

            try:
                if hasattr(browser, 'is_connected') and not browser.is_connected():
                    if self.debug:
                        logger.warning(f"Browser {index}: Browser disconnected, skipping")
                    await self._close_session(session)
                    await self.browser_pool.put((index, browser, browser_config, None))
                    acquired = False
                    await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0, "error": "browser_disconnected"})
                    return
            except Exception as e:
                if self.debug:
                    logger.warning(f"Browser {index}: Cannot check browser state: {str(e)}")

            try:
                session = await self._ensure_session(browser, browser_config, session, index)
                if not session or not session.get("page"):
                    await save_result(task_id, "turnstile", {
                        "value": "CAPTCHA_FAIL",
                        "elapsed_time": round(time.time() - start_time, 3),
                        "error": "session_create_failed",
                    })
                    return

                page = session["page"]
                proxy = session.get("proxy")

                if self.debug:
                    logger.debug(
                        f"Browser {index}: Starting Turnstile solve for URL: {url} "
                        f"with Sitekey: {sitekey} | Action: {action} | Cdata: {cdata} | "
                        f"Proxy: {proxy} | reuse={self.reuse_pages}"
                    )

                await self._block_rendering(page)
                try:
                    await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                finally:
                    try:
                        await self._unblock_rendering(page)
                    except Exception:
                        pass

                if self.debug:
                    logger.debug(f"Browser {index}: Injecting Turnstile widget directly into target site")

                await self._inject_captcha_directly(page, sitekey, action or '', cdata or '', index)

                # Poll immediately — no fixed 3s dead wait. CF often resolves in <2s.
                locator = page.locator('input[name="cf-turnstile-response"]')
                max_attempts = 40
                click_count = 0
                max_clicks = 10

                for attempt in range(max_attempts):
                    try:
                        try:
                            count = await locator.count()
                        except Exception as e:
                            if self.debug:
                                logger.debug(f"Browser {index}: Locator count failed on attempt {attempt + 1}: {str(e)}")
                            count = 0

                        if count == 0:
                            if self.debug and attempt % 5 == 0:
                                logger.debug(f"Browser {index}: No token elements found on attempt {attempt + 1}")
                        elif count == 1:
                            try:
                                token = await locator.input_value(timeout=300)
                                if token:
                                    elapsed_time = round(time.time() - start_time, 3)
                                    logger.success(f"Browser {index}: Successfully solved captcha - {COLORS.get('MAGENTA')}{token[:10]}{COLORS.get('RESET')} in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds")
                                    await save_result(task_id, "turnstile", {"value": token, "elapsed_time": elapsed_time})
                                    solved_ok = True
                                    return
                            except Exception as e:
                                if self.debug:
                                    logger.debug(f"Browser {index}: Single token element check failed: {str(e)}")
                        else:
                            if self.debug:
                                logger.debug(f"Browser {index}: Found {count} token elements, checking all")
                            for i in range(count):
                                try:
                                    element_token = await locator.nth(i).input_value(timeout=300)
                                    if element_token:
                                        elapsed_time = round(time.time() - start_time, 3)
                                        logger.success(f"Browser {index}: Successfully solved captcha - {COLORS.get('MAGENTA')}{element_token[:10]}{COLORS.get('RESET')} in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds")
                                        await save_result(task_id, "turnstile", {"value": element_token, "elapsed_time": elapsed_time})
                                        solved_ok = True
                                        return
                                except Exception as e:
                                    if self.debug:
                                        logger.debug(f"Browser {index}: Token element {i} check failed: {str(e)}")
                                    continue

                        if attempt > 1 and attempt % 3 == 0 and click_count < max_clicks:
                            click_success = await self._try_click_strategies(page, index)
                            click_count += 1
                            if click_success and self.debug:
                                logger.debug(f"Browser {index}: Click successful (click #{click_count}/{max_clicks})")
                            elif not click_success and self.debug:
                                logger.debug(f"Browser {index}: All click strategies failed on attempt {attempt + 1} (click #{click_count}/{max_clicks})")

                        wait_time = min(0.2 + (attempt * 0.03), 1.0)
                        await asyncio.sleep(wait_time)

                        if self.debug and attempt % 5 == 0:
                            logger.debug(f"Browser {index}: Attempt {attempt + 1}/{max_attempts} - Waiting for token (clicks: {click_count}/{max_clicks})")

                    except Exception as e:
                        if self.debug:
                            logger.debug(f"Browser {index}: Attempt {attempt + 1} error: {str(e)}")
                        continue

                elapsed_time = round(time.time() - start_time, 3)
                await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time, "error": "timeout"})
                if self.debug:
                    logger.error(f"Browser {index}: Error solving Turnstile in {COLORS.get('RED')}{elapsed_time}{COLORS.get('RESET')} Seconds")
            except Exception as e:
                elapsed_time = round(time.time() - start_time, 3)
                await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time, "error": str(e)})
                logger.error(f"Browser {index}: Error solving Turnstile: {str(e)}")
            finally:
                # Keep healthy pages when reuse is on; drop failed/dirty sessions.
                if not self.reuse_pages or not solved_ok:
                    if self.debug:
                        logger.debug(
                            f"Browser {index}: Dropping page session "
                            f"(reuse={self.reuse_pages}, solved={solved_ok})"
                        )
                    await self._close_session(session)
                    session = None
                elif self.debug:
                    logger.debug(f"Browser {index}: Reusing page session for next solve")

                try:
                    if acquired and browser is not None and index is not None:
                        connected = True
                        try:
                            if hasattr(browser, 'is_connected'):
                                connected = bool(browser.is_connected())
                        except Exception:
                            connected = True
                        if connected:
                            await self.browser_pool.put((index, browser, browser_config, session))
                            if self.debug:
                                logger.debug(f"Browser {index}: Browser returned to pool")
                        else:
                            await self._close_session(session)
                            if self.debug:
                                logger.warning(f"Browser {index}: Browser disconnected, not returning to pool")
                except Exception as e:
                    if self.debug:
                        logger.warning(f"Browser {index}: Error returning browser to pool: {str(e)}")
        finally:
            # Always release in-flight even on early return / unexpected exception.
            if self._in_flight > 0:
                self._in_flight -= 1
            self._last_used = time.time()






    def _check_client_key(self, client_key: Optional[str]) -> Optional[dict]:
        """校验 clientKey。未设置 API_KEY 时跳过鉴权。"""
        expected = os.getenv("API_KEY", "").strip()
        if not expected:
            return None
        if not client_key or client_key.strip() != expected:
            return {
                "errorId": 1,
                "errorCode": "ERROR_KEY_DOES_NOT_EXIST",
                "errorDescription": "Invalid clientKey"
            }
        return None

    async def _enqueue_turnstile(
        self,
        url: str,
        sitekey: str,
        action: Optional[str] = None,
        cdata: Optional[str] = None,
    ):
        """创建任务并异步求解，返回 (task_id, error_response)。"""
        if not url or not sitekey:
            return None, {
                "errorId": 1,
                "errorCode": "ERROR_WRONG_PAGEURL",
                "errorDescription": "Both 'url' and 'sitekey' are required"
            }

        task_id = str(uuid.uuid4())
        await save_result(task_id, "turnstile", {
            "status": "CAPTCHA_NOT_READY",
            "createTime": int(time.time()),
            "url": url,
            "sitekey": sitekey,
            "action": action,
            "cdata": cdata
        })

        try:
            asyncio.create_task(
                self._solve_turnstile(
                    task_id=task_id,
                    url=url,
                    sitekey=sitekey,
                    action=action,
                    cdata=cdata,
                )
            )
            if self.debug:
                logger.debug(f"Request completed with taskid {task_id}.")
            return task_id, None
        except Exception as e:
            logger.error(f"Unexpected error processing request: {str(e)}")
            return None, {
                "errorId": 1,
                "errorCode": "ERROR_UNKNOWN",
                "errorDescription": str(e)
            }

    def _format_task_result(self, task_id: str, result) -> dict:
        """统一格式化任务结果（兼容 YesCaptcha）。"""
        if not result:
            return {
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Task not found"
            }

        if result == "CAPTCHA_NOT_READY" or (
            isinstance(result, dict) and result.get("status") == "CAPTCHA_NOT_READY"
        ):
            return {
                "errorId": 0,
                "status": "processing",
                "taskId": task_id,
            }

        if isinstance(result, dict) and result.get("value") == "CAPTCHA_FAIL":
            return {
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Workers could not solve the Captcha"
            }

        if isinstance(result, dict) and result.get("value") and result.get("value") != "CAPTCHA_FAIL":
            return {
                "errorId": 0,
                "status": "ready",
                "taskId": task_id,
                "solution": {
                    "token": result["value"]
                }
            }

        return {
            "errorId": 1,
            "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
            "errorDescription": "Workers could not solve the Captcha"
        }

    async def process_turnstile(self):
        """Handle the /turnstile endpoint requests."""
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')
        action = request.args.get('action')
        cdata = request.args.get('cdata')

        task_id, err = await self._enqueue_turnstile(url, sitekey, action, cdata)
        if err:
            return jsonify(err), 200
        return jsonify({"errorId": 0, "taskId": task_id}), 200

    async def get_result(self):
        """Return solved data"""
        task_id = request.args.get('id')

        if not task_id:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_WRONG_CAPTCHA_ID",
                "errorDescription": "Invalid task ID/Request parameter"
            }), 200

        result = await load_result(task_id)
        return jsonify(self._format_task_result(task_id, result)), 200

    async def create_task(self):
        """YesCaptcha 兼容：POST /createTask"""
        try:
            body = await request.get_json(force=True, silent=True) or {}
        except Exception:
            body = {}

        auth_err = self._check_client_key(body.get("clientKey"))
        if auth_err:
            return jsonify(auth_err), 200

        task = body.get("task") or {}
        task_type = (task.get("type") or "").strip()
        # Local Camoufox solver only has one Turnstile path. Accept YesCaptcha /
        # CapSolver premium aliases (M1/M2) as Proxyless so registration clients
        # that default premium=True do not fail createTask with
        # ERROR_TASK_NOT_SUPPORTED before falling back.
        supported = {
            "TurnstileTaskProxyless",
            "TurnstileTaskProxylessM1",
            "TurnstileTaskProxylessM2",
            "TurnstileTask",
            "AntiTurnstileTaskProxyLess",
            "AntiTurnstileTaskProxyless",
            "AntiTurnstileTask",
        }
        if task_type and task_type not in supported:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_TASK_NOT_SUPPORTED",
                "errorDescription": f"Unsupported task type: {task_type}"
            }), 200

        url = task.get("websiteURL") or task.get("websiteUrl") or task.get("url")
        sitekey = task.get("websiteKey") or task.get("sitekey") or task.get("siteKey")
        action = task.get("action") or task.get("pageAction")
        cdata = task.get("cdata") or task.get("data")

        # CapSolver 风格 metadata
        metadata = task.get("metadata") or {}
        if isinstance(metadata, dict):
            action = action or metadata.get("action")
            cdata = cdata or metadata.get("cdata")

        task_id, err = await self._enqueue_turnstile(url, sitekey, action, cdata)
        if err:
            return jsonify(err), 200
        return jsonify({"errorId": 0, "taskId": task_id}), 200

    async def get_task_result(self):
        """YesCaptcha 兼容：POST /getTaskResult"""
        try:
            body = await request.get_json(force=True, silent=True) or {}
        except Exception:
            body = {}

        auth_err = self._check_client_key(body.get("clientKey"))
        if auth_err:
            return jsonify(auth_err), 200

        task_id = body.get("taskId") or body.get("id")
        if not task_id:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_WRONG_CAPTCHA_ID",
                "errorDescription": "Invalid task ID/Request parameter"
            }), 200

        result = await load_result(task_id)
        return jsonify(self._format_task_result(task_id, result)), 200



    async def health(self):
        """Lightweight pool status for ops / memory debugging."""
        idle_for = None
        if self._last_used:
            idle_for = round(time.time() - self._last_used, 1)
        return jsonify({
            "ok": True,
            "lazy": bool(self.lazy_browsers),
            "reuse_page": bool(self.reuse_pages),
            "idle_sec": self.idle_sec,
            "pool_ready": bool(self._pool_ready),
            "thread": self.thread_count,
            "browser_type": self.browser_type,
            "queue": self.browser_pool.qsize(),
            "owned": len(self._owned_browsers or []),
            "in_flight": int(self._in_flight or 0),
            "idle_for_sec": idle_for,
        }), 200

    async def reclaim(self):
        """Force reclaim browser pool (manual memory drop)."""
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()
        async with self._pool_lock:
            owned = len(self._owned_browsers or [])
            qsize = self.browser_pool.qsize()
            in_flight = int(self._in_flight or 0)
            await self._shutdown_browsers()
        return jsonify({
            "ok": True,
            "reclaimed": True,
            "owned_before": owned,
            "queue_before": qsize,
            "in_flight_before": in_flight,
            "pool_ready": bool(self._pool_ready),
            "owned": len(self._owned_browsers or []),
            "queue": self.browser_pool.qsize(),
            "in_flight": int(self._in_flight or 0),
        }), 200

    @staticmethod
    async def index():
        """Serve the API documentation page."""
        return """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Turnstile Solver API</title>
                <script src="https://cdn.tailwindcss.com"></script>
            </head>
            <body class="bg-gray-900 text-gray-200 min-h-screen flex items-center justify-center">
                <div class="bg-gray-800 p-8 rounded-lg shadow-md max-w-2xl w-full border border-red-500">
                    <h1 class="text-3xl font-bold mb-6 text-center text-red-500">Welcome to Turnstile Solver API</h1>

                    <p class="mb-4 text-gray-300">支持两套协议：原生 GET，以及 YesCaptcha/CapSolver 兼容 POST。</p>

                    <h2 class="text-xl font-semibold mb-3 text-red-400">1) 原生协议</h2>
                    <ul class="list-disc pl-6 mb-4 text-gray-300">
                        <li><code>GET /turnstile?url=...&amp;sitekey=...</code> → 返回 taskId</li>
                        <li><code>GET /result?id=TASK_ID</code> → 轮询结果</li>
                    </ul>
                    <div class="bg-gray-700 p-4 rounded-lg mb-6 border border-red-500">
                        <p class="font-semibold mb-2 text-red-400">Example:</p>
                        <code class="text-sm break-all text-red-300">/turnstile?url=https://example.com&sitekey=0x4AAAA...</code>
                    </div>

                    <h2 class="text-xl font-semibold mb-3 text-red-400">2) YesCaptcha 兼容协议</h2>
                    <ul class="list-disc pl-6 mb-4 text-gray-300">
                        <li><code>POST /createTask</code></li>
                        <li><code>POST /getTaskResult</code></li>
                    </ul>
                    <div class="bg-gray-700 p-4 rounded-lg mb-6 border border-red-500">
                        <p class="font-semibold mb-2 text-red-400">createTask body:</p>
                        <pre class="text-sm text-red-300 whitespace-pre-wrap">{
  "clientKey": "optional-if-API_KEY-set",
  "task": {
    "type": "TurnstileTaskProxyless",
    "websiteURL": "https://example.com",
    "websiteKey": "0x4AAAA..."
  }
}</pre>
                    </div>


                    <div class="bg-gray-700 p-4 rounded-lg mb-6">
                        <p class="text-gray-200 font-semibold mb-3">📢 Connect with Us</p>
                        <div class="space-y-2 text-sm">
                            <p class="text-gray-300">
                                📢 <strong>Channel:</strong>
                                <a href="https://t.me/D3_vin" class="text-red-300 hover:underline">https://t.me/D3_vin</a>
                                - Latest updates and releases
                            </p>
                            <p class="text-gray-300">
                                💬 <strong>Chat:</strong>
                                <a href="https://t.me/D3vin_chat" class="text-red-300 hover:underline">https://t.me/D3vin_chat</a>
                                - Community support and discussions
                            </p>
                            <p class="text-gray-300">
                                📁 <strong>GitHub:</strong>
                                <a href="https://github.com/D3-vin" class="text-red-300 hover:underline">https://github.com/D3-vin</a>
                                - Source code and development
                            </p>
                        </div>
                    </div>
                </div>
            </body>
            </html>
        """


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Turnstile API Server")

    parser.add_argument('--no-headless', action='store_true', help='Run the browser with GUI (disable headless mode). By default, headless mode is enabled.')
    parser.add_argument('--useragent', type=str, help='User-Agent string (if not specified, random configuration is used)')
    parser.add_argument('--debug', action='store_true', help='Enable or disable debug mode for additional logging and troubleshooting information (default: False)')
    parser.add_argument('--browser_type', type=str, default='chromium', help='Specify the browser type for the solver. Supported options: chromium, chrome, msedge, camoufox (default: chromium)')
    parser.add_argument('--thread', type=int, default=4, help='Set the number of browser threads to use for multi-threaded mode. Increasing this will speed up execution but requires more resources (default: 1)')
    parser.add_argument('--proxy', action='store_true', help='Enable proxy support for the solver (Default: False)')
    parser.add_argument('--random', action='store_true', help='Use random User-Agent and Sec-CH-UA configuration from pool')
    parser.add_argument('--browser', type=str, help='Specify browser name to use (e.g., chrome, firefox)')
    parser.add_argument('--version', type=str, help='Specify browser version to use (e.g., 139, 141)')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Specify the IP address where the API solver runs. (Default: 127.0.0.1)')
    parser.add_argument('--port', type=str, default='5072', help='Set the port for the API solver to listen on. (Default: 5072)')
    return parser.parse_args()


def create_app(headless: bool, useragent: str, debug: bool, browser_type: str, thread: int, proxy_support: bool, use_random_config: bool, browser_name: str, browser_version: str) -> Quart:
    server = TurnstileAPIServer(headless=headless, useragent=useragent, debug=debug, browser_type=browser_type, thread=thread, proxy_support=proxy_support, use_random_config=use_random_config, browser_name=browser_name, browser_version=browser_version)
    return server.app


if __name__ == '__main__':
    args = parse_args()
    browser_types = [
        'chromium',
        'chrome',
        'msedge',
        'camoufox',
    ]
    if args.browser_type not in browser_types:
        logger.error(f"Unknown browser type: {COLORS.get('RED')}{args.browser_type}{COLORS.get('RESET')} Available browser types: {browser_types}")
    else:
        app = create_app(
            headless=not args.no_headless,
            debug=args.debug,
            useragent=args.useragent,
            browser_type=args.browser_type,
            thread=args.thread,
            proxy_support=args.proxy,
            use_random_config=args.random,
            browser_name=args.browser,
            browser_version=args.version
        )
        app.run(host=args.host, port=int(args.port))
