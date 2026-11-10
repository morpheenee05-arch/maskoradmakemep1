"""
On-agent archive of account wipes (ban / spam / dead session / captcha).

A screenshot + short page dump is taken WHILE Chrome is still on the bad
page, then old files are pruned so the disk does not fill up. The controller
pulls the index over Redis; /wipes is an inline archive (server / account /
cause + screenshot fetch). The PNGs stay on the agent.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger

import config

_lock = threading.Lock()
INDEX_NAME = "index.json"
_KIND_MARKERS = (
    ("banned", ("banned", "disabled", "grandcentral", "40039")),
    ("captcha", ("captcha", "recaptcha", "robot check")),
    ("spam", ("spam",)),
    ("expired", ("expired", "logged out", "session dead", "logged-out")),
)


def archive_dir() -> Path:
    raw = (getattr(config, "WIPE_HISTORY_DIR", None) or "").strip()
    return Path(raw) if raw else (config.BASE_DIR / "wipe_history")


def keep_shots() -> int:
    return max(3, min(40, int(getattr(config, "GV_WIPE_HISTORY_KEEP", 12))))


def keep_events() -> int:
    return max(keep_shots(), min(80, keep_shots() * 3))


def infer_kind(reason: str, fallback: str = "wipe") -> str:
    text = (reason or "").lower()
    for kind, marks in _KIND_MARKERS:
        if any(m in text for m in marks):
            return kind
    return fallback or "wipe"


def _index_path() -> Path:
    return archive_dir() / INDEX_NAME


def _load_index() -> list:
    path = _index_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_index(events: list) -> None:
    root = archive_dir()
    root.mkdir(parents=True, exist_ok=True)
    _index_path().write_text(
        json.dumps(events, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _safe_stem(value: str, fallback: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "_", value or "")
    text = text.strip("._")[:40]
    return text or fallback


def _page_bits(driver) -> dict:
    out = {"url": "", "title": "", "text": ""}
    if driver is None:
        return out
    try:
        out["url"] = (driver.current_url or "")[:240]
    except Exception:
        pass
    try:
        out["title"] = (driver.title or "")[:160]
    except Exception:
        pass
    try:
        raw = driver.execute_script(
            "return document.body ? document.body.innerText : ''"
        )
        out["text"] = re.sub(r"\s+", " ", raw or "").strip()[:700]
    except Exception:
        pass
    return out


def _take_shot(driver, dest: Path) -> bool:
    if driver is None:
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        driver.save_screenshot(str(dest))
        return dest.exists() and dest.stat().st_size > 0
    except Exception as e:
        logger.debug(f"wipe archive screenshot failed — {e}")
        return False


def prune(root: Optional[Path] = None) -> None:
    """Keep the newest screenshots / index rows only."""
    root = root or archive_dir()
    if not root.exists():
        return
    shots = sorted(
        [p for p in root.glob("wipe_*.png") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in shots[keep_shots():]:
        try:
            old.unlink()
        except Exception:
            pass
    events = _load_index()
    if len(events) > keep_events():
        _save_index(events[-keep_events():])


def snapshot_worker(worker, reason: str = "", kind: str = "") -> Optional[dict]:
    """Record why this profile is being vacated. Never raises."""
    if getattr(worker, "_skip_wipe_archive", False):
        return None
    try:
        return _snapshot_worker_inner(worker, reason, kind)
    except Exception as e:
        logger.debug(f"wipe archive skipped — {e}")
        return None


def _snapshot_worker_inner(worker, reason: str, kind: str) -> dict:
    driver = getattr(worker, "driver", None)
    wid = getattr(worker, "worker_id", "?")
    email = (
        getattr(worker, "account_email", None)
        or getattr(worker, "credential_email", None)
        or ""
    )
    phone = getattr(worker, "phone_number", None) or ""
    profile = ""
    pdir = getattr(worker, "profile_dir", None)
    if pdir is not None:
        profile = getattr(pdir, "name", None) or str(pdir)
    why = (reason or getattr(worker, "last_error", None) or "account vacated").strip()
    kind = infer_kind(why, kind or "wipe")
    now = time.time()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
    stem = (
        f"wipe_{stamp}_w{wid}_{kind}_"
        + _safe_stem(email.split("@")[0] if email else profile, "acct")
    )
    root = archive_dir()
    root.mkdir(parents=True, exist_ok=True)
    shot_name = stem + ".png"
    bits = _page_bits(driver)
    shot_ok = _take_shot(driver, root / shot_name)
    event = {
        "ts": now,
        "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "kind": kind,
        "reason": why[:240],
        "worker_id": wid,
        "email": email,
        "phone": phone,
        "profile": profile,
        "login_state": getattr(worker, "login_state", None),
        "login_outcome": getattr(worker, "login_outcome", None),
        "login_detail": (getattr(worker, "login_detail", None) or "")[:200],
        "url": bits["url"],
        "title": bits["title"],
        "text": bits["text"],
        "screenshot": shot_name if shot_ok else None,
        "has_browser": driver is not None,
    }
    with _lock:
        events = _load_index()
        events.append(event)
        if len(events) > keep_events():
            events = events[-keep_events():]
        _save_index(events)
        prune(root)
    logger.warning(
        f"Worker {wid}: wipe archived ({kind})"
        f"{' ' + email if email else ''}"
        f"{' shot=' + shot_name if shot_ok else ' (no screenshot)'}"
        f" — {why[:80]}"
    )
    return event


def list_events(limit: int = 15) -> list:
    events = _load_index()
    if limit and limit > 0:
        events = events[-limit:]
    return list(reversed(events))


def screenshot_file(name: str) -> Optional[Path]:
    """Resolve a wipe PNG inside the archive dir, or None if the name is unsafe."""
    raw = (name or "").strip()
    if not raw or raw != Path(raw).name:
        return None
    if ".." in raw or "/" in raw or "\\" in raw:
        return None
    if not raw.lower().endswith(".png"):
        return None
    root = archive_dir().resolve()
    path = (root / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path


def latest_screenshots(limit: int = 1) -> list:
    """Newest existing PNG paths, newest first."""
    root = archive_dir()
    if not root.exists():
        return []
    shots = sorted(
        [p for p in root.glob("wipe_*.png") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return shots[: max(0, int(limit))]
