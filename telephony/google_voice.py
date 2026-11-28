"""
Telephony Integration — Google Voice browser automation.

Speed design:
 - _setup_browser_only()  → spawns Chrome ONLY (called under UC lock by pool)
 - _login_to_account()    → navigates + waits for login (runs in parallel)
 - WebDriverWait replaces manual sleep/poll loops throughout
"""
import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from loguru import logger


import undetected_chromedriver as uc
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
SELENIUM_AVAILABLE = True

try:
    import psutil
except ImportError:  # optional — memory telemetry falls back to the JS heap
    psutil = None

import config
from telephony.uc_driver import ensure_persistent_uc_driver, resolve_uc_version_main


# uc.Chrome() patches its own binary and corrupts it when two workers spawn at
# the same moment, so every spawn — first boot or restart — goes through this.
uc_spawn_lock = asyncio.Lock()

# Chrome running out of memory never reaches Python as a MemoryError: the
# renderer dies and Selenium reports a crashed tab or a deleted session, so the
# only way to recognise it is by matching the driver's message text.
_OOM_MARKERS = (
    "out of memory",
    "cannot allocate memory",
    "bad_alloc",
    "enomem",
    "memory allocation failed",
)
_DEAD_SESSION_MARKERS = (
    "session deleted because of page crash",
    "session deleted",
    "tab crashed",
    "target crashed",
    "page crash",
    "cannot determine loading status",
    "chrome not reachable",
    "invalid session id",
    "no such session",
    "not connected to devtools",
    "unable to connect to renderer",
    "unable to send message to renderer",
    "renderer process",
    "browser has closed",
    "failed to check if window was closed",
    "web view not found",
)


def classify_browser_error(exc: BaseException) -> Optional[str]:
    """Return 'oom' | 'dead' when this exception means the browser is gone."""
    msg = str(exc or "").lower()
    if not msg:
        return None
    if any(m in msg for m in _OOM_MARKERS):
        return "oom"
    if any(m in msg for m in _DEAD_SESSION_MARKERS):
        return "dead"
    return None


def host_free_mb() -> Optional[float]:
    """Available host RAM in MB, or None when psutil is unavailable."""
    if psutil is None:
        return None
    try:
        return psutil.virtual_memory().available / 1048576.0
    except Exception:
        return None


