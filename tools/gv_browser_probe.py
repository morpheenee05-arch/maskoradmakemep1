#!/usr/bin/env python3
"""
Browser-call probe: capture EVERYTHING about a real Google Voice web dialer
call so we can diff it against our SIP stack and find what gives us away.

Run ON AN AGENT (Windows RDP). The browser opens VISIBLE — you log in and
place ONE call by hand; the script records the rest.

    py -3 tools/gv_browser_probe.py                     # fresh probe profile, manual login
    py -3 tools/gv_browser_probe.py --worker 1          # reuse pool profile .chrome_profile_1
                                                        # (stop the agent first — one Chrome per profile!)
    py -3 tools/gv_browser_probe.py --profile D:\\path\\to\\profile

What it captures (into tools/gv_probe_out/<timestamp>/):
  webrtc_probe.json — full SDP offers/answers, ICE candidates, connection
                      events, getStats() timeline (codecs, bitrate, jitter,
                      packet timing) from every frame of the GV page
  network.jsonl     — CDP performance log: every HTTP request the dialer
                      makes (call setup RPCs, headers, timings)
  summary.txt       — human-readable highlights: negotiated codecs, opus
                      params, telephone-event config, ICE/DTLS details,
                      packetization — the stuff bans are made of
"""
import argparse
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

GV_URL = "https://voice.google.com/u/0/calls"

# Injected BEFORE any page script in every frame (addScriptToEvaluateOnNew
# Document). Wraps RTCPeerConnection and records the whole call lifecycle
# into window.__GV_PROBE.
HOOK_JS = r"""
(function(){
  if (window.__GV_PROBE_INSTALLED) return "already";
  window.__GV_PROBE_INSTALLED = true;
  var P = window.__GV_PROBE = {
    t0: Date.now(), href: location.href, ua: navigator.userAgent,
    sdps: [], candidates: [], stats: [], configs: [], events: []
  };
  var OrigPC = window.RTCPeerConnection;
  if (!OrigPC) return "no-rtcpeerconnection";
  var INTERESTING = {"codec":1,"outbound-rtp":1,"inbound-rtp":1,
    "remote-inbound-rtp":1,"remote-outbound-rtp":1,"candidate-pair":1,
    "local-candidate":1,"remote-candidate":1,"media-source":1,"transport":1};
  window.RTCPeerConnection = function(cfg, legacy){
    var pc = new OrigPC(cfg, legacy);
    var id = P.configs.length;
    P.configs.push({t: Date.now(), cfg: JSON.stringify(cfg || null)});
    function wrap(method, dir){
      var orig = pc[method].bind(pc);
      pc[method] = function(desc){
        try {
          P.sdps.push({t: Date.now(), pc: id, dir: dir,
            type: desc && desc.type || null,
            sdp: desc && desc.sdp || null});
        } catch(e){}
        return orig.apply(pc, arguments);
      };
    }
    wrap("setLocalDescription", "local");
    wrap("setRemoteDescription", "remote");
    pc.addEventListener("icecandidate", function(e){
      try { P.candidates.push({t: Date.now(), pc: id,
        cand: e.candidate ? e.candidate.candidate : null}); } catch(_){}
    });
    ["connectionstatechange","iceconnectionstatechange",
     "signalingstatechange","negotiationneeded"].forEach(function(ev){
      pc.addEventListener(ev, function(){
        try { P.events.push({t: Date.now(), pc: id, ev: ev,
          conn: pc.connectionState, ice: pc.iceConnectionState,
          sig: pc.signalingState}); } catch(_){}
      });
    });
    var timer = setInterval(function(){
      if (pc.connectionState === "closed") { clearInterval(timer); return; }
      pc.getStats().then(function(rs){
        var slim = [];
        try {
          rs.forEach(function(r){
            if (INTERESTING[r.type]) slim.push(r);
          });
          if (slim.length) P.stats.push({t: Date.now(), pc: id, rs: slim});
        } catch(_){}
      }).catch(function(){});
    }, 1000);
    return pc;
  };
  window.RTCPeerConnection.prototype = OrigPC.prototype;
  return "ok";
})();
"""

