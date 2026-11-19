"""Harvest Google auth cookies from a live signed-in Chrome (Selenium).

CDP Storage.getCookies sees HttpOnly cookies (SID/HSID/SAPISID) —
document.cookie does not, and the Voice API endpoints want the full set.
Shared by the SIP test path (agent/main.py) and the SIP worker pool.
"""
from __future__ import annotations

import time


def harvest_google_cookies(driver, timeout: float = 40.0,
                           navigate: bool = True) -> dict:
    """Return {name: {"value":..., "domain":...}} for .google.com cookies.

    Raises RuntimeError if no SAPISID-family cookie shows up in time.
    Blocking (Selenium) — call from a thread.
    """
    if navigate and "voice.google.com" not in (driver.current_url or ""):
        driver.get("https://voice.google.com/u/0/calls")
    deadline = time.time() + timeout
    while time.time() < deadline:
        cookies = {}
        try:
            res = driver.execute_cdp_cmd("Storage.getCookies", {})
            for c in res.get("cookies", []):
                if "google.com" in (c.get("domain") or ""):
                    cookies[c["name"]] = {
                        "value": c.get("value", ""),
                        "domain": c.get("domain", ""),
                    }
        except Exception:
            cookies = {}
        if not cookies:
            raw = driver.execute_script("return document.cookie") or ""
            cookies = {
                p.strip().split("=", 1)[0]: p.strip().split("=", 1)[1]
                for p in raw.split(";") if "=" in p
            }
        if any(k in cookies for k in (
                "SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")):
            return cookies
        time.sleep(2)
    raise RuntimeError(
        "no SAPISID-family cookies on voice.google.com — "
        "session expired? (re-login this worker)")
