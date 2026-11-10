"""
Persistent undetected_chromedriver bootstrap helpers.

UC's default patcher flow can spend tens of seconds downloading or patching a
driver binary on every browser launch. We instead prepare one persistent,
patched binary per configured Chrome major version and reuse it across runs.
"""
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger

import config

try:
    from undetected_chromedriver.patcher import Patcher
    UC_PATCHER_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    Patcher = None
    UC_PATCHER_AVAILABLE = False


_driver_prepare_lock = threading.Lock()
_prepared_driver_paths: set[str] = set()


def resolve_uc_version_main() -> Optional[int]:
    """Return the configured Chrome major version, if pinned."""
    raw = (config.CHROME_VERSION_MAIN or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"Ignoring invalid CHROME_VERSION_MAIN={raw!r}")
        return None


def _driver_cache_path(version_main: Optional[int]) -> Path:
    suffix = str(version_main) if version_main is not None else "auto"
    ext = ".exe" if os.name == "nt" else ""
    cache_dir = config.BASE_DIR / ".uc_driver_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"undetected_chromedriver_{suffix}{ext}"


def ensure_persistent_uc_driver(version_main: Optional[int] = None) -> Path:
    """
    Ensure a patched Chromedriver binary exists at a stable path and return it.

    Using a custom executable path prevents UC from deleting the binary on exit,
    which avoids the expensive re-patch or re-download cycle on every launch.
    """
    if not UC_PATCHER_AVAILABLE:
        raise RuntimeError("undetected_chromedriver is not installed")

    resolved_version = resolve_uc_version_main() if version_main is None else version_main
    driver_path = _driver_cache_path(resolved_version)
    cache_key = str(driver_path)

    with _driver_prepare_lock:
        if cache_key in _prepared_driver_paths and driver_path.exists():
            return driver_path

        needs_prepare = True
        if driver_path.exists():
            try:
                patcher = Patcher(
                    executable_path=str(driver_path),
                    version_main=resolved_version or 0,
                    user_multi_procs=False,
                )
                needs_prepare = not patcher.is_binary_patched(str(driver_path))
            except Exception:
                needs_prepare = True

        if needs_prepare:
            try:
                if driver_path.exists():
                    driver_path.unlink()
            except Exception:
                pass

            logger.info(
                "Preparing persistent undetected_chromedriver binary"
                f" at: {driver_path}"
            )
            start_t = time.time()
            bootstrap_patcher = Patcher(
                version_main=resolved_version or 0,
                user_multi_procs=False,
            )
            bootstrap_patcher.auto()
            shutil.copy2(bootstrap_patcher.executable_path, driver_path)
            logger.info(
                "Persistent undetected_chromedriver ready"
                f" ({time.time() - start_t:.2f}s)"
            )

        _prepared_driver_paths.add(cache_key)
        return driver_path
