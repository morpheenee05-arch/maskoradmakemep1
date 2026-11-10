# AI Caller - Telegram Bot + Telephony

Ultra-low latency outbound AI caller.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set up .env file with your credentials
cp .env.example .env

# Run the bot
python main.py
```

## Telegram Commands

- `/call <phone_number>` - Make an outbound AI call
- `/p1 <phone> [phone2] [phone3]` - Scripted IVR call (bulk supported)
- `/p1stop` - Stop bulk queue
- `/settings` - Switch script profile
- `/status` - Check system status
- `/hangup [call_id]` - End an active call
- `/help` - Show all commands
- `/spamcheck [seq] [pN] [secs]` - Profiles ring each other (no answer), then check the callee's history for a "Suspected spam" flag on the caller's number
- `/accounts [@agent]` - Which Chrome profiles hold a live Google session, and which need a login
- `/login` - Send the logins/passwords for those profiles; the agents sign them in automatically
- Send a `.txt` file to extract numbers for bulk calling

## Architecture

```
Telegram Bot → Telephony (Selenium + undetected_chromedriver) → Google Voice → Phone
                              ↓
                    Audio Pipeline (subprocess isolation)
                    ├── Capture: WASAPI loopback → FFT DTMF detection (freq-precision + contrast + harmonic gates)
                    ├── Playback: Deepgram TTS → Virtual Audio Cable
                    └── STT: Deepgram streaming WebSocket
```

## Deploying on a new Windows / RDP server

This is a Windows-bound app (Selenium + undetected_chromedriver, WASAPI/VAC
audio). It is NOT containerizable as-is — provision it on the box directly.

1. **Get the code**
   ```
   git clone <repo-url> ai-caller && cd ai-caller
   ```
2. **Python deps** (Python 3.13)
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
   `requirements.txt` is pinned. Pin `deepgram-sdk` / `soxr` from your runtime
   the first time (`pip freeze | findstr /I "deepgram-sdk soxr"`).
3. **Chromedriver** — drop the matching `chromedriver-win64\chromedriver.exe`
   into the project root (kept out of git). If your Chrome major version is
   not 146, set it in `.env`:
   ```
   CHROME_VERSION_MAIN=<your_major>
   ```
   Custom driver location? Set `CHROMEDRIVER_PATH=` in `.env`.
4. **Config** — `copy .env.example .env`, fill `TELEGRAM_BOT_TOKEN`,
   `DEEPGRAM_API_KEY`, `GV_MAX_WORKERS`, etc.
5. **Audio drivers** — required for `/call` / `/p1` (AI audio).
   See **[Audio drivers (Virtual Audio Cable)](#audio-drivers-virtual-audio-cable)**.
   `/spamcheck` does **not** need them (set `GV_FAKE_AUDIO=1`).
6. **Profiles** — see [Profile backup / restore](#profile-backup--restore-avoid-re-login-per-server).
7. **Run**
   ```
   python main.py
   ```
8. **Multiple servers?** — see [Cluster mode](#cluster-mode-controller--agents).

## Profile backup / restore (avoid re-login per server)

Chrome profiles hold the Google Voice session and **cannot be committed to
git**. Log in once, then move profiles between servers as an archive.

- **First time / source machine** — log every worker profile in:
  ```
  python login_helper.py        # enter profile id, sign in to GV, Ctrl+C
  ```
  Then snapshot all profiles into one zip:
  ```
  powershell -ExecutionPolicy Bypass -File tools\backup_profiles.ps1
  ```
  Output: `profile_backups\profiles_<timestamp>.zip`.

- **New server** — copy that zip over and restore:
  ```
  powershell -ExecutionPolicy Bypass -File tools\restore_profiles.ps1
  ```
  (uses the newest zip in `profile_backups\`, or pass `-ZipPath`).

Sessions can still be challenged by Google on a new IP — if a profile lands
on the auth page, re-run `login_helper.py` for just that profile.

## Audio drivers (Virtual Audio Cable)

WebRTC calls in Google Voice need a real audio input. A Windows Server /
RDP VM has **no audio device by default**.

- `/spamcheck` (ring + spam check, no AI audio): you do **not** need drivers —
  set `GV_HEADLESS=1` and `GV_FAKE_AUDIO=1` in `.env`; Chrome uses a built-in
  synthetic mic and the call connects without any cable.
- `/call` and `/p1` (live AI audio with Deepgram): you **need** real virtual
  audio cables. Set `GV_FAKE_AUDIO=0` (or omit it) on these nodes.

### 1. Enable Windows audio services (Server only — off by default)

```powershell
Set-Service Audiosrv -StartupType Automatic
Set-Service AudioEndpointBuilder -StartupType Automatic
Start-Service AudioEndpointBuilder
Start-Service Audiosrv
```

### 2. Install Virtual Audio Cable

Use **Virtual Audio Cable (VAC) by Eugene Muzychenko** — official site
**vac.muzychenko.net** (paid). It creates multiple cables named
`Line 1`, `Line 2`, … which is exactly what `config.py` expects.
VB-CABLE (only 1–2 cables) is **not** sufficient.

Run the installer over RDP (it has a GUI). The driver is signed; a reboot
may be required.

### 3. Configure 16 cables

In **VAC Control Panel**: set `Cables = 16` (8 workers × capture+playback
pair). Format 16-bit, 48000 Hz (the app resamples via `soxr`). Keep the
default cable names `Line 1` … `Line 16` — do not rename.

`config.WORKER_POOL` maps: W1 → capture `Line 1` / playback `Line 2`,
W2 → `Line 3/4`, … W8 → `Line 15/16`.

### 4. Verify the OS and the app see the devices (headless)

```powershell
Get-CimInstance Win32_SoundDevice | Select-Object Name,Status
```
```
python -c "import sounddevice; print(sounddevice.query_devices())"
```
You must see both input and output entries `Line 1 … Line 16`. Empty list =
the audio service from step 1 is not running.

### 5. Map each profile in Google Voice

Run `python login_helper.py` per profile — it prints which `Line` to set as
**Microphone / Speaker** in Google Voice settings for that worker. Set it
once per account.

> Caveat: device names are matched by substring, so `Line 1` also matches
> `Line 10`–`Line 16`. With 16 cables verify (via `query_devices()`) that
> each worker binds the intended device.

## Cluster mode (controller + agents)

Run one **controller** (Telegram bot + dispatcher) and many **agent**
servers (each runs a local pool). Selected via `NODE_ROLE` in `.env`:

| Role | Runs | Needs |
|------|------|-------|
| `standalone` (default) | bot + local pool (single box) | Telegram + Deepgram |
| `controller` | Telegram bot, aggregates agents | Telegram + `REDIS_URL` |
| `agent` | local pool, no Telegram | Deepgram + `REDIS_URL` |

### 1. Redis on the controller (Windows: Memurai)

Install Memurai (runs as a service on 6379). In `memurai.conf`:
```
requirepass <ALPHANUMERIC_ONLY>   # no special chars — they break the URL
bind 0.0.0.0
protected-mode no
```
`net stop Memurai && net start Memurai` after edits. Open inbound TCP 6379
to **the agent IP only** (Windows Firewall + the host provider's firewall):
```powershell
New-NetFirewallRule -DisplayName "Redis from agent" -Direction Inbound `
  -Protocol TCP -LocalPort 6379 -RemoteAddress <AGENT_PUBLIC_IP> -Action Allow
```