COLLECT_JS = "return JSON.stringify(window.__GV_PROBE || null);"


def _print(*a):
    try:
        print(*a)
    except UnicodeEncodeError:
        print(*[str(x).encode("ascii", "replace").decode() for x in a])


def spawn_browser(profile_dir: Path, port: int):
    import undetected_chromedriver as uc
    from selenium.webdriver.chrome.service import Service

    profile_dir.mkdir(parents=True, exist_ok=True)
    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--window-size=1280,860")
    options.add_argument("--use-fake-ui-for-media-stream")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.page_load_strategy = "eager"
    # Network capture via the CDP performance log.
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver_path = config.resolve_chromedriver_path()
    service = Service(executable_path=str(driver_path), port=port,
                      service_args=["--host=127.0.0.1"])
    driver = uc.Chrome(
        options=options,
        service=service,
        driver_executable_path=str(driver_path),
        use_subprocess=True,
        version_main=config.resolve_chrome_version_main(),
        keep_alive=True,
    )
    return driver


def install_hook(driver) -> str:
    """Register the hook for future documents AND inject into live frames."""
    try:
        driver.execute_cdp_cmd("Page.enable", {})
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument", {"source": HOOK_JS}
        )
    except Exception as e:
        _print(f"  ! CDP addScriptToEvaluateOnNewDocument failed: {e}")
    results = []
    try:
        driver.switch_to.default_content()
        results.append(driver.execute_script(
            f"(function(){{ var s = document.createElement('script');"
            f" s.textContent = {json.dumps(HOOK_JS)};"
            f" document.documentElement.prepend(s); s.remove();"
            f" return window.__GV_PROBE_INSTALLED ? 'ok' : 'fail'; }})()"))
    except Exception as e:
        results.append(f"main: {e}")
    frames = driver.find_elements("tag name", "iframe")
    for el in frames:
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(el)
            results.append(driver.execute_script(
                f"(function(){{ var s = document.createElement('script');"
                f" s.textContent = {json.dumps(HOOK_JS)};"
                f" document.documentElement.prepend(s); s.remove();"
                f" return window.__GV_PROBE_INSTALLED ? 'ok' : 'fail'; }})()"))
        except Exception as e:
            results.append(f"frame: {type(e).__name__}")
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    return ", ".join(str(r) for r in results)


def collect_probes(driver) -> list:
    """Read window.__GV_PROBE from the main page and every iframe."""
    out = []
    targets = [("main", None)]
    try:
        for el in driver.find_elements("tag name", "iframe"):
            targets.append(("iframe", el))
    except Exception:
        pass
    for kind, el in targets:
        try:
            driver.switch_to.default_content()
            if el is not None:
                driver.switch_to.frame(el)
            raw = driver.execute_script(COLLECT_JS)
            if raw:
                data = json.loads(raw)
                if data and (data.get("sdps") or data.get("events")):
                    data["_frame"] = kind
                    data["_href"] = data.get("href", "")
                    out.append(data)
        except Exception:
            continue
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    return out


