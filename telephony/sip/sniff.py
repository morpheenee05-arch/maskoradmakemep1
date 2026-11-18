"""
In-page capture of the real sipregisterinfo exchange.

The 401s on the replayed API call proved the SAPISIDHASH recipe has a
detail we can't see from outside (origin string, cookie set, header mix).
Rather than guessing, we let the browser make the call itself and sniff it:

  * Page.addScriptToEvaluateOnNewDocument installs an XHR hook in EVERY
    frame — including the gapi proxy iframe on clients6.google.com where
    the request actually runs.
  * The hook records request headers, request body and response body of any
    XHR to *sipregisterinfo* and ships it to the top frame via postMessage
    (cross-origin safe).
  * Selenium then reads window.__sipCredCapture from the top page.

The captured Authorization header is compared against locally computed
SAPISIDHASH variants to pin down the exact origin string Google expects —
that result feeds back into gv_auth.py.
"""
import hashlib
import re
from typing import Mapping, Optional

SIP_HOOK_JS = r"""
(function () {
  if (window.__sipHooked) return;
  window.__sipHooked = true;

  if (window.top === window) {
    window.addEventListener('message', function (ev) {
      if (ev.data && ev.data.__sipCred) {
        window.__sipCredCapture = ev.data.__sipCred;
      }
    });
  }

  function report(data) {
    try {
      if (window.top === window) {
        window.__sipCredCapture = data;
      } else {
        window.top.postMessage({ __sipCred: data }, '*');
      }
    } catch (e) {}
  }

  var _open = XMLHttpRequest.prototype.open;
  var _setH = XMLHttpRequest.prototype.setRequestHeader;
  var _send = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url) {
    this.__capUrl = String(url || '');
    this.__capHeaders = {};
    return _open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    if (this.__capHeaders) this.__capHeaders[k] = v;
    return _setH.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    var xhr = this;
    try {
      if (xhr.__capUrl.indexOf('sipregisterinfo') !== -1) {
        var reqBody = arguments.length ? String(arguments[0]) : '';
        xhr.addEventListener('load', function () {
          report({
            url: xhr.__capUrl,
            reqHeaders: xhr.__capHeaders,
            reqBody: reqBody,
            status: xhr.status,
            body: xhr.responseText,
          });
        });
      }
    } catch (e) {}
    return _send.apply(this, arguments);
  };
})();
"""

# Candidate origins for the SAPISIDHASH input, in likelihood order.
_ORIGIN_CANDIDATES = (
    "https://voice.google.com",
    "https://clients6.google.com",
)


def _cookie_value(cookies: Mapping, name: str) -> str:
    v = cookies.get(name)
    if isinstance(v, dict):
        return v.get("value", "")
    return v or ""


def diagnose_auth_origin(authorization: str, cookies: Mapping) -> Optional[str]:
    """Compare a sniffed Authorization header against locally computed
    SAPISIDHASH values; return the origin string that matches (or None)."""
    m = re.search(r"SAPISIDHASH (\d+)_([0-9a-f]{40})", authorization or "")
    sapisid = _cookie_value(cookies, "SAPISID")
    if not m or not sapisid:
        return None
    ts, captured = m.group(1), m.group(2)
    for origin in _ORIGIN_CANDIDATES:
        digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
        if digest == captured:
            return origin
    return None
