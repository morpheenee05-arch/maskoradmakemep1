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


# ── X-GV-PlaceCallContext oracle ──────────────────────────────────────────────
#
# The web client stamps every INVITE with X-GV-PlaceCallContext — a ~5.5KB
# encrypted jspb blob produced in-page by `encryptPlaceCallContext` (seen in
# the V8 code cache). Without it Google sends the call out without caller ID
# (callee sees "Unknown"). The blob is per-call and cannot be forged outside
# the page, so we use the page itself as the generator:
#
#   PCC_HOOK_JS  wraps WebSocket.prototype.send; while armed it intercepts the
#                page's own INVITE, stores the header in window.__pccCapture,
#                SWALLOWS the frame (the page's call never hits the network)
#                and feeds the page stack a synthetic 603 Decline so it
#                settles immediately.
#   PCC_DIAL_JS  drives the dialer UI: finds the number input, types the
#                number, presses Enter / clicks the call button.

PCC_HOOK_JS = r"""
(function () {
  if (window.__pccHooked) return 'already';
  window.__pccHooked = true;
  window.__pccArm = false;
  window.__pccCapture = null;
  window.__pccKeys = [];
  window.__pccPlain = [];

  // ── WebCrypto hooks: grab the 64-byte key halves and the plaintext ──
  // The app builds a fresh Tink AES-CTR-HMAC crypter per call:
  //   importKey(raw, 32B, AES-CTR) + importKey(raw, 32B, HMAC/SHA-256),
  //   then subtle.encrypt(AES-CTR, key, plaintextBytes).
  try {
    var _ik = crypto.subtle.importKey.bind(crypto.subtle);
    crypto.subtle.importKey = function (format, keyData, alg, extractable, usages) {
      try {
        if (format === 'raw' && keyData) {
          var name = (alg && alg.name) || '';
          if (name === 'AES-CTR' || name === 'HMAC') {
            var bytes = new Uint8Array(keyData);
            var bin = '';
            for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
            window.__pccKeys.push({
              alg: name, hash: (alg.hash && alg.hash.name) || '',
              key: btoa(bin), ts: Date.now(),
            });
          }
        }
      } catch (e) {}
      return _ik(format, keyData, alg, extractable, usages);
    };
    var _enc = crypto.subtle.encrypt.bind(crypto.subtle);
    crypto.subtle.encrypt = function (alg, key, data) {
      try {
        if (alg && alg.name === 'AES-CTR' && data) {
          var bytes = new Uint8Array(data);
          var bin = '';
          for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
          window.__pccPlain.push({ data: btoa(bin), ts: Date.now() });
        }
      } catch (e) {}
      return _enc(alg, key, data);
    };
  } catch (e) {}

  function fake603(invite) {
    function h(name) {
      var m = invite.match(new RegExp('^' + name + ':[ \\t]*(.*)$', 'mi'));
      return m ? m[1].replace(/\r/g, '').trim() : '';
    }
    var via = h('Via'), fr = h('From'), to = h('To'), ci = h('Call-ID'), cs = h('CSeq');
    if (to.indexOf('tag=') === -1) to += ';tag=gvdecline';
    return 'SIP/2.0 603 Decline\r\n' +
      'Via: ' + via + '\r\n' +
      'From: ' + fr + '\r\n' +
      'To: ' + to + '\r\n' +
      'Call-ID: ' + ci + '\r\n' +
      'CSeq: ' + cs + '\r\n' +
      'Content-Length: 0\r\n\r\n';
  }

  var _send = WebSocket.prototype.send;
  WebSocket.prototype.send = function (data) {
    try {
      if (window.__pccArm && typeof data === 'string' && data.indexOf('INVITE sip:') === 0) {
        var m = data.match(/^X-GV-PlaceCallContext:[ \t]*jspb=(\S+)/mi);
        if (m) {
          window.__pccCapture = { header: m[1], ts: Date.now() };
        }
        var sock = this;
        var resp = fake603(data);
        setTimeout(function () {
          try { sock.dispatchEvent(new MessageEvent('message', { data: resp })); } catch (e) {}
        }, 30);
        return;  // swallowed — the page's call never reaches the network
      }
    } catch (e) {}
    return _send.apply(this, arguments);
  };
  return 'installed';
})();
"""

PCC_DIAL_JS = r"""
var number = arguments[0];
var done = arguments[arguments.length - 1];
var log = [];

function vis(el) {
  try { var r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }
  catch (e) { return false; }
}
function describe(el) {
  return el.tagName + '|' + (el.getAttribute('aria-label') || '') + '|' +
         (el.placeholder || '') + '|' + (el.textContent || '').trim().slice(0, 40);
}
function findInput() {
  var cands = document.querySelectorAll('input');
  for (var i = 0; i < cands.length; i++) {
    var el = cands[i];
    var s = ((el.getAttribute('aria-label') || '') + ' ' + (el.placeholder || '') +
             ' ' + (el.id || '') + ' ' + (el.name || '')).toLowerCase();
    if (/number|номер|phone|dial|телефон/.test(s) && vis(el)) return el;
  }
  return null;
}
function findCallStart() {
  var btns = document.querySelectorAll('button,[role="button"]');
  for (var i = 0; i < btns.length; i++) {
    var b = btns[i];
    var s = ((b.getAttribute('aria-label') || '') + ' ' + (b.textContent || '')).toLowerCase();
    if (/make a call|позвонить|new call|вызов/.test(s) && vis(b)) return b;
  }
  return null;
}
function findCallGo() {
  var btns = document.querySelectorAll('button,[role="button"]');
  for (var i = 0; i < btns.length; i++) {
    var b = btns[i];
    var a = (b.getAttribute('aria-label') || '').toLowerCase();
    if ((/^call\b/.test(a) || /^позвонить/.test(a)) && vis(b)) return b;
  }
  return null;
}
function typeNumber(input) {
  input.focus();
  var setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, number);
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  log.push('typed ' + number + ' into ' + describe(input));
}
function pressEnter(input) {
  ['keydown', 'keypress', 'keyup'].forEach(function (t) {
    input.dispatchEvent(new KeyboardEvent(t, {
      key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true,
    }));
  });
  log.push('pressed Enter');
}

var t0 = Date.now();
var dialed = false;
var clickedGo = false;
(function poll() {
  if (window.__pccCapture) {
    log.push('captured after ' + (Date.now() - t0) + 'ms');
    return done({ ok: true, log: log });
  }
  if (Date.now() - t0 > 25000) {
    var seen = Array.prototype.map.call(
      document.querySelectorAll('button,[role="button"],input'), describe);
    return done({ ok: false, log: log, seen: seen.slice(0, 60) });
  }
  var input = findInput();
  if (!input) {
    var start = findCallStart();
    if (start && !start.__pccClicked) {
      start.__pccClicked = true;
      start.click();
      log.push('clicked call-start: ' + describe(start));
    }
  } else if (!dialed) {
    dialed = true;
    typeNumber(input);
    pressEnter(input);
  } else if (dialed && !clickedGo && Date.now() - t0 > 6000) {
    var go = findCallGo();
    if (go) {
      clickedGo = true;
      go.click();
      log.push('clicked call-go: ' + describe(go));
    }
  }
  setTimeout(poll, 500);
})();
"""