def summarize(probes: list) -> str:
    lines = []
    for p in probes:
        lines.append(f"== frame: {p.get('_frame')} · {p.get('_href','')[:90]}")
        lines.append(f"   UA: {p.get('ua','')}")
        answers = [s for s in p.get("sdps", []) if s.get("type") == "answer"]
        offers = [s for s in p.get("sdps", []) if s.get("type") == "offer"]
        lines.append(f"   SDPs: {len(offers)} offer(s), {len(answers)} answer(s)")
        sdp = (answers or offers or [{}])[-1].get("sdp") or ""
        if not sdp:
            continue
        m_lines = [l for l in sdp.splitlines() if l.startswith("m=audio")]
        for l in m_lines:
            lines.append(f"   {l}")
        for l in sdp.splitlines():
            if any(k in l for k in ("rtpmap", "fmtp", "rtcp-fb", "extmap",
                                    "fingerprint", "setup:", "ptime",
                                    "maxptime", "rtcp-mux", "ice-ufrag",
                                    "ice-pwd", "mid")):
                lines.append(f"   {l.strip()}")
        cands = [c["cand"] for c in p.get("candidates", []) if c.get("cand")]
        lines.append(f"   ICE candidates seen: {len(cands)}")
        for c in cands[:12]:
            lines.append(f"     {c}")
        # packet timing from outbound-rtp stats
        for st in p.get("stats", [])[-3:]:
            for r in st.get("rs", []):
                if r.get("type") == "outbound-rtp" and r.get("kind") == "audio":
                    lines.append(
                        f"   outbound-rtp: packets={r.get('packetsSent')} "
                        f"bytes={r.get('bytesSent')} "
                        f"headerBytes={r.get('headerBytesSent')} "
                        f"codec={r.get('codecId')} mid={r.get('mid')}"
                    )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", type=int, default=None,
                    help="reuse pool profile .chrome_profile_N (stop the agent first!)")
    ap.add_argument("--profile", type=str, default=None,
                    help="explicit Chrome profile dir")
    ap.add_argument("--out", type=str, default=None)
    ap.add_argument("--port", type=int, default=19900)
    args = ap.parse_args()

    if args.worker is not None:
        profile_dir = config.BASE_DIR / f".chrome_profile_{args.worker}"
        if not profile_dir.exists():
            raise SystemExit(f"profile not found: {profile_dir}")
    elif args.profile:
        profile_dir = Path(args.profile)
    else:
        profile_dir = config.BASE_DIR / ".gv_probe_profile"

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else (
        Path(__file__).resolve().parent / "gv_probe_out" / stamp
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    _print(f"[*] Profile: {profile_dir}")
    _print(f"[*] Output:  {out_dir}")
    driver = spawn_browser(profile_dir, args.port)
    netlog = []
    stop_flag = threading.Event()

    def drain_netlog():
        while not stop_flag.is_set():
            try:
                for e in driver.get_log("performance"):
                    netlog.append(e)
            except Exception:
                pass
            time.sleep(1.5)

    t = threading.Thread(target=drain_netlog, daemon=True)
    t.start()

    try:
        driver.get(GV_URL)
        _print("\n[1] Браузер открыт на voice.google.com.")
        _print("    Залогинься в GV-аккаунт (обычный логин, 2FA — как всегда).")
        input("    Когда увидишь дайлер — нажми Enter здесь... ")

        _print("[2] Ставлю перехватчики WebRTC и перезагружаю страницу…")
        res = install_hook(driver)
        _print(f"    hook inject: {res}")
        driver.refresh()
        time.sleep(6)
        res = install_hook(driver)  # и в свежие фреймы сразу
        _print(f"    hook re-inject: {res}")

        _print("\n[3] Сделай ОДИН звонок с дайлера на свой номер,")
        _print("    поговори 20–30 секунд (или послушай), повесь трубку.")
        probes = []
        while True:
            cmd = input("    Когда звонок завершён — нажми Enter (или 's' + Enter для промежуточного снапшота)... ").strip().lower()
            probes = collect_probes(driver)
            n_sdp = sum(len(p.get("sdps", [])) for p in probes)
            _print(f"    captured: frames={len(probes)} sdp_events={n_sdp} netlog={len(netlog)}")
            if cmd != "s":
                break

        _print("[4] Сохраняю дамп…")
        (out_dir / "webrtc_probe.json").write_text(
            json.dumps(probes, indent=1, ensure_ascii=False), encoding="utf-8")
        with open(out_dir / "network.jsonl", "w", encoding="utf-8") as fh:
            for e in netlog:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        summary = summarize(probes)
        (out_dir / "summary.txt").write_text(summary, encoding="utf-8")
        _print("\n" + summary[:4000])
        _print(f"\n[✓] Всё сохранено в {out_dir}")
        _print("    Пришли мне: webrtc_probe.json + summary.txt (+ network.jsonl если попрошу)")
    finally:
        stop_flag.set()
        _print("\n[*] Enter — закрыть браузер…")
        try:
            input()
        except Exception:
            pass
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