### 2. `.env`

Controller:
```
NODE_ROLE=controller
REDIS_URL=redis://:PASS@127.0.0.1:6379/0
TELEGRAM_BOT_TOKEN=<token>
```
Agent:
```
NODE_ROLE=agent
REDIS_URL=redis://:PASS@<CONTROLLER_PUBLIC_IP>:6379/0
AGENT_ID=srv-1
DEEPGRAM_API_KEY=<key>
GV_FAKE_AUDIO=0        # 1 only if this agent does spamcheck-only
```

### 3. Verify the bus (no Chrome needed)

On **both** machines:
```
python -m tools.bus_selftest
```
Expect `-- ALL CHECKS PASSED --`. It prints the exact reason on failure
(WRONGPASS / connection refused / NOAUTH / bad URL).

### 4. Run & monitor

- Controller: `python main.py` → bot online + live controller panel
  (connected agents, workers, heartbeats, Redis status).
- Agent: `python main.py` → auto-routes to agent, registers, heartbeats.
- Telegram `/status` lists every live agent; a dead agent drops off
  automatically after `AGENT_TTL_SEC` (~35 s).

### Onboard a new agent (checklist)

1. Copy the **whole** project folder (never partial — version skew breaks it).
2. `py -m pip install -r requirements.txt`
3. Restore Chrome profiles: `tools\restore_profiles.ps1`
4. Audio drivers (if it will run `/call` / `/p1`) — see section above.
5. `.env` with `NODE_ROLE=agent`, `REDIS_URL`, `AGENT_ID`, `DEEPGRAM_API_KEY`.
6. `python -m tools.bus_selftest` → PASSED.
7. `python main.py` → appears in `/status` on the bot.

## Automatic Google sign-in

A worker is only useful while its Chrome profile holds a live Google session,
and those sessions die: cookies expire, Google signs an account out, or a
profile was never signed in at all. Instead of hand-driving each browser, the
pool classifies every profile at startup and signs in the ones it can.

Three shapes are told apart, because they need different handling:

