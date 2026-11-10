"""
Upload the controller payload to Ubuntu and restart the bot.

The controller is Telegram + Redis only: it never runs Chrome. This script
therefore sends Python sources and storage, and leaves behind Chrome binaries,
profiles, drivers, logs, venvs and screenshots.

After a successful upload the bot is fully restarted on the server:
a matching systemd unit if one exists, otherwise the main.py process is
killed and started again with nohup. The restart output and a short health
check are printed at the end.

Usage:
    python upload.py                 # interactive: host, user, password
    python upload.py --dry-run       # print the file list, do not connect
    python upload.py --host 1.2.3.4 --user root --no-restart

Host/user/password can also come from UPLOAD_HOST / UPLOAD_USER /
UPLOAD_PASSWORD env vars (prompts use them as defaults).
"""
import argparse
import getpass
import os
import threading
from pathlib import Path

import paramiko
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_HOST = os.getenv("UPLOAD_HOST", "31.77.124.38")
DEFAULT_USER = os.getenv("UPLOAD_USER", "root")
DEFAULT_REMOTE_DIR = os.getenv("UPLOAD_REMOTE_DIR", "/opt/controller")
DEFAULT_PORT = int(os.getenv("UPLOAD_PORT", "22"))
WORKERS = 8
SSH_KEY = os.getenv("UPLOAD_KEY") or None  # e.g. r"C:\Users\YOU\.ssh\id_rsa"

ROOT = Path(__file__).resolve().parent

# Connection parameters chosen at runtime (prompts / CLI flags).
CFG = {
    "host": DEFAULT_HOST,
    "user": DEFAULT_USER,
    "port": DEFAULT_PORT,
    "remote_dir": DEFAULT_REMOTE_DIR,
    "key": SSH_KEY,
}

# ── Controller allow-list ─────────────────────────────────────────────────────
# Everything else (Chrome, chromedriver, profiles, .venv, logs, zips, agent
# helpers, login_helper) stays on the Windows box.

CONTROLLER_FILES = (
    "main.py",
    "config.py",
    "console_ui.py",
    "requirements.txt",
    ".env.example",
    "README.md",
    "audio/__init__.py",
    "audio/script_profiles.py",
)

CONTROLLER_TREES = (
    "bot",
    "infra",
    "storage",
)

# Inside included trees: skip generated/secret files.
SKIP_NAMES = {
    ".env",
    "gv_accounts.json",
}
SKIP_DIRS = {
    "__pycache__",
    "tts_cache",
    "wipe_history",
    ".git",
}
SKIP_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".log",
    ".png",
    ".jpg",
    ".pcm",
}


def collect_controller_files() -> list[Path]:
    """Return the local files that belong on the Ubuntu controller."""
    files: list[Path] = []

    for rel in CONTROLLER_FILES:
        path = ROOT / rel
        if path.is_file():
            files.append(path)
        else:
            print(f"skip missing {rel}")

    for tree in CONTROLLER_TREES:
        base = ROOT / tree
        if not base.is_dir():
            print(f"skip missing tree {tree}/")
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            parts = set(path.relative_to(ROOT).parts)
            if parts & SKIP_DIRS:
                continue
            if path.name in SKIP_NAMES:
                continue
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            files.append(path)

    files.sort(key=lambda p: str(p).lower())
    return files


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


_tls = threading.local()


def _connect_ssh(password: str | None) -> paramiko.SSHClient:
    """Open one SSH connection with the current CFG parameters."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = {
        "hostname": CFG["host"],
        "port": CFG["port"],
        "username": CFG["user"],
        "timeout": 20,
    }
    if CFG["key"]:
        key = paramiko.RSAKey.from_private_key_file(CFG["key"])
        ssh.connect(pkey=key, **kwargs)
    else:
        ssh.connect(password=password, **kwargs)
    return ssh


def _sftp(password: str | None):
    """One SSH/SFTP session per worker thread."""
    client = getattr(_tls, "ssh", None)
    if client is not None:
        return _tls.sftp

    ssh = _connect_ssh(password)
    _tls.ssh = ssh
    _tls.sftp = ssh.open_sftp()
    return _tls.sftp


def _close_tls():
    sftp = getattr(_tls, "sftp", None)
    ssh = getattr(_tls, "ssh", None)
    _tls.sftp = None
    _tls.ssh = None
    if sftp is not None:
        try:
            sftp.close()
        except Exception:
            pass
    if ssh is not None:
        try:
            ssh.close()
        except Exception:
            pass


def _mkdirs(sftp, remote_dir: str):
    parts = remote_dir.split("/")
    current = ""
    for folder in parts:
        if not folder:
            continue
        current += "/" + folder
        try:
            sftp.stat(current)
        except FileNotFoundError:
            try:
                sftp.mkdir(current)
            except OSError:
                pass


def upload_file(local_path: Path, password: str | None):
    relative = local_path.relative_to(ROOT).as_posix()
    remote_path = f"{CFG['remote_dir']}/{relative}"
    try:
        sftp = _sftp(password)
        _mkdirs(sftp, os.path.dirname(remote_path))
        sftp.put(str(local_path), remote_path)
        return relative, True, None
    except Exception as e:
        _close_tls()
        return relative, False, str(e)


# ── Remote restart ────────────────────────────────────────────────────────────

# Runs on the server after a successful upload. Finds a systemd unit for the
# controller when there is one, otherwise hard-restarts the bare process.
# Pattern [m]ain\.py is used so pkill never matches this very script.
_RESTART_SCRIPT = r"""
set -u
cd "{remote_dir}" || {{ echo "FATAL: {remote_dir} missing"; exit 1; }}

