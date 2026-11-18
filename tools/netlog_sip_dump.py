#!/usr/bin/env python3
"""
Parse a Chrome net-export log (netlog JSON) from a live Google Voice call
and extract everything needed to rebuild the SIP-over-WebSocket calling path.

Usage:
    py -3 tools/netlog_sip_dump.py captures/gv_call.netlog.json [-o captures/out]

What it pulls out (into the output dir):
    urls.txt          — every URL_REQUEST in the log (find the Voice API calls)
    http_*.txt        — interesting request/response headers (cookie-auth mint)
    ws_frames.txt     — all WebSocket frames, client frames unmasked
    sip.txt           — SIP messages found anywhere (WS payloads / sockets)
    summary printed to stdout

The parser is best-effort: netlog formats drift between Chrome versions, so
every section degrades gracefully instead of crashing.
"""
import argparse
import json
import re
import sys
from pathlib import Path


# ── netlog structure ─────────────────────────────────────────────────────────

def load_netlog(path: Path):
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    const = data.get("constants", {}) or {}

    def rev(table_name):
        table = const.get(table_name, {}) or {}
        out = {}
        for name, val in table.items():
            out[val] = name
            if isinstance(name, str) and name.isdigit():
                out[int(name)] = val
        return out

    event_names = rev("logEventTypes")
    source_names = rev("logSourceType")
    return data.get("events", []) or [], event_names, source_names


def iter_events(events, event_names, source_names):
    for ev in events:
        src = ev.get("source", {}) or {}
        etype = ev.get("type")
        ename = event_names.get(etype, str(etype))
        sname = source_names.get(src.get("type"), str(src.get("type")))
        try:
            t = float(ev.get("timeTicks", 0)) / 1e6
        except (TypeError, ValueError):
            t = 0.0
        yield src.get("id"), sname, ename, t, ev.get("params", {}) or {}


# ── collectors ───────────────────────────────────────────────────────────────

def collect_urls(events, event_names, source_names):
    """URL_REQUEST id -> {url, method, headers_out, headers_in, t0}."""
    reqs = {}
    for sid, sname, ename, t, params in iter_events(events, event_names, source_names):
        if "URL_REQUEST" not in sname:
            continue
        r = reqs.setdefault(sid, {})
        if ename == "URL_REQUEST_START_JOB" and params.get("url"):
            r["url"] = params.get("url")
            r["method"] = params.get("method", "?")
            r.setdefault("t0", t)
        elif ename == "HTTP_TRANSACTION_SEND_REQUEST_HEADERS":
            r["line_out"] = params.get("line", "")
            r["headers_out"] = params.get("headers", [])
        elif ename == "HTTP_TRANSACTION_READ_RESPONSE_HEADERS":
            r["line_in"] = params.get("line", "")
            r["headers_in"] = params.get("headers", [])
    return reqs


def collect_socket_streams(events, event_names, source_names):
    """(socket_id, direction) -> concatenated bytes in time order."""
    chunks = {}
    for sid, sname, ename, t, params in iter_events(events, event_names, source_names):
        if "SOCKET" not in sname or "BYTES" not in ename:
            continue
        raw = params.get("hex_bytes")
        if not raw:
            continue
        direction = "sent" if "SENT" in ename else "recv"
        try:
            data = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", raw))
        except ValueError:
            continue
        chunks.setdefault((sid, direction), []).append((t, data))
    streams = {}
    for key, parts in chunks.items():
        parts.sort(key=lambda p: p[0])
        streams[key] = b"".join(p[1] for p in parts)
    return streams


# ── WebSocket frame parsing ──────────────────────────────────────────────────

