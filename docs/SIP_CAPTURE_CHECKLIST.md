# SIP capture checklist (Phase 0)

Goal: capture one **real** Google Voice web call so we can replay it
(SIP-over-WebSocket + WebRTC media). Do this in a regular Chrome with a test
account — never in an automation profile.

## Files we need at the end (put them in `captures/`)

1. `*.netlog.json` — Chrome net-export, **with cookies/credentials enabled**
2. `webrtc_internals_dump*.json` — from chrome://webrtc-internals
3. Screenshots of DevTools → Network → WS → Frames (first ~30 frames)
4. `notes.txt` — call time, callee number, account email, anything odd

> These files contain live cookies and SIP credentials. `captures/` is
> git-ignored — do not commit or send them anywhere public.

## Step by step

1. Regular Chrome → sign into `voice.google.com` with the test account.
   Close unrelated tabs (cleaner log).
2. New tab → `chrome://net-export` → pick the output file → tick
   **"Include cookies and credentials"** → **Start Logging**.
3. Voice tab → F12 → **Network** panel → filter **WS**. Keep it open.
4. New tab → `chrome://webrtc-internals`. Keep it open.
5. In Voice, call your own mobile. Let it ring and connect. **Hold the call
   40–60 s** (we want the session-timer re-INVITE Google sends ~30 s in).
   Press a couple of keypad digits in the Voice UI. Hang up.
6. `chrome://net-export` → **Stop Logging**.
7. DevTools → WS → click the socket → **Frames** tab → screenshot the first
   ~30 frames (backup if the parser misses something).
8. `chrome://webrtc-internals` → expand the PeerConnection →
   **"Download the PeerConnection updates and stats data"** (JSON with SDP,
   codecs, ICE).
9. Write `captures/notes.txt`: account email, callee, call timestamp.

## Then run

```bash
py -3 tools/netlog_sip_dump.py captures/<your>.netlog.json
```

It writes `urls.txt`, `http_*.txt`, `ws_frames.txt`, `sip.txt` into
`captures/<name>/`. What we look for:

- **Credential mint**: a Voice/clients*.google.com POST (protojson body)
  whose response carries the SIP username/password — pairs with the
  `REGISTER` that follows in `sip.txt`.
- **wss endpoint + headers**: `Origin`, cookies, User-Agent on the Upgrade.
- **SIP flow**: `REGISTER` (expect 401 → digest → 200), then
  `INVITE → 100 → 183 (PRACK) → 200 → ACK`, `BYE` at hangup.
- **SDP shape**: from webrtc-internals (Opus? telephone-event payload types,
  ICE ufrag/pwd, DTLS fingerprint).
