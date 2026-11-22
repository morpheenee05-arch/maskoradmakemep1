"""
Remote-tunable agent settings.

The controller bot writes key/value pairs to the Redis hash ``gv:settings``
(standalone mode: ``storage/bot_settings.json``). Agents poll the hash in
their watchdog loop and apply diffs live via :func:`apply_settings`.

Only names listed in :data:`SETTABLE` are honoured — everything else is
ignored, so a typo in the bot can never corrupt the agent. Entries marked
``restart_required`` are applied to the config object anyway (so the value
is not lost), but some subsystems (the worker pool, headless Chrome, audio
capture subprocesses) only read them at startup.

Values are stored as plain strings; ``typ`` controls parsing:
    int / float / str / bool / keys (Deepgram key list → DG key pool)

Each entry: name -> (type, restart_required, emoji). The emoji is purely
cosmetic — the bot settings screen shows it next to the setting name.
"""
import json

from loguru import logger

# name -> (type, restart_required, emoji)
SETTABLE: dict[str, tuple[str, bool, str]] = {
    # Deepgram
    "DEEPGRAM_API_KEYS": ("keys", False, "🔑"),
    "DG_LISTEN_MODEL": ("str", False, "🎙"),
    "DG_SPEAK_MODEL": ("str", False, "🔊"),
    "DG_THINK_MODEL": ("str", False, "🧠"),
    # Browser startup / login
    "GV_INIT_LOGIN_CONCURRENCY": ("int", False, "🚀"),
    "GV_LOGIN_CONCURRENCY": ("int", False, "🔐"),
    "GV_MAX_LOGIN_ATTEMPTS": ("int", False, "🔁"),
    "GV_LOGIN_TIMEOUT_SEC": ("int", False, "⏱"),
    # Pool / self-heal
    "GV_MAX_WORKERS": ("int", True, "👷"),
    "GV_RECYCLE_AFTER_CALLS": ("int", False, "🧹"),
    "GV_RESTART_AFTER_CALLS": ("int", False, "♻️"),
    "GV_WORKER_STUCK_SEC": ("int", False, "🛡"),
    # Calls
    "P1_CALL_HARD_TIMEOUT_SEC": ("int", False, "📞"),
    "SIP_STREAMS_PER_ACCOUNT": ("int", False, "📶"),
    "GV_CALL_RECORD": ("bool", False, "🎬"),
    "P1_SEND_RECORDINGS": ("bool", False, "📤"),
    "P1_SEND_ALL_RECORDINGS": ("bool", False, "📡"),
}

# Settings whose values must never be shown in full (bot display masking).
SECRET_SETTINGS = {"DEEPGRAM_API_KEYS"}

_TRUE_WORDS = {"1", "true", "yes", "on", "y"}
_FALSE_WORDS = {"0", "false", "no", "off", "n", ""}


def parse_value(name: str, raw: str):
    """Parse a raw string into the typed value for `name`.

    Returns the typed value, or raises ValueError with a user-readable
    message. The "keys" type returns the cleaned list of keys.
    """
    spec = SETTABLE.get(name)
    if spec is None:
        raise ValueError(f"unknown setting: {name}")
    typ = spec[0]
    raw = str(raw).strip()
    if typ == "int":
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{name} expects an integer, got {raw!r}")
    if typ == "float":
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"{name} expects a number, got {raw!r}")
    if typ == "bool":
        low = raw.lower()
        if low in _TRUE_WORDS:
            return True
        if low in _FALSE_WORDS:
            return False
        raise ValueError(f"{name} expects 1/0 or true/false, got {raw!r}")
    if typ == "keys":
        from infra.dg_keys import parse_keys
        keys = parse_keys(raw)
        if not keys:
            raise ValueError("no API keys found in the input")
        return keys
    return raw


def display_value(name: str, raw: str) -> str:
    """Value for the bot screen — secrets masked."""
    if name in SECRET_SETTINGS:
        from infra.dg_keys import mask_key, parse_keys
        keys = parse_keys(raw)
        if not keys:
            return "(empty)"
        return ", ".join(mask_key(k) for k in keys)
    return str(raw)


def apply_settings(target_config, values: dict) -> list[str]:
    """Apply validated settings to the config module. Returns applied names.

    Unknown names and unparseable values are skipped with a warning, never
    fatal. DEEPGRAM_API_KEYS updates the shared DG key pool instead of a
    config attribute.
    """
    applied: list[str] = []
    for name, raw in (values or {}).items():
        if name not in SETTABLE:
            continue
        try:
            value = parse_value(name, raw)
        except ValueError as e:
            logger.warning(f"Remote setting skipped: {e}")
            continue
        try:
            if SETTABLE[name][0] == "keys":
                from infra.dg_keys import get_pool
                get_pool().set_keys(value)
            else:
                setattr(target_config, name, value)
            applied.append(name)
        except Exception as e:
            logger.warning(f"Remote setting {name} apply failed: {e}")
    return applied


# ── Standalone persistence (no Redis) ────────────────────────────────────

def _store_path():
    from pathlib import Path
    return Path(__file__).parent.parent / "storage" / "bot_settings.json"


def load_local_settings() -> dict:
    try:
        raw = _store_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return {}


def save_local_setting(name: str, value: str) -> dict:
    data = load_local_settings()
    data[name] = str(value)
    try:
        path = _store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except Exception as e:
        logger.warning(f"bot_settings.json write failed: {e}")
    return data
