#!/usr/bin/env python3
"""One-off: extract sipregisterinfo POST request/response bodies from netlog."""
import gzip
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from netlog_sip_dump import load_netlog, iter_events, collect_socket_streams  # noqa: E402


def main():
    netlog = Path(sys.argv[1] if len(sys.argv) > 1 else "tests-requests/second_raw_log.json")
    events, event_names, source_names = load_netlog(netlog)

    # find the URL_REQUEST source id for sipregisterinfo
    target_sid = None
    for sid, sname, ename, t, params in iter_events(events, event_names, source_names):
        if "URL_REQUEST" in sname and params.get("url") and "sipregisterinfo" in params["url"]:
            target_sid = sid
            print(f"target URL_REQUEST source id={sid}")
            break
    if target_sid is None:
        print("sipregisterinfo request not found")
        return

    # dump ALL events for that source id
    print("\n── all events for the target request ──")
    for sid, sname, ename, t, params in iter_events(events, event_names, source_names):
        if sid != target_sid:
            continue
        keys = {}
        for k, v in params.items():
            s = str(v)
            keys[k] = s if len(s) < 2000 else s[:2000] + "…(truncated)"
        print(f"[{t:10.3f}] {ename}: {json.dumps(keys, ensure_ascii=False)[:2500]}")

    # socket streams: find the socket whose sent stream mentions sipregisterinfo
    streams = collect_socket_streams(events, event_names, source_names)
    for (sid, direction), data in sorted(streams.items()):
        if direction != "sent" or b"sipregisterinfo" not in data:
            continue
        print(f"\n── socket {sid} SENT ({len(data)} bytes) ──")
        head = data[:3000].decode("utf-8", "replace")
        print(head)
        recv = streams.get((sid, "recv"), b"")
        print(f"\n── socket {sid} RECV ({len(recv)} bytes) ──")
        m = re.search(rb"HTTP/1\.1 200[^\r]*\r\n(.*?)\r\n\r\n", recv, re.DOTALL)
        if m:
            print(m.group(0)[:1500].decode("utf-8", "replace"))
            body = recv[m.end():]
            # response may be content-length delimited; try raw then chunked
            for candidate in (body, _dechunk(body)):
                try:
                    out = gzip.decompress(candidate)
                    print("\n── gunzipped response body ──")
                    print(out.decode("utf-8", "replace"))
                    break
                except Exception:
                    continue
            else:
                print("\n(raw body tail, not gzipped or truncated):")
                print(body[:800])
        else:
            print(recv[:1500].decode("utf-8", "replace"))


def _dechunk(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while True:
        j = data.find(b"\r\n", i)
        if j < 0:
            return bytes(out)
        try:
            size = int(data[i:j].split(b";")[0], 16)
        except ValueError:
            return bytes(out)
        if size == 0:
            return bytes(out)
        i = j + 2
        out += data[i:i + size]
        i += size + 2


if __name__ == "__main__":
    main()