class PhoneClient:
    """
    Telephony automation client using Selenium + undetected_chromedriver.

    Lifecycle (called by PhonePool):
        1. _setup_browser_only()  — spawns Chrome (serialized across workers)
        2. _login_to_account()    — navigates + authenticates (parallel)
        3. make_call() / hangup() — normal call operations
        4. close()                — quit browser
    """

    LOGIN_UI_SELECTORS = [
        "gv-make-call-panel",
        "gv-call-sidebar",
        "gv-call-as-banner",
        ".did-display",
        "[gv-a11y-grid]",
        "input[placeholder*='name or number' i]",
        "input[placeholder*='phone' i]",
        "input[type='tel']",
        "button[aria-label='Make a call']",
        "button[aria-label='Dialpad']",
    ]

    AUTH_URL_MARKERS = (
        "accounts.google.com",
        "/signin",
        "service/login",
        "challenge",
    )

    # Chrome interstitial / host offline. URL often stays on voice.google.com
    # so these must be checked BEFORE treating a missing dialer as logout.
    OFFLINE_PAGE_MARKERS = (
        "dns_probe",
        "err_internet_disconnected",
        "err_name_not_resolved",
        "err_network_changed",
        "err_connection_reset",
        "err_connection_timed_out",
        "err_connection_refused",
        "err_address_unreachable",
        "err_timed_out",
        "err_proxy_connection_failed",
        "this site can’t be reached",
        "this site can't be reached",
        "dns address could not be found",
        "checking the network cables",
        "no internet",
        "chrome://network-error",
        "chrome://chrome-error",
    )

    BAN_INDICATORS = (
        "support.google.com/accounts/answer/40039",
        "support.google.com/voice/answer/16053145",
        "support.google.com/accounts/contact/suspended",
        "unable to access a google product",
        "unable to access google voice",
        "your access to this google product has been suspended",
        "your access to google voice has been suspended",
    )

    # Healthy Chrome prefs are typically < 2MB. A multi-GB Preferences file
    # with an invalid JSON escape is what made every spawn fail identically.
    PREFS_MAX_BYTES = 20 * 1048576

    def __init__(self, profile_dir=None, capture_device="Line 1", playback_device="Line 2"):
        self.driver: Optional[object] = None

        self.is_authenticated = False
        self.is_banned = False
        self.is_spam_flagged = False
        self.phone_number: Optional[str] = None
        self.last_error: Optional[str] = None
        self.init_started_at: Optional[float] = None
        self.init_finished_at: Optional[float] = None
        self.browser_start_seconds: float = 0.0
        self.login_seconds: float = 0.0
        self.number_fetch_seconds: float = 0.0
        self.current_dir = Path(__file__).parent.parent.absolute()
        self.profile_dir =  profile_dir
        self.capture_device = capture_device
        self.playback_device = playback_device

        # Selector caches
        self._cached_dial_selector: Optional[str] = None
        self._cached_input_selector: Optional[str] = None
        self._active_calls = {}
        self._p1_handler = None
        self._p1_handler_profile: Optional[str] = None
        self._p1_handler_warm: bool = False

        # Set by PhonePool
        self.worker_id: int = -1

        # Cached logged-in Google account email (for spam-probe reporting)
        self.account_email: Optional[str] = None
        # Last spam-probe debug artifact paths ({"screenshot","html"})
        self._last_spam_debug: Optional[dict] = None

        # ── Memory guard state ──
        # The Google Voice SPA leaks steadily (history rows, listeners), so the
        # tab is reloaded every few calls and Chrome is respawned periodically.
        self.calls_served: int = 0
        self.restart_count: int = 0
        # Consecutive *failed* restarts — reset by a successful one. A healthy
        # worker restarts many times over a long run, so only a run of
        # failures may take it out of the pool for good.
        self.failed_restarts: int = 0
        self.oom_events: int = 0
        self.needs_restart: bool = False
        self.restart_reason: Optional[str] = None
        self.last_rss_mb: Optional[float] = None
        self.last_heap_pct: Optional[int] = None
        self._calls_since_recycle: int = 0
        # A crashed session is moved here so restart_browser() can still reap
        # the orphaned chromedriver/chrome processes it left behind.
        self._dead_driver: Optional[object] = None
        # Set while a revive/restart is running so the watchdog never queues a
        # second respawn on the same profile (that was spawning extra Chromes).
        self._revive_running: bool = False
        # Set while a sign-in is driving this browser so a concurrent respawn
        # (init retry, watchdog) does not kill the tab out from under it.
        self._login_running: bool = False
        # VAC devices applied in the GV Audio settings dialog this session.
        self._gv_audio_configured: bool = False
        # Bumped to invalidate a Chrome constructor that outlived its wait_for.
        self._spawn_generation: int = 0

        # ── Auto-login state ──
        # Credentials are supplied at runtime (operator → controller → agent)
        # and stay in memory unless GV_SAVE_CREDENTIALS is on.
        self.credentials: Optional[dict] = None
        # ready | needs_login | expired | challenge | banned | unknown
        self.login_state: str = "unknown"
        self.login_outcome: Optional[str] = None
        self.login_detail: Optional[str] = None
        self.autologin_attempts: int = 0

    def _build_chrome_options(self, headless_mode: str):
        """Build Chrome options for one startup attempt."""
        options = uc.ChromeOptions()

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        options.add_argument(f"--user-data-dir={self.profile_dir}")

        if headless_mode == "new":
            options.add_argument("--headless=new")
        elif headless_mode == "legacy":
            options.add_argument("--headless")

        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-software-rasterizer")
        options.add_argument("--window-size=1024,720")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-ipc-flooding-protection")
        options.add_argument("--use-fake-ui-for-media-stream")
        options.add_argument(
            "--disable-features=Translate,MediaRouter,OptimizationHints,"
            "AudioServiceOutOfProcess,CalculateNativeWinOcclusion,"
            "IntensiveWakeUpThrottling,BackForwardCache,"
            "WebAuthentication,WebAuthenticationPasskeysPlatformAuthenticator"
        )
        # 8 headless Chromes on 4–6 vCPU: cut process/thread/RAM footprint hard.
        if getattr(config, "GV_LOW_END_MODE", True):
            options.add_argument("--enable-low-end-device-mode")
            options.add_argument("--renderer-process-limit=1")
            options.add_argument("--num-raster-threads=1")
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-component-extensions-with-background-pages")
            options.add_argument("--disable-default-apps")
            options.add_argument("--disable-sync")
            options.add_argument("--disable-hang-monitor")
            options.add_argument("--disable-breakpad")
            options.add_argument("--disable-domain-reliability")
            options.add_argument("--disable-client-side-phishing-detection")
            options.add_argument("--disable-component-update")
            options.add_argument("--metrics-recording-only")

        # Memory guard. The V8 cap has to be generous enough for the Google
        # Voice Angular app — the old hard-coded 192 MB is what crashed the
        # renderer with "out of memory" mid-run — while still bounding a leak.
        # --expose-gc lets the pool force a collection between calls.
        heap_mb = max(256, int(getattr(config, "GV_JS_HEAP_MB", 512)))
        options.add_argument(
            f"--js-flags=--max-old-space-size={heap_mb} --expose-gc"
        )
        options.add_argument("--aggressive-cache-discard")
        options.add_argument("--disk-cache-size=33554432")
        options.add_argument("--media-cache-size=33554432")
        options.add_argument("--disable-crash-reporter")
        options.add_argument("--log-level=3")
        # GV_FAKE_AUDIO=1 → give Chrome a synthetic mic/speaker so WebRTC
        # calls connect WITHOUT Virtual Audio Cable (needed on servers that
        # have no VAC; perfect for the audio-less spam-probe ring test).
        if os.getenv("GV_FAKE_AUDIO", "0").strip().lower() not in (
            "0", "false", "no", "off", ""
        ):
            options.add_argument("--use-fake-device-for-media-stream")
            options.add_argument("--autoplay-policy=no-user-gesture-required")
            logger.warning(
                f"Worker {self.worker_id}: GV_FAKE_AUDIO on — using Chrome "
                f"synthetic audio device (no VAC needed)"
            )
        options.page_load_strategy = "eager"

        prefs = {
            "profile.default_content_setting_values.media_stream_mic": 1,
            "profile.default_content_setting_values.media_stream_camera": 1,
            "profile.default_content_setting_values.notifications": 2,
            "profile.managed_default_content_settings.images": 2,
        }
        options.add_experimental_option("prefs", prefs)
        return options

    def _cleanup_failed_launch_artifacts(self):
        """Best-effort removal of stale singleton/lock files after a failed
        Chrome bootstrap.

        Never touches self.driver: on a failed spawn the assignment never
        happened, so whatever driver is set belongs to a DIFFERENT, possibly
        freshly signed-in browser (an orphaned init thread used to quit it
        here, killing a completed login).
        """
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"):
            p = self.profile_dir / name
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────────────
    # Step 1: Browser spawn only (serialized by pool via _uc_spawn_lock)
    # ──────────────────────────────────────────────────────────────────────────

    def _setup_browser_only(self) -> bool:
        """
        Запуск Chrome с использованием 100% существующего профиля.
        """
        self.init_started_at = time.time()
        self._gv_audio_configured = False
        gen = getattr(self, "_spawn_generation", 0)

        # 1. Проверка пути профиля (уже должен быть сформирован в __init__ как .chrome_profile_{id+1})
        if not self.profile_dir.exists():
            self.last_error = f"КРИТИЧЕСКАЯ ОШИБКА: Профиль не найден в {self.profile_dir}. Запусти setup_profile.py!"
            logger.error(self.last_error)
            return False

        # 2. Reap any Chrome still sitting on this profile. Deleting
        # SingletonLock *before* those processes die is what produces
        # "chrome not reachable" on the next spawn.
        if self.driver is None:
            self._kill_profile_processes()
            self._wait_profile_free(10.0)
            # Repair BEFORE spawn: a 2GB Preferences file makes Chrome fail
            # every launch, and unlink fails while a zombie still holds it.
            if self._prefs_unhealthy():
                logger.warning(
                    f"Worker {self.worker_id}: oversized/corrupt profile "
                    f"prefs — repairing before spawn"
                )
                self._repair_corrupted_profile_prefs()
                self._cleanup_failed_launch_artifacts()

        # 3. Путь к локальному драйверу (uc_driver) — берётся из конфига
        local_driver = config.resolve_chromedriver_path()
        if not local_driver.exists():
            self.last_error = f"Драйвер не найден в {local_driver}!"
            logger.error(self.last_error)
            return False

        logger.info(
            f"Worker {self.worker_id}: Spawning Chrome (Port {9500 + self.worker_id}) with Profile {self.worker_id + 1}")

        # 4. Сборка опций (уже включает прокси и путь к профилю)
        headless_mode = "new" if config.GV_HEADLESS else ""
        if not config.GV_HEADLESS:
            logger.warning(
                f"Worker {self.worker_id}: GV_HEADLESS=0 — launching VISIBLE Chrome"
            )
        options = self._build_chrome_options(headless_mode)  # включает --user-data-dir

        # 5. Настройка сервиса с уникальным портом для каждого воркера
        worker_port = 9500 + int(self.worker_id)

        def _spawn():
            chrome_service = Service(
                executable_path=str(local_driver),
                port=worker_port,
                service_args=["--host=127.0.0.1"]
            )
            # 6. Запуск через локальный бинарник с привязкой к версии
            # version_main=146 соответствует твоему скачанному драйверу
            return uc.Chrome(
                options=options,
                service=chrome_service,
                driver_executable_path=str(local_driver),
                use_subprocess=True,
                version_main=config.resolve_chrome_version_main(),
                keep_alive=True
            )

        try:
            try:
                new_driver = _spawn()
            except Exception as first:
                # Corrupted prefs, leftover Chrome holding the profile lock,
                # or "chrome not reachable" after a zombie — kill, repair,
                # and retry once. Matching only the JSON-escape used to miss
                # launches that failed because the file was still locked.
                if not self._is_retryable_spawn_failure(first):
                    raise
                logger.warning(
                    f"Worker {self.worker_id}: launch failed "
                    f"({first}) — killing leftovers, repairing prefs, "
                    f"retrying once"
                )
                self._kill_profile_processes()
                self._wait_profile_free(8.0)
                if self._is_profile_corruption(first) or self._prefs_unhealthy():
                    self._repair_corrupted_profile_prefs()
                self._cleanup_failed_launch_artifacts()
                new_driver = _spawn()

            if gen != getattr(self, "_spawn_generation", 0):
                logger.warning(
                    f"Worker {self.worker_id}: spawn superseded — discarding "
                    f"the late Chrome (timeout/abort already moved on)"
                )
                try:
                    new_driver.quit()
                except Exception:
                    pass
                self._kill_profile_processes()
                return False

            if self.driver is not None:
                # A concurrent spawn (an orphaned init thread racing a revive)
                # already gave this worker a browser — keep that one and drop
                # the duplicate instead of leaking a second Chrome on the
                # same profile.
                logger.warning(
                    f"Worker {self.worker_id}: duplicate spawn discarded — "
                    f"a browser is already running on this profile"
                )
                try:
                    new_driver.quit()
                except Exception:
                    pass
                return True

            self.driver = new_driver
            if gen != getattr(self, "_spawn_generation", 0):
                logger.warning(
                    f"Worker {self.worker_id}: spawn superseded after attach — "
                    f"dropping the late driver"
                )
                self.driver = None
                try:
                    new_driver.quit()
                except Exception:
                    pass
                self._kill_profile_processes()
                return False
            # Hard caps so driver.get / execute_script cannot hang the whole agent.
            try:
                self.driver.set_page_load_timeout(
                    max(5, int(getattr(config, "GV_PAGE_LOAD_TIMEOUT_SEC", 25)))
                )
                self.driver.set_script_timeout(
                    max(5, int(getattr(config, "GV_SCRIPT_TIMEOUT_SEC", 15)))
                )
            except Exception:
                pass
            # Hide Chrome's native passkey / WebAuthn prompt so "Not now" in
            # the Google page is the dialog we actually click, not a OS sheet
            # Selenium cannot reach.
            try:
                self.driver.execute_cdp_cmd(
                    "WebAuthn.enable", {"enableUI": False}
                )
            except Exception:
                pass

            self.browser_start_seconds = time.time() - self.init_started_at
            logger.info(
                f"Worker {self.worker_id}: Chrome ready using profile {self.profile_dir.name} in {self.browser_start_seconds:.2f}s")
            return True

        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Chrome failed to start - {e}")
            self.last_error = str(e)
            # Если не запустился — пробуем еще раз почистить хвосты
            self._cleanup_failed_launch_artifacts()
            return False

    @staticmethod
    def _is_profile_corruption(exc: Exception) -> bool:
        """True when Chrome failed to parse its own JSON prefs.

        Must be a specific JSON-escape signature — matching the word
        'Preferences' alone deleted healthy 0-byte prefs after unrelated
        launch errors.
        """
        msg = str(exc)
        low = msg.lower()
        return (
            "Invalid \\uXXXX escape" in msg
            or ("invalid \\u" in low and "escape" in low)
            or ("invalid" in low and "escape" in low and "column" in low)
        )

    def _is_retryable_spawn_failure(self, exc: Exception) -> bool:
        """True when one kill+repair+retry has a real chance of booting Chrome."""
        if self._is_profile_corruption(exc) or self._prefs_unhealthy():
            return True
        msg = str(exc).lower()
        return (
            "chrome not reachable" in msg
            or "devtoolsactiveport" in msg
            or "disconnected: not connected to chrome" in msg
        )

    def _prefs_paths(self):
        return (
            self.profile_dir / "Default" / "Preferences",
            self.profile_dir / "Default" / "Secure Preferences",
            self.profile_dir / "Local State",
        )

    def _prefs_unhealthy(self) -> bool:
        """True when a prefs file is pathologically large (corrupt runaway)."""
        limit = int(getattr(self, "PREFS_MAX_BYTES", 20 * 1048576))
        for p in self._prefs_paths():
            try:
                if p.exists() and p.stat().st_size > limit:
                    return True
            except Exception:
                continue
        return False

    def _repair_corrupted_profile_prefs(self, only_oversized: bool = False) -> bool:
        """Delete Chrome's unparseable JSON prefs. Returns True if any file
        was removed. Cookies are stored separately, so the account session
        survives; only browser settings are reset."""
        removed = False
        limit = int(getattr(self, "PREFS_MAX_BYTES", 20 * 1048576))
        for p in self._prefs_paths():
            try:
                if not p.exists():
                    continue
                size_mb = p.stat().st_size / 1048576.0
                if only_oversized and p.stat().st_size <= limit:
                    continue
                p.unlink()
                removed = True
                logger.warning(
                    f"Worker {self.worker_id}: removed corrupted prefs "
                    f"{p.name} ({size_mb:.1f}MB)"
                )
            except Exception as e:
                logger.debug(
                    f"Worker {self.worker_id}: could not remove {p.name}: {e}"
                )
        return removed

    # ──────────────────────────────────────────────────────────────────────────
    # Legacy combined entry (used by direct callers, not the pool)
    # ──────────────────────────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        """
        Full init in one call (kept for backwards compatibility).
        PhonePool calls _setup_browser_only() + _login_to_account() separately.
        """
        if not SELENIUM_AVAILABLE:
            return False
        self.init_started_at = time.time()
        try:
            loop = asyncio.get_running_loop()
            ok = await asyncio.wait_for(
                loop.run_in_executor(None, self._setup_browser),
                timeout=max(10, config.GV_STARTUP_TIMEOUT_SEC),
            )
            self.init_finished_at = time.time()
            return ok
        except asyncio.TimeoutError:
            self.last_error = f"startup timeout after {config.GV_STARTUP_TIMEOUT_SEC}s"
            logger.error(f"Worker {self.worker_id}: startup timeout")
            return False
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"Worker {self.worker_id}: init error — {e}")
            return False

    def _setup_browser(self) -> bool:
        """Old combined path: spawn + login."""
        if not self._setup_browser_only():
            return False
        return self._login_to_account()

    # ──────────────────────────────────────────────────────────────────────────
    # Step 2: Login + number-fetch (runs in parallel across workers)
    # ──────────────────────────────────────────────────────────────────────────

    def _login_to_account(self) -> bool:
        """Navigate to GV and confirm authentication, then best-effort fetch phone number.

        Fast: the page state is read the moment it is conclusive instead of the
        old 90s blind poll. A signed-in session is confirmed as soon as the
        dialer paints; a profile that is not signed in is known in seconds.
        """
        from telephony import gv_login

        login_start = time.time()
        self.is_authenticated = False
        self.phone_number = None
        nav_attempts = 2

        try:
            for nav_attempt in range(1, nav_attempts + 1):
                logger.info(
                    f"Worker {self.worker_id}: navigating to dialer "
                    f"(attempt {nav_attempt}/{nav_attempts})…"
                )
                self.driver.get("https://voice.google.com/u/0/calls")

                page = self._classify_page_fast()

                if page == gv_login.DIALER:
                    self.is_authenticated = True
                    self.login_state = "ready"
                    self.login_seconds = time.time() - login_start
                    logger.info(
                        f"Worker {self.worker_id}: logged in "
                        f"({self.login_seconds:.2f}s)"
                    )
                    if not self._fetch_phone_number():
                        logger.warning(
                            f"Worker {self.worker_id}: logged in, but GV number is not visible"
                        )
                    self._configure_gv_audio()
                    return True

                if page == gv_login.BANNED:
                    self.is_banned = True
                    self.last_error = "account banned by Google"
                    logger.warning(
                        f"Worker {self.worker_id}: BAN DETECTED on login page"
                    )
                    return False

                if page == gv_login.CHALLENGE:
                    signer = self._signer()
                    if signer._is_captcha():
                        self.login_state = "captcha"
                        self.login_outcome = gv_login.CAPTCHA
                        self.last_error = "CAPTCHA — robot check"
                        logger.warning(
                            f"Worker {self.worker_id}: CAPTCHA — this account "
                            f"requires a robot check @ {self._url_short()}"
                        )
                        return False
                    self.login_state = "challenge"
                    self.last_error = "auth challenge (2FA/captcha)"
                    logger.warning(
                        f"Worker {self.worker_id}: login needs a human "
                        f"(2FA/captcha) @ {self._url_short()}"
                    )
                    return False

                if page in (
                    gv_login.LANDING,
                    gv_login.IDENTIFIER,
                    gv_login.PASSWORD,
                    gv_login.CHOOSER,
                ):
                    # Not signed in — known in seconds, no 90s wait. If the
                    # operator supplied credentials, auto_login() takes over.
                    self.login_state = "needs_login"
                    self.last_error = "not signed in (session expired or never logged in)"
                    logger.warning(
                        f"Worker {self.worker_id}: not signed in (page={page}) — "
                        f"will auto-login if credentials exist"
                    )
                    return False

                # UNKNOWN after the settle budget: the dialer never painted on
                # a voice.google.com page. Give the SPA exactly one reload
                # before calling it expired.
                if nav_attempt < nav_attempts:
                    logger.info(
                        f"Worker {self.worker_id}: dialer did not paint, "
                        f"reloading (attempt {nav_attempt}/{nav_attempts})…"
                    )
                    continue

            # Page stayed ambiguous after reload — the session is unusable.
            self.login_state = "expired"
            current_url = (self.driver.current_url or "").lower()
            self.last_error = f"login: dialer never rendered @ {current_url[:120]}"
            logger.warning(
                f"Worker {self.worker_id}: Google Voice dialer never rendered"
            )
            return False

        except Exception as e:
            self.last_error = f"login error: {e}"
            logger.error(f"Worker {self.worker_id}: login error — {e}")
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Login / number helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _url_short(self, limit: int = 120) -> str:
        try:
            return (self.driver.current_url or "")[:limit]
        except Exception:
            return ""

    @staticmethod
    def _normalize_phone_number(raw_value: str) -> Optional[str]:
        digits = "".join(filter(str.isdigit, raw_value or ""))
        if len(digits) == 10:
            digits = "1" + digits
        if len(digits) != 11 or not digits.startswith("1"):
            return None
        return "+" + digits

    @classmethod
    def _looks_like_auth_page(cls, url: str) -> bool:
        u = (url or "").lower()
        return any(marker in u for marker in cls.AUTH_URL_MARKERS)

    @classmethod
    def _looks_like_offline_blob(cls, *parts: str) -> bool:
        blob = "\n".join((p or "").lower() for p in parts)
        if not blob.strip():
            return False
        if blob.startswith("chrome://") or "\nchrome://" in blob or "chrome://new-tab" in blob:
            return True
        if "about:blank" in blob or "chrome://newtab" in blob:
            return True
        return any(marker in blob for marker in cls.OFFLINE_PAGE_MARKERS)

    def _page_looks_offline(self) -> bool:
        """True when Chrome is on an interstitial / NTP, not a Google logout."""
        if not self.driver:
            return False
        url = title = text = ""
        try:
            url = (self.driver.current_url or "").lower()
        except Exception:
            pass
        if url.startswith("chrome://") or url.startswith("about:"):
            return True
        try:
            title = (self.driver.title or "").lower()
        except Exception:
            pass
        try:
            text = self.driver.execute_script(
                "return document.body ? document.body.innerText.toLowerCase() : ''"
            ) or ""
            text = text[:1200]
        except Exception:
            pass
        return self._looks_like_offline_blob(url, title, text)

    def _has_any_selector(self, selectors: list[str]) -> bool:
        for sel in selectors:
            try:
                if self.driver.find_elements(By.CSS_SELECTOR, sel):
                    return True
            except Exception:
                continue
        return False

    def _fetch_phone_number(self) -> bool:
        """
        Fetch the GV phone number from the UI.

        Uses WebDriverWait to avoid busy-looping with time.sleep().
        Falls back to JS scraping if the standard wait finds the element
        but it has no text yet.
        """
        logger.info(f"Worker {self.worker_id}: fetching phone number…")
        fetch_start = time.time()
        retries = max(2, config.GV_NUMBER_FETCH_RETRIES)

        # Number selectors tried in order. Some accounts expose the assigned
        # number in the call-as banner instead of the older did-display block.
        NUMBER_SELECTORS = [
            ".phone-number-details span:nth-child(2)",
            ".did-display .phone-number-details span:nth-child(2)",
            ".did-display span:nth-child(2)",
            ".did-display",
            "gv-call-as-banner",
            "gv-call-as-banner *",
            "[aria-label*='google voice number' i]",
            "[aria-label*='voice number' i]",
            "[aria-label*='call as' i]",
            "[data-tooltip*='+1']",
        ]

        for attempt in range(retries):
            try:
                # Single JS pass across all selectors — fastest approach.
                number = self.driver.execute_script(
                    """
                    const selectors = arguments[0];
                    const phoneRegex = /(?:\\+?1[\\s().-]*)?(?:\\(\\d{3}\\)|\\d{3})[\\s().-]*\\d{3}[\\s().-]*\\d{4}/;

                    const normalizeCandidate = (value) => {
                        const text = (value || "").trim();
                        if (!text) return null;
                        const match = text.match(phoneRegex);
                        return match ? match[0] : null;
                    };

                    const readElement = (el) => {
                        if (!el) return null;
                        for (const value of [
                            el.innerText,
                            el.textContent,
                            el.getAttribute && el.getAttribute("aria-label"),
                            el.getAttribute && el.getAttribute("data-tooltip"),
                            el.getAttribute && el.getAttribute("title"),
                        ]) {
                            const candidate = normalizeCandidate(value);
                            if (candidate) return candidate;
                        }
                        return null;
                    };

                    for (const selector of selectors) {
                        for (const el of document.querySelectorAll(selector)) {
                            const candidate = readElement(el);
                            if (candidate) return candidate;
                        }
                    }

                    const fallbackRoots = document.querySelectorAll(
                        ".did-display, .phone-number-details, gv-call-as-banner, gv-make-call-panel"
                    );
                    for (const el of fallbackRoots) {
                        const candidate = readElement(el);
                        if (candidate) return candidate;
                    }

                    return null;
                    """,
                    NUMBER_SELECTORS,
                )
                normalized = self._normalize_phone_number(number or "")
                if normalized:
                    self.phone_number = normalized
                    self.number_fetch_seconds = time.time() - fetch_start
                    logger.info(
                        f"Worker {self.worker_id}: number={self.phone_number} "
                        f"({self.number_fetch_seconds:.2f}s)"
                    )
                    return True

                logger.debug(
                    f"Worker {self.worker_id}: number not visible yet "
                    f"({attempt + 1}/{retries})"
                )
                # Short, capped sleep — WebDriverWait isn't useful here because
                # the element may be present but empty (Angular lazy-fill).
                time.sleep(0.4)

            except Exception as e:
                logger.debug(f"Worker {self.worker_id}: number fetch error ({attempt + 1}): {e}")
                time.sleep(0.5)

        self.phone_number = None
        self.last_error = "phone number unavailable"
        logger.warning(
            f"Worker {self.worker_id}: could not fetch phone number after {retries} attempts"
        )
        return False

    _VAC_LINE_RE = re.compile(r"Line\s+(\d+)", re.I)
    _VAC_PREFIX_RE = re.compile(
        r"^(по умолчанию|default|оборудование|equipment)\b", re.I
    )

    @classmethod
    def _vac_line_label(cls, device_name: str) -> str:
        """Turn 'Line 2' / 'Line 2 (Virtual Audio Cable)' into the token 'Line 2'."""
        m = cls._VAC_LINE_RE.search(device_name or "")
        return f"Line {m.group(1)}" if m else (device_name or "").strip()

    @classmethod
    def _vac_option_score(cls, option_text: str, line: str) -> int:
        """Rank a mat-option against the target Line N. 0 = no match.

        Prefer the bare 'Line N (Virtual Audio Cable)' entry over the
        'По умолчанию - …' / 'Оборудование - …' aliases. Line 1 must not
        match Line 12.
        """
        text = " ".join((option_text or "").split())
        if not text or not line:
            return 0
        token = re.compile(
            r"(?:^|[\s\-])" + re.escape(line) + r"(?:[\s\(]|$)",
            re.I,
        )
        if not token.search(text):
            return 0
        if cls._VAC_PREFIX_RE.match(text):
            return 1
        if text.lower().startswith(line.lower()):
            return 3
        return 2

    @classmethod
    def _pick_vac_option(cls, options: list, line: str):
        best = None
        best_score = 0
        for opt in options or []:
            score = cls._vac_option_score(opt.get("text") or "", line)
            if score > best_score:
                best_score = score
                best = opt
        return best

    def _configure_gv_audio(self) -> bool:
        """Set GV mic / ringing / speakers to this worker's VAC pair.

        Profile 1 → mic Line 2, ring Line 1, speakers Line 1
        Profile 2 → mic Line 4, ring Line 3, speakers Line 3
        (gv_microphone = playback_device, gv_ringing/speakers = capture_device)

        Best-effort: a miss is logged and never fails the login.
        """
        if not self.driver:
            return False
        if self._gv_audio_configured:
            return True

        mic = self._vac_line_label(self.playback_device)
        ring = self._vac_line_label(self.capture_device)
        speaker = self._vac_line_label(self.capture_device)
        started = time.time()
        try:
            if not self._open_gv_audio_dialog():
                logger.warning(
                    f"Worker {self.worker_id}: audio settings button not found — "
                    f"VAC not applied (mic={mic} ring={ring} speakers={speaker})"
                )
                return False

            ready_deadline = time.time() + 1.0
            while time.time() < ready_deadline:
                if self.driver.execute_script(
                    "return !!document.querySelector("
                    "'mat-select[aria-labelledby*=\"mic_label\"]');"
                ):
                    break
                time.sleep(0.04)

            ok_mic = self._set_gv_audio_select("mic_label", mic)
            ok_ring = self._set_gv_audio_select("ring_label", ring)
            ok_spk = self._set_gv_audio_select("speaker_label", speaker)
            self._close_gv_audio_dialog()

            elapsed = time.time() - started
            if ok_mic and ok_ring and ok_spk:
                self._gv_audio_configured = True
                logger.info(
                    f"Worker {self.worker_id}: GV audio set "
                    f"mic={mic} ring={ring} speakers={speaker} ({elapsed:.2f}s)"
                )
                return True

            logger.warning(
                f"Worker {self.worker_id}: GV audio incomplete "
                f"mic={'ok' if ok_mic else mic} "
                f"ring={'ok' if ok_ring else ring} "
                f"speakers={'ok' if ok_spk else speaker} ({elapsed:.2f}s)"
            )
            return False
        except Exception as e:
            logger.warning(
                f"Worker {self.worker_id}: GV audio setup failed — {e}"
            )
            try:
                self._close_gv_audio_dialog()
            except Exception:
                pass
            return False

    def _open_gv_audio_dialog(self) -> bool:
        """Click Audio settings once and wait for gv-audio-controls."""
        try:
            if self.driver.execute_script(
                "return !!document.querySelector('gv-audio-controls');"
            ):
                return True
        except Exception:
            return False

        deadline = time.time() + 2.5
        clicked = False
        while time.time() < deadline:
            try:
                if self.driver.execute_script(
                    "return !!document.querySelector('gv-audio-controls');"
                ):
                    return True
                if not clicked:
                    state = self.driver.execute_script(
                        """
                        const btn = document.querySelector(
                            '[gv-test-id="audio-ogb-button"]'
                        ) || document.querySelector(
                            'button[aria-label="Audio settings"]'
                        );
                        if (!btn) return 'missing';
                        btn.click();
                        return 'clicked';
                        """
                    )
                    if state == "clicked":
                        clicked = True
            except Exception:
                return False
            time.sleep(0.05)
        return False

    def _gv_select_value(self, label_id: str) -> str:
        try:
            return self.driver.execute_script(
                """
                const sel = document.querySelector(
                    'mat-select[aria-labelledby*="' + arguments[0] + '"]'
                );
                if (!sel) return '';
                return (sel.innerText || sel.textContent || '')
                    .replace(/\\s+/g, ' ').trim();
                """,
                label_id,
            ) or ""
        except Exception:
            return ""

    def _set_gv_audio_select(self, label_id: str, line: str) -> bool:
        """Open one mat-select and pick the matching Virtual Audio Cable line."""
        current = self._gv_select_value(label_id)
        if self._vac_option_score(current, line) > 0:
            return True

        opened = self.driver.execute_script(
            """
            const sel = document.querySelector(
                'mat-select[aria-labelledby*="' + arguments[0] + '"]'
            );
            if (!sel) return false;
            const trigger = sel.querySelector('.mat-mdc-select-trigger') || sel;
            trigger.click();
            return true;
            """,
            label_id,
        )
        if not opened:
            return False

        option = None
        deadline = time.time() + 1.2
        while time.time() < deadline:
            options = self.driver.execute_script(
                """
                const panel = document.querySelector(
                    '.mat-mdc-select-panel.mdc-menu-surface--open, '
                    + '[role="listbox"].mdc-menu-surface--open'
                );
                const root = panel || document;
                return Array.from(root.querySelectorAll('mat-option[role="option"]'))
                    .map(o => ({
                        id: o.id,
                        text: (o.innerText || o.textContent || '')
                            .replace(/\\s+/g, ' ').trim()
                    }))
                    .filter(o => o.text);
                """
            ) or []
            option = self._pick_vac_option(options, line)
            if option:
                break
            time.sleep(0.04)
        if not option:
            self._press_escape(close_dialog=False)
            return False

        clicked = False
        try:
            clicked = bool(self.driver.execute_script(
                """
                const el = document.getElementById(arguments[0]);
                if (!el) return false;
                el.scrollIntoView({block: 'nearest'});
                for (const type of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                    el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true}));
                }
                el.click();
                return true;
                """,
                option["id"],
            ))
        except Exception:
            clicked = False
        if not clicked:
            try:
                el = self.driver.find_element(By.ID, option["id"])
                el.click()
            except Exception:
                self._press_escape(close_dialog=False)
                return False

        settle = time.time() + 0.6
        while time.time() < settle:
            if self._vac_option_score(self._gv_select_value(label_id), line) > 0:
                return True
            time.sleep(0.04)

        # JS click sometimes does not commit a Material option — try a real one.
        try:
            el = self.driver.find_element(By.ID, option["id"])
            el.click()
        except Exception:
            pass
        settle = time.time() + 0.5
        while time.time() < settle:
            if self._vac_option_score(self._gv_select_value(label_id), line) > 0:
                return True
            time.sleep(0.04)
        self._press_escape(close_dialog=False)
        return self._vac_option_score(self._gv_select_value(label_id), line) > 0

    def _press_escape(self, close_dialog: bool = False) -> None:
        try:
            self.driver.execute_script(
                """
                document.dispatchEvent(new KeyboardEvent('keydown', {
                    key: 'Escape', code: 'Escape', keyCode: 27, which: 27,
                    bubbles: true, cancelable: true
                }));
                if (arguments[0]) {
                    const bd = document.querySelector('.cdk-overlay-backdrop');
                    if (bd) bd.click();
                }
                """,
                close_dialog,
            )
        except Exception:
            pass

    def _close_gv_audio_dialog(self) -> None:
        """Dismiss the select overlay, then the audio dialog."""
        self._press_escape(close_dialog=False)
        time.sleep(0.05)
        self._press_escape(close_dialog=True)

    def _dismiss_blocking_overlays(self) -> None:
        """Close CDK backdrops / Google account callout that steal clicks."""
        if not self.driver:
            return
        try:
            self.driver.execute_script(
                """
                document.querySelectorAll('.cdk-overlay-backdrop').forEach(el => {
                    try { el.click(); } catch (e) {}
                });
                document.dispatchEvent(new KeyboardEvent('keydown', {
                    key: 'Escape', code: 'Escape', keyCode: 27, which: 27,
                    bubbles: true, cancelable: true
                }));
                document.querySelectorAll(
                    'iframe[name="callout"], iframe[src*="ogs.google.com"],' +
                    ' iframe[src*="accounts.google.com"]'
                ).forEach(f => { try { f.remove(); } catch (e) {} });
                // Google account flyout (z-index ~991) that intercepts the dial input.
                document.querySelectorAll('div[style*="z-index"]').forEach(el => {
                    const z = parseInt(el.style.zIndex || '0', 10);
                    if (z >= 900 && z < 10000) {
                        try { el.remove(); } catch (e) {}
                    }
                });
                """
            )
        except Exception:
            pass

    def session_lost_reason(self) -> Optional[str]:
        """Diagnostic: what the current page looks like.

        Only ``banned`` is a Google burn. Logout / landing / missing dialer
        are session or host problems — the pool must not wipe cookies for them.
        """
        if not self.driver:
            return None
        try:
            if not self._is_driver_alive():
                return None
            url = (self.driver.current_url or "").lower()
            # Host DNS/offline interstitial keeps the Voice URL. Wiping cookies
            # here is what deleted live accounts on BtcBeer-3.
            if self._page_looks_offline():
                return None
            if self._looks_like_auth_page(url):
                return "logged out (auth page)"
            if self._check_if_logged_in():
                return None
            from telephony import gv_login
            page = self._classify_page_fast(budget=1.2)
            if page == gv_login.BANNED:
                return "banned"
            if page in (gv_login.CHALLENGE, gv_login.CAPTCHA):
                return "auth challenge"
            if page in (
                gv_login.LANDING, gv_login.IDENTIFIER,
                gv_login.PASSWORD, gv_login.CHOOSER,
            ):
                return f"logged out (page={page})"
            # Unknown / NTP / mid-navigation is not a burned Google session.
            if page == gv_login.UNKNOWN:
                return None
            # Dialer CSS leftover after a logout still means the session is dead
            # if LOGIN_UI_SELECTORS already missed it above.
            if "voice.google.com" in url:
                return "dialer gone (session dead)"
            return f"not ready (page={page})"
        except Exception:
            return None

    def _check_if_logged_in(self) -> bool:
        """Return True if the logged-in GV dialer UI is present."""
        if not self.driver:
            return False
        try:
            url = (self.driver.current_url or "").lower()
            if "voice.google.com" not in url or self._looks_like_auth_page(url):
                return False

            for sel in self.LOGIN_UI_SELECTORS:
                try:
                    if self.driver.find_elements(By.CSS_SELECTOR, sel):
                        logger.debug(f"Worker {self.worker_id}: logged-in indicator: {sel}")
                        return True
                except Exception:
                    pass
            return False

        except Exception as e:
            logger.debug(f"Worker {self.worker_id}: login check error — {e}")
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Auto-login: session classification + credential sign-in
    # ──────────────────────────────────────────────────────────────────────────

    def set_credentials(
        self,
        email: str,
        password: str,
        recovery_email: Optional[str] = None,
        totp_seed: Optional[str] = None,
        backup_codes: Optional[list] = None,
        used_backup_codes: Optional[list] = None,
    ) -> None:
        """Attach the Google account this profile should sign in to.

        `recovery_email` answers Google's 'confirm your recovery email' prompt
        when it appears; without it that challenge is left for a human.
        `totp_seed` (base32) lets the authenticator 2FA step be answered
        locally; `backup_codes` covers the backup-code step, with
        `used_backup_codes` tracking the ones Google already accepted.
        """
        self.credentials = {
            "email": email,
            "password": password,
            "recovery_email": recovery_email,
            "totp_seed": totp_seed,
            "backup_codes": list(backup_codes or []),
            "used_backup_codes": list(used_backup_codes or []),
        }

    def has_credentials(self) -> bool:
        creds = self.credentials or {}
        return bool(creds.get("email") and creds.get("password"))

    def clear_credentials(self) -> None:
        """Drop in-memory Google credentials so a new /login can take the slot."""
        self.credentials = None

    @property
    def credential_email(self) -> Optional[str]:
        return (self.credentials or {}).get("email")

    def _signer(self):
        from telephony import gv_login

        return gv_login.GoogleSignIn(
            self.driver,
            dialer_selectors=self.LOGIN_UI_SELECTORS,
            worker_id=self.worker_id,
            log=self._note_signin,
        )

    def _note_signin(self, msg: str) -> None:
        """Keep the live /login progress line in sync with the form."""
        self.login_detail = msg
        logger.info(f"Worker {self.worker_id}: sign-in {msg}")

    def clear_session_cookies(self) -> bool:
        """Drop this profile's cookies while leaving the browser running.

        Used for an expired session: the account page is gone but Chrome is
        healthy, so there is no reason to pay for a respawn.
        """
        if not self.driver:
            return False
        ok = self._clear_cookies_in_browser()
        logger.warning(
            f"Worker {self.worker_id}: session cookies dropped "
            f"(expired Google session, browser kept)"
        )
        return ok

    def _clear_chooser_session(self) -> bool:
        """Drop cookies because the saved-account picker is on screen.

        Credentials stay. We never click a chooser tile — after this the
        sign-in form starts from a blank identifier page.
        """
        from telephony import gv_login

        logger.warning(
            f"Worker {self.worker_id}: account picker on screen — "
            f"clearing cookies instead of picking an account"
        )
        self.login_detail = "account picker — clearing cookies"
        self.clear_session_cookies()
        try:
            self.driver.get(gv_login.LANDING_URL)
        except Exception as e:
            self._note_browser_error(e, "chooser cookie wipe")
            return False
        time.sleep(1.2)
        return True

    def _classify_page_fast(self, budget: Optional[float] = None) -> str:
        """Return the gv_login page constant as soon as it is conclusive.

        A definitive verdict — dialer up, a sign-in form, an auth/challenge
        page, a ban — is returned the instant it is seen, with no fixed wait.
        The only thing worth re-probing is a voice.google.com page that has not
        painted its Angular dialer yet (UNKNOWN on that domain). An UNKNOWN
        anywhere else — an unrelated tab, a dead session — is conclusive on the
        spot, so the probe budget is never burned there. This is what makes
        detection near-instant instead of the old 90s blind poll.
        """
        from telephony import gv_login

        if budget is None:
            budget = float(getattr(config, "GV_SESSION_PROBE_SEC", 12.0))
        settle = max(0.1, float(getattr(config, "GV_SESSION_SETTLE_SEC", 0.35)))

        signer = self._signer()
        deadline = time.time() + max(0.0, budget)
        while True:
            page = signer.state()
            if page != gv_login.UNKNOWN:
                return page
            # UNKNOWN is only worth waiting out while the voice.google.com SPA
            # is still painting; off that domain it is already the answer.
            if "voice.google.com" not in signer._url():
                return page
            if time.time() >= deadline:
                return page
            time.sleep(settle)

    def detect_session_state(
        self, navigate: bool = True, budget: Optional[float] = None
    ) -> str:
        """Classify the profile's Google session. Blocking — call in executor.

        Returns 'ready' (dialer is up), 'needs_login' (landing page or a
        sign-in form), 'expired' (some other page — the session died),
        'challenge' (2FA/captcha, a human is required), 'banned', or
        'no_browser'. Fast: it returns the moment the page is conclusive.
        """
        from telephony import gv_login

        if not self.driver:
            self.login_state = "no_browser"
            return self.login_state

        started = time.time()
        if navigate:
            try:
                self.driver.get(gv_login.DIALER_URL)
            except Exception as e:
                self._note_browser_error(e, "session probe")
                if not self._is_driver_alive():
                    self.login_state = "no_browser"
                    return self.login_state

        try:
            page = self._classify_page_fast(budget)
        except Exception as e:
            self._note_browser_error(e, "session probe")
            self.login_state = "no_browser" if not self._is_driver_alive() else "expired"
            return self.login_state

        self.login_state = {
            gv_login.DIALER: "ready",
            gv_login.LANDING: "needs_login",
            gv_login.IDENTIFIER: "needs_login",
            gv_login.PASSWORD: "needs_login",
            gv_login.CHOOSER: "needs_login",
            gv_login.CHALLENGE: "challenge",
            gv_login.BANNED: "banned",
        }.get(page, "expired")
        if self.login_state == "banned":
            self.is_banned = True
        if (
            page in (gv_login.IDENTIFIER, gv_login.CHALLENGE, gv_login.PASSWORD)
            and self._signer()._is_captcha()
        ):
            self.login_state = "captcha"

        logger.info(
            f"Worker {self.worker_id}: session state={self.login_state} "
            f"(page={page}, {time.time() - started:.1f}s)"
        )
        return self.login_state

    def auto_login(self, timeout: Optional[float] = None) -> bool:
        """Sign this profile in with its stored credentials. Blocking.

        Covers both broken shapes a profile shows up in: never signed in (the
        landing page, where the Sign in link opens the form in a second tab)
        and expired (any other page — the cookies are dropped first, then the
        same form runs).
        """
        from telephony import gv_login

        self.login_outcome = None
        self.login_detail = None

        if not self.driver:
            self.login_outcome = "no_browser"
            return False
        if not self.has_credentials():
            self.login_state = "needs_login"
            self.login_outcome = "no_credentials"
            self.login_detail = "no email/password supplied for this profile"
            logger.warning(
                f"Worker {self.worker_id}: cannot sign in — no credentials "
                f"for profile {self.profile_dir.name}"
            )
            return False

        email = self.credentials["email"]
        password = self.credentials["password"]
        recovery_email = self.credentials.get("recovery_email")
        totp_seed = self.credentials.get("totp_seed")
        used_codes = list(self.credentials.get("used_backup_codes") or [])
        # A code Google already accepted is never sent again. Seller-style
        # 4-char groups are usually one code split in two — submit pairs;
        # Google's own 8-digit codes go verbatim.
        raw_codes = [
            c for c in (self.credentials.get("backup_codes") or [])
            if not any(c in u for u in used_codes)
        ]
        if raw_codes and all(len(c) == 4 for c in raw_codes):
            backup_codes = [
                a + b for a, b in zip(raw_codes[::2], raw_codes[1::2])
            ] or raw_codes
        else:
            backup_codes = raw_codes

        def _backup_code_used(code: str) -> None:
            used_codes.append(code)
            if self.credentials:
                self.credentials["used_backup_codes"] = list(used_codes)
            hook = getattr(self, "on_backup_code_used", None)
            if callable(hook):
                try:
                    hook(code)
                except Exception:
                    pass

        budget = float(
            timeout if timeout is not None
            else getattr(config, "GV_LOGIN_TIMEOUT_SEC", 120)
        )
        self.autologin_attempts += 1
        self._login_running = True
        started = time.time()
        self.login_detail = "checking session"
        extras = []
        if recovery_email:
            extras.append("recovery email")
        if totp_seed:
            extras.append("TOTP 2FA")
        if backup_codes:
            extras.append(f"{len(backup_codes)} backup code(s)")
        logger.info(
            f"Worker {self.worker_id}: auto-login attempt "
            f"#{self.autologin_attempts} as {email}"
            + (f" (with {', '.join(extras)})" if extras else "")
        )

        try:
            try:
                state = self.detect_session_state(navigate=True)
            except Exception as e:
                # A closed target window here means the browser is gone, not that
                # the login failed — report it so the pool restarts the browser
                # instead of looping on a dead handle.
                self._note_browser_error(e, "auto-login probe")
                self.login_state = "no_browser"
                self.login_outcome = "no_browser"
                self.login_detail = f"browser session lost before sign-in: {e}"
                logger.warning(
                    f"Worker {self.worker_id}: browser gone before sign-in — {e}"
                )
                return False
            if state == "ready":
                self.is_authenticated = True
                self.login_outcome = gv_login.OK
                self.login_detail = "already signed in"
                self.account_email = self.account_email or email
                if not self.phone_number:
                    self._fetch_phone_number()
                self._configure_gv_audio()
                return True
            if state == "banned":
                self.is_banned = True
                self.login_outcome = gv_login.DISABLED
                self.login_detail = "Google disabled this account"
                return False
            if state == "no_browser":
                self.login_outcome = "no_browser"
                return False
            if state == "captcha":
                self.login_outcome = gv_login.CAPTCHA
                self.login_detail = "reCAPTCHA / robot check — sign in by hand once"
                logger.warning(
                    f"Worker {self.worker_id}: CAPTCHA — account {email} "
                    f"requires a robot check"
                )
                return False
            if state == "challenge":
                # Captcha is not recoverable with a recovery email — don't
                # pretend the form can continue.
                signer_probe = self._signer()
                if signer_probe._is_captcha():
                    self.login_state = "captcha"
                    self.login_outcome = gv_login.CAPTCHA
                    self.login_detail = "reCAPTCHA / robot check — sign in by hand once"
                    logger.warning(
                        f"Worker {self.worker_id}: CAPTCHA — account {email} "
                        f"requires a robot check"
                    )
                    return False
                # Recovery-email picker is a CHALLENGE page but NOT 2FA.
                # Try another way on that screen skips type 12 — never do it.
                if (
                    signer_probe._find_recovery_tile()
                    or signer_probe._recovery_picker_on_screen()
                    or signer_probe._is_recovery_challenge()
                ):
                    logger.info(
                        f"Worker {self.worker_id}: Confirm your recovery email "
                        f"(challengetype=12) — continuing the form"
                    )
                else:
                    dismissed = signer_probe.dismiss_interstitial()
                    if dismissed or signer_probe._is_skippable_auth_nag():
                        logger.info(
                            f"Worker {self.worker_id}: skippable passkey/stay-signed-in "
                            f"nag — continuing the form (Not now)"
                        )
                    elif not (recovery_email or totp_seed or backup_codes):
                        self.login_outcome = gv_login.CHALLENGE_REQUIRED
                        self.login_detail = "2FA/captcha — sign in by hand once"
                        return False
                    else:
                        logger.info(
                            f"Worker {self.worker_id}: challenge page — will try "
                            f"the recovery-email step"
                        )

            if state == "expired":
                # Neither a sign-in form nor a working dialer: the session is dead,
                # so start from a clean slate exactly like a fresh profile.
                self.clear_session_cookies()
                try:
                    self.driver.get(gv_login.LANDING_URL)
                except Exception as e:
                    self._note_browser_error(e, "auto-login navigation")
                    self.login_outcome = "no_browser"
                    return False

            # Saved-account picker before the form starts: wipe immediately
            # and never click a tile. The form then types the email.
            try:
                if self._signer().state() == gv_login.CHOOSER:
                    if not self._clear_chooser_session():
                        self.login_outcome = gv_login.ACCOUNT_CHOOSER
                        self.login_detail = (
                            "account picker on screen — could not clear cookies"
                        )
                        return False
            except Exception as e:
                self._note_browser_error(e, "chooser probe")

            signer = self._signer()
            result = None
            try:
                result = signer.run(
                    email, password,
                    timeout=max(30.0, budget - (time.time() - started)),
                    recovery_email=recovery_email,
                    cookie_wiper=self._clear_chooser_session,
                    totp_seed=totp_seed,
                    backup_codes=backup_codes,
                    backup_code_used=_backup_code_used,
                )
            except Exception as e:
                self._note_browser_error(e, "auto-login")
                self.login_outcome = gv_login.ERROR
                self.login_detail = str(e)
                return False
            finally:
                try:
                    signer.consolidate_tabs()
                except Exception:
                    pass

            if not result:
                self.login_outcome = gv_login.ERROR
                self.login_detail = "sign-in returned nothing"
                return False

            self.login_outcome = result["outcome"]
            self.login_detail = result["detail"]

            if result["outcome"] != gv_login.OK:
                # Chooser/passkey races report no_form while the Voice dialer
                # is already up. Trust the dialer, never wipe a live session.
                if self._check_if_logged_in():
                    logger.warning(
                        f"Worker {self.worker_id}: sign-in reported "
                        f"{result['outcome']} but the dialer is up — "
                        f"treating as success"
                    )
                    result = {
                        "outcome": gv_login.OK,
                        "detail": "dialer reached after form race",
                    }
                    self.login_outcome = gv_login.OK
                    self.login_detail = result["detail"]
                else:
                    self.login_state = (
                        "captcha" if result["outcome"] == gv_login.CAPTCHA
                        else "challenge" if result["outcome"] == gv_login.CHALLENGE_REQUIRED
                        else "banned" if result["outcome"] == gv_login.DISABLED
                        else "needs_login"
                    )
                    if result["outcome"] == gv_login.ACCOUNT_CHOOSER:
                        # Picker survived an in-browser wipe — drop cookie
                        # files too, keep the password, next /login types it.
                        try:
                            self.reset_to_login_page(
                                "account picker — cookies cleared, will sign in fresh",
                                True,
                            )
                        except Exception as e:
                            logger.warning(
                                f"Worker {self.worker_id}: chooser cookie "
                                f"file wipe failed — {e}"
                            )
                        self.login_outcome = gv_login.ACCOUNT_CHOOSER
                    if result["outcome"] == gv_login.DISABLED:
                        self.is_banned = True
                    self.last_error = f"auto-login {result['outcome']}: {result['detail']}"
                    if result["outcome"] == gv_login.CAPTCHA:
                        logger.warning(
                            f"Worker {self.worker_id}: CAPTCHA — account {email} "
                            f"requires a robot check"
                        )
                    else:
                        logger.warning(
                            f"Worker {self.worker_id}: auto-login failed "
                            f"({result['outcome']}) — {result['detail']}"
                        )
                    return False

            # Signer already confirmed the dialer. Reloading with driver.get()
            # here is what produced the ~30s stall: Selenium waits out the
            # previous (still in-flight) page-load timeout, then loads again.
            if not self._check_if_logged_in():
                deadline = time.time() + 8.0
                dialer_up = False
                browser_died = False
                while time.time() < deadline:
                    if self.driver is None:
                        browser_died = True
                        break
                    if self._check_if_logged_in():
                        dialer_up = True
                        break
                    time.sleep(0.3)
                if not dialer_up:
                    self.login_state = "needs_login"
                    self.login_outcome = "no_dialer"
                    self.login_detail = (
                        "browser died right after sign-in"
                        if browser_died else
                        "signed in, but the dialer never rendered"
                    )
                    logger.warning(
                        f"Worker {self.worker_id}: signed in as {email} but the dialer "
                        f"did not render ({self.login_detail})"
                    )
                    return False

            self.is_authenticated = True
            self.login_state = "ready"
            self.account_email = email
            self.login_seconds = time.time() - started
            if not self._fetch_phone_number():
                logger.warning(
                    f"Worker {self.worker_id}: signed in as {email}, GV number not "
                    f"visible yet"
                )
            self._configure_gv_audio()
            logger.info(
                f"Worker {self.worker_id}: AUTO-LOGIN OK as {email} "
                f"({self.login_seconds:.1f}s) number={self.phone_number}"
            )
            return True
        finally:
            self._login_running = False

    async def login_or_autologin(self, timeout: Optional[float] = None) -> bool:
        """Bring this worker to a signed-in dialer, signing in if it has to.

        The single entry point used by both pool init and browser restart, so a
        session that dies mid-run is recovered the same way as one that was
        never signed in. Every blocking step runs in an executor.
        """
        loop = asyncio.get_running_loop()
        budget = max(10, int(timeout or config.GV_STARTUP_TIMEOUT_SEC))
        login_budget = max(30, int(getattr(config, "GV_LOGIN_TIMEOUT_SEC", 120)))

        async def _run(fn, limit):
            try:
                return await asyncio.wait_for(
                    loop.run_in_executor(None, fn), timeout=limit
                )
            except asyncio.TimeoutError:
                self.last_error = f"{getattr(fn, '__name__', 'login')} timeout"
                logger.error(
                    f"Worker {self.worker_id}: {self.last_error} ({limit}s)"
                )
                return False
            except Exception as e:
                self.last_error = str(e)
                logger.error(f"Worker {self.worker_id}: login error — {e}")
                return False

        autologin_on = bool(getattr(config, "GV_AUTOLOGIN", True))
        # detect_session_state returns as soon as the page is conclusive, so
        # this watchdog only covers a genuinely hung browser, not a wait loop.
        probe_watch = float(getattr(config, "GV_SESSION_PROBE_SEC", 12.0)) + 15.0

        # A /login already driving this browser owns it — a concurrent init or
        # restart must not navigate or respawn the same tab.
        if self._login_running:
            logger.info(
                f"Worker {self.worker_id}: sign-in already running — "
                f"login_or_autologin deferred"
            )
            return False

        if autologin_on and self.has_credentials():
            # Knowing the credentials, classify first: a session that will
            # never appear is identified in seconds, then we just sign in.
            state = await _run(self.detect_session_state, probe_watch)
            if state in ("needs_login", "expired"):
                return await _run(
                    lambda: self.auto_login(login_budget), login_budget + 30
                )
            if state in ("challenge", "captcha", "banned", "no_browser"):
                self.last_error = f"session {state}"
                return False

        self._login_running = True
        try:
            ok = await _run(self._login_to_account, budget)
        finally:
            self._login_running = False
        if ok and self.is_authenticated:
            self.login_state = "ready"
            return True

        # Classify the failure so the controller can tell the operator whether
        # this profile needs a password or a human with a phone.
        state = await _run(self.detect_session_state, probe_watch)
        if autologin_on and self.has_credentials() and state in ("needs_login", "expired"):
            return await _run(
                lambda: self.auto_login(login_budget), login_budget + 30
            )
        return False

    def check_ban_status(self) -> tuple:
        """Check if GV account is banned. Returns (is_banned, reason)."""
        if not config.BAN_DETECTION_ENABLED:
            return False, None
        if not self.driver:
            return False, None
        try:
            from telephony import gv_login

            url = (self.driver.current_url or "").lower()
            if self._looks_like_auth_page(url):
                self.is_authenticated = False
                self.login_state = "needs_login"
                self.last_error = "auth required (session expired)"
                logger.warning(f"Worker {self.worker_id}: auth required before call")
                return False, "auth_required"
            if gv_login.is_voice_ban_page(url):
                self.is_banned = True
                self.login_state = "banned"
                logger.warning(
                    f"Worker {self.worker_id}: BAN DETECTED (URL: {url[:160]})"
                )
                return True, f"URL: {url[:160]}"
            if "voice.google.com" not in url and "workspace.google.com" not in url:
                logger.warning(f"Worker {self.worker_id}: unexpected redirect during ban check: {url[:80]}")
                return False, f"unexpected_redirect: {url[:80]}"
            content = self.driver.execute_script(
                "return document.body ? document.body.innerText.toLowerCase() : ''"
            )
            if gv_login.is_voice_ban_page(url, content or ""):
                self.is_banned = True
                self.login_state = "banned"
                logger.warning(
                    f"Worker {self.worker_id}: BAN DETECTED (page text)"
                )
                return True, "Unable to access a Google product"
        except Exception as e:
            self._note_browser_error(e, "ban check")
            if not self._is_driver_alive():
                return False, "browser_dead"
            logger.warning(f"Worker {self.worker_id}: ban check failed; treating as transient: {e}")
            return False, f"check_error: {e}"
        return False, None

    def _is_driver_alive(self) -> bool:
        """Quick health check - is the browser session still alive?"""
        if not self.driver:
            return False
        try:
            _ = self.driver.title
            return True
        except Exception as e:
            self._note_browser_error(e, "liveness probe")
            self._mark_session_dead(f"liveness probe: {e}")
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Memory guard: OOM detection, telemetry, page recycle, browser restart
    # ──────────────────────────────────────────────────────────────────────────

    def _note_browser_error(self, exc: BaseException, context: str) -> Optional[str]:
        """Flag the worker for restart if `exc` means the browser died."""
        kind = classify_browser_error(exc)
        if kind is None:
            return None
        if kind == "oom":
            self.oom_events += 1
            logger.error(
                f"Worker {self.worker_id}: Chrome OUT OF MEMORY during {context} "
                f"(oom #{self.oom_events}, {self.calls_served} calls on this "
                f"browser, rss={self.last_rss_mb}MB heap={self.last_heap_pct}%) "
                f"— browser will be restarted"
            )
        else:
            logger.warning(
                f"Worker {self.worker_id}: browser session lost during {context} "
                f"— {exc}"
            )
        # A profile still waiting on a Google login is not a dead browser to
        # resurrect — flagging it needs_restart here is what made the watchdog
        # keep spawning fresh Chromes on an account-less profile.
        if self.login_state not in (
            "needs_login", "challenge", "captcha", "logging_in"
        ):
            self.needs_restart = True
            self.restart_reason = f"{kind}: {context}"
        return kind

    def _mark_session_dead(self, reason: str):
        """Park a dead session so its processes can still be reaped."""
        logger.warning(f"Worker {self.worker_id}: browser session is dead ({reason})")
        self.is_authenticated = False
        self.needs_restart = True
        if not self.restart_reason:
            self.restart_reason = reason
        if self.driver is not None:
            self._dead_driver, self.driver = self.driver, None

    def js_heap_stats(self) -> Optional[dict]:
        """V8 heap usage of the dialer tab via Chrome's performance.memory."""
        if not self.driver:
            return None
        try:
            raw = self.driver.execute_script(
                "const m = window.performance && window.performance.memory;"
                "return m ? {used: m.usedJSHeapSize, limit: m.jsHeapSizeLimit}"
                " : null;"
            )
        except Exception as e:
            self._note_browser_error(e, "js heap probe")
            return None
        if not raw or not raw.get("limit"):
            return None
        used_mb = float(raw["used"]) / 1048576.0
        limit_mb = float(raw["limit"]) / 1048576.0
        pct = int(round(100.0 * used_mb / limit_mb)) if limit_mb else 0
        self.last_heap_pct = pct
        return {
            "used_mb": round(used_mb, 1),
            "limit_mb": round(limit_mb, 1),
            "pct": pct,
        }

    def _browser_pids(self) -> list:
        """Chrome + chromedriver PIDs owned by this worker."""
        pids = []
        for drv in (self.driver, self._dead_driver):
            if drv is None:
                continue
            pid = getattr(drv, "browser_pid", None)
            if pid:
                pids.append(int(pid))
            try:
                svc_pid = drv.service.process.pid
            except Exception:
                svc_pid = None
            if svc_pid:
                pids.append(int(svc_pid))
        return pids

    def chrome_rss_mb(self) -> Optional[float]:
        """Resident memory of this worker's whole Chrome process tree (MB)."""
        if psutil is None:
            return None
        total = 0.0
        seen = set()
        for pid in self._browser_pids():
            try:
                proc = psutil.Process(pid)
            except Exception:
                continue
            for p in [proc, *proc.children(recursive=True)]:
                if p.pid in seen:
                    continue
                seen.add(p.pid)
                try:
                    total += p.memory_info().rss
                except Exception:
                    continue
        if not seen:
            return None
        self.last_rss_mb = round(total / 1048576.0, 1)
        return self.last_rss_mb

    def memory_snapshot(self) -> dict:
        """Current memory picture. Blocking — run it off the event loop."""
        heap = self.js_heap_stats()
        return {
            "rss_mb": self.chrome_rss_mb(),
            "heap_pct": (heap or {}).get("pct"),
            "heap_used_mb": (heap or {}).get("used_mb"),
            "calls_served": self.calls_served,
            "restart_count": self.restart_count,
            "oom_events": self.oom_events,
        }

    async def refresh_memory(self, budget: float = 8.0) -> Optional[dict]:
        """Update cached memory figures without blocking the event loop."""
        if self.driver is None:
            return None
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self.memory_snapshot), timeout=budget
            )
        except asyncio.TimeoutError:
            logger.debug(
                f"Worker {self.worker_id}: memory probe timed out ({budget}s)"
            )
            return None
        except Exception as e:
            logger.debug(f"Worker {self.worker_id}: memory probe failed: {e}")
            return None

    def _kill_profile_processes(self):
        """Kill any Chrome still holding this worker's profile directory.

        driver.quit() leaks the browser when the session is already dead, and a
        leftover process keeps both the profile lock and its RAM.
        Matching must tolerate quoted Windows paths — an exact
        `--user-data-dir={Path}` miss is what left Worker 2's Chrome alive
        while the next spawn hung on 'chrome not reachable'.
        """
        pids = self._iter_profile_holder_pids()
        for pid in pids:
            self._kill_pid_tree(pid)
        if pids:
            logger.warning(
                f"Worker {self.worker_id}: killed {len(pids)} leftover "
                f"Chrome/chromedriver process(es) on {self.profile_dir.name}"
            )
        if psutil is not None and pids:
            procs = []
            for pid in pids:
                try:
                    procs.append(psutil.Process(pid))
                except Exception:
                    continue
            if procs:
                try:
                    psutil.wait_procs(procs, timeout=4)
                except Exception:
                    pass
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    [
                        "wmic", "process", "where",
                        f"CommandLine like '%{self.profile_dir.name}%'",
                        "delete",
                    ],
                    capture_output=True, check=False,
                )
            else:
                marker = f"--user-data-dir={self.profile_dir}"
                subprocess.run(
                    ["pkill", "-f", marker], capture_output=True, check=False
                )
        except Exception as e:
            logger.debug(f"Worker {self.worker_id}: profile process cleanup: {e}")

    @staticmethod
    def _cmdline_holds_profile(cmdline, profile_dir) -> bool:
        """True if this process command line is using `profile_dir`."""
        if not cmdline or not profile_dir:
            return False
        if isinstance(cmdline, (list, tuple)):
            text = " ".join(str(x) for x in cmdline)
        else:
            text = str(cmdline)
        norm = text.replace("/", "\\").lower().replace('"', "")
        path = str(profile_dir).replace("/", "\\").lower()
        if path:
            idx = 0
            while True:
                found = norm.find(path, idx)
                if found < 0:
                    break
                nxt = norm[found + len(path): found + len(path) + 1]
                if not nxt.isdigit():
                    return True
                idx = found + 1
        name = Path(str(profile_dir)).name.lower()
        if not name or name not in norm:
            return False
        start = 0
        while True:
            idx = norm.find(name, start)
            if idx < 0:
                return False
            nxt = norm[idx + len(name): idx + len(name) + 1]
            if not nxt.isdigit():
                return True
            start = idx + 1

    def _iter_profile_holder_pids(self) -> list:
        """PIDs of Chrome/chromedriver still attached to this profile."""
        pids = []
        seen = set()
        for pid in self._browser_pids():
            if pid not in seen:
                seen.add(pid)
                pids.append(pid)
        if psutil is None:
            return pids
        port = None
        try:
            if int(self.worker_id) >= 0:
                port = 9500 + int(self.worker_id)
        except Exception:
            port = None
        try:
            procs = psutil.process_iter(["pid", "name", "cmdline"])
        except Exception:
            return pids
        for proc in procs:
            try:
                info = proc.info
                pid = info.get("pid")
                if pid is None or pid in seen:
                    continue
                cmdline = info.get("cmdline") or []
                exe = (info.get("name") or "").lower()
                if self._cmdline_holds_profile(cmdline, self.profile_dir):
                    seen.add(pid)
                    pids.append(int(pid))
                    continue
                if port and "chromedriver" in exe:
                    joined = " ".join(str(x) for x in cmdline).lower().replace('"', "")
                    if f"--port={port}" in joined or f"--port {port}" in joined:
                        seen.add(pid)
                        pids.append(int(pid))
            except Exception:
                continue
        return pids

    def _kill_pid_tree(self, pid: int) -> None:
        if psutil is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True, check=False,
                    )
                else:
                    os.kill(pid, 9)
            except Exception:
                pass
            return
        try:
            proc = psutil.Process(pid)
        except Exception:
            return
        kids = []
        try:
            kids = proc.children(recursive=True)
        except Exception:
            pass
        for p in (*kids, proc):
            try:
                p.kill()
            except Exception:
                pass

    def _wait_profile_free(self, timeout: float = 12.0) -> bool:
        """Block until nothing is holding this profile, then drop lock files."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            holders = self._iter_profile_holder_pids()
            if not holders:
                self._cleanup_failed_launch_artifacts()
                time.sleep(0.25)
                if not self._iter_profile_holder_pids():
                    return True
            else:
                for pid in holders:
                    self._kill_pid_tree(pid)
            time.sleep(0.35)
        self._cleanup_failed_launch_artifacts()
        left = self._iter_profile_holder_pids()
        if left:
            logger.error(
                f"Worker {self.worker_id}: profile {self.profile_dir.name} still "
                f"held by pids {left} after {timeout:.0f}s"
            )
            return False
        return True

    def _abort_inflight_spawn(self):
        """Invalidate a timed-out uc.Chrome() thread and reap what it started."""
        self._spawn_generation = getattr(self, "_spawn_generation", 0) + 1
        logger.warning(
            f"Worker {self.worker_id}: aborting in-flight Chrome spawn "
            f"(generation now {self._spawn_generation})"
        )
        self._close_browser_for_restart()

    def _force_gc(self):
        """Ask V8 to collect now (requires the --expose-gc launch flag)."""
        if not self.driver:
            return
        try:
            self.driver.execute_script("if (window.gc) { window.gc(); }")
        except Exception:
            pass

    async def recycle_page(self, reason: str = "") -> bool:
        """Tear the dialer tab down to about:blank and load it fresh.

        Navigating away destroys the SPA's document, which is the only reliable
        way to release the DOM nodes and listeners it accumulates per call.
        """
        if not self.driver:
            return False
        budget = max(
            10.0, float(getattr(config, "GV_RECOVERY_TIMEOUT_SEC", 20.0)) + 10.0
        )
        loop = asyncio.get_running_loop()
        try:
            ok = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: self._recycle_page_sync(reason)),
                timeout=budget,
            )
        except asyncio.TimeoutError:
            self.last_error = "page recycle hard-timeout"
            logger.warning(
                f"Worker {self.worker_id}: page recycle hard-timeout ({budget:.0f}s)"
            )
            return False
        except Exception as e:
            self._note_browser_error(e, "page recycle")
            logger.warning(f"Worker {self.worker_id}: page recycle crashed: {e}")
            return False
        if ok:
            self._calls_since_recycle = 0
        return ok

    def _recycle_page_sync(self, reason: str = "") -> bool:
        if not self._is_driver_alive():
            return False

        before = self.last_rss_mb
        logger.info(
            f"Worker {self.worker_id}: recycling dialer tab ({reason}) — "
            f"calls={self.calls_served} rss={before}MB heap={self.last_heap_pct}%"
        )
        self._active_calls.clear()
        self._cached_dial_selector = None
        self._cached_input_selector = None

        try:
            self.driver.get("about:blank")
            self._force_gc()
            self.driver.get("https://voice.google.com/u/0/calls")
            WebDriverWait(self.driver, 15).until(
                lambda driver: self._check_if_logged_in()
                or self._looks_like_auth_page((driver.current_url or "").lower())
            )
        except TimeoutException:
            self.last_error = "page recycle timeout"
            logger.warning(f"Worker {self.worker_id}: page recycle timed out")
            try:
                alive = self._is_driver_alive()
            except Exception:
                alive = False
            if alive and not self._check_if_logged_in():
                self.is_authenticated = False
                self.login_state = "needs_login"
            return False
        except Exception as e:
            self._note_browser_error(e, "page recycle")
            self.last_error = f"page recycle error: {e}"
            return False

        if self._looks_like_auth_page((self.driver.current_url or "").lower()):
            self.is_authenticated = False
            self.login_state = "needs_login"
            self.last_error = "auth required (session expired)"
            return False

        if not self._check_if_logged_in():
            self.is_authenticated = False
            self.login_state = "needs_login"
            self.last_error = "dialer not ready after recycle"
            return False

        self.is_authenticated = True
        self.last_error = None
        # Re-measure so the thresholds see the freed memory instead of the
        # pre-reload numbers, which would re-trigger on the next release.
        after = self.chrome_rss_mb()
        self.js_heap_stats()
        logger.info(
            f"Worker {self.worker_id}: dialer tab recycled "
            f"(rss {before}MB → {after}MB, heap {self.last_heap_pct}%)"
        )
        return True

    def _close_browser_for_restart(self):
        """Quit both the live and the parked session, then reap leftovers.

        Never runs while a sign-in is driving the browser — closing the tab
        mid-form is what produced 'no such window' and lost the login.
        """
        if self._login_running:
            logger.warning(
                f"Worker {self.worker_id}: close deferred — sign-in in progress"
            )
            return
        for attr in ("driver", "_dead_driver"):
            drv = getattr(self, attr, None)
            if drv is None:
                continue
            try:
                drv.quit()
            except Exception:
                pass
            setattr(self, attr, None)
        self._kill_profile_processes()
        self._wait_profile_free(12.0)
        self.is_authenticated = False
        self._active_calls.clear()
        self._cached_dial_selector = None
        self._cached_input_selector = None
        self.last_rss_mb = None
        self.last_heap_pct = None

    async def restart_browser(self, reason: str = "") -> bool:
        """Respawn Chrome for this worker and log back into Google Voice.

        This is the recovery path for an OOM-killed renderer: the old process
        tree is reaped and a clean browser takes its place on the same profile.
        """
        # Re-entrancy guard: a profile already mid-respawn must not be queued
        # again — each overlapping spawn left another Chrome on this profile.
        if self._revive_running:
            logger.warning(
                f"Worker {self.worker_id}: restart already running — skipped "
                f"(reason={reason or self.restart_reason})"
            )
            return False
        # A sign-in is driving this browser right now; respawning would close
        # its tab out from under the form and lose the login.
        if self._login_running:
            logger.warning(
                f"Worker {self.worker_id}: sign-in in progress — restart "
                f"deferred (reason={reason or self.restart_reason})"
            )
            return False
        self._revive_running = True
        self.restart_count += 1
        free_mb = host_free_mb()
        logger.warning(
            f"Worker {self.worker_id}: RESTARTING browser (restart "
            f"#{self.restart_count}) — reason={reason or self.restart_reason} "
            f"calls={self.calls_served} host_free={free_mb}MB"
        )
        try:
            ok = await self._restart_browser_inner()
        finally:
            self._revive_running = False
        if ok:
            self.failed_restarts = 0
        else:
            self.failed_restarts += 1
            logger.error(
                f"Worker {self.worker_id}: restart failed "
                f"({self.failed_restarts} in a row) — {self.last_error}"
            )
        return ok

    async def _restart_browser_inner(self) -> bool:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._close_browser_for_restart)

        startup = max(10, config.GV_STARTUP_TIMEOUT_SEC)
        try:
            async with uc_spawn_lock:
                spawned = await asyncio.wait_for(
                    loop.run_in_executor(None, self._setup_browser_only),
                    timeout=startup,
                )
        except asyncio.TimeoutError:
            self.last_error = "restart: browser spawn timeout"
            logger.error(f"Worker {self.worker_id}: restart spawn timed out")
            await loop.run_in_executor(None, self._abort_inflight_spawn)
            return False
        except Exception as e:
            self.last_error = f"restart spawn error: {e}"
            logger.error(f"Worker {self.worker_id}: restart spawn error — {e}")
            await loop.run_in_executor(None, self._abort_inflight_spawn)
            return False

        if not spawned:
            logger.error(
                f"Worker {self.worker_id}: restart failed to spawn Chrome — "
                f"{self.last_error}"
            )
            await loop.run_in_executor(None, self._abort_inflight_spawn)
            return False

        try:
            # Same path as init: a fresh browser on a profile whose Google
            # session expired must be able to sign itself back in.
            logged_in = await self.login_or_autologin(timeout=startup)
        except Exception as e:
            self.last_error = f"restart login error: {e}"
            logger.error(f"Worker {self.worker_id}: restart login error — {e}")
            return False

        if not (logged_in and self.is_authenticated):
            logger.error(
                f"Worker {self.worker_id}: restart could not re-authenticate — "
                f"{self.last_error}"
            )
            return False

        self.needs_restart = False
        self.restart_reason = None
        self.calls_served = 0
        self._calls_since_recycle = 0
        self.last_error = None
        self.last_heap_pct = None
        self.last_rss_mb = None
        rss = self.chrome_rss_mb()
        logger.info(
            f"Worker {self.worker_id}: browser restarted and logged in "
            f"(number={self.phone_number} rss={rss}MB)"
        )
        return True

    def note_call_finished(self):
        """Count a completed call — drives the periodic recycle thresholds."""
        self.calls_served += 1
        self._calls_since_recycle += 1

    def _hard_recycle_reason(self) -> Optional[str]:
        """Why this worker needs a full browser respawn, if it does."""
        if self.needs_restart:
            return self.restart_reason or "browser marked for restart"
        limit = int(getattr(config, "GV_RESTART_AFTER_CALLS", 200))
        if limit > 0 and self.calls_served >= limit:
            return f"served {self.calls_served} calls (limit {limit})"
        hard_rss = int(getattr(config, "GV_WORKER_RSS_HARD_MB", 1400))
        if hard_rss > 0 and (self.last_rss_mb or 0) >= hard_rss:
            return f"rss {self.last_rss_mb}MB over hard limit {hard_rss}MB"
        return None

    def _soft_recycle_reason(self) -> Optional[str]:
        """Why this worker needs a tab reload, if it does."""
        every = int(getattr(config, "GV_RECYCLE_AFTER_CALLS", 25))
        if every > 0 and self._calls_since_recycle >= every:
            return f"{self._calls_since_recycle} calls since last reload"
        soft_pct = int(getattr(config, "GV_JS_HEAP_SOFT_PCT", 70))
        if soft_pct > 0 and (self.last_heap_pct or 0) >= soft_pct:
            return f"js heap at {self.last_heap_pct}% of its limit"
        soft_rss = int(getattr(config, "GV_WORKER_RSS_SOFT_MB", 900))
        if soft_rss > 0 and (self.last_rss_mb or 0) >= soft_rss:
            return f"rss {self.last_rss_mb}MB over soft limit {soft_rss}MB"
        return None

    def recycle_plan(self) -> tuple:
        """Decide what this worker needs before its next call.

        Returns (action, reason) where action is None, 'reload' or 'restart'.
        Pure decision over the cached telemetry — call refresh_memory() first.
        """
        hard = self._hard_recycle_reason()
        if hard:
            # A proactive respawn while the host is already short on RAM is
            # what invites the OOM killer, so downgrade it to a tab reload.
            # A browser that is already dead has to be restarted regardless.
            free_mb = host_free_mb()
            floor = int(getattr(config, "GV_HOST_FREE_MB_MIN", 700))
            if not self.needs_restart and free_mb is not None and free_mb < floor:
                logger.warning(
                    f"Worker {self.worker_id}: postponing respawn ({hard}) — "
                    f"host has only {free_mb:.0f}MB free (<{floor}MB); "
                    f"reloading the tab instead"
                )
                return "reload", f"{hard} (respawn postponed, low host RAM)"
            return "restart", hard

        soft = self._soft_recycle_reason()
        if soft:
            return "reload", soft
        return None, None

    # ──────────────────────────────────────────────────────────────────────────
    # Call operations
    # ──────────────────────────────────────────────────────────────────────────

    async def recover_dialer_state(self, reason: str = "") -> bool:
        """Try to recover the worker back to the calls dialer after a transient failure."""
        if not self.driver:
            return False

        budget = max(5.0, float(getattr(config, "GV_RECOVERY_TIMEOUT_SEC", 20.0)))
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: self._recover_dialer_state_sync(reason)
                ),
                timeout=budget,
            )
        except asyncio.TimeoutError:
            self.last_error = "dialer recovery hard-timeout"
            logger.warning(
                f"Worker {self.worker_id}: dialer recovery hard-timeout ({budget:.0f}s)"
            )
            return False
        except Exception as e:
            logger.warning(f"Worker {self.worker_id}: dialer recovery crashed: {e}")
            return False

    def _recover_dialer_state_sync(self, reason: str = "") -> bool:
        """Sync dialer recovery path used before safe retries and requeueing."""
        if not self._is_driver_alive():
            self.last_error = "browser session dead"
            return False

        suffix = f" ({reason})" if reason else ""
        logger.warning(f"Worker {self.worker_id}: recovering dialer state{suffix}")
        self._active_calls.clear()
        self._cached_dial_selector = None
        self._cached_input_selector = None
        self._dismiss_blocking_overlays()

        try:
            # Prefer soft reload first (cheaper than full navigation on sticky tabs).
            try:
                self.driver.refresh()
            except Exception:
                pass
            self.driver.get("https://voice.google.com/u/0/calls")
            WebDriverWait(self.driver, 8).until(
                lambda driver: self._check_if_logged_in()
                or self._looks_like_auth_page((driver.current_url or "").lower())
            )
        except TimeoutException:
            self.last_error = "dialer recovery timeout"
            logger.warning(f"Worker {self.worker_id}: dialer recovery timed out")
            # 8s with neither dialer nor auth URL: the Google session is gone.
            # Leave needs_login so the pool wipes cookies instead of reviving
            # the same dead account (Chrome crash still looks like a dead driver).
            try:
                alive = self._is_driver_alive()
            except Exception:
                alive = False
            if alive and not self._check_if_logged_in():
                self.is_authenticated = False
                self.login_state = "needs_login"
            return False
        except Exception as e:
            self._note_browser_error(e, "dialer recovery")
            self.last_error = f"dialer recovery error: {e}"
            logger.warning(f"Worker {self.worker_id}: dialer recovery error: {e}")
            return False

        current_url = (self.driver.current_url or "").lower()
        if self._looks_like_auth_page(current_url):
            self.is_authenticated = False
            self.login_state = "needs_login"
            self.last_error = "auth required (session expired)"
            logger.warning(f"Worker {self.worker_id}: recovery reached auth flow")
            return False

        is_banned, reason_text = self.check_ban_status()
        if is_banned:
            self.last_error = f"account banned: {reason_text}"
            return False

        if not self._check_if_logged_in():
            self.is_authenticated = False
            self.login_state = "needs_login"
            self.last_error = "dialer not ready after recovery"
            logger.warning(f"Worker {self.worker_id}: dialer still not ready after recovery")
            return False

        self.is_authenticated = True
        self.last_error = None
        return True

    async def make_call(self, phone_number: str, call_id: str, attempts: int = 2) -> bool:
        """Initiate an outbound call via Google Voice."""
        if not self.is_authenticated:
            logger.error(f"Worker {self.worker_id}: not authenticated ? cannot call")
            return False

        loop = asyncio.get_running_loop()
        max_attempts = max(1, attempts)

        for attempt in range(1, max_attempts + 1):
            try:
                success = await loop.run_in_executor(
                    None, lambda: self._initiate_call(phone_number, call_id)
                )
            except Exception as e:
                logger.error(f"Worker {self.worker_id}: make_call failed ? {e}")
                success = False

            if success:
                return True

            if call_id in self._active_calls:
                logger.warning(
                    f"Worker {self.worker_id}: call {call_id} left active state after failed init; "
                    f"treating it as started"
                )
                return True

            if attempt >= max_attempts or self.is_banned or not self.is_authenticated or self.driver is None:
                break

            logger.warning(
                f"Worker {self.worker_id}: call init attempt {attempt}/{max_attempts} failed; "
                f"recovering dialer and retrying once"
            )
            recovered = await self.recover_dialer_state(f"call-init retry {attempt}")
            if not recovered:
                break

        return False

    def _find_element_cached(self, cache_attr: str, selectors: list, condition="clickable"):
        """Try cached selector first (fast path), fall back to sequential search."""
        cached = getattr(self, cache_attr, None)
        ec_fn = (
            EC.element_to_be_clickable
            if condition == "clickable"
            else EC.presence_of_element_located
        )

        if cached:
            try:
                el = WebDriverWait(self.driver, 0.5).until(
                    ec_fn((By.CSS_SELECTOR, cached))
                )
                return el, cached
            except TimeoutException:
                logger.debug(f"Cached selector '{cached}' missed — falling back")
                setattr(self, cache_attr, None)

        for selector in selectors:
            try:
                el = WebDriverWait(self.driver, 1).until(
                    ec_fn((By.CSS_SELECTOR, selector))
                )
                setattr(self, cache_attr, selector)
                logger.debug(f"Worker {self.worker_id}: cached selector: {selector}")
                return el, selector
            except TimeoutException:
                continue

        return None, None

    def _initiate_call(self, phone_number: str, call_id: str) -> bool:
        """Browser-side call initiation."""
        try:
            # Health + ban check before dialing
            if not self._is_driver_alive():
                self.last_error = "browser session dead"
                return False
            current_url = (self.driver.current_url or "").lower()
            if self._looks_like_auth_page(current_url):
                self.is_authenticated = False
                self.login_state = "needs_login"
                self.last_error = "auth required (session expired)"
                logger.warning(f"Worker {self.worker_id}: cannot dial because auth is required")
                return False
            is_banned, reason = self.check_ban_status()
            if is_banned:
                self.last_error = f"account banned: {reason}"
                return False

            masked = phone_number[:4] + "...." + phone_number[-4:]
            logger.debug(f"Worker {self.worker_id}: dialing {masked}")

            # Navigate to calls page only if needed.
            current_url = (self.driver.current_url or "").lower()
            if "voice.google.com" not in current_url or "/calls" not in current_url:
                logger.info(f"Worker {self.worker_id}: navigating to calls page…")
                self.driver.get("https://voice.google.com/u/0/calls")
                try:
                    WebDriverWait(self.driver, 6).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "gv-call-sidebar"))
                    )
                except TimeoutException:
                    logger.warning(
                        f"Worker {self.worker_id}: calls UI slow to load — proceeding anyway"
                    )

            dial_number = phone_number.lstrip("+")
            logger.info(
                f"Worker {self.worker_id}: dialing {dial_number} "
                f"(from GV {self.phone_number})"
            )

            # Reset stale selector caches — they can resolve to a detached
            # element and silently break number entry.
            self._cached_dial_selector = None
            self._cached_input_selector = None
            self._dismiss_blocking_overlays()

            # Locate the REAL make-call input (from live GV DOM:
            # <input id="il1" class="input" placeholder="Enter a name or number">
            # inside <gv-make-call-panel>). Most specific first.
            input_selectors = [
                "gv-make-call-panel input.input",
                "gv-make-call-panel input[placeholder*='name or number' i]",
                "gv-make-call-panel input[type='text']",
                "input#il1",
                "input[placeholder='Enter a name or number']",
                "input[placeholder*='name or number' i]",
                "input[type='tel']",
            ]

            def _find_make_call_input():
                for sel in input_selectors:
                    try:
                        for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                            if el.is_displayed() and el.is_enabled():
                                return el, sel
                    except Exception:
                        continue
                return None, None

            phone_input, used_sel = _find_make_call_input()

            if not phone_input:
                # Try to open the make-call panel, then re-find.
                logger.warning(
                    f"Worker {self.worker_id}: make-call input not found — "
                    f"reloading calls page"
                )
                self.driver.get("https://voice.google.com/u/0/calls")
                try:
                    WebDriverWait(self.driver, 8).until(
                        lambda d: _find_make_call_input()[0] is not None
                    )
                except TimeoutException:
                    pass
                phone_input, used_sel = _find_make_call_input()
                if not phone_input:
                    self.last_error = "make-call input not found"
                    logger.error(
                        f"Worker {self.worker_id}: make-call input not found"
                    )
                    return False

            logger.info(
                f"Worker {self.worker_id}: input found via '{used_sel}'"
            )

            def _input_value():
                try:
                    return (phone_input.get_attribute("value") or "").strip()
                except Exception:
                    return ""

            def _clear_input():
                try:
                    phone_input.click()
                except Exception:
                    pass
                # Select-all + delete via JS native setter (robust clear).
                try:
                    self.driver.execute_script(
                        """
                        const i = arguments[0];
                        const s = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        s.call(i, '');
                        i.dispatchEvent(new Event('input', {bubbles:true}));
                        """,
                        phone_input,
                    )
                except Exception:
                    pass

            # Attempt 1: real keystrokes (Angular needs real key events).
            _clear_input()
            try:
                phone_input.click()
                phone_input.send_keys(dial_number)
            except Exception as e:
                logger.warning(
                    f"Worker {self.worker_id}: send_keys raised {e}"
                )
                self._dismiss_blocking_overlays()
            time.sleep(0.4)
            val = _input_value()
            logger.info(
                f"Worker {self.worker_id}: input value after send_keys = "
                f"{val!r}"
            )

            # Attempt 2: if digits did not land, JS native-setter injection.
            if dial_number not in val.replace(" ", "").replace("-", ""):
                logger.warning(
                    f"Worker {self.worker_id}: digits not in input — "
                    f"retrying via JS native setter"
                )
                self.driver.execute_script(
                    """
                    const input = arguments[0], val = arguments[1];
                    const proto = window.HTMLInputElement.prototype;
                    const setter =
                        Object.getOwnPropertyDescriptor(proto, 'value').set;
                    input.focus();
                    setter.call(input, val);
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    """,
                    phone_input,
                    dial_number,
                )
                time.sleep(0.4)
                val = _input_value()
                logger.info(
                    f"Worker {self.worker_id}: input value after JS = {val!r}"
                )

            if dial_number not in val.replace(" ", "").replace("-", ""):
                self.last_error = (
                    f"could not enter number into dialer (value={val!r})"
                )
                logger.error(
                    f"Worker {self.worker_id}: {self.last_error}"
                )
                return False

            # Click the REAL Google Voice call button once Angular enables it.
            # The button is <button gv-test-id="new-call-button" ... disabled>
            # and stays disabled until a valid number is registered.
            call_btn_selectors = [
                "button[gv-test-id='new-call-button']",
                "button[gv-test-id='in-call-call-button']",
                "button[aria-label='Call']",
                "button[data-tooltip='Call']",
            ]
            call_clicked = False
            click_deadline = time.time() + 6
            while time.time() < click_deadline and not call_clicked:
                for sel in call_btn_selectors:
                    try:
                        btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                    except Exception:
                        continue
                    if not btn:
                        continue
                    try:
                        disabled = (
                            btn.get_attribute("disabled")
                            or btn.get_attribute("aria-disabled") == "true"
                        )
                        if btn.is_displayed() and not disabled:
                            try:
                                btn.click()
                            except Exception:
                                self._dismiss_blocking_overlays()
                                self.driver.execute_script(
                                    "arguments[0].click();", btn
                                )
                            call_clicked = True
                            logger.info(
                                f"Worker {self.worker_id}: clicked call button ({sel})"
                            )
                            break
                    except Exception:
                        continue
                if not call_clicked:
                    time.sleep(0.25)

            if not call_clicked:
                # Last-resort: synthetic Enter on the input.
                try:
                    self.driver.execute_script(
                        """
                        const input = arguments[0];
                        const e = { key:'Enter', code:'Enter', keyCode:13,
                                    which:13, bubbles:true };
                        input.dispatchEvent(new KeyboardEvent('keydown', e));
                        input.dispatchEvent(new KeyboardEvent('keypress', e));
                        input.dispatchEvent(new KeyboardEvent('keyup', e));
                        """,
                        phone_input,
                    )
                    logger.warning(
                        f"Worker {self.worker_id}: call button never enabled — "
                        f"fell back to Enter key"
                    )
                except Exception:
                    pass

            self._active_calls[call_id] = {"phone": phone_number, "status": "dialing"}
            self.note_call_finished()

            # Quick verification window — 4 × 200ms = 0.8s max.
            for attempt in range(3):
                time.sleep(0.15)
                status = self.check_call_status()
                if status in ("ringing", "connected"):
                    logger.info(
                        f"Worker {self.worker_id}: call confirmed "
                        f"(status={status}, attempt={attempt + 1})"
                    )
                    self._active_calls[call_id]["status"] = status
                    self.last_error = None
                    return True

            logger.info(
                f"Worker {self.worker_id}: proceeding without immediate UI confirmation "
                "(wait_for_connect will verify)"
            )
            self.last_error = None
            return True

        except Exception as e:
            self.last_error = f"call initiate error: {e}"
            logger.error(f"Worker {self.worker_id}: call initiation aborted — {e}")
            if self._note_browser_error(e, "call initiation"):
                return False
            try:
                self.driver.save_screenshot("call_init_error.png")
            except Exception:
                pass
            return False

    async def hangup(self, call_id: str) -> bool:
        """Hang up an active call (hard-timeout so Selenium cannot freeze the agent)."""
        if call_id not in self._active_calls:
            # Still clear stale state if hangup was requested after local drop.
            self._active_calls.pop(call_id, None)
            return False
        budget = max(3.0, float(getattr(config, "GV_HANGUP_TIMEOUT_SEC", 15.0)))
        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, self._do_hangup),
                timeout=budget,
            )
            self._active_calls.pop(call_id, None)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                f"Worker {self.worker_id}: hangup hard-timeout ({budget:.0f}s) — "
                f"clearing local call state"
            )
            self._active_calls.pop(call_id, None)
            return False
        except Exception as e:
            logger.error(f"Worker {self.worker_id}: hangup error — {e}")
            self._active_calls.pop(call_id, None)
            return False

    async def wait_until_dialer_ready(
        self, timeout: float = 0.75, poll_interval: float = 0.05
    ) -> bool:
        """Wait until the Google Voice dialer is ready for the next call."""
        if not self.driver:
            return False

        loop = asyncio.get_running_loop()
        deadline = time.time() + timeout

        while time.time() < deadline:
            ready = await loop.run_in_executor(None, self._is_dialer_ready)
            if ready:
                return True
            await asyncio.sleep(poll_interval)

        return False

    def _is_dialer_ready(self) -> bool:
        """Return True once the in-call UI is gone and the dialer can accept input."""
        if not self.driver:
            return False

        try:
            return bool(
                self.driver.execute_script(
                    """
                    const isVisible = (el) => {
                        if (!el) return false;
                        return el.offsetParent !== null
                            || (el.getClientRects && el.getClientRects().length > 0);
                    };

                    const inCallSelectors = [
                        "button[gv-test-id='in-call-end-call']",
                        "button[aria-label='Hang up call']",
                        "[aria-label='End call']",
                        "[data-tooltip='End call']",
                    ];
                    for (const selector of inCallSelectors) {
                        if (isVisible(document.querySelector(selector))) {
                            return false;
                        }
                    }

                    const dialerSelectors = [
                        "input[placeholder*='name or number' i]",
                        "input[placeholder*='phone' i]",
                        "input[aria-label*='phone' i]",
                        "input[type='tel']",
                        "[gv-a11y-grid]",
                        "button[aria-label='Make a call']",
                        "button[data-tooltip='Make a call']",
                        "button[aria-label='Dialpad']",
                    ];
                    for (const selector of dialerSelectors) {
                        if (isVisible(document.querySelector(selector))) {
                            return true;
                        }
                    }

                    return false;
                    """
                )
            )
        except Exception as e:
            self._note_browser_error(e, "dialer readiness check")
            return False

    def _do_hangup(self):
        """Press the hangup button."""
        try:
            # 1. Fast JS sniper.
            if self.driver.execute_script(
                """
                var btn = document.querySelector("button[gv-test-id='in-call-end-call']")
                    || document.querySelector("button[aria-label='Hang up call']")
                    || document.querySelector("[aria-label='End call']")
                    || document.querySelector("[data-tooltip='End call']");
                if (btn) { btn.click(); return true; }
                return false;
                """
            ):
                logger.info(f"Worker {self.worker_id}: call ended via JS sniper")
                return

            # 2. Selenium fallback.
            for sel in [
                "button[gv-test-id='in-call-end-call']",
                "button[aria-label='Hang up call']",
                "[aria-label='End call']",
                "[data-tooltip='End call']",
                "button[aria-label*='end' i]",
            ]:
                try:
                    el = self.driver.find_element(By.CSS_SELECTOR, sel)
                    if el and el.is_displayed():
                        el.click()
                        logger.info(f"Worker {self.worker_id}: call ended via selector: {sel}")
                        return
                except Exception:
                    continue

            # Fallback: reload page to force-end call
            try:
                self.driver.get("https://voice.google.com/u/0/calls")
                logger.info(f"Worker {self.worker_id}: force-ended via page reload")
                return
            except Exception:
                pass
            logger.warning(f"Worker {self.worker_id}: hangup failed completely")
        except Exception as e:
            self._note_browser_error(e, "hangup")
            logger.error(f"Worker {self.worker_id}: hangup error — {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # Call state monitoring
    # ──────────────────────────────────────────────────────────────────────────

    async def wait_for_connect(self, call_id: str, timeout: int = 60) -> bool:
        """Block until the call is answered (connected) or times out."""
        if call_id not in self._active_calls:
            return False

        loop = asyncio.get_running_loop()
        start_time = time.time()
        consecutive_active = 0
        logger.info(f"Worker {self.worker_id}: waiting for call {call_id} to be answered…")

        while True:
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                logger.warning(
                    f"Worker {self.worker_id}: call {call_id} connection timeout ({timeout}s)"
                )
                return False

            status = await loop.run_in_executor(None, self.check_call_status)

            if status == "connected":
                logger.info(f"Worker {self.worker_id}: call {call_id} answered ✅")
                self._active_calls[call_id]["status"] = "connected"
                return True

            if status == "active":
                consecutive_active += 1
                self._active_calls[call_id]["status"] = "active"
                # GV occasionally fails to expose the timer even after answer.
                # If the in-call UI stays active and no ringing state is reported
                # for several polls, treat it as answered instead of timing out.
                if elapsed >= 6 and consecutive_active >= 4:
                    logger.info(
                        f"Worker {self.worker_id}: call {call_id} active without timer "
                        f"(assuming answered at {elapsed:.1f}s)"
                    )
                    self._active_calls[call_id]["status"] = "connected"
                    return True
            else:
                consecutive_active = 0

            # Ignore early false-ended spikes for the first few seconds.
            if status in ("ended", "force_ended", "disconnected") and elapsed > 8:
                logger.info(
                    f"Worker {self.worker_id}: call {call_id} {status} before answer "
                    f"(confirmed at {elapsed:.1f}s)"
                )
                return False

            if status == "ringing":
                self._active_calls[call_id]["status"] = "ringing"

            poll_delay = 0.4 if elapsed < 10 else 0.8
            await asyncio.sleep(poll_delay)

    def check_call_status(self) -> str:
        """
        Return current call state via a single JS probe.
        States: 'ringing' | 'connected' | 'active' | 'ended' | 'unknown'
        """
        if not self.driver:
            return "unknown"
        try:
            status = self.driver.execute_script(
                """
                const endSelectors = [
                    "button[gv-test-id='in-call-end-call']",
                    "button[aria-label='Hang up call']",
                    "[aria-label='End call']",
                    "[data-tooltip='End call']",
                ];
                const dialerSelectors = [
                    "input[placeholder*='name or number' i]",
                    "input[placeholder*='phone' i]",
                    "input[aria-label*='phone' i]",
                    "input[type='tel']",
                    "[gv-a11y-grid]",
                    "button[aria-label='Make a call']",
                    "button[data-tooltip='Make a call']",
                    "button[aria-label='Dialpad']",
                ];
                const timerSelectors = [
                    "gv-in-call-status [gv-test-id='in-call-callduration']",
                    "[gv-test-id='in-call-callduration']",
                    ".call-duration",
                    ".call-timer",
                    "[class*='duration']",
                    "[class*='timer']",
                ];
                const statusSelectors = [
                    "gv-in-call-status",
                    ".status",
                    ".remote-display-subtitle",
                    "[role='status']",
                    "[aria-live='polite']",
                ];
                const controlSelectors = [
                    "button[aria-label*='mute' i]",
                    "button[aria-label*='keypad' i]",
                    "button[aria-label*='dialpad' i]",
                    "button[aria-label*='speaker' i]",
                ];
                const errorSelectors = [
                    "div.error-message",
                    "[role='alert']",
                    "[aria-live='assertive']",
                ];
                const isVisible = (el) => {
                    if (!el) return false;
                    return el.offsetParent !== null
                        || (el.getClientRects && el.getClientRects().length > 0);
                };
                const anyVisible = (selectors) => {
                    for (const selector of selectors) {
                        if (isVisible(document.querySelector(selector))) return true;
                    }
                    return false;
                };
                const collectText = (selectors) => {
                    const parts = [];
                    for (const selector of selectors) {
                        for (const el of document.querySelectorAll(selector)) {
                            if (!isVisible(el)) continue;
                            const txt = (el.innerText || el.textContent || "").trim().toLowerCase();
                            if (txt) parts.push(txt);
                        }
                    }
                    return parts.join(" ");
                };

                if (!anyVisible(endSelectors)) {
                    if (anyVisible(dialerSelectors)) return "force_ended";
                    return "ended";
                }

                const errorText = collectText(errorSelectors);
                if (/(your call was disconnected|call failed|couldn.t complete|try again)/.test(errorText)) {
                    return "disconnected";
                }

                const timerText = collectText(timerSelectors);
                if (/\\b\\d{1,2}:\\d{2}(?::\\d{2})?\\b/.test(timerText)) {
                    return "connected";
                }

                const statusText = collectText(statusSelectors.concat(timerSelectors));
                if (/(^|\\s)(calling|ringing|connecting)(\\s|$)/.test(statusText)) return "ringing";
                if (/(^|\\s)(connected|in call|call in progress|live)(\\s|$)/.test(statusText)) {
                    return "connected";
                }
                if (anyVisible(controlSelectors)) return "active";

                return "active";
                """
            )
            return status if status in {"ended", "ringing", "connected", "active", "disconnected", "force_ended"} else "unknown"
        except Exception as e:
            self._note_browser_error(e, "call status check")
            logger.debug(f"Worker {self.worker_id}: call status check error — {e}")
            return "unknown"

    async def monitor_call(self, call_id: str, on_end_callback=None):
        """Monitor call and fire callback when it ends (confirmed 3× consecutive ended)."""
        if call_id not in self._active_calls:
            return

        loop = asyncio.get_running_loop()
        consecutive_ended = 0
        required = 3

        await asyncio.sleep(2)

        while call_id in self._active_calls:
            status = await loop.run_in_executor(None, self.check_call_status)
            if status in ("ended", "force_ended", "disconnected"):
                consecutive_ended += 1
                if consecutive_ended >= required:
                    logger.info(f"Worker {self.worker_id}: call {call_id} {status} (confirmed)")
                    self._active_calls.pop(call_id, None)
                    if on_end_callback:
                        await on_end_callback(call_id)
                    break
            else:
                if consecutive_ended > 0:
                    consecutive_ended = 0
                if status == "connected":
                    if self._active_calls.get(call_id, {}).get("status") != "connected":
                        self._active_calls[call_id]["status"] = "connected"
                        logger.info(f"Worker {self.worker_id}: call {call_id} connected")
            await asyncio.sleep(2)

    # ──────────────────────────────────────────────────────────────────────────
    # Health
    # ──────────────────────────────────────────────────────────────────────────

    def get_active_calls(self) -> dict:
        return self._active_calls.copy()

    def get_health_snapshot(self) -> dict:
        return {
            "authenticated": bool(self.is_authenticated),
            "phone_number": self.phone_number,
            "last_error": self.last_error,
            "browser_start_seconds": round(self.browser_start_seconds, 2),
            "login_seconds": round(self.login_seconds, 2),
            "number_fetch_seconds": round(self.number_fetch_seconds, 2),
            "calls_served": self.calls_served,
            "rss_mb": self.last_rss_mb,
            "heap_pct": self.last_heap_pct,
            "restart_count": self.restart_count,
            "oom_events": self.oom_events,
            "login_state": self.login_state,
            "login_outcome": self.login_outcome,
            "login_detail": self.login_detail,
            "account_email": self.account_email or self.credential_email,
            "has_credentials": self.has_credentials(),
            "profile": self.profile_dir.name if self.profile_dir else None,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Cross-profile spam probe (no answer — just ring, then inspect history)
    # ──────────────────────────────────────────────────────────────────────────

    def _get_account_email_sync(self) -> Optional[str]:
        """Best-effort scrape of the logged-in Google account email."""
        if not self.driver:
            return None
        try:
            return self.driver.execute_script(
                r"""
                const re = /[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}/;
                const sels = [
                    "a[aria-label*='Google Account' i]",
                    "a[href*='SignOutOptions']",
                    "[data-email]",
                    "[aria-label*='@']",
                    "img[alt*='@']",
                ];
                for (const s of sels) {
                    for (const el of document.querySelectorAll(s)) {
                        for (const v of [
                            el.getAttribute && el.getAttribute('aria-label'),
                            el.getAttribute && el.getAttribute('data-email'),
                            el.getAttribute && el.getAttribute('alt'),
                            el.getAttribute && el.getAttribute('title'),
                            el.textContent,
                        ]) {
                            if (v) { const m = v.match(re); if (m) return m[0]; }
                        }
                    }
                }
                const body = document.body ? document.body.innerText : '';
                const m2 = body.match(re);
                return m2 ? m2[0] : null;
                """
            )
        except Exception as e:
            logger.debug(f"Worker {self.worker_id}: account email scrape error — {e}")
            return None

    async def fetch_account_email(self) -> Optional[str]:
        """Return (and cache) the logged-in Google account email."""
        if self.account_email:
            return self.account_email
        if not self.driver:
            return None
        loop = asyncio.get_running_loop()
        email = await loop.run_in_executor(None, self._get_account_email_sync)
        self.account_email = email
        return email

    def _scan_history_for_spam_sync(self, caller_number: str) -> str:
        """Scan the visible call history for a spam label on caller_number.

        Returns 'spam' | 'clean' | 'unknown'.
        """
        if not self.driver:
            return "unknown"
        digits = "".join(filter(str.isdigit, caller_number or ""))[-7:]
        if len(digits) < 7:
            return "unknown"
        try:
            result = self.driver.execute_script(
                r"""
                const digits = arguments[0];
                const spamRe =
                    /(suspected spam|spam likely|potential spam|suspected caller|likely spam)/i;

                const containers = document.querySelectorAll(
                    "gv-call-row, gv-text-thread-item, [role='listitem'], " +
                    "[gv-test-id*='call'], li, tr"
                );
                for (const row of containers) {
                    const t = (row.innerText || row.textContent || "");
                    if (!t) continue;
                    if (t.replace(/\D/g, '').indexOf(digits) !== -1 && spamRe.test(t)) {
                        return 'spam';
                    }
                }
                // Badge / aria-label fallback near the matching row.
                for (const el of document.querySelectorAll("[aria-label],[title]")) {
                    const lab = (el.getAttribute('aria-label') || '') + ' ' +
                                (el.getAttribute('title') || '');
                    if (!spamRe.test(lab)) continue;
                    const box = el.closest(
                        "gv-call-row, [role='listitem'], li, tr"
                    ) || el;
                    const around = (box.innerText || box.textContent || "");
                    if (around.replace(/\D/g, '').indexOf(digits) !== -1) {
                        return 'spam';
                    }
                }
                // Number present in history but no spam marker → clean.
                for (const row of containers) {
                    const t = (row.innerText || row.textContent || "");
                    if (t && t.replace(/\D/g, '').indexOf(digits) !== -1) {
                        return 'clean';
                    }
                }
                return 'unknown';
                """,
                digits,
            )
            return result if result in ("spam", "clean", "unknown") else "unknown"
        except Exception as e:
            logger.debug(f"Worker {self.worker_id}: spam scan error — {e}")
            return "unknown"

    def _dump_debug_sync(self, caller_number: str, tag: str) -> dict:
        """Save screenshot + HTML of the current page (stable 3-arg signature)."""
        import time as _t
        out = {"screenshot": None, "html": None}
        if not self.driver:
            return out
        ts = _t.strftime("%Y%m%d_%H%M%S")
        digits = "".join(filter(str.isdigit, caller_number or ""))[-7:]
        base = config.BASE_DIR / f"spamdbg_w{self.worker_id}_{tag}_{digits}_{ts}"
        try:
            self.driver.save_screenshot(str(base) + ".png")
            out["screenshot"] = str(base) + ".png"
        except Exception as e:
            logger.debug(f"Worker {self.worker_id}: debug screenshot failed — {e}")
        try:
            html = self.driver.page_source or ""
            (config.BASE_DIR / (base.name + ".html")).write_text(
                html, encoding="utf-8", errors="ignore"
            )
            out["html"] = str(base) + ".html"
        except Exception as e:
            logger.debug(f"Worker {self.worker_id}: debug html dump failed — {e}")
        logger.info(
            f"Worker {self.worker_id}: spam-debug saved "
            f"({out['screenshot']}, {out['html']})"
        )
        return out

    async def check_incoming_spam(
        self,
        caller_number: str,
        settle_sec: float = 4.0,
        refresh: bool = True,
        debug: bool = False,
    ) -> str:
        """After a missed inbound call, inspect THIS profile's history for a
        'Suspected spam' label on caller_number. Returns 'spam'|'clean'|'unknown'.

        debug=True also dumps a screenshot + page HTML so the history
        selectors can be verified against the live Google Voice DOM.
        """
        self._last_spam_debug = None
        if not self.driver:
            return "unknown"
        loop = asyncio.get_running_loop()
        await asyncio.sleep(settle_sec)  # let GV register the missed call

        if refresh:
            try:
                await loop.run_in_executor(
                    None,
                    lambda: self.driver.get("https://voice.google.com/u/0/calls"),
                )
                await asyncio.sleep(2.0)
            except Exception:
                pass

        verdict = "unknown"
        for _ in range(3):
            verdict = await loop.run_in_executor(
                None, lambda: self._scan_history_for_spam_sync(caller_number)
            )
            if verdict in ("spam", "clean"):
                break
            await asyncio.sleep(1.5)

        # Scan completed but caller number not present anywhere in the
        # receiver's history → the call never registered on the callee.
        if verdict == "unknown":
            verdict = "absent"

        # ALWAYS capture a screenshot of the RECEIVER's GV page so the
        # operator can eyeball the number's status in the browser.
        self._last_spam_debug = await loop.run_in_executor(
            None, lambda: self._dump_debug_sync(caller_number, f"receiver_{verdict}")
        )
        return verdict

    # Chrome keeps its cookie database under the profile's Default directory.
    # The Network/ subdirectory is the modern layout; the flat file is the old
    # one. Both are checked so the cleanup works across Chrome versions.
    COOKIE_DB_RELPATHS = (
        Path("Default") / "Network" / "Cookies",
        Path("Default") / "Cookies",
    )

    def _clear_cookies_in_browser(self) -> bool:
        """Drop every cookie through DevTools, across all domains."""
        if not self.driver:
            return False
        try:
            self.driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
            return True
        except Exception as e:
            logger.debug(
                f"Worker {self.worker_id}: CDP cookie clear failed ({e}) — "
                f"falling back to delete_all_cookies()"
            )
        try:
            # Selenium's own call only covers the currently open domain.
            self.driver.delete_all_cookies()
            return True
        except Exception as e:
            logger.warning(f"Worker {self.worker_id}: in-browser cookie clear failed: {e}")
            return False

    def _delete_cookie_files(self) -> list:
        """Delete the on-disk cookie databases, leaving the profile in place."""
        removed = []
        for rel in self.COOKIE_DB_RELPATHS:
            base = self.profile_dir / rel
            for path in (base, base.with_name(base.name + "-journal")):
                try:
                    if path.exists():
                        path.unlink()
                        removed.append(str(path.relative_to(self.profile_dir)))
                except Exception as e:
                    logger.warning(
                        f"Worker {self.worker_id}: could not delete {path}: {e}"
                    )
        return removed

    def wipe_cookies(self, reason: str = ""):
        """Clear this profile's cookies and leave an empty slot for /login.

        Used after spam and after a mid-run logout / dead Google session.
        Only the cookies go: the profile directory itself survives.
        """
        wid = getattr(self, "worker_id", "?")
        number = self.phone_number
        detail = reason or (
            f"previous number {number} flagged as spam — cookies cleared"
            if number else
            "cookies cleared — waiting for /login"
        )

        try:
            from telephony.wipe_archive import snapshot_worker
            snapshot_worker(self, detail, kind="wipe")
        except Exception:
            pass

        # Clear through DevTools while the session is alive, then again on disk
        # after the browser closes, in case the live call did not land.
        cleared_live = self._clear_cookies_in_browser()
        self.close()
        removed = self._delete_cookie_files()

        # The burned account is gone. This worker is now a vacant login slot —
        # not a permanently dead spam/ban profile.
        self.is_spam_flagged = False
        self.is_banned = False
        self.is_authenticated = False
        self.phone_number = None
        self.account_email = None
        self.clear_credentials()
        self.login_state = "needs_login"
        self.login_outcome = None
        self.login_detail = detail
        self.last_error = detail
        self.autologin_attempts = 0
        self._gv_audio_configured = False

        if cleared_live or removed:
            logger.warning(
                f"Worker {wid}: cookies CLEARED"
                f"{' for ' + number if number else ''} — profile kept at "
                f"{self.profile_dir}, waiting for /login "
                f"(devtools={cleared_live}, files={removed or 'none'})"
            )
        else:
            logger.error(
                f"Worker {wid}: could not clear cookies for {number} — "
                f"the session may survive at {self.profile_dir}"
            )

    def reset_to_login_page(self, reason: str = "", keep_credentials: bool = True):
        """Close Chrome, drop cookies, keep the Google login for a later /login.

        Used after a reCAPTCHA: the sticky robot-check session has to die, but
        the operator's password must stay so the slot can be signed in again.
        """
        wid = getattr(self, "worker_id", "?")
        email = self.account_email or self.credential_email
        saved = dict(self.credentials) if keep_credentials and self.credentials else None
        outcome = self.login_outcome
        try:
            from telephony.wipe_archive import snapshot_worker
            snapshot_worker(self, reason or "restarted onto the login page", kind="captcha")
        except Exception:
            pass
        self._clear_cookies_in_browser()
        self.close()
        self._delete_cookie_files()
        self.is_authenticated = False
        self.phone_number = None
        self._gv_audio_configured = False
        if saved:
            self.credentials = saved
            self.account_email = email
        else:
            self.clear_credentials()
            self.account_email = None
        self.login_state = "needs_login"
        self.login_outcome = outcome
        self.login_detail = reason or self.login_detail
        self.last_error = reason or self.last_error
        logger.warning(
            f"Worker {wid}: restarted onto the login page"
            f"{' (' + email + ')' if email else ''}"
            f"{' — ' + reason if reason else ''}"
        )

    def close(self):
        """Quit the browser and reset state.

        A sign-in driving this browser owns it — closing now would kill the
        form mid-login, so the quit is skipped and left to the owner.
        """
        if self._login_running:
            logger.warning(
                f"Worker {self.worker_id}: close skipped — sign-in in progress"
            )
            return
        handler = getattr(self, "_p1_handler", None)
        if handler is not None:
            try:
                # Synchronous cleanup: set flags to stop loops, close websocket.
                # Avoids the cross-event-loop issue with asyncio.run() in threads.
                handler._listening = False
                handler._stt_connected = False
                handler._prepared = False
                handler._vm_detection_active = False
                handler._response_detection_active = False
                try:
                    handler.audio.stop()
                except Exception:
                    pass
                # Close STT websocket synchronously if possible
                ws = getattr(handler, "_stt_ws", None)
                if ws is not None:
                    try:
                        asyncio.get_running_loop()
                    except RuntimeError:
                        # No loop running — safe to use asyncio.run for ws.close()
                        try:
                            asyncio.run(ws.close())
                        except Exception:
                            pass
                    # If loop IS running, just set flags; the tasks will self-terminate
                handler._stt_ws = None
            except Exception as e:
                logger.debug(f"Worker {self.worker_id}: failed to stop warm P1 handler: {e}")
            setattr(self, "_p1_handler", None)
            setattr(self, "_p1_handler_profile", None)
            setattr(self, "_p1_handler_warm", False)

        if self.driver or self._dead_driver:
            for attr in ("driver", "_dead_driver"):
                drv = getattr(self, attr, None)
                if drv is None:
                    continue
                try:
                    drv.quit()
                except Exception:
                    pass
                setattr(self, attr, None)
            # quit() cannot reap a crashed browser, so sweep the profile too.
            self._kill_profile_processes()
            logger.info(f"Worker {self.worker_id}: browser closed")
            self.is_authenticated = False
            self.phone_number = None
            self._active_calls.clear()
