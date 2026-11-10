"""
Operator-made P1 script templates.

Built-in profiles live in audio/script_profiles.py. Custom ones are stored
here so /settings can add, edit, delete and pick the default for batch calls.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

import aiofiles
from loguru import logger

from audio.script_profiles import (
    DEFAULT_PROFILE,
    SCRIPT_PROFILES,
    SCRIPT_SLOTS,
    normalize_profile,
    serialize_bundle,
)
from config import SCRIPT_TEMPLATES_FILE

_lock = asyncio.Lock()

_EMOJI_CYCLE = ("🎬", "📞", "🏦", "📦", "🔐", "💳", "🏥", "🚗", "📧", "🏠")


def _empty_store() -> dict:
    return {"active": DEFAULT_PROFILE, "custom": {}}


def _ensure_parent():
    SCRIPT_TEMPLATES_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_store_sync() -> dict:
    if not SCRIPT_TEMPLATES_FILE.exists():
        return _empty_store()
    try:
        raw = json.loads(SCRIPT_TEMPLATES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"script templates unreadable — {e}")
        return _empty_store()
    if not isinstance(raw, dict):
        return _empty_store()
    custom = raw.get("custom") if isinstance(raw.get("custom"), dict) else {}
    active = raw.get("active") or DEFAULT_PROFILE
    return {"active": active, "custom": custom}


def save_store_sync(store: dict) -> None:
    _ensure_parent()
    SCRIPT_TEMPLATES_FILE.write_text(
        json.dumps(store, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def get_custom_sync(key: str) -> Optional[dict]:
    return load_store_sync().get("custom", {}).get(key)


def get_active_key_sync() -> str:
    store = load_store_sync()
    key = store.get("active") or DEFAULT_PROFILE
    if key in SCRIPT_PROFILES or key in store.get("custom", {}):
        return key
    return DEFAULT_PROFILE


async def _load() -> dict:
    if not SCRIPT_TEMPLATES_FILE.exists():
        return _empty_store()
    async with aiofiles.open(SCRIPT_TEMPLATES_FILE, "r", encoding="utf-8") as f:
        raw = await f.read()
    try:
        data = json.loads(raw)
    except Exception:
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    custom = data.get("custom") if isinstance(data.get("custom"), dict) else {}
    return {"active": data.get("active") or DEFAULT_PROFILE, "custom": custom}


async def _save(store: dict) -> None:
    _ensure_parent()
    async with aiofiles.open(SCRIPT_TEMPLATES_FILE, "w", encoding="utf-8") as f:
        await f.write(json.dumps(store, indent=2, ensure_ascii=False))


def slugify(name: str, existing: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:24]
    if not base:
        base = "script"
    if base in SCRIPT_PROFILES:
        base = f"custom_{base}"
    key = base
    n = 2
    while key in existing or key in SCRIPT_PROFILES:
        key = f"{base}_{n}"[:28]
        n += 1
    return key


def _decorate_label(label: str, existing_labels: list[str]) -> str:
    text = (label or "").strip() or "Script"
    first = text[0]
    if not first.isalnum() and not first.isspace():
        return text[:40]
    emoji = _EMOJI_CYCLE[len(existing_labels) % len(_EMOJI_CYCLE)]
    return f"{emoji} {text}"[:40]


def list_catalog(store: Optional[dict] = None) -> list[dict]:
    """Built-ins first, then custom — each row is ready for the picker UI."""
    store = store or load_store_sync()
    active = store.get("active") or DEFAULT_PROFILE
    rows = []
    for key, raw in SCRIPT_PROFILES.items():
        rows.append({
            "key": key,
            "label": raw.get("label") or key,
            "builtin": True,
            "active": key == active,
        })
    for key, raw in (store.get("custom") or {}).items():
        rows.append({
            "key": key,
            "label": raw.get("label") or key,
            "builtin": False,
            "active": key == active,
        })
    return rows


def get_profile_dict(key: str, store: Optional[dict] = None) -> Optional[dict]:
    if key in SCRIPT_PROFILES:
        p = normalize_profile(key, SCRIPT_PROFILES[key])
        p["builtin"] = True
        return p
    store = store or load_store_sync()
    raw = (store.get("custom") or {}).get(key)
    if not raw:
        return None
    p = normalize_profile(key, raw)
    p["builtin"] = False
    return p


def bundle_for(key: str, store: Optional[dict] = None) -> dict:
    profile = get_profile_dict(key, store) or normalize_profile(
        DEFAULT_PROFILE, SCRIPT_PROFILES[DEFAULT_PROFILE]
    )
    return serialize_bundle(profile.get("key") or key, profile)


async def list_catalog_async() -> list[dict]:
    async with _lock:
        store = await _load()
    return list_catalog(store)


async def get_profile_async(key: str) -> Optional[dict]:
    async with _lock:
        store = await _load()
    return get_profile_dict(key, store)


async def set_active(key: str) -> bool:
    async with _lock:
        store = await _load()
        if key not in SCRIPT_PROFILES and key not in store["custom"]:
            return False
        store["active"] = key
        await _save(store)
    logger.info(f"Active script template → {key}")
    return True


async def add_custom(
    label: str,
    intro: str,
    pressed_1: str,
    pressed_2: str,
    no_response: str,
    retry: str,
    key: Optional[str] = None,
) -> Optional[str]:
    async with _lock:
        store = await _load()
        existing = set(store["custom"])
        labels = [r["label"] for r in list_catalog(store)]
        pretty = _decorate_label(label, labels)
        new_key = key or slugify(label, existing)
        store["custom"][new_key] = {
            "key": new_key,
            "label": pretty,
            "intro": intro.strip(),
            "pressed_1": pressed_1.strip(),
            "pressed_2": pressed_2.strip(),
            "no_response": no_response.strip(),
            "retry": retry.strip(),
        }
        await _save(store)
    logger.info(f"Added script template {new_key}")
    return new_key


async def update_custom(key: str, **fields) -> bool:
    allowed = {"label", *SCRIPT_SLOTS}
    async with _lock:
        store = await _load()
        if key not in store["custom"]:
            return False
        for name, value in fields.items():
            if name not in allowed or value is None:
                continue
            store["custom"][key][name] = str(value).strip()
        await _save(store)
    logger.info(f"Updated script template {key}: {sorted(fields)}")
    return True


async def delete_custom(key: str) -> bool:
    if key in SCRIPT_PROFILES:
        return False
    async with _lock:
        store = await _load()
        if key not in store["custom"]:
            return False
        del store["custom"][key]
        if store.get("active") == key:
            store["active"] = DEFAULT_PROFILE
        await _save(store)
    logger.info(f"Deleted script template {key}")
    return True


async def duplicate(key: str) -> Optional[str]:
    src = await get_profile_async(key)
    if not src:
        return None
    texts = {}
    for slot in SCRIPT_SLOTS:
        val = src.get(slot)
        texts[slot] = val[1] if isinstance(val, (list, tuple)) else (val or "")
    label = (src.get("label") or key) + " copy"
    return await add_custom(label, **texts)
