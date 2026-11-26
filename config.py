"""
Configuration management for AI Caller.
Loads settings from environment variables.
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from loguru import logger

# Pin BLAS/OpenMP to 1 thread BEFORE numpy/soxr import. With 8 workers each
# spawning audio subprocesses, multi-threaded BLAS oversubscribes weak CPUs.
for _thr_env in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    os.environ.setdefault(_thr_env, "1")

# ── Force websockets / aiohttp / urllib3 to use certifi's CA bundle on
# Windows Server boxes where Python's ssl module doesn't have a usable
# root store (causes 'CERTIFICATE_VERIFY_FAILED: unable to get local
# issuer certificate' when connecting to Deepgram). Runs once at import.
try:
    import certifi as _certifi
    _ca = _certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", _ca)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca)
    os.environ.setdefault("CURL_CA_BUNDLE", _ca)
except Exception:
    pass


def _get_int_env(name: str, default: int) -> int:
    """Read an integer env var with a safe fallback."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"Invalid integer for {name}={raw!r}; using default {default}")
        return default


def _get_float_env(name: str, default: float) -> float:
    """Read a float env var with a safe fallback."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"Invalid float for {name}={raw!r}; using default {default}")
        return default


# Load .env file
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

# Telegram Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_REQUEST_POOL_SIZE = _get_int_env("TELEGRAM_REQUEST_POOL_SIZE", 128)
TELEGRAM_UPDATES_POOL_SIZE = _get_int_env("TELEGRAM_UPDATES_POOL_SIZE", 16)
TELEGRAM_CONNECT_TIMEOUT_SEC = _get_float_env("TELEGRAM_CONNECT_TIMEOUT_SEC", 12.0)
TELEGRAM_READ_TIMEOUT_SEC = _get_float_env("TELEGRAM_READ_TIMEOUT_SEC", 30.0)
TELEGRAM_WRITE_TIMEOUT_SEC = _get_float_env("TELEGRAM_WRITE_TIMEOUT_SEC", 30.0)
TELEGRAM_POOL_TIMEOUT_SEC = _get_float_env("TELEGRAM_POOL_TIMEOUT_SEC", 8.0)
TELEGRAM_MEDIA_WRITE_TIMEOUT_SEC = _get_float_env("TELEGRAM_MEDIA_WRITE_TIMEOUT_SEC", 60.0)
# Optional SOCKS/HTTP proxy for api.telegram.org, e.g. socks5://127.0.0.1:1080
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip()
# Broken IPv6 to api.telegram.org is a common Ubuntu hang (exactly 30s TimedOut).
# Default on: Linux. Set TELEGRAM_FORCE_IPV4=0 to allow IPv6.
_TELEGRAM_IPV4_DEFAULT = "1" if os.name != "nt" else "0"
TELEGRAM_FORCE_IPV4 = os.getenv(
    "TELEGRAM_FORCE_IPV4", _TELEGRAM_IPV4_DEFAULT
).strip().lower() not in ("0", "false", "no", "off", "")
# Local Bot API server, if used. Example: http://127.0.0.1:8081/bot
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "").strip()

# Telephony Configuration

# Deepgram API Key (for Voice Agent API)
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")


def _parse_key_list(raw: str) -> list:
    import re as _re
    return [k.strip() for k in _re.split(r"[,\s;]+", raw or "") if k.strip()]


# Optional pool of Deepgram keys (comma/space separated). The first key is
# used until Deepgram rejects it, then the pool rotates automatically.
DEEPGRAM_API_KEYS = _parse_key_list(os.getenv("DEEPGRAM_API_KEYS", ""))
if not DEEPGRAM_API_KEYS and DEEPGRAM_API_KEY:
    DEEPGRAM_API_KEYS = [DEEPGRAM_API_KEY]
elif DEEPGRAM_API_KEYS and not DEEPGRAM_API_KEY:
    DEEPGRAM_API_KEY = DEEPGRAM_API_KEYS[0]

# Deepgram Voice Agent model configuration (optional overrides)
DG_LISTEN_MODEL = os.getenv("DG_LISTEN_MODEL", "nova-3")
DG_LISTEN_VERSION = os.getenv("DG_LISTEN_VERSION", "")
DG_THINK_PROVIDER = os.getenv("DG_THINK_PROVIDER", "open_ai")
DG_THINK_MODEL = os.getenv("DG_THINK_MODEL", "gpt-4o-mini")
DG_THINK_TEMPERATURE = os.getenv("DG_THINK_TEMPERATURE", "")
DG_THINK_ENDPOINT_URL = os.getenv("DG_THINK_ENDPOINT_URL", "")
DG_THINK_ENDPOINT_HEADERS = os.getenv("DG_THINK_ENDPOINT_HEADERS", "")
DG_SPEAK_MODEL = os.getenv("DG_SPEAK_MODEL", "aura-2-helena-en")

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Max phone numbers accepted from one .txt bulk upload / batch
# 0 = no cap on bulk .txt uploads (the old 500 default is gone).
MAX_BATCH_SIZE = _get_int_env("MAX_BATCH_SIZE", 0)

# Telephony stability and startup tuning
CHROME_VERSION_MAIN = os.getenv("CHROME_VERSION_MAIN", "").strip()
# Chrome major version assumed when CHROME_VERSION_MAIN is not set in .env.
# Keep equal to the bundled chromedriver's major version.
CHROME_VERSION_MAIN_DEFAULT = _get_int_env("CHROME_VERSION_MAIN_DEFAULT", 146)
GV_STARTUP_TIMEOUT_SEC = _get_int_env("GV_STARTUP_TIMEOUT_SEC", 120)
GV_WORKER_ACQUIRE_TIMEOUT_SEC = _get_int_env("GV_WORKER_ACQUIRE_TIMEOUT_SEC", 60)
GV_NUMBER_FETCH_RETRIES = _get_int_env("GV_NUMBER_FETCH_RETRIES", 8)
GV_INIT_RETRY_COUNT = _get_int_env("GV_INIT_RETRY_COUNT", 3)
GV_INIT_RETRY_DELAY_SEC = _get_int_env("GV_INIT_RETRY_DELAY_SEC", 30)
# Max parallel GV logins after Chrome spawn (serialize UC spawn separately).
# On 4–6 vCPU boxes, unbounded parallel login thrashes CPU/RAM with 8 workers.
GV_INIT_LOGIN_CONCURRENCY = _get_int_env("GV_INIT_LOGIN_CONCURRENCY", 2)
# soxr quality for live capture/TTS: QQ/LQ/MQ/HQ/VHQ. LQ is best for weak CPUs.
AUDIO_RESAMPLE_QUALITY = os.getenv("AUDIO_RESAMPLE_QUALITY", "LQ").strip() or "LQ"
# Capture process: larger blocks + slower poll = less CPU on oversubscribed hosts.
AUDIO_CAPTURE_BLOCKSIZE = _get_int_env("AUDIO_CAPTURE_BLOCKSIZE", 4096)
AUDIO_CAPTURE_POLL_SEC = _get_float_env("AUDIO_CAPTURE_POLL_SEC", 0.012)
AUDIO_CAPTURE_SEND_INTERVAL_SEC = _get_float_env("AUDIO_CAPTURE_SEND_INTERVAL_SEC", 0.080)
# Extra Chrome low-end flags for multi-worker on weak CPUs (1=on).
GV_LOW_END_MODE = os.getenv("GV_LOW_END_MODE", "1").strip().lower() not in (
    "0", "false", "no", "off", ""
)

# ── Anti-hang / self-heal (agent + pool) ──
# Selenium page load / script caps (prevents infinite driver.get hangs).
GV_PAGE_LOAD_TIMEOUT_SEC = _get_int_env("GV_PAGE_LOAD_TIMEOUT_SEC", 25)
GV_SCRIPT_TIMEOUT_SEC = _get_int_env("GV_SCRIPT_TIMEOUT_SEC", 15)
# Dialer recovery + hangup must finish within these budgets.
GV_RECOVERY_TIMEOUT_SEC = _get_float_env("GV_RECOVERY_TIMEOUT_SEC", 20.0)
GV_HANGUP_TIMEOUT_SEC = _get_float_env("GV_HANGUP_TIMEOUT_SEC", 15.0)
GV_RELEASE_TIMEOUT_SEC = _get_float_env("GV_RELEASE_TIMEOUT_SEC", 25.0)
# Checked-out worker older than this is force-reclaimed (seconds).
GV_WORKER_STUCK_SEC = _get_int_env("GV_WORKER_STUCK_SEC", 240)

# ── Browser memory guard (Chrome "out of memory" self-heal) ──
# V8 old-space cap for each worker tab. The Google Voice SPA needs real
# headroom: a cap that is too low makes the renderer crash with "out of
# memory" mid-run, while no cap at all lets one leaking tab eat the host.
GV_JS_HEAP_MB = _get_int_env("GV_JS_HEAP_MB", 512)
# Soft recycle: reload the dialer tab after this many calls, which discards
# the call-history DOM and listeners the SPA accumulates. 0 disables.
GV_RECYCLE_AFTER_CALLS = _get_int_env("GV_RECYCLE_AFTER_CALLS", 25)
# Hard recycle: respawn Chrome after this many calls. 0 disables.
GV_RESTART_AFTER_CALLS = _get_int_env("GV_RESTART_AFTER_CALLS", 200)
# Reload the tab once the V8 heap reaches this share of its own limit.
GV_JS_HEAP_SOFT_PCT = _get_int_env("GV_JS_HEAP_SOFT_PCT", 70)
# Per-worker Chrome process-tree RSS budget (MB): soft → reload, hard → respawn.
# Needs psutil; without it only the JS-heap and call counters apply.
GV_WORKER_RSS_SOFT_MB = _get_int_env("GV_WORKER_RSS_SOFT_MB", 900)
GV_WORKER_RSS_HARD_MB = _get_int_env("GV_WORKER_RSS_HARD_MB", 1400)
# Skip *proactive* respawns while the host has less free RAM than this (MB) —
# spawning a second Chrome during a memory crunch is what invites the OOM killer.
GV_HOST_FREE_MB_MIN = _get_int_env("GV_HOST_FREE_MB_MIN", 700)
# Give up on a worker after this many failed browser restarts.
GV_MAX_RESTARTS = _get_int_env("GV_MAX_RESTARTS", 5)
# Parallel browser restarts. Keep low: each respawn costs CPU and RAM.
GV_REVIVE_CONCURRENCY = _get_int_env("GV_REVIVE_CONCURRENCY", 1)
# Hard wall-clock for one P1 call (connect + IVR + slack).
P1_CALL_HARD_TIMEOUT_SEC = _get_int_env("P1_CALL_HARD_TIMEOUT_SEC", 210)
# How often agent watchdog scans for stuck workers / zombie p1.
AGENT_WATCHDOG_SEC = _get_int_env("AGENT_WATCHDOG_SEC", 20)

# ── Automatic Google sign-in ──
# A worker whose profile has no live Google session signs itself in, provided
# the operator supplied that account's password (/login on the controller).
GV_AUTOLOGIN = os.getenv("GV_AUTOLOGIN", "1").strip().lower() not in (
    "0", "false", "no", "off", ""
)
# Wall clock for one sign-in (landing page → form → dialer).
GV_LOGIN_TIMEOUT_SEC = _get_int_env("GV_LOGIN_TIMEOUT_SEC", 120)
# Session state (signed-in / needs-login / expired) is read from the page the
# instant it is conclusive. Only an in-transit page — voice.google.com still
# painting its Angular dialer — is re-probed, and only up to this budget. This
# is what replaced the old 90s blind wait, so keep it small.
GV_SESSION_PROBE_SEC = _get_float_env("GV_SESSION_PROBE_SEC", 12.0)
# Poll interval while waiting for that SPA to finish rendering.
GV_SESSION_SETTLE_SEC = _get_float_env("GV_SESSION_SETTLE_SEC", 0.35)
# Parallel sign-ins per agent. Each worker has its own browser, so this is real
# concurrency; keep it low — several accounts authenticating at once from one IP
# is what makes Google start asking for verification codes.
GV_LOGIN_CONCURRENCY = _get_int_env("GV_LOGIN_CONCURRENCY", 2)
# Give up on a profile after this many sign-in attempts. A rejected password or
# a 2FA prompt is never retried at all — that is how accounts get locked.
GV_MAX_LOGIN_ATTEMPTS = _get_int_env("GV_MAX_LOGIN_ATTEMPTS", 3)
# On-agent wipe archive: screenshot + reason before a slot is vacated.
# Files stay on the agent (wipe_history/). Old shots are pruned.
GV_WIPE_HISTORY_KEEP = _get_int_env("GV_WIPE_HISTORY_KEEP", 12)
WIPE_HISTORY_DIR = os.getenv("GV_WIPE_HISTORY_DIR", "").strip()
# VAC cable recording: capture (remote) + playback (our TTS). Non-P1 files are
# deleted when the call ends; P1 keeps them until uploaded to Telegram.
GV_CALL_RECORD = os.getenv("GV_CALL_RECORD", "1").strip().lower() not in (
    "0", "false", "no", "off", ""
)
P1_SEND_RECORDINGS = os.getenv("GV_P1_SEND_RECORDINGS", "1").strip().lower() not in (
    "0", "false", "no", "off", ""
)
# Debug toggle: upload the call recording to Telegram after EVERY call,
# not just hot hits (results 1/2). Remote-settable via the bot settings.
P1_SEND_ALL_RECORDINGS = os.getenv("GV_P1_SEND_ALL_RECORDINGS", "0").strip().lower() not in (
    "0", "false", "no", "off", ""
)
# Which p1 results get their recording uploaded (comma-separated result
# names). Remote-settable via the bot settings. Examples: "1,2", "hangup",
# "1,2,hangup". P1_SEND_ALL_RECORDINGS=1 overrides this list.
P1_SEND_RECORDING_RESULTS = tuple(
    s.strip() for s in os.getenv("GV_P1_SEND_RECORDING_RESULTS", "1,2").split(",")
    if s.strip()
)


def p1_send_recording_results() -> tuple:
    """Effective set of p1 results whose recordings are uploaded.

    Reads the live module attribute (a remote settings push replaces it with
    a raw comma-separated string), so changes apply without a restart.
    """
    raw = globals().get("P1_SEND_RECORDING_RESULTS", ("1", "2"))
    if isinstance(raw, str):
        return tuple(s.strip() for s in raw.split(",") if s.strip())
    try:
        return tuple(raw)
    except TypeError:
        return ("1", "2")
# Post-call audio verification of 'hangup' results: the callee channel of the
# call recording is re-transcribed (Deepgram prerecorded) and the result is
# upgraded when Google's account-banned intercept is found. This is the second,
# independent safety net behind live STT (which can miss a phrase on a
# websocket reconnect or a final that arrives after the far end hangs up).
# Costs one prerecorded STT request per hangup. Remote-settable.
P1_HANGUP_AUDIO_RECHECK = os.getenv("GV_P1_HANGUP_AUDIO_RECHECK", "1").strip().lower() not in (
    "0", "false", "no", "off", ""
)
# Parallel outbound calls per GV account on the SIP backend. Each stream is
# a full independent line (own SIP registration, media, STT, recording) —
# the dual-call test proved 2 work from one account. Clamped to 1–4 at the
# SipPool. Remote-settable via the bot settings; applies to the next campaign.
SIP_STREAMS_PER_ACCOUNT = _get_int_env("GV_SIP_STREAMS_PER_ACCOUNT", 1)
# Delay between workers' first dials of a campaign (ms). A simultaneous burst
# of INVITEs + DTLS handshakes at t=0 reliably kills a few media sessions
# (observed: 'DTLS handshake failed (connection error)' on start bursts).
P1_START_STAGGER_MS = _get_int_env("GV_P1_START_STAGGER_MS", 250)
P1_RECORD_MAX_MB = _get_int_env("GV_P1_RECORD_MAX_MB", 3)
CALL_RECORD_DIR = os.getenv("GV_CALL_RECORD_DIR", "").strip()
# Cache credentials on the agent so a restart can sign in without the operator.
# WARNING: this writes Google passwords to disk in plain text (mode 0600).
GV_SAVE_CREDENTIALS = os.getenv("GV_SAVE_CREDENTIALS", "0").strip().lower() not in (
    "0", "false", "no", "off", ""
)

# Set GV_HEADLESS=0 (or false/no) in .env to launch VISIBLE Chrome windows
# for debugging — you can then watch what each profile does during a call.
GV_HEADLESS = os.getenv("GV_HEADLESS", "1").strip().lower() not in (
    "0", "false", "no", "off", ""
)

# ── Distributed mode (controller / agent over Redis) ──
# standalone : current single-box behaviour (default, no Redis needed)
# controller : runs the Telegram bot, dispatches jobs, aggregates agents
# agent      : runs a local PhonePool, registers with Redis, takes jobs
import socket as _socket

NODE_ROLE = os.getenv("NODE_ROLE", "standalone").strip().lower()
REDIS_URL = os.getenv("REDIS_URL", "").strip()
AGENT_ID = os.getenv("AGENT_ID", "").strip() or _socket.gethostname()
AGENT_HEARTBEAT_SEC = _get_int_env("AGENT_HEARTBEAT_SEC", 10)
AGENT_TTL_SEC = _get_int_env("AGENT_TTL_SEC", 35)

# Paths
BASE_DIR = Path(__file__).parent
PROMPTS_FILE = BASE_DIR / "storage" / "prompts.json"
SCRIPT_TEMPLATES_FILE = BASE_DIR / "storage" / "script_templates.json"
CHROME_PROFILE_DIR = BASE_DIR / ".chrome_profile"
# Only written when GV_SAVE_CREDENTIALS=1 (see the warning above).
GV_ACCOUNTS_FILE = Path(
    os.getenv("GV_ACCOUNTS_FILE", "").strip()
    or BASE_DIR / "storage" / "gv_accounts.json"
)


def _default_chromedriver_path() -> Path:
    """Bundled chromedriver location (overridable via CHROMEDRIVER_PATH env)."""
    if os.name == "nt":
        return BASE_DIR / "chromedriver-win64" / "chromedriver.exe"
    return BASE_DIR / "chromedriver-linux64" / "chromedriver"


def resolve_chromedriver_path() -> Path:
    """Resolve the chromedriver binary path (env override → bundled default)."""
    raw = os.getenv("CHROMEDRIVER_PATH", "").strip()
    return Path(raw) if raw else _default_chromedriver_path()


def resolve_chrome_version_main() -> int:
    """Pinned Chrome major version: CHROME_VERSION_MAIN env → safe default."""
    raw = (CHROME_VERSION_MAIN or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning(f"Invalid CHROME_VERSION_MAIN={raw!r}; using default")
    return CHROME_VERSION_MAIN_DEFAULT

# ── Parallel Worker Pool Configuration ──
# To run N parallel calls, define N workers here.
# Each worker needs:
# 1. A unique Chrome profile directory (so sessions don't collide)
# 2. A unique Virtual Audio Cable pair: (Capture/Mic Line, Playback/Speaker Line)
_WORKER_POOL_RAW = [
    {
        "profile_dir": BASE_DIR / ".chrome_profile_1",
        "capture_device": "Line 1",
        "playback_device": "Line 2",
    },
    {
        "profile_dir": BASE_DIR / ".chrome_profile_2",
        "capture_device": "Line 3",
        "playback_device": "Line 4",
    },
    {
        "profile_dir": BASE_DIR / ".chrome_profile_3",
        "capture_device": "Line 5",
        "playback_device": "Line 6",
    },
    {
        "profile_dir": BASE_DIR / ".chrome_profile_4",
        "capture_device": "Line 7",
        "playback_device": "Line 8",
    },
    {
        "profile_dir": BASE_DIR / ".chrome_profile_5",
        "capture_device": "Line 9",
        "playback_device": "Line 10",
    },
    {
        "profile_dir": BASE_DIR / ".chrome_profile_6",
        "capture_device": "Line 11",
        "playback_device": "Line 12",
    },
    {
        "profile_dir": BASE_DIR / ".chrome_profile_7",
        "capture_device": "Line 13",
        "playback_device": "Line 14",
    },
    {
        "profile_dir": BASE_DIR / ".chrome_profile_8",
        "capture_device": "Line 15",
        "playback_device": "Line 16",
    },
]

# Temporary worker disable list.
DISABLED_WORKER_PROFILES = set()

# Optional runtime limit: only use the first N enabled workers from _WORKER_POOL_RAW.
# Set GV_MAX_WORKERS=8 in .env to run exactly 8 workers even if more profiles exist.
GV_MAX_WORKERS = _get_int_env("GV_MAX_WORKERS", len(_WORKER_POOL_RAW))

WORKER_POOL = [
    worker for worker in _WORKER_POOL_RAW
    if worker["profile_dir"].name not in DISABLED_WORKER_PROFILES
][:GV_MAX_WORKERS]


def get_worker_audio_routing(worker_index: int) -> dict:
    """Return app-side and Google Voice-side audio routing for one worker.

    Each worker owns a VAC pair (capture, playback). Google Voice is wired
    the other way around so the app hears the call on the capture line:

      Profile 1  capture=Line 1  playback=Line 2  →  GV mic=2, ring=1, speakers=1
      Profile 2  capture=Line 3  playback=Line 4  →  GV mic=4, ring=3, speakers=3
    """
    worker = WORKER_POOL[worker_index]
    capture_device = worker["capture_device"]
    playback_device = worker["playback_device"]
    return {
        "profile_dir": worker["profile_dir"],
        "capture_device": capture_device,
        "playback_device": playback_device,
        "gv_microphone": playback_device,
        "gv_ringing": capture_device,
        "gv_speaker": capture_device,
    }

# Validate required config
def validate_config():
    """Check that all required configuration is present."""
    errors = []
    
    role = NODE_ROLE if NODE_ROLE in {"standalone", "controller", "agent"} else "standalone"

    # Telegram is only needed where the bot actually runs.
    if role in {"standalone", "controller"} and not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN is required")
    # Deepgram audio runs where the calls happen (standalone box or agents).
    if role in {"standalone", "agent"} and not (DEEPGRAM_API_KEY or DEEPGRAM_API_KEYS):
        errors.append("DEEPGRAM_API_KEY is required")
    # Distributed roles must know how to reach the bus.
    if role in {"controller", "agent"} and not REDIS_URL:
        errors.append(f"REDIS_URL is required when NODE_ROLE={role}")

    if errors:
        for err in errors:
            logger.error(f"Config error: {err}")
        raise ValueError(f"Configuration errors: {', '.join(errors)}")
    logger.debug("Configuration validated successfully")

# Configure loguru: Log strictly to file so the Live Console Dashboard remains clean
def _setup_logging():
    level = os.getenv("LOG_LEVEL", "INFO")
    
    # Identify if we are in the main process or a spawned subprocess (audio cap/play)
    import multiprocessing
    proc_name = multiprocessing.current_process().name
    
    logger.remove()
    
    if proc_name == "MainProcess":
        # Global application log
        logger.add(
            "ai-caller.log",
            rotation="10 MB",
            level=level,
            enqueue=True
        )
    else:
        # Subprocess specific logs (prevent PermissionError/WinError 32 on rotation)
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(exist_ok=True)
        
        # Sanitize process name for filename
        safe_proc_name = "".join([c if c.isalnum() or c in ("-", "_") else "_" for c in proc_name])
        logger.add(
            log_dir / f"ai-caller-{safe_proc_name}.log",
            rotation="5 MB",
            level=level,
            enqueue=True
        )

_setup_logging()