UNIT=""
for u in ai-caller.service ai-caller-controller.service controller.service; do
  if systemctl cat "$u" >/dev/null 2>&1; then UNIT="$u"; break; fi
done
if [ -z "$UNIT" ]; then
  UNIT=$(systemctl list-units --type=service --all --no-legend 2>/dev/null \
    | grep -i 'controller\|ai-caller' | awk '{{print $1}}' | head -n1)
fi

if [ -n "$UNIT" ]; then
  echo "[restart] systemd unit found: $UNIT"
  systemctl restart "$UNIT"
  sleep 3
  if systemctl is-active --quiet "$UNIT"; then
    echo "[restart] OK — $UNIT is active"
  else
    echo "[restart] WARNING — $UNIT is NOT active:"
    systemctl --no-pager --lines=10 status "$UNIT" || true
    exit 1
  fi
else
  echo "[restart] no systemd unit — restarting bare process"
  pkill -f '[m]ain\.py' || true
  sleep 1
  PY="{remote_dir}/.venv/bin/python"
  [ -x "$PY" ] || PY="$(command -v python3)"
  echo "[restart] starting with $PY"
  nohup "$PY" main.py >> ai-caller.log 2>&1 &
  sleep 4
  if pgrep -af '[m]ain\.py'; then
    echo "[restart] OK — process is up"
  else
    echo "[restart] WARNING — process not found after start, last log lines:"
    tail -n 15 ai-caller.log || true
    exit 1
  fi
fi

echo "── last log lines ──"
tail -n 8 "{remote_dir}/ai-caller.log" 2>/dev/null || true
"""


def restart_bot(password: str | None) -> bool:
    """Full bot restart on the server; prints remote output. True = healthy."""
    print()
    print("=" * 50)
    print("Перезапуск бота на сервере…")
    ssh = None
    try:
        ssh = _connect_ssh(password)
        script = _RESTART_SCRIPT.format(remote_dir=CFG["remote_dir"])
        _, stdout, stderr = ssh.exec_command(f"bash -s", timeout=90)
        stdin = _
        stdin.write(script)
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace").strip()
        err = stderr.read().decode("utf-8", "replace").strip()
        code = stdout.channel.recv_exit_status()
        if out:
            print(out)
        if err:
            print(err)
        if code == 0:
            print("✅ Бот перезапущен и поднялся")
            return True
        print(f"❌ Перезапуск завершился с кодом {code} — проверь сервер")
        return False
    except Exception as e:
        print(f"❌ Не удалось перезапустить бота: {e}")
        return False
    finally:
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass


def _ask(prompt: str, default: str) -> str:
    raw = input(f"{prompt} [{default}]: ").strip()
    return raw or default


def main():
    parser = argparse.ArgumentParser(
        description="Upload the controller set and restart the bot."
    )
    parser.add_argument("--host", help="Server IP/host (prompted if omitted)")
    parser.add_argument("--user", help="SSH user (prompted if omitted)")
    parser.add_argument("--port", type=int, help="SSH port")
    parser.add_argument("--remote-dir", help="Target directory on the server")
    parser.add_argument("--key", help="Path to an SSH private key (skip password)")
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Upload only; do not restart the bot afterwards.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files and sizes, do not connect.",
    )
    args = parser.parse_args()

    files = collect_controller_files()
    total = sum(p.stat().st_size for p in files)

    print(f"Files: {len(files)}  |  {_fmt_size(total)}")
    print("Not uploaded: Chrome, chromedriver, profiles, .venv, logs, zips, agent/")
    print()
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        print(f"  {rel:40}  {_fmt_size(path.stat().st_size)}")
    print()

    if args.dry_run:
        print("Dry run - nothing uploaded.")
        return

    if not files:
        print("Nothing to upload.")
        return

    # ── Connection details (CLI flag → env default → interactive prompt) ──
    CFG["host"] = args.host or _ask("Хост сервера", DEFAULT_HOST)
    CFG["user"] = args.user or _ask("SSH пользователь", DEFAULT_USER)
    CFG["port"] = args.port or DEFAULT_PORT
    CFG["remote_dir"] = args.remote_dir or _ask(
        "Каталог на сервере", DEFAULT_REMOTE_DIR
    )
    CFG["key"] = args.key or SSH_KEY

    target = f"{CFG['user']}@{CFG['host']}:{CFG['remote_dir']}"
    print(f"\nController payload -> {target} (port {CFG['port']})")

    password = None
    if not CFG["key"]:
        password = os.getenv("UPLOAD_PASSWORD") or getpass.getpass(
            f"Пароль {CFG['user']}: "
        )

    completed = 0
    failed = []

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [
            executor.submit(upload_file, path, password) for path in files
        ]
        try:
            for future in as_completed(futures):
                filename, success, error = future.result()
                completed += 1
                if success:
                    print(f"[{completed}/{len(files)}] OK    {filename}")
                else:
                    print(f"[{completed}/{len(files)}] ERROR {filename}: {error}")
                    failed.append((filename, error))
        finally:
            # Each worker thread still holds an SSH session; closing from
            # here is racy, so workers close on error and the process exit
            # reaps the rest. Force a join first.
            pass

    print()
    print("=" * 50)
    print(f"Done: {len(files) - len(failed)}/{len(files)}")
    if failed:
        print(f"Ошибок: {len(failed)}")
        for filename, error in failed:
            print(filename)
            print(" ", error)
        print("Перезапуск пропущен — сначала исправь ошибки загрузки.")
        return

    if args.no_restart:
        print("Перезапуск пропущен (--no-restart).")
        return

    restart_bot(password)


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