- **Signed in** — the Voice dialer renders. Nothing to do.
- **Never signed in** — the voice.google.com landing page ("A professional-grade
  phone plan…") with its Sign in link. The link carries `target="_blank"`, so
  the form opens in a *second tab*; the worker switches to it, fills the email,
  presses Далее/Next, fills the password, presses it again, and folds the tabs
  back into one.
- **Expired** — neither a sign-in form nor a working dialer. The cookies are
  dropped (the browser keeps running — no respawn needed), the landing page is
  reopened, and the sign-in above runs from scratch.

On the controller, `/accounts` polls every agent and lists which profiles need a
login. `/login` takes them all in one message:

```
/login
user1@gmail.com password1
user2@gmail.com password2
```

Lines are matched to the `/accounts` listing in order; `user@gmail.com:password`
and an explicit `agent-01/w3 user@gmail.com password` prefix also work. The
controller routes each account to the agent that owns the profile, and the agent
signs `GV_LOGIN_CONCURRENCY` browsers in at a time (each worker has its own
Chrome, so these are genuinely parallel). The agent watchdog retries a profile
whose session dies later on, so a long run does not bleed capacity.

Sign-ins that a password cannot fix are reported instead of retried: a rejected
password, an unknown account, a Google throttle, and above all a 2FA or captcha
prompt (`🧩 2FA needed`) — retrying those is how accounts get locked. Sign such
a profile in by hand once with `python login_helper.py` (it opens a visible
Chrome on that profile) and the session persists afterwards. `/status` shows how
many profiles are in each bucket.

**Credentials handling.** They travel operator → controller → Redis → agent in
plaintext, so keep Redis private. The Telegram message is deleted the moment it
is parsed (give the bot "Delete messages" rights in the chat), the job entry is
removed from the stream when the agent acks it, and nothing is ever written to
the log — only `<N chars>`. Agents hold them in memory only unless
`GV_SAVE_CREDENTIALS=1`, which caches them to `storage/gv_accounts.json`
(mode 0600, gitignored) so a restart can sign in unattended.

## Chrome memory guard (worker "out of memory" crashes)

The Google Voice dialer is a long-lived Angular app: every call adds history
rows, listeners and closures that it never releases. A worker that keeps the
same tab for hundreds of calls eventually exhausts the V8 heap, and the
renderer dies with `out of memory` / `tab crashed`. Three things prevent that:

- **The tab is reloaded every `GV_RECYCLE_AFTER_CALLS` calls** (default 25),
  and Chrome is respawned every `GV_RESTART_AFTER_CALLS` calls (default 200).
  Recycling only ever happens between calls, while the worker is idle.
- **Memory is measured after every call.** Crossing `GV_JS_HEAP_SOFT_PCT` of
  the V8 limit or `GV_WORKER_RSS_SOFT_MB` triggers a reload;
  `GV_WORKER_RSS_HARD_MB` triggers a respawn. Proactive respawns are postponed
  while the host has less than `GV_HOST_FREE_MB_MIN` free, since launching a
  second Chrome during a crunch is what invites the Linux OOM killer.
- **A browser that dies anyway is restarted automatically.** The crash is
  recognised from the driver's message, the worker leaves the pool, its process
  tree is reaped and a fresh Chrome logs back into the same profile — in the
  background, so the run keeps going. The agent watchdog retries any worker
  still down, giving up only after `GV_MAX_RESTARTS` consecutive failures.

`GV_JS_HEAP_MB` (default 512) caps the heap per tab. Do not set it much lower:
the old hard-coded 192 MB is what crashed the renderer mid-run.

The live dashboard shows `RAM`, `heap %` and the call count per worker, plus
`♻️ Restarting` while a browser is being respawned. Agent logs carry a
`Chrome memory: total=… host_free=… restarts=… oom=…` line every 5 minutes.
Install `psutil` (in `requirements.txt`) on agents — without it the guard still
works but loses RSS accounting and orphan-process reaping.

## Fleet updates (git + one click in the bot)

The repo is the single source of truth. One-time setup per server, then all
updates are one button.

### One-time bootstrap (each server)

```bash
# Controller (Ubuntu):
git clone <repo-url> /opt/controller
cd /opt/controller && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# copy your .env into /opt/controller/.env   (untracked — survives updates)

# Agent (Windows RDP), in the project folder:
git clone <repo-url> C:\ai-caller
cd C:\ai-caller; py -3 -m venv .venv; .venv\Scripts\pip install -r requirements.txt
# copy .env (NODE_ROLE=agent, REDIS_URL, AGENT_ID) into C:\ai-caller\.env
```

`.env`, Chrome profiles, `storage/*.json` (prompts, scripts, credentials) and
logs are all git-ignored — updates never wipe them.

### Updating the whole fleet

1. Commit and push your changes to the repo.
2. In the bot: `/menu` → ⚙️ Settings → **📦 Fleet update** → ✅.
3. The controller pulls itself; every agent picks the signal up in its
   watchdog (~20–40s), runs `git fetch + reset --hard` and restarts itself
   gracefully (calls in flight finish their shutdown path first).
4. Check versions in 🖥 Agents — each agent reports its commit hash in the
   heartbeat.

If an agent is not a git checkout, it logs `Fleet update FAILED … staying on
old code` once per signal and keeps running — bootstrap git there when handy.
