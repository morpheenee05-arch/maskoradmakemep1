"""
Git-based fleet self-update.

One-time bootstrap per server: install git, clone the repo, keep .env in the
checkout (untracked, survives `git reset --hard`). After that the controller
bot pushes an update signal to Redis and every node pulls the latest commit
and restarts itself — no manual copying to dozens of boxes.

Update flow on a node:
    apply_update()  → git fetch + git reset --hard origin/<branch>
    restart_self()  → spawn a fresh `python main.py` and exit this process
"""
import os
import subprocess
import sys

from loguru import logger

import config

REPO_DIR = config.BASE_DIR

_GIT_TIMEOUT = 180


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )


def available() -> bool:
    """True when this checkout can self-update (git binary + .git present)."""
    try:
        if not (REPO_DIR / ".git").exists():
            return False
        return _git("rev-parse", "--is-inside-work-tree").returncode == 0
    except Exception:
        return False


def current_version() -> str:
    """Short commit hash of this checkout ('nogit' when unavailable)."""
    try:
        r = _git("rev-parse", "--short", "HEAD")
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return "nogit"


def current_branch() -> str:
    try:
        r = _git("rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode == 0 and r.stdout.strip() not in ("", "HEAD"):
            return r.stdout.strip()
    except Exception:
        pass
    return "main"


def apply_update(ref: str = "") -> tuple:
    """Fetch and hard-reset to the remote branch head. Returns (ok, detail).

    Untracked files (.env, logs, profiles, storage jsons) are untouched, so
    per-server configuration survives every update.
    """
    if not available():
        return False, "not a git checkout (or git missing) — update skipped"
    try:
        before = current_version()
        r = _git("fetch", "--prune", "origin")
        if r.returncode != 0:
            return False, f"git fetch failed: {(r.stderr or r.stdout)[-300:]}"
        target = ref or f"origin/{current_branch()}"
        r = _git("reset", "--hard", target)
        if r.returncode != 0:
            return False, f"git reset failed: {(r.stderr or r.stdout)[-300:]}"
        after = current_version()
        changed = before != after
        detail = f"{before} → {after}" + ("" if changed else " (no change)")
        logger.info(f"Fleet update applied: {detail}")
        return True, detail
    except subprocess.TimeoutExpired:
        return False, "git timed out"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def restart_self(extra_argv: list | None = None):
    """Replace this process with a fresh `python <same argv>` and exit.

    Called AFTER graceful shutdown finished. The child is fully detached so
    it survives this process exiting (console on Windows, nohup-style on
    Linux); logs keep going to ai-caller.log via loguru.
    """
    argv = [sys.executable] + list(sys.argv if extra_argv is None else extra_argv)
    logger.info(f"Restarting after update: {' '.join(argv)}")
    kwargs = {"cwd": str(REPO_DIR)}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **kwargs,
        )
    except Exception as e:
        logger.error(f"Self-restart spawn failed: {e}")
    os._exit(0)
