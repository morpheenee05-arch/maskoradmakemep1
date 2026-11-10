"""
Google account credentials for worker profiles.

The operator hands over logins in one Telegram message; the controller parses
them here, ships them to the agent that owns the profile, and the agent feeds
them to its browsers. Passwords therefore pass through Redis and, optionally,
one file on the agent — see the security notes in README before enabling
GV_SAVE_CREDENTIALS.

Nothing here ever logs a password: use mask() for anything user-visible.
"""
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger

import config
from infra import totp

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# A backup-code group: either Google's own 8-digit codes or the 4-char
# groups account sellers hand out (often app-password style, 4+4 pairs).
_BACKUP_CODE_RE = re.compile(r"^(?:[A-Za-z0-9]{4}|\d{8})$")


def mask(password: Optional[str]) -> str:
    """Human-readable stand-in for a password — never the password itself."""
    if not password:
        return "<empty>"
    return f"<{len(password)} chars>"


def parse_target(raw: str) -> dict:
    """Understand an optional routing prefix on a credentials line.

    Accepted: `w3`, `3`, `.chrome_profile_3`, `chrome_profile_3`,
    `agent-01/w3`, `agent-01`.
    """
    out = {"agent": None, "worker": None, "profile": None}
    token = (raw or "").strip()
    if not token:
        return out

    if "/" in token:
        agent, _, token = token.partition("/")
        out["agent"] = agent.strip() or None
        token = token.strip()
    if not token:
        return out

    low = token.lower()
    if "profile" in low:
        out["profile"] = token if token.startswith(".") else f".{token.lstrip('.')}"
        return out
    if low.startswith("w") and low[1:].isdigit():
        out["worker"] = int(low[1:])
        return out
    if low.isdigit():
        out["worker"] = int(low)
        return out

    # A bare word that is not a worker or a profile is an agent id.
    out["agent"] = out["agent"] or token
    return out


def parse_credentials(text: str) -> Tuple[List[dict], List[str]]:
    """Parse an operator message into credential entries.

    One account per line, in any of:
        user@gmail.com password
        user@gmail.com:password
        user@gmail.com password recovery@gmail.com
        user@gmail.com password recovery@gmail.com (415) 493-8486
        user@gmail.com password (https://t.me/note) recovery@gmail.com (415) 493-8486
        user@gmail.com password 5lpg ul5m beax bjcy (650) 735-1754
        user@gmail.com password (415) 879-8182 https://2fa.live/tok/<seed>
        w3 user@gmail.com password recovery@gmail.com
        agent-01/w3 user@gmail.com:password

    One account may also span several lines (a pasted card where the email,
    password, phone and 2FA link sit on their own lines): a line without an
    email continues the previous account.

    The password is the single whitespace-separated token right after the
    email and is taken verbatim — it may contain any characters, including
    "@" and ":". A later email-shaped token is the recovery (backup) address,
    used when Google asks to confirm it during sign-in. A 2fa.live link or a
    bare base32 seed is the TOTP seed for the authenticator step, and 4-char
    or 8-digit groups are kept as backup codes. Anything else after the
    password (phones, links, notes) is ignored.

    Returns (entries, errors). Each entry is
    {"agent", "worker", "profile", "email", "password", "recovery_email",
     "totp_seed", "backup_codes"}.
    """
    entries: List[dict] = []
    errors: List[str] = []

    # Join multi-line account cards: a line without "@" continues the
    # previous account (password / phone / 2FA link on their own lines).
    logical: List[Tuple[int, str]] = []
    for lineno, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # A pasted /login command keeps its own first line.
        if line.startswith("/"):
            line = line.split(None, 1)[1].strip() if " " in line else ""
            if not line:
                continue
        if "@" in line or not logical:
            # Every line with an email starts a new account (a batch of
            # multi-line cards is the common paste); lines without one —
            # password, phone, 2FA link — continue the card above.
            logical.append((lineno, line))
        else:
            first_lineno, prev = logical[-1]
            logical[-1] = (first_lineno, prev + " " + line)

    for lineno, line in logical:
        tokens = line.split()
        idx = next((i for i, t in enumerate(tokens) if "@" in t), None)
        if idx is None:
            errors.append(f"line {lineno}: no email found in {line!r}")
            continue

        email_token = tokens[idx]
        target = parse_target(" ".join(tokens[:idx]))
        tail = tokens[idx + 1:]

        if ":" in email_token:
            email, _, inline_pw = email_token.partition(":")
        else:
            email, inline_pw = email_token, ""

        # The password is the single token right after the email. It is taken
        # verbatim: passwords may contain any characters ("@", ":", "&", …),
        # so it must never be re-split or pattern-matched. Everything after it
        # is optional metadata — the first valid email there is the recovery
        # address, and phones / links / other junk are dropped.
        password = inline_pw
        rest: List[str] = tail
        if not password:
            if not tail:
                errors.append(f"line {lineno}: no password for {email}")
                continue
            password = tail[0]
            rest = tail[1:]

        recovery_email = None
        totp_seed = None
        code_candidates: List[str] = []
        for t in rest:
            cand = t.strip().strip(",;")
            if recovery_email is None and _EMAIL_RE.match(cand):
                recovery_email = cand
                continue
            if totp_seed is None:
                seed = totp.extract_seed(cand)
                if seed:
                    totp_seed = seed
                    continue
            if _BACKUP_CODE_RE.match(cand):
                code_candidates.append(cand)
        # A lone 4-char word is usually a note, not a code — sellers hand out
        # whole sets. Google's own 8-digit codes are kept even when single.
        backup_codes = [
            c for c in code_candidates
            if len(c) == 8 or len(code_candidates) >= 2
        ]

        email = email.strip().strip(",;")
        if not _EMAIL_RE.match(email):
            errors.append(f"line {lineno}: {email!r} is not a valid email")
            continue
        if not password:
            errors.append(f"line {lineno}: no password for {email}")
            continue

        entries.append({
            "agent": target["agent"],
            "worker": target["worker"],
            "profile": target["profile"],
            "email": email,
            "password": password,
            "recovery_email": recovery_email,
            "totp_seed": totp_seed,
            "backup_codes": backup_codes,
        })

    return entries, errors


