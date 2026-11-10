"""Self-test for the 2FA auto-login plumbing: TOTP math, seed extraction,
credential parsing (incl. multi-line cards) and the credential store.

Run:  py -3 _twofa_selftest.py   (from update/)
"""
import sys
import tempfile
import time
from pathlib import Path

from infra import totp
from infra.gv_credentials import CredentialStore, parse_credentials

FAILURES = []


def check(name, cond):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name}")


# ── TOTP against the RFC 6238 SHA-1 test vectors ────────────────────────────
# ASCII seed "12345678901234567890" in base32.
SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
VECTORS = {
    59: "287082",
    1111111109: "081804",
    1111111111: "050471",
    1234567890: "005924",
    2000000000: "279037",
}
print("TOTP (RFC 6238 vectors)")
for t, want in VECTORS.items():
    got = totp.totp_at(SEED, t)
    check(f"T={t} -> {want}", got == want)

code, remaining = totp.totp_now(SEED)
check("totp_now: 6 digits", len(code) == 6 and code.isdigit())
check("totp_now: remaining in 1..30", 1 <= remaining <= 30)
check("totp_now matches totp_at", code == totp.totp_at(SEED, time.time()))

print("seed helpers")
check(
    "normalize: lower/dashes/spaces",
    totp.normalize_seed("gezd gnbv-gy3tqo jqgezdgnbvgy3tqojq") == SEED,
)
check("normalize: junk rejected", totp.normalize_seed("not a seed!") is None)
check("normalize: short rejected", totp.normalize_seed("ABC") is None)
check(
    "extract: 2fa.live url",
    totp.extract_seed("https://2fa.live/tok/btdbfvhpzgkqq3qnnfsoipikbyunyclo")
    == "BTDBFVHPZGKQQ3QNNFSOIPIKBYUNYCLO",
)
check(
    "extract: bare seed",
    totp.extract_seed("btdbfvhpzgkqq3qnnfsoipikbyunyclo")
    == "BTDBFVHPZGKQQ3QNNFSOIPIKBYUNYCLO",
)
check("extract: password rejected", totp.extract_seed("cibnez-0daqfu-Sokcuw") is None)
check("extract: other url rejected", totp.extract_seed("https://t.me/note") is None)
check("valid()", totp.valid(SEED) and not totp.valid("!!!"))

# ── credential parsing ───────────────────────────────────────────────────────
print("parse_credentials")

entries, errors = parse_credentials(
    "itdigging050@gmail.com wudnur-qeghef-6Nendo "
    "5lpg ul5m beax bjcy fhzz hj7h 6lts bfge (650) 735-1754"
)
check("format 1: one entry, no errors", len(entries) == 1 and not errors)
if entries:
    e = entries[0]
    check("format 1: email", e["email"] == "itdigging050@gmail.com")
    check("format 1: password verbatim", e["password"] == "wudnur-qeghef-6Nendo")
    check(
        "format 1: 8 backup-code groups",
        e["backup_codes"] == ["5lpg", "ul5m", "beax", "bjcy", "fhzz", "hj7h", "6lts", "bfge"],
    )
    check("format 1: no totp seed (phone ignored)", e["totp_seed"] is None)
    check("format 1: no recovery", e["recovery_email"] is None)

entries, errors = parse_credentials(
    "yoliem1974@gmail.com\n"
    "cibnez-0daqfu-Sokcuw\n"
    "\n"
    "  (415) 879-8182\n"
    "https://2fa.live/tok/btdbfvhpzgkqq3qnnfsoipikbyunyclo"
)
check("format 2: multi-line card merged", len(entries) == 1 and not errors)
if entries:
    e = entries[0]
    check("format 2: email", e["email"] == "yoliem1974@gmail.com")
    check("format 2: password", e["password"] == "cibnez-0daqfu-Sokcuw")
    check(
        "format 2: totp seed from 2fa.live",
        e["totp_seed"] == "BTDBFVHPZGKQQ3QNNFSOIPIKBYUNYCLO",
    )
    check("format 2: no backup codes", e["backup_codes"] == [])

entries, errors = parse_credentials(
    "user1@gmail.com pass1\n"
    "user2@gmail.com:pass2\n"
    "w3 user3@gmail.com pass3 recovery@gmail.com\n"
    "agent-01/w4 user4@gmail.com pass4"
)
check("classic formats: 4 entries", len(entries) == 4 and not errors)
if len(entries) == 4:
    check("inline colon password", entries[1]["password"] == "pass2")
    check("worker prefix", entries[2]["worker"] == 3)
    check("recovery email", entries[2]["recovery_email"] == "recovery@gmail.com")
    check("agent prefix", entries[3]["agent"] == "agent-01" and entries[3]["worker"] == 4)

entries, errors = parse_credentials(
    "card1@gmail.com\n"
    "pass-one\n"
    "https://2fa.live/tok/GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ\n"
    "card2@gmail.com\n"
    "pass-two\n"
    "1111 2222 3333 4444\n"
)
check("batch of cards: 2 entries", len(entries) == 2 and not errors)
if len(entries) == 2:
    check("card 1 seed", entries[0]["totp_seed"] == SEED)
    check("card 2 codes", entries[1]["backup_codes"] == ["1111", "2222", "3333", "4444"])
    check("card 2 password", entries[1]["password"] == "pass-two")

entries, errors = parse_credentials("user@gmail.com password note")
check("lone 4-char word is not a code", entries and entries[0]["backup_codes"] == [])

entries, errors = parse_credentials("user@gmail.com password 12345678")
check("single 8-digit code kept", entries and entries[0]["backup_codes"] == ["12345678"])

entries, errors = parse_credentials("user@gmail.com")
check("email alone errors out", not entries and errors)

# ── credential store ─────────────────────────────────────────────────────────
print("CredentialStore")
with tempfile.TemporaryDirectory() as td:
    path = Path(td) / "gv_accounts.json"
    store = CredentialStore(path=path, persist=True)
    store.put(
        ".chrome_profile_1", "user@gmail.com", "pw",
        recovery_email="rec@gmail.com",
        totp_seed=SEED,
        backup_codes=["aaaa", "bbbb", "12345678"],
    )
    store.mark_backup_code_used(".chrome_profile_1", "aaaabbbb")
    check("save() persists", store.save())

    fresh = CredentialStore(path=path, persist=True)
    check("load() reads back", fresh.load() == 1)
    entry = fresh.get(".chrome_profile_1")
    check("totp_seed survives", entry["totp_seed"] == SEED)
    check("backup codes survive", entry["backup_codes"] == ["aaaa", "bbbb", "12345678"])
    check("used codes survive", entry["used_backup_codes"] == ["aaaabbbb"])

    # Re-putting the account (operator resent the line) keeps the used list.
    fresh.put(".chrome_profile_1", "user@gmail.com", "pw2",
              backup_codes=["aaaa", "bbbb"])
    entry = fresh.get(".chrome_profile_1")
    check("re-put keeps used codes", entry["used_backup_codes"] == ["aaaabbbb"])
    check("re-put updates password", entry["password"] == "pw2")
    check("re-put without seed clears it", entry["totp_seed"] is None)

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s)")
    sys.exit(1)
print("ALL OK")