def parse_ws_frames(stream: bytes, start: int, masked: bool):
    """Yield (fin, opcode, payload) for every frame from `start`."""
    i = start
    n = len(stream)
    while i + 2 <= n:
        b1, b2 = stream[i], stream[i + 1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        has_mask = bool(b2 & 0x80)
        ln = b2 & 0x7F
        j = i + 2
        if ln == 126:
            if j + 2 > n:
                break
            ln = int.from_bytes(stream[j:j + 2], "big")
            j += 2
        elif ln == 127:
            if j + 8 > n:
                break
            ln = int.from_bytes(stream[j:j + 8], "big")
            j += 8
        mask = b""
        if has_mask:
            if j + 4 > n:
                break
            mask = stream[j:j + 4]
            j += 4
        if j + ln > n:
            break  # truncated tail — stop, keep what we have
        payload = stream[j:j + ln]
        if mask:
            payload = bytes(c ^ mask[k % 4] for k, c in enumerate(payload))
        yield fin, opcode, payload
        i = j + ln


def find_ws_handshake_end(recv: bytes) -> int:
    """Offset right after the 101 Switching Protocols header block."""
    m = re.search(rb"101[^\r\n]*\r\n(.*?\r\n\r\n)", recv, re.DOTALL)
    return m.end() if m else -1


def find_ws_request_end(sent: bytes) -> int:
    m = re.search(rb"GET [^\r\n]*\r\n(.*?\r\n\r\n)", sent, re.DOTALL)
    return m.end() if m else -1


# ── SIP extraction ───────────────────────────────────────────────────────────

SIP_METHODS = (
    b"INVITE ", b"ACK ", b"BYE ", b"CANCEL ", b"REGISTER ", b"OPTIONS ",
    b"PRACK ", b"UPDATE ", b"REFER ", b"NOTIFY ", b"SUBSCRIBE ", b"MESSAGE ",
)


def looks_like_sip(data: bytes) -> bool:
    head = data[:16]
    return head.startswith(b"SIP/2.0") or any(
        data.startswith(m) for m in SIP_METHODS
    )


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("netlog", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=None)
    args = ap.parse_args()

    out_dir = args.out or (Path("captures") / args.netlog.stem)
    out_dir.mkdir(parents=True, exist_ok=True)

    events, event_names, source_names = load_netlog(args.netlog)
    print(f"events: {len(events)}")

    # ── URLs ──
    reqs = collect_urls(events, event_names, source_names)
    url_lines = []
    for sid, r in sorted(reqs.items(), key=lambda kv: kv[1].get("t0", 0)):
        if r.get("url"):
            url_lines.append(f"[{r.get('t0', 0):10.3f}] {r.get('method','?'):6} {r['url']}")
    (out_dir / "urls.txt").write_text("\n".join(url_lines), encoding="utf-8")
    print(f"url requests: {len(url_lines)} → {out_dir/'urls.txt'}")

    # ── Interesting HTTP (Voice API, credential mint) ──
    interesting = []
    for sid, r in reqs.items():
        url = r.get("url", "")
        if not url:
            continue
        if any(k in url.lower() for k in ("voice", "sip", "client", "googleapis")):
            interesting.append((sid, r))
    http_files = []
    for idx, (sid, r) in enumerate(interesting):
        body = [
            f"{r.get('method','?')} {r.get('url','?')}",
            r.get("line_out", "").strip(),
            *[str(h) for h in r.get("headers_out", [])],
            "",
            r.get("line_in", "").strip(),
            *[str(h) for h in r.get("headers_in", [])],
        ]
        p = out_dir / f"http_{idx:02d}.txt"
        p.write_text("\n".join(body), encoding="utf-8")
        http_files.append(p)
    print(f"voice-ish http requests: {len(http_files)} → http_*.txt")

    # ── Socket streams → WS frames + SIP ──
    streams = collect_socket_streams(events, event_names, source_names)
    ws_out, sip_out = [], []
    sockets = {sid for sid, _ in streams}
    ws_count = 0
    for sid in sorted(sockets):
        sent = streams.get((sid, "sent"), b"")
        recv = streams.get((sid, "recv"), b"")
        is_ws = b"Upgrade: websocket" in sent or b"101 Switching" in recv
        if is_ws:
            ws_count += 1
            sent_end = find_ws_request_end(sent)
            recv_end = find_ws_handshake_end(recv)
            for label, buf, start, masked in (
                ("C→S", sent, sent_end, True),
                ("S→C", recv, recv_end, False),
            ):
                if start < 0:
                    continue
                for fin, opcode, payload in parse_ws_frames(buf, start, masked):
                    opname = {1: "text", 2: "bin", 8: "close", 9: "ping", 10: "pong"}.get(opcode, str(opcode))
                    if opcode in (9, 10):
                        continue
                    text = payload.decode("utf-8", "replace")
                    ws_out.append(
                        f"── {label} {opname} fin={int(fin)} len={len(payload)} ──\n{text}\n"
                    )
                    if looks_like_sip(payload):
                        sip_out.append(f"── {label} (ws frame) ──\n{text}\n")
        # raw SIP straight in a socket stream (non-WS transports)
        for label, buf in (("C→S", sent), ("S→C", recv)):
            if looks_like_sip(buf):
                head = buf.decode("utf-8", "replace")[:4000]
                sip_out.append(f"── {label} (raw socket) ──\n{head}\n")

    (out_dir / "ws_frames.txt").write_text("\n".join(ws_out), encoding="utf-8")
    (out_dir / "sip.txt").write_text("\n".join(sip_out), encoding="utf-8")
    print(f"websocket sockets: {ws_count}")
    print(f"ws frames extracted: {len(ws_out)} → ws_frames.txt")
    print(f"sip messages found: {len(sip_out)} → sip.txt")
    print()
    print("Next: look for the credential-mint call in urls.txt "
          "(a Voice/clients googleapis POST whose http_*.txt response pairs "
          "with a SIP REGISTER in sip.txt).")


if __name__ == "__main__":
    sys.exit(main())