class CredentialStore:
    """Per-profile Google credentials, in memory and optionally on disk."""

    def __init__(self, path: Optional[Path] = None, persist: Optional[bool] = None):
        self.path = Path(path or getattr(
            config, "GV_ACCOUNTS_FILE", config.BASE_DIR / "storage" / "gv_accounts.json"
        ))
        self.persist = (
            bool(getattr(config, "GV_SAVE_CREDENTIALS", False))
            if persist is None else bool(persist)
        )
        self._accounts: Dict[str, dict] = {}

    # ── access ────────────────────────────────────────────────────────────────

    def get(self, profile_name: str) -> Optional[dict]:
        entry = self._accounts.get(profile_name)
        if not entry or not entry.get("email") or not entry.get("password"):
            return None
        return dict(entry)

    def put(
        self,
        profile_name: str,
        email: str,
        password: str,
        recovery_email: Optional[str] = None,
        totp_seed: Optional[str] = None,
        backup_codes: Optional[List[str]] = None,
    ) -> None:
        # Backup codes already spent on earlier sign-ins survive a re-put of
        # the same account so they are never submitted twice.
        prev = self._accounts.get(profile_name) or {}
        used = list(prev.get("used_backup_codes") or [])
        self._accounts[profile_name] = {
            "email": email,
            "password": password,
            "recovery_email": recovery_email,
            "totp_seed": totp_seed,
            "backup_codes": list(backup_codes or []),
            "used_backup_codes": used,
        }

    def mark_backup_code_used(self, profile_name: str, code: str) -> None:
        """Record a spent backup code — Google never accepts it again."""
        entry = self._accounts.get(profile_name)
        if not entry or not code:
            return
        used = entry.setdefault("used_backup_codes", [])
        if code not in used:
            used.append(code)

    def drop(self, profile_name: str) -> None:
        self._accounts.pop(profile_name, None)

    def emails(self) -> Dict[str, str]:
        """profile → email, safe to log and to send to the controller."""
        return {
            name: entry.get("email", "")
            for name, entry in self._accounts.items()
        }

    def __len__(self) -> int:
        return len(self._accounts)

    # ── disk ──────────────────────────────────────────────────────────────────

    def load(self) -> int:
        """Read cached credentials, if a file was left by a previous run."""
        if not self.path.exists():
            return 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Credentials file unreadable ({self.path}): {e}")
            return 0
        accounts = raw.get("accounts") if isinstance(raw, dict) else None
        if not isinstance(accounts, dict):
            logger.warning(f"Credentials file has no 'accounts' map: {self.path}")
            return 0
        loaded = 0
        for name, entry in accounts.items():
            if isinstance(entry, dict) and entry.get("email") and entry.get("password"):
                self._accounts[name] = {
                    "email": entry["email"],
                    "password": entry["password"],
                    "recovery_email": entry.get("recovery_email"),
                    "totp_seed": entry.get("totp_seed"),
                    "backup_codes": list(entry.get("backup_codes") or []),
                    "used_backup_codes": list(entry.get("used_backup_codes") or []),
                }
                loaded += 1
        logger.info(f"Loaded credentials for {loaded} profile(s) from {self.path}")
        return loaded

    def save(self) -> bool:
        """Persist credentials so a restart can sign in without the operator.

        Off unless GV_SAVE_CREDENTIALS=1: this writes Google passwords to disk
        in plain text. The file is created 0600 where the OS supports it.
        """
        if not self.persist:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"accounts": self._accounts}
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except (OSError, NotImplementedError):
                pass
            logger.info(
                f"Saved credentials for {len(self._accounts)} profile(s) "
                f"to {self.path}"
            )
            return True
        except Exception as e:
            logger.warning(f"Could not save credentials to {self.path}: {e}")
            return False
