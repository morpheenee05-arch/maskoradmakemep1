"""
Telephony Worker Pool — Parallel initialization, fast acquire/release, live status.

Key design decisions:
  - undetected_chromedriver (UC) patches its binary on startup and CRASHES
    if two threads call uc.Chrome() at the same time on Windows.
    We serialize only that one call with _uc_spawn_lock.
  - Login + phone-number-fetch are I/O-bound and run in parallel across all workers.
  - A rich console banner is printed once all workers finish initializing.
"""
import asyncio
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from loguru import logger

import config
from config import WORKER_POOL
from infra.gv_credentials import CredentialStore, mask
from telephony.google_voice import PhoneClient, host_free_mb, uc_spawn_lock

# Shared with PhoneClient.restart_browser() so a mid-run respawn can never race
# an initial spawn: UC's binary patcher only tolerates one at a time.
_uc_spawn_lock = uc_spawn_lock


class PhonePool:
    """Manages a pool of authenticated, isolated PhoneClient workers."""

    def __init__(self):
        self.workers: List[PhoneClient] = []
        self._pool_lock = asyncio.Lock()
        self._available_workers: asyncio.Queue = asyncio.Queue()
        self._checked_out_worker_ids: set = set()
        # Ids currently sitting in _available_workers. The queue itself cannot
        # answer "is X inside?", and without this an interrupted release used
        # to orphan a worker: not checked out, not queued, still 'ready'.
        self._in_queue: set = set()
        self._checked_out_at: Dict[int, float] = {}
        self._number_refresh_tasks: Dict[int, asyncio.Task] = {}
        # Cap concurrent post-spawn logins so 8 Chromes don't thrash a 4–6 vCPU host.
        self._login_sem = asyncio.Semaphore(
            max(1, int(getattr(config, "GV_INIT_LOGIN_CONCURRENCY", 2)))
        )

        self._state = "not_started"
        self._init_started_at: Optional[float] = None
        self._init_finished_at: Optional[float] = None
        self._worker_states: Dict[int, dict] = {}
        self._init_event = asyncio.Event()
        self._recover_lock = asyncio.Lock()
        self._last_recovery_attempt_at: float = 0.0
        self._recovery_cooldown_sec: float = 45.0

        # Browser restarts (OOM self-heal) run in the background so a dead
        # worker never blocks the call that released it. Bounded because each
        # respawn costs a Chrome launch plus a Google Voice login.
        self._revive_tasks: Dict[int, asyncio.Task] = {}
        self._abandoned_workers: set = set()
        self._revive_sem = asyncio.Semaphore(
            max(1, int(getattr(config, "GV_REVIVE_CONCURRENCY", 1)))
        )

        # Google credentials per profile, supplied at runtime by the operator.
        # Parallel sign-ins are capped separately from init logins: several
        # Chrome instances authenticating at once from one IP is what makes
        # Google start asking for verification.
        self._credentials = CredentialStore()
        self._credentials.load()
        self._last_login_retry_at: float = 0.0
        # Captcha recycle respawns Chrome onto the login page in the
        # background so other profiles keep signing in.
        self._respawn_tasks: Dict[int, asyncio.Task] = {}

    # ──────────────────────────────────────────────────────────────────────────
    # Initialization
    # ──────────────────────────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        """Initialize all workers in PARALLEL (only uc.Chrome() is serialized)."""
        async with self._pool_lock:
            if self._state == "initializing":
                logger.info("Telephony Pool init already in progress — waiting.")
                wait_event = self._init_event
            elif self._state in {"ready", "degraded"} and self.workers:
                snap = self.get_status_snapshot()
                logger.info(
                    f"Telephony Pool already initialized "
                    f"({snap['ready_workers']}/{snap['total_workers']} ready)."
                )
                return snap["ready_workers"] > 0
            else:
                wait_event = None

        if wait_event is not None:
            await wait_event.wait()
            snap = self.get_status_snapshot()
            return snap["ready_workers"] > 0

        # ── Fresh init ────────────────────────────────────────────────────────
        async with self._pool_lock:
            self._init_event.clear()
            self._state = "initializing"
            self._init_started_at = time.time()
            self._init_finished_at = None
            old_workers = self.workers
            self.workers = []
            self._worker_states = {}
            # КРИТИЧНО: Используем пути относительно текущего файла



            self._checked_out_worker_ids = set()
            self._checked_out_at = {}
            self._available_workers = asyncio.Queue()
            self._in_queue = set()
            self._number_refresh_tasks = {}
            for task in self._revive_tasks.values():
                if not task.done():
                    task.cancel()
            self._revive_tasks = {}
            self._abandoned_workers = set()

        # A re-init after a failed attempt must not leave the previous
        # workers' Chromes running: they hold the profile locks, and every
        # new spawn on the same profile then dies with 'chrome not reachable'.
        if old_workers:
            logger.warning(
                f"Pool re-init: closing {len(old_workers)} leftover browser(s) first"
            )
            loop = asyncio.get_running_loop()
            await asyncio.gather(
                *(loop.run_in_executor(None, self._close_old_worker, w)
                  for w in old_workers),
                return_exceptions=True,
            )

        total = len(WORKER_POOL)
        logger.info(f"Initializing Telephony Pool: {total} workers (parallel)…")

        # Pre-create client objects so they're addressable by index
        for i, wcfg in enumerate(WORKER_POOL):
            client = PhoneClient(

                profile_dir=wcfg["profile_dir"],
                capture_device=wcfg["capture_device"],
                playback_device=wcfg["playback_device"],
            )
            client.worker_id = i
            self.workers.append(client)
            self._worker_states[i] = {
                "worker_id": i,
                "capture_device": client.capture_device,
                "playback_device": client.playback_device,
                "state": "initializing",
                "authenticated": False,
                "number_fetched": False,
                "phone_number": None,
                "last_error": None,
                "browser_start_seconds": 0.0,
                "login_seconds": 0.0,
                "number_fetch_seconds": 0.0,
                "rss_mb": None,
                "heap_pct": None,
                "calls_served": 0,
                "restart_count": 0,
                "profile": wcfg["profile_dir"].name,
                "login_state": "unknown",
                "login_outcome": None,
                "account_email": None,
                "has_credentials": False,
            }
            self._sync_worker_to_dashboard(i)

        # Launch all workers concurrently — each one waits its turn for UC spawn.
        results = await asyncio.gather(
            *[self._init_one_worker(client) for client in self.workers],
            return_exceptions=True,
        )

        ready_count = sum(1 for ok in results if ok is True)

        self._init_finished_at = time.time()
        if ready_count == total and total > 0:
            self._state = "ready"
        elif ready_count > 0:
            self._state = "degraded"
        else:
            self._state = "failed"

        # ── Final console report ───────────────────────────────────────────────
        elapsed = round(self._init_finished_at - self._init_started_at, 1)
        snap = self.get_status_snapshot()

        self._print_ready_banner(snap, elapsed)
        self._update_pool_status_banner(elapsed_override=elapsed)

        self._init_event.set()
        return ready_count > 0

    @staticmethod
    def _close_old_worker(worker: PhoneClient) -> None:
        """Close a worker from a previous (failed) init pass and reap any
        Chrome still holding its profile. Runs in an executor."""
        try:
            worker.close()
        except Exception:
            pass
        # close() refuses to run while a sign-in owns the browser — in that
        # case the profile sweep must not kill the form mid-login either.
        if getattr(worker, "_login_running", False):
            return
        try:
            worker._kill_profile_processes()
        except Exception:
            pass

    async def _init_one_worker(self, client: PhoneClient) -> bool:
        """
        Initialize one worker.

        UC's binary patching is serialized via _uc_spawn_lock so two Chrome
        processes don't race each other on Windows.  Everything after the
        browser is spawned (login, number-fetch) runs freely in parallel.
        """
        i = client.worker_id

        # ─ Step 1: Acquire the spawn lock so UC doesn't corrupt itself ─────────
        logger.info(f"Worker {i}: waiting for UC spawn slot…")
        async with _uc_spawn_lock:
            logger.info(f"Worker {i}: spawning Chrome…")
            try:
                loop = asyncio.get_running_loop()
                browser_ok = await asyncio.wait_for(
                    loop.run_in_executor(None, client._setup_browser_only),
                    timeout=max(10, config.GV_STARTUP_TIMEOUT_SEC),
                )
            except asyncio.TimeoutError:
                client.last_error = "browser spawn timeout"
                logger.error(f"Worker {i}: Chrome spawn timed out")
                try:
                    await loop.run_in_executor(None, client._abort_inflight_spawn)
                except Exception as abort_exc:
                    logger.warning(
                        f"Worker {i}: spawn-timeout abort failed — {abort_exc}"
                    )
                browser_ok = False
            except Exception as exc:
                client.last_error = str(exc)
                logger.error(f"Worker {i}: Chrome spawn error — {exc}")
                browser_ok = False

        if not browser_ok:
            self._mark_worker_failed(i, client)
            return False

        # ─ Step 2: Login + number-fetch (bounded concurrency on weak CPUs) ──
        # Signing in is part of this step: a profile whose Google session
        # expired, or that was never signed in, is recovered here if the
        # operator has supplied its credentials.
        self._apply_stored_credentials(client)
        try:
            async with self._login_sem:
                login_ok = await client.login_or_autologin()
        except Exception as exc:
            client.last_error = str(exc)
            logger.error(f"Worker {i}: login error — {exc}")
            login_ok = False

        client.init_finished_at = time.time()

        health = client.get_health_snapshot()
        if not login_ok and (
            getattr(client, "is_banned", False)
            or health.get("login_state") == "banned"
            or health.get("login_outcome") == "disabled"
        ):
            await self.vacate_banned_account(client)
            return False
        if not login_ok and (
            health.get("login_state") == "captcha"
            or health.get("login_outcome") == "captcha"
        ):
            await self.recycle_captcha_account(client, wait_spawn=True)
            return False

        # Number fetch can fail while auth is still valid; do not discard a healthy dialer.
        is_ready = bool(login_ok and health["authenticated"])
        state = "ready" if is_ready else self._offline_state(health)

        self._worker_states[i].update(
            {
                "state": state,
                "authenticated": bool(health["authenticated"]),
                "number_fetched": bool(health["phone_number"]),
                "phone_number": health["phone_number"],
                "last_error": health["last_error"],
                "browser_start_seconds": health["browser_start_seconds"],
                "login_seconds": health["login_seconds"],
                "number_fetch_seconds": health["number_fetch_seconds"],
                "rss_mb": client.chrome_rss_mb() if is_ready else None,
                "calls_served": 0,
                "login_state": health["login_state"],
                "login_outcome": health["login_outcome"],
                "account_email": health["account_email"],
                "has_credentials": health["has_credentials"],
                "profile": health["profile"],
            }
        )

        if is_ready:
            self._in_queue.add(i)
            await self._available_workers.put(client)
            logger.info(
                f"✅ Worker {i} ready | "
                f"number={health['phone_number']} | "
                f"browser={health['browser_start_seconds']:.1f}s "
                f"login={health['login_seconds']:.1f}s "
                f"fetch={health['number_fetch_seconds']:.1f}s"
            )
        else:
            logger.warning(
                f"❌ Worker {i} failed | "
                f"auth={health['authenticated']} "
                f"number={health['phone_number']} "
                f"error={health['last_error']}"
            )

        self._sync_worker_to_dashboard(i)
        if is_ready and not health["phone_number"]:
            self._schedule_number_refresh(client)
        return is_ready

    # ──────────────────────────────────────────────────────────────────────────
    # Auto-login: credentials, account inventory, parallel sign-in
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _offline_state(health: dict) -> str:
        """Why a worker is not ready, in terms the operator can act on."""
        login_state = health.get("login_state")
        if login_state == "banned":
            return "banned"
        if login_state == "captcha":
            return "captcha"
        if login_state == "challenge":
            return "challenge"
        if login_state in ("needs_login", "expired", "unknown"):
            return "needs_login"
        return "failed"

    def _apply_stored_credentials(self, client: PhoneClient) -> bool:
        """Hand a worker the credentials held for its profile, if any."""
        profile = client.profile_dir.name if client.profile_dir else None
        if not profile:
            return False
        entry = self._credentials.get(profile)
        if not entry:
            return False
        client.set_credentials(
            entry["email"],
            entry["password"],
            entry.get("recovery_email"),
            totp_seed=entry.get("totp_seed"),
            backup_codes=entry.get("backup_codes"),
            used_backup_codes=entry.get("used_backup_codes"),
        )
        self._wire_backup_code_tracking(client, profile)
        return True

    def _wire_backup_code_tracking(self, client: PhoneClient, profile: str) -> None:
        """Spent backup codes land in the store so they are never reused."""
        def _used(code: str) -> None:
            self._credentials.mark_backup_code_used(profile, code)
            try:
                self._credentials.save()
            except Exception:
                pass
        client.on_backup_code_used = _used

    def _worker_by_selector(
        self, worker: Optional[int] = None, profile: Optional[str] = None
    ) -> Optional[PhoneClient]:
        for w in self.workers:
            if worker is not None and w.worker_id == worker:
                return w
            if profile and w.profile_dir and w.profile_dir.name == profile:
                return w
        return None

    def accounts_report(self) -> List[dict]:
        """Per-worker account inventory: who is signed in, who needs a password."""
        rows: List[dict] = []
        for w in self.workers:
            state = self._worker_states.get(w.worker_id, {})
            profile = w.profile_dir.name if w.profile_dir else None
            creds = w.credentials or {}
            used = list(creds.get("used_backup_codes") or [])
            codes_left = sum(
                1 for c in (creds.get("backup_codes") or [])
                if not any(c in u for u in used)
            )
            rows.append({
                "worker_id": w.worker_id,
                "profile": profile,
                "state": state.get("state", "unknown"),
                "login_state": w.login_state,
                "login_outcome": w.login_outcome,
                "login_detail": w.login_detail,
                "phone_number": w.phone_number,
                "email": w.account_email or w.credential_email,
                "has_credentials": w.has_credentials(),
                "has_totp": bool(creds.get("totp_seed")),
                "backup_codes_left": codes_left,
                "authenticated": bool(w.is_authenticated),
                "banned": bool(w.is_banned),
                "spam_flagged": bool(w.is_spam_flagged),
                "autologin_attempts": w.autologin_attempts,
            })
        return rows

    def workers_needing_login(self) -> List[PhoneClient]:
        """Workers that fresh credentials would actually fix (bans would not).

        Includes challenge (2FA) profiles — with a TOTP seed or backup codes
        /login now gets those through — and vacant profiles left after a
        spam-cookie wipe: empty Chrome slots waiting for a new account.
        """
        out = []
        for w in self.workers:
            if w.is_banned:
                continue
            if w.is_authenticated and w.driver is not None:
                continue
            pool_state = self._worker_states.get(w.worker_id, {}).get("state")
            if w.login_state in (
                "needs_login", "expired", "unknown", "challenge"
            ) or pool_state in (
                "needs_login", "spam",
            ):
                out.append(w)
        return out

    def apply_credentials(self, entries: List[dict]) -> dict:
        """Store operator-supplied credentials against workers.

        Entries may address a worker explicitly (`worker` index or `profile`
        name); unaddressed ones fill the workers that need a login, in order.
        Returns {"assigned": [...], "unassigned": [...]}.
        """
        assigned, unassigned = [], []
        queue = [w for w in self.workers_needing_login()]
        # Deterministic order so the operator's Nth line lands on the Nth
        # account listed by /accounts.
        queue.sort(key=lambda w: w.worker_id)

        for entry in entries:
            email = entry.get("email")
            password = entry.get("password")
            if not email or not password:
                continue

            target = None
            if entry.get("worker") is not None or entry.get("profile"):
                target = self._worker_by_selector(
                    worker=entry.get("worker"), profile=entry.get("profile")
                )
                if target is None:
                    unassigned.append({
                        "email": email,
                        "reason": (
                            f"no worker matches "
                            f"{entry.get('profile') or 'w' + str(entry.get('worker'))}"
                        ),
                    })
                    continue
            else:
                # Prefer a profile already tied to this email — re-sending the
                # same list must not shuffle accounts between profiles.
                target = next(
                    (
                        w for w in self.workers
                        if (w.account_email or w.credential_email or "").lower()
                        == email.lower()
                    ),
                    None,
                )
                if target is None:
                    while queue:
                        cand = queue.pop(0)
                        target = cand
                        break
                if target is None:
                    unassigned.append(
                        {"email": email, "reason": "no worker is waiting for a login"}
                    )
                    continue

            if target in queue:
                queue.remove(target)
            recovery_email = entry.get("recovery_email")
            totp_seed = entry.get("totp_seed")
            backup_codes = entry.get("backup_codes")
            target.set_credentials(
                email, password, recovery_email,
                totp_seed=totp_seed, backup_codes=backup_codes,
            )
            profile = target.profile_dir.name if target.profile_dir else None
            if profile:
                self._credentials.put(
                    profile, email, password, recovery_email,
                    totp_seed=totp_seed, backup_codes=backup_codes,
                )
                self._wire_backup_code_tracking(target, profile)
            self._worker_states.get(target.worker_id, {}).update({
                "account_email": email, "has_credentials": True
            })
            assigned.append({
                "worker_id": target.worker_id,
                "profile": profile,
                "email": email,
            })
            logger.info(
                f"Worker {target.worker_id}: credentials set for {email} "
                f"password={mask(password)}"
                f"{' recovery=' + recovery_email if recovery_email else ''}"
                f"{' totp-seed' if totp_seed else ''}"
                f"{f' {len(backup_codes)} backup codes' if backup_codes else ''}"
            )

        if assigned:
            self._credentials.save()
        return {"assigned": assigned, "unassigned": unassigned}

    async def auto_login_workers(
        self,
        worker_ids: Optional[List[int]] = None,
        concurrency: Optional[int] = None,
        on_progress: Optional[Callable[[List[dict]], Awaitable[None]]] = None,
    ) -> List[dict]:
        """Sign the requested workers in, several at a time.

        Each worker owns its own Chrome and chromedriver port, so sign-ins are
        genuinely parallel; the semaphore only keeps the host (and Google's
        patience) from being overrun.
        """
        targets = [
            w for w in self.workers
            if (worker_ids is None or w.worker_id in worker_ids)
            and not w.is_banned
            # Never yank a worker that is on a call.
            and w.worker_id not in self._checked_out_worker_ids
        ]
        if not targets:
            return []

        limit = max(1, int(
            concurrency or getattr(config, "GV_LOGIN_CONCURRENCY", 2)
        ))
        sem = asyncio.Semaphore(limit)
        logger.info(
            f"Auto-login starting for {len(targets)} worker(s), "
            f"{limit} at a time"
        )

        async def _ensure_browser(w: PhoneClient) -> bool:
            """Spawn Chrome for a vacant slot. Do NOT go through revive:
            revive is serialized to 1 and also signs in, which made /login
            process workers one-by-one and then sign in a second time.
            """
            pending = self._respawn_tasks.get(w.worker_id)
            if pending is not None and not pending.done():
                try:
                    await pending
                except Exception:
                    pass
            if w.driver is not None:
                return True
            wid = w.worker_id
            logger.warning(
                f"Worker {wid}: no browser — spawning before sign-in"
            )
            loop = asyncio.get_running_loop()
            startup = max(10, int(getattr(config, "GV_STARTUP_TIMEOUT_SEC", 120)))
            try:
                async with _uc_spawn_lock:
                    spawned = await asyncio.wait_for(
                        loop.run_in_executor(None, w._setup_browser_only),
                        timeout=startup,
                    )
            except asyncio.TimeoutError:
                w.last_error = "browser spawn timeout"
                logger.error(f"Worker {wid}: Chrome spawn timed out")
                abort = getattr(w, "_abort_inflight_spawn", None)
                if callable(abort):
                    try:
                        await loop.run_in_executor(None, abort)
                    except Exception:
                        pass
                spawned = False
            except Exception as e:
                w.last_error = str(e)
                logger.error(f"Worker {wid}: Chrome spawn error — {e}")
                spawned = False
            return bool(spawned and w.driver is not None)

        async def _one(w: PhoneClient) -> dict:
            self._apply_stored_credentials(w)
            wid = w.worker_id
            # A worker already serving from the queue must not be enqueued
            # twice once it re-authenticates.
            was_ready = self._worker_states.get(wid, {}).get("state") == "ready"
            row = {
                "worker_id": wid,
                "profile": w.profile_dir.name if w.profile_dir else None,
                "email": w.credential_email,
            }
            if not w.has_credentials():
                row.update({"ok": False, "outcome": "no_credentials"})
                return row
            if w.driver is None:
                w.login_detail = "starting Chrome"
                spawned = await _ensure_browser(w)
                if not spawned or w.driver is None:
                    row.update({"ok": False, "outcome": "no_browser"})
                    return row

            # Already signed in (session survived, or a concurrent revive
            # finished first). Do not drive the form a second time.
            if w.is_authenticated and w.login_state == "ready" and w.driver is not None:
                row.update({
                    "ok": True,
                    "outcome": w.login_outcome or "ok",
                    "detail": w.login_detail or "already signed in",
                    "phone_number": w.phone_number,
                    "email": w.account_email or w.credential_email,
                })
                self._worker_states.get(wid, {}).update({
                    "state": "ready",
                    "authenticated": True,
                    "number_fetched": bool(w.phone_number),
                    "phone_number": w.phone_number,
                    "login_state": "ready",
                    "account_email": row["email"],
                    "last_error": None,
                })
                self._sync_worker_to_dashboard(wid)
                if (
                    not was_ready
                    and wid not in self._checked_out_worker_ids
                    and wid not in self._in_queue
                ):
                    self._in_queue.add(wid)
                    await self._available_workers.put(w)
                return row

            async with sem:
                self._worker_states.get(wid, {}).update({"state": "logging_in"})
                self._sync_worker_to_dashboard(wid)
                loop = asyncio.get_running_loop()
                budget = max(30, int(getattr(config, "GV_LOGIN_TIMEOUT_SEC", 120)))
                try:
                    ok = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: w.auto_login(budget)),
                        timeout=budget + 30,
                    )
                except asyncio.TimeoutError:
                    ok = False
                    w.login_outcome = "timeout"
                    w.login_detail = f"sign-in exceeded {budget + 30}s"
                except Exception as e:
                    ok = False
                    w.login_outcome = "error"
                    w.login_detail = str(e)
                    logger.warning(f"Worker {wid}: auto-login crashed — {e}")

            row.update({
                "ok": bool(ok),
                "outcome": w.login_outcome,
                "detail": w.login_detail,
                "stage": self.login_stage_label(
                    w, self._worker_states.get(wid, {}).get("state")
                ),
                "phone_number": w.phone_number,
                "email": w.account_email or w.credential_email,
            })

            if (
                getattr(w, "is_banned", False)
                or w.login_outcome == "disabled"
                or w.login_state == "banned"
            ):
                await self.vacate_banned_account(w)
            elif w.login_outcome == "captcha" or w.login_state == "captcha":
                # Cookie wipe + close immediately; Chrome respawn is
                # backgrounded so it does not hold up the other sign-ins.
                await self.recycle_captcha_account(w, wait_spawn=False)
            elif w.login_outcome == "account_chooser":
                # Picker survived the in-browser wipe; Chrome was closed
                # with credentials kept. Spawn once and type the email.
                spawned = await _ensure_browser(w)
                if spawned and w.has_credentials() and w.driver is not None:
                    try:
                        ok = await asyncio.wait_for(
                            loop.run_in_executor(
                                None, lambda: w.auto_login(budget)
                            ),
                            timeout=budget + 30,
                        )
                    except asyncio.TimeoutError:
                        ok = False
                        w.login_outcome = "timeout"
                        w.login_detail = f"sign-in exceeded {budget + 30}s"
                    except Exception as e:
                        ok = False
                        w.login_outcome = "error"
                        w.login_detail = str(e)
                    row.update({
                        "ok": bool(ok),
                        "outcome": w.login_outcome,
                        "detail": w.login_detail,
                        "phone_number": w.phone_number,
                        "email": w.account_email or w.credential_email,
                    })

            health = w.get_health_snapshot()
            self._worker_states.get(wid, {}).update({
                "state": "ready" if ok else self._offline_state(health),
                "authenticated": bool(w.is_authenticated),
                "number_fetched": bool(w.phone_number),
                "phone_number": w.phone_number,
                "login_state": w.login_state,
                "login_outcome": w.login_outcome,
                "account_email": row["email"],
                "has_credentials": True,
                "last_error": None if ok else w.last_error,
            })
            self._sync_worker_to_dashboard(wid)

            if (
                ok
                and not was_ready
                and wid not in self._checked_out_worker_ids
                # _revive_worker() above already enqueued it — a second put
                # would hand the same browser to two callers at once.
                and wid not in self._in_queue
            ):
                # A freshly signed-in worker is idle capacity — make it usable
                # without waiting for a pool restart.
                w.is_spam_flagged = False
                self._in_queue.add(wid)
                await self._available_workers.put(w)
                if not w.phone_number:
                    self._schedule_number_refresh(w)
            elif ok:
                w.is_spam_flagged = False
            return row

        stop = asyncio.Event()

        async def _progress_tick():
            while not stop.is_set():
                if on_progress is not None:
                    try:
                        await on_progress(self.login_progress_rows(targets))
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.5)
                except asyncio.TimeoutError:
                    continue

        if on_progress is not None:
            try:
                await on_progress(self.login_progress_rows(targets))
            except Exception:
                pass
        tick = asyncio.create_task(_progress_tick())
        try:
            results = await asyncio.gather(
                *[_one(w) for w in targets], return_exceptions=True
            )
        finally:
            stop.set()
            tick.cancel()
            try:
                await tick
            except (asyncio.CancelledError, Exception):
                pass
        rows = []
        for w, res in zip(targets, results):
            if isinstance(res, BaseException):
                logger.warning(f"Worker {w.worker_id}: auto-login task failed — {res}")
                rows.append({
                    "worker_id": w.worker_id, "ok": False,
                    "outcome": "error", "detail": str(res),
                })
            else:
                rows.append(res)

        ok_count = sum(1 for r in rows if r.get("ok"))
        logger.info(f"Auto-login finished: {ok_count}/{len(rows)} signed in")
        self._update_pool_status_banner()
        return rows

    def login_progress_rows(self, workers: List[PhoneClient]) -> List[dict]:
        """Live per-account lines for the Telegram /login status message."""
        rows = []
        for w in workers:
            pool_state = self._worker_states.get(w.worker_id, {}).get("state")
            rows.append({
                "worker_id": w.worker_id,
                "profile": w.profile_dir.name if w.profile_dir else None,
                "email": w.account_email or w.credential_email,
                "ok": bool(w.is_authenticated and w.login_state == "ready"),
                "state": pool_state or w.login_state,
                "outcome": w.login_outcome,
                "detail": w.login_detail,
                "stage": self.login_stage_label(w, pool_state),
                "phone_number": w.phone_number,
            })
        return rows

    @staticmethod
    def login_stage_label(worker: PhoneClient, pool_state: Optional[str] = None) -> str:
        """One-line 'what this account is doing right now'."""
        if worker.is_authenticated and worker.login_state == "ready":
            num = worker.phone_number or "number pending"
            return f"signed in — {num}"
        oc = worker.login_outcome
        if oc == "captcha" or worker.login_state == "captcha":
            return "captcha — restarting onto the login page"
        if oc == "disabled" or worker.login_state == "banned":
            return "banned by Google"
        if oc == "challenge_required" or worker.login_state == "challenge":
            return "2FA / extra verification"
        if oc in ("bad_password", "unknown_email", "timeout", "no_form", "no_dialer"):
            return oc.replace("_", " ")
        d = (worker.login_detail or "").lower()
        if oc == "account_chooser" or "account picker" in d or "clearing cookies" in d:
            return "clearing cookies (account picker)"
        if "starting chrome" in d or "spawning" in d:
            return "starting Chrome"
        if "restarted onto the login page" in d or "restarting onto the login" in d:
            return "captcha — restarting onto the login page"
        if "skipping passkey" in d or "not now" in d:
            return "skipping passkey (Not now)"
        if "trying another sign-in" in d:
            return "skipping passkey (Try another way)"
        if "entering password" in d or "state=password" in d:
            return "entering password"
        if "entering account" in d or "state=identifier" in d:
            return "entering email"
        if "picking" in d or "state=chooser" in d:
            return "clearing cookies (account picker)"
        if "recovery" in d:
            return "recovery email"
        if "sign in" in d or "state=landing" in d:
            return "opening Sign in"
        if "state=dialer" in d or "dialer" in d:
            return "opening Voice dialer"
        if "checking session" in d:
            return "checking session"
        if pool_state == "logging_in":
            return "signing in…"
        if pool_state == "restarting":
            return "starting Chrome"
        return worker.login_detail or pool_state or worker.login_state or "waiting"

    def _login_retry_candidates(self) -> List[PhoneClient]:
        """Workers a retry could plausibly fix — never a wrong password."""
        from telephony import gv_login

        max_attempts = max(1, int(getattr(config, "GV_MAX_LOGIN_ATTEMPTS", 3)))
        out = []
        for w in self.workers:
            wid = w.worker_id
            if self._worker_states.get(wid, {}).get("state") != "needs_login":
                continue
            if wid in self._checked_out_worker_ids:
                continue
            self._apply_stored_credentials(w)
            if not w.has_credentials():
                continue
            # Retrying a rejected password or a 2FA prompt only burns the
            # account; those need the operator.
            if w.login_outcome in gv_login.FATAL_OUTCOMES:
                continue
            if w.autologin_attempts >= max_attempts:
                continue
            out.append(w)
        return out

    async def retry_pending_logins(self, cooldown_sec: float = 300.0) -> int:
        """Re-attempt sign-in for workers that have credentials but no session.

        Called by the agent watchdog, so a session that expires mid-run comes
        back on its own instead of costing capacity until the next restart.
        """
        now = time.time()
        if now - self._last_login_retry_at < cooldown_sec:
            return 0
        candidates = self._login_retry_candidates()
        if not candidates:
            return 0
        self._last_login_retry_at = now
        logger.warning(
            f"Watchdog: retrying sign-in for {len(candidates)} worker(s) "
            f"— {[w.worker_id for w in candidates]}"
        )
        rows = await self.auto_login_workers([w.worker_id for w in candidates])
        return sum(1 for r in rows if r.get("ok"))

    def _mark_worker_failed(self, i: int, client: PhoneClient):
        task = self._number_refresh_tasks.pop(i, None)
        if task and not task.done():
            task.cancel()
        self._worker_states[i].update(
            {
                "state": "failed",
                "authenticated": False,
                "number_fetched": False,
                "phone_number": None,
                "last_error": client.last_error,
            }
        )
        self._sync_worker_to_dashboard(i)

    # ──────────────────────────────────────────────────────────────────────────
    # Console banner
    # ──────────────────────────────────────────────────────────────────────────

    def _print_ready_banner(self, snap: dict, elapsed: float):
        """Print a clear, human-readable summary to the console after init."""
        total = snap["total_workers"]
        ready = snap["ready_workers"]
        auth = snap["authenticated_workers"]
        numbered = snap["numbers_fetched_workers"]
        all_good = snap["all_logged_in_and_numbered"]
        all_ready = snap["all_workers_ready"]

        sep = "═" * 55
        logger.info(sep)
        if all_good:
            logger.info(
                f"✅  ALL {total}/{total} GV WORKERS FULLY LOGGED IN & NUMBERED"
                f"  ({elapsed}s)"
            )
        else:
            logger.warning(
                f"⚠️  GV Pool init finished in {elapsed}s — "
                f"{ready}/{total} ready | "
                f"auth={auth}/{total} | numbered={numbered}/{total}"
            )

        for ws in snap["workers"]:
            wi = ws["worker_id"]
            st = ws["state"].upper()
            num = ws.get("phone_number") or "—"
            err = ws.get("last_error") or ""
            b = ws.get("browser_start_seconds", 0)
            l_ = ws.get("login_seconds", 0)
            f = ws.get("number_fetch_seconds", 0)
            if ws["state"] == "ready":
                logger.info(
                    f"  Worker {wi}  [{st}]  {num}  "
                    f"(browser={b:.1f}s  login={l_:.1f}s  fetch={f:.1f}s)"
                )
            else:
                logger.warning(f"  Worker {wi}  [{st}]  {num}  error={err!r}")

        logger.info(sep)

        # Push status to the live Rich dashboard header.
        try:
            from console_ui import dashboard
            if all_good:
                dashboard.set_pool_status(
                    f"✅ ALL {total}/{total} GV WORKERS READY  ({elapsed}s)"
                )
            elif ready > 0:
                dashboard.set_pool_status(
                    f"⚠️ DEGRADED — {ready}/{total} ready  ({elapsed}s)"
                )
            else:
                dashboard.set_pool_status(f"❌ FAILED — 0/{total} ready")
        except Exception:
            pass


    # ──────────────────────────────────────────────────────────────────────────
    # Acquire / Release
    # ──────────────────────────────────────────────────────────────────────────

    def _schedule_number_refresh(self, client: PhoneClient):
        worker_id = getattr(client, "worker_id", -1)
        if worker_id < 0:
            return

        existing = self._number_refresh_tasks.get(worker_id)
        if existing and not existing.done():
            return

        self._number_refresh_tasks[worker_id] = asyncio.create_task(
            self._refresh_missing_number(client),
            name=f"gv_number_refresh_{worker_id}",
        )

    async def _refresh_missing_number(self, client: PhoneClient):
        worker_id = getattr(client, "worker_id", -1)
        loop = asyncio.get_running_loop()

        for delay_sec in (5, 12, 25):
            await asyncio.sleep(delay_sec)

            if client.driver is None or not client.is_authenticated or client.phone_number:
                return

            try:
                fetched = await loop.run_in_executor(None, client._fetch_phone_number)
            except Exception as exc:
                logger.debug(f"Worker {worker_id}: late number refresh error - {exc}")
                continue

            if not fetched or not client.phone_number:
                continue

            self._worker_states.get(worker_id, {}).update(
                {
                    "number_fetched": True,
                    "phone_number": client.phone_number,
                    "last_error": None,
                    "number_fetch_seconds": client.number_fetch_seconds,
                }
            )
            logger.info(
                f"Worker {worker_id}: late number fetch succeeded | "
                f"number={client.phone_number}"
            )
            self._sync_worker_to_dashboard(worker_id)
            return

    def _pool_status_text(self, snap: dict, elapsed_override: Optional[float] = None) -> str:
        total = snap["total_workers"]
        ready = snap["ready_workers"]
        numbered = snap["numbers_fetched_workers"]
        elapsed = elapsed_override if elapsed_override is not None else snap.get("init_seconds")
        elapsed_suffix = f" ({elapsed}s)" if elapsed is not None else ""

        if total > 0 and ready == total:
            if numbered == total:
                return f"ALL {ready}/{total} GV WORKERS READY{elapsed_suffix}"
            return (
                f"ALL {ready}/{total} GV WORKERS READY"
                f"{elapsed_suffix} | numbered={numbered}/{total}"
            )
        if self._state == "initializing":
            return f"INITIALIZING - {ready}/{total} ready"
        if ready > 0:
            return f"DEGRADED - {ready}/{total} ready"
        return f"FAILED - 0/{total} ready"

    def _update_pool_status_banner(self, elapsed_override: Optional[float] = None):
        try:
            from console_ui import dashboard
        except Exception:
            return

        snap = self.get_status_snapshot()
        dashboard.set_pool_status(self._pool_status_text(snap, elapsed_override))

    async def _attempt_pool_recovery(self, reason: str) -> bool:
        """Reinitialize the pool when no ready workers are available."""
        snap = self.get_status_snapshot()
        if snap["ready_workers"] > 0:
            return True
        if self._state == "initializing":
            return False

        now = time.time()
        if now - self._last_recovery_attempt_at < self._recovery_cooldown_sec:
            return False

        async with self._recover_lock:
            snap = self.get_status_snapshot()
            if snap["ready_workers"] > 0:
                return True
            now = time.time()
            if now - self._last_recovery_attempt_at < self._recovery_cooldown_sec:
                return False
            self._last_recovery_attempt_at = now

            # Guard: if workers are checked out (busy), don't kill them
            if self._checked_out_worker_ids:
                logger.warning(
                    f"Pool recovery skipped — {len(self._checked_out_worker_ids)} worker(s) "
                    f"still checked out (active calls)"
                )
                return False

            logger.warning(
                f"No ready GV workers ({reason}); attempting pool recovery re-init"
            )
            # Close ONLY idle workers before re-init to prevent Chrome leaks
            # (checked_out_worker_ids is empty here, so shutdown() is safe)
            self.shutdown()
            try:
                return await self.initialize()
            except Exception as e:
                logger.error(f"Pool recovery initialize failed: {e}")
                return False

    # ──────────────────────────────────────────────────────────────────────────
    # Browser restart / OOM self-heal
    # ──────────────────────────────────────────────────────────────────────────

    def _schedule_revive(self, worker: PhoneClient, reason: str = ""):
        """Queue a background browser restart for a worker that lost Chrome."""
        worker_id = getattr(worker, "worker_id", -1)
        if worker_id < 0:
            return
        if getattr(worker, "is_banned", False) or getattr(worker, "is_spam_flagged", False):
            return
        # A profile waiting on a Google login (no account, or mid-sign-in) is
        # not a dead browser — restarting it would just spawn a second Chrome
        # on the same profile over and over. It revives via /login instead.
        if getattr(worker, "login_state", None) in (
            "needs_login", "challenge", "captcha", "logging_in"
        ):
            return
        if worker_id in self._checked_out_worker_ids:
            return
        # A respawn already running on this profile must not be doubled.
        if getattr(worker, "_revive_running", False):
            return
        existing = self._revive_tasks.get(worker_id)
        if existing and not existing.done():
            return

        max_restarts = max(1, int(getattr(config, "GV_MAX_RESTARTS", 5)))
        if worker.failed_restarts >= max_restarts:
            # Park it as failed once and stay quiet: the watchdog retries every
            # tick and would otherwise repeat this error forever.
            if worker_id not in self._abandoned_workers:
                self._abandoned_workers.add(worker_id)
                logger.error(
                    f"Worker {worker_id}: giving up after "
                    f"{worker.failed_restarts} consecutive failed browser "
                    f"restarts — needs manual attention "
                    f"(last error: {worker.last_error})"
                )
            self._worker_states.get(worker_id, {}).update(
                {
                    "state": "failed",
                    "last_error": (
                        f"{worker.failed_restarts} failed browser restarts: "
                        f"{worker.last_error}"
                    ),
                }
            )
            self._sync_worker_to_dashboard(worker_id)
            return

        self._revive_tasks[worker_id] = asyncio.create_task(
            self._revive_worker(worker, reason),
            name=f"gv_revive_{worker_id}",
        )

    async def _revive_worker(self, worker: PhoneClient, reason: str = "") -> bool:
        """Respawn one worker's browser and put it back into rotation."""
        worker_id = getattr(worker, "worker_id", -1)
        why = reason or worker.restart_reason or worker.last_error or "unhealthy"

        async with self._revive_sem:
            if worker_id in self._checked_out_worker_ids:
                return False
            free_mb = host_free_mb()
            logger.warning(
                f"Worker {worker_id}: reviving browser ({why}) — "
                f"host_free={free_mb}MB"
            )
            self._worker_states.get(worker_id, {}).update(
                {"state": "restarting", "authenticated": False, "last_error": why}
            )
            self._sync_worker_to_dashboard(worker_id)

            try:
                ok = await worker.restart_browser(why)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Worker {worker_id}: revive crashed — {e}")
                ok = False

        health = worker.get_health_snapshot()
        if not ok:
            dead = self._dead_account_reason(worker)
            if dead:
                await self.vacate_dead_account(worker, dead, respawn=True)
                return False
            self._worker_states.get(worker_id, {}).update(
                {
                    "state": "failed",
                    "authenticated": False,
                    "last_error": health["last_error"] or f"revive failed: {why}",
                }
            )
            self._sync_worker_to_dashboard(worker_id)
            logger.error(f"Worker {worker_id}: revive FAILED — {health['last_error']}")
            return False

        self._worker_states.get(worker_id, {}).update(
            {
                "state": "ready",
                "authenticated": True,
                "number_fetched": bool(health["phone_number"]),
                "phone_number": health["phone_number"],
                "last_error": None,
                "rss_mb": health.get("rss_mb"),
                "calls_served": 0,
                "restart_count": health.get("restart_count"),
            }
        )
        self._in_queue.add(worker_id)
        await self._available_workers.put(worker)
        self._sync_worker_to_dashboard(worker_id)
        logger.info(
            f"Worker {worker_id}: revived and back in the pool "
            f"(number={health['phone_number']})"
        )
        if not worker.phone_number:
            self._schedule_number_refresh(worker)
        return True

    def revive_failed_workers(self) -> int:
        """Schedule restarts for every idle worker that lost its browser.

        Called by the agent watchdog so an OOM-killed worker rejoins the pool
        on its own instead of sitting out the rest of the run.
        """
        scheduled = 0
        for worker in list(self.workers):
            worker_id = getattr(worker, "worker_id", -1)
            state = self._worker_states.get(worker_id, {}).get("state")
            if state != "failed":
                continue
            # Needs a Google login, not a respawn — Chrome respawning is what
            # was opening extra browsers on an account-less profile.
            if getattr(worker, "login_state", None) in (
            "needs_login", "challenge", "captcha", "logging_in"
        ):
                continue
            if worker_id in self._checked_out_worker_ids:
                continue
            if getattr(worker, "_revive_running", False):
                continue
            existing = self._revive_tasks.get(worker_id)
            if existing and not existing.done():
                continue
            self._schedule_revive(worker, "watchdog revive")
            fresh = self._revive_tasks.get(worker_id)
            if fresh is not None and fresh is not existing:
                scheduled += 1
        return scheduled

    def memory_report(self) -> list:
        """Per-worker memory figures for logging (no browser calls)."""
        rows = []
        for worker in self.workers:
            rss = worker.last_rss_mb if worker.driver is not None else None
            rows.append(
                {
                    "worker_id": getattr(worker, "worker_id", -1),
                    "rss_mb": rss,
                    "heap_pct": worker.last_heap_pct if worker.driver is not None else None,
                    "calls_served": worker.calls_served,
                    "restarts": worker.restart_count,
                    "oom_events": worker.oom_events,
                }
            )
        return rows

    async def acquire(self, timeout: Optional[int] = None) -> Optional[PhoneClient]:
        """Wait for and acquire a healthy available worker."""
        if timeout is None:
            timeout = config.GV_WORKER_ACQUIRE_TIMEOUT_SEC

        deadline = time.time() + timeout
        recovery_attempted = False
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                if not recovery_attempted:
                    recovery_attempted = True
                    snap = self.get_status_snapshot()
                    if snap["ready_workers"] == 0:
                        recovered = await self._attempt_pool_recovery("acquire timeout")
                        if recovered:
                            deadline = time.time() + max(15, int(timeout / 2))
                            continue
                snap = self.get_status_snapshot()
                logger.error(
                    f"Timeout waiting for an available worker ({timeout}s). "
                    f"state={snap['state']} ready={snap['ready_workers']}/{snap['total_workers']} "
                    f"checked_out={snap['checked_out_workers']} "
                    f"queued={self._available_workers.qsize()}"
                )
                return None

            try:
                worker = await asyncio.wait_for(
                    self._available_workers.get(), timeout=min(remaining, 2.0)
                )
            except asyncio.TimeoutError:
                if not recovery_attempted:
                    snap = self.get_status_snapshot()
                    if snap["ready_workers"] == 0 and self._state in {"failed", "not_started", "stopped"}:
                        recovery_attempted = await self._attempt_pool_recovery("acquire wait")
                continue

            worker_id = getattr(worker, "worker_id", -1)
            self._in_queue.discard(worker_id)
            if not worker.is_authenticated or worker.driver is None or worker.needs_restart:
                logger.warning(
                    f"Skipping unhealthy worker {worker_id}: "
                    f"auth={worker.is_authenticated} number={worker.phone_number} "
                    f"needs_restart={worker.needs_restart}"
                )
                self._worker_states.get(worker_id, {}).update({"state": "failed"})
                self._sync_worker_to_dashboard(worker_id)
                self._schedule_revive(worker, "unhealthy on acquire")
                continue

            self._checked_out_worker_ids.add(worker_id)
            self._checked_out_at[worker_id] = time.time()
            return worker

    async def release(self, worker: PhoneClient, recycle: bool = True):
        """Return a worker back to the available pool (never hang forever).

        `recycle=False` skips the memory recycle for callers that must not
        block (/p1stop, watchdog reclaim). The counters are untouched, so the
        worker is recycled on its next normal release instead.
        """
        if worker not in self.workers:
            return

        worker_id = getattr(worker, "worker_id", -1)

        # Guard against double-release: only release if currently checked out
        if worker_id not in self._checked_out_worker_ids:
            logger.warning(f"Worker {worker_id} double-release blocked (not checked out)")
            return
        self._checked_out_worker_ids.discard(worker_id)
        self._checked_out_at.pop(worker_id, None)

        try:
            await self._release_inner(worker, worker_id, recycle)
        except asyncio.CancelledError:
            # An outer timeout/cancel must not orphan the worker: it is
            # already out of _checked_out_worker_ids but never made it back
            # to the queue — account for it synchronously, then propagate.
            self._reclaim_orphaned(worker, worker_id, "release cancelled")
            raise
        except Exception as e:
            logger.warning(
                f"Worker {worker_id}: release failed ({e}) — reclaiming worker"
            )
            self._reclaim_orphaned(worker, worker_id, f"release error: {e}")

    def _reclaim_orphaned(self, worker: PhoneClient, worker_id: int, why: str):
        """Synchronously account for a worker whose release was interrupted.

        Without this, a cancelled release left the worker in limbo — not
        checked out, not queued, still 'ready' on the dashboard — so real
        capacity shrank while the snapshot kept reporting it as available.
        """
        if worker_id in self._checked_out_worker_ids or worker_id in self._in_queue:
            return
        healthy = (
            worker.is_authenticated
            and worker.driver is not None
            and not worker.needs_restart
            and not getattr(worker, "is_banned", False)
            and getattr(worker, "login_state", None) not in (
                "needs_login", "challenge", "captcha", "logging_in"
            )
        )
        if healthy:
            logger.warning(
                f"Worker {worker_id}: release interrupted ({why}) — requeued"
            )
            self._in_queue.add(worker_id)
            self._available_workers.put_nowait(worker)
            self._worker_states.get(worker_id, {}).update({"state": "ready"})
        elif getattr(worker, "is_banned", False):
            logger.warning(
                f"Worker {worker_id}: release interrupted ({why}) — "
                f"banned account will be wiped"
            )
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self.vacate_banned_account(worker),
                    name=f"vacate-banned-{worker_id}",
                )
            except Exception:
                self._worker_states.get(worker_id, {}).update(
                    {"state": "needs_login", "last_error": "account banned"}
                )
        elif getattr(worker, "login_state", None) in (
            "needs_login", "challenge", "captcha", "logging_in"
        ) or getattr(worker, "is_spam_flagged", False):
            parked = (
                "logging_in" if worker.login_state == "logging_in"
                else "challenge" if worker.login_state == "challenge"
                else "captcha" if worker.login_state == "captcha"
                else "needs_login"
            )
            logger.warning(
                f"Worker {worker_id}: release interrupted ({why}) — "
                f"left as {parked}"
            )
            self._worker_states.get(worker_id, {}).update({"state": parked})
        else:
            logger.warning(
                f"Worker {worker_id}: release interrupted ({why}) — "
                f"scheduling browser restart"
            )
            self._worker_states.get(worker_id, {}).update(
                {"state": "failed", "last_error": why}
            )
            self._schedule_revive(worker, why)
        self._sync_worker_to_dashboard(worker_id)

    async def _release_inner(
        self, worker: PhoneClient, worker_id: int, recycle: bool
    ):
        # Banned Voice accounts are wiped (cookies + Chrome restart) so the
        # slot can take a new /login. Parking them forever left a dead Chrome
        # on the ban help page and a profile that kept erroring.
        if getattr(worker, "is_banned", False):
            await self.vacate_banned_account(worker)
            return

        # Vacant / waiting-for-login slots (including post-spam cookie wipes)
        # stay out of the call queue until /login signs a new account in.
        login_wait = getattr(worker, "login_state", None) in (
            "needs_login", "challenge", "captcha", "logging_in", "expired"
        ) or getattr(worker, "is_spam_flagged", False)
        if login_wait:
            if getattr(worker, "is_spam_flagged", False):
                await self.disable_worker_spam(worker)
                return
            lost = self._dead_account_reason(worker)
            if self._is_ban_lost(lost) or getattr(worker, "is_banned", False):
                await self.vacate_banned_account(worker)
                return
            # Expired / needs_login is not a Google burn. Keep cookies and
            # stored credentials so auto_login or /login can reuse the account.
            parked = (
                "logging_in" if worker.login_state == "logging_in"
                else "challenge" if worker.login_state == "challenge"
                else "captcha" if worker.login_state == "captcha"
                else "needs_login"
            )
            logger.info(
                f"Worker {worker_id}: not returning to call pool — {parked}"
            )
            self._worker_states.get(worker_id, {}).update({"state": parked})
            self._sync_worker_to_dashboard(worker_id)
            return

        # A dead or OOM-flagged browser is restarted in the background: doing it
        # inline would stall the call loop for a full Chrome launch + GV login.
        if not worker.is_authenticated or worker.driver is None or worker.needs_restart:
            lost = self._dead_account_reason(worker)
            if self._is_ban_lost(lost) or getattr(worker, "is_banned", False):
                await self.vacate_banned_account(worker)
                return
            if lost and self._is_google_burn(lost):
                await self.vacate_dead_account(worker, lost, respawn=True)
                return
            logger.warning(
                f"Not returning worker {worker_id} to queue — unhealthy "
                f"(auth={worker.is_authenticated}, number={worker.phone_number}, "
                f"needs_restart={worker.needs_restart}); scheduling browser restart"
            )
            self._worker_states.get(worker_id, {}).update(
                {
                    "state": "failed",
                    "last_error": worker.restart_reason or worker.last_error,
                }
            )
            self._sync_worker_to_dashboard(worker_id)
            self._schedule_revive(worker, "unhealthy on release")
            return

        # Decide the memory recycle before arming the cleanup timeout: a tab
        # reload takes longer than a plain release, and mistaking one for a
        # stuck worker would cost a needless browser restart.
        action, reason = (None, None)
        if recycle:
            await worker.refresh_memory()
            action, reason = worker.recycle_plan()
            self._worker_states.get(worker_id, {}).update(
                {
                    "rss_mb": worker.last_rss_mb,
                    "heap_pct": worker.last_heap_pct,
                    "calls_served": worker.calls_served,
                }
            )

        if action == "restart":
            # Respawning inline would hold the caller for a Chrome launch plus
            # a Google Voice login, so it runs in the background instead.
            logger.warning(
                f"Worker {worker_id}: browser needs a respawn ({reason}) — "
                f"leaving the pool until the restart finishes"
            )
            self._worker_states.get(worker_id, {}).update(
                {"state": "restarting", "last_error": reason}
            )
            self._sync_worker_to_dashboard(worker_id)
            self._schedule_revive(worker, reason)
            return

        budget = max(5.0, float(getattr(config, "GV_RELEASE_TIMEOUT_SEC", 25.0)))
        if action == "reload":
            budget += max(10.0, float(getattr(config, "GV_RECOVERY_TIMEOUT_SEC", 20.0))) + 15.0
        try:
            await asyncio.wait_for(
                self._release_cleanup(worker, worker_id, action, reason),
                timeout=budget,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Worker {worker_id}: release cleanup timed out ({budget:.0f}s) — "
                f"marking failed (self-heal will reclaim later if needed)"
            )
            try:
                getattr(worker, "_active_calls", {}).clear()
            except Exception:
                pass
            self._worker_states.get(worker_id, {}).update(
                {"state": "failed", "last_error": "release timeout"}
            )
            self._sync_worker_to_dashboard(worker_id)
            self._schedule_revive(worker, "release cleanup timeout")
            return

    async def _release_cleanup(
        self,
        worker: PhoneClient,
        worker_id: int,
        action: Optional[str] = None,
        reason: Optional[str] = None,
    ):
        """Optional memory recycle, then dialer readiness check, then requeue."""
        # Between calls is the only safe moment to reload the tab, and doing it
        # on a schedule is what keeps Chrome from ever reaching OOM.
        if action == "reload":
            if not await worker.recycle_page(reason):
                lost = self._dead_account_reason(worker)
                if lost:
                    await self.vacate_dead_account(worker, lost, respawn=True)
                    return
                logger.warning(
                    f"Worker {worker_id}: tab reload failed ({reason}) — "
                    f"falling back to a browser restart"
                )
                self._worker_states.get(worker_id, {}).update(
                    {"state": "failed", "last_error": f"reload failed: {reason}"}
                )
                self._sync_worker_to_dashboard(worker_id)
                self._schedule_revive(worker, f"reload failed: {reason}")
                return
            hard = int(getattr(config, "GV_WORKER_RSS_HARD_MB", 1400))
            if hard > 0 and (worker.last_rss_mb or 0) >= hard:
                worker.needs_restart = True
                worker.restart_reason = (
                    f"rss {worker.last_rss_mb}MB still over {hard}MB after reload"
                )
                free_mb = host_free_mb()
                floor = int(getattr(config, "GV_HOST_FREE_MB_MIN", 700))
                if free_mb is None or free_mb >= floor:
                    logger.warning(
                        f"Worker {worker_id}: {worker.restart_reason} — "
                        f"respawning Chrome"
                    )
                    self._worker_states.get(worker_id, {}).update(
                        {
                            "state": "restarting",
                            "last_error": worker.restart_reason,
                        }
                    )
                    self._sync_worker_to_dashboard(worker_id)
                    self._schedule_revive(worker, worker.restart_reason)
                    return

        needs_recovery = bool(getattr(worker, "_active_calls", {}))
        if not needs_recovery:
            needs_recovery = not await worker.wait_until_dialer_ready(timeout=0.35)

        if needs_recovery:
            logger.warning(
                f"Worker {worker_id} not clean on release — attempting dialer recovery"
            )
            recovered = await worker.recover_dialer_state("pool release")
            if not recovered:
                lost = self._dead_account_reason(worker)
                if self._is_ban_lost(lost) or getattr(worker, "is_banned", False):
                    await self.vacate_banned_account(worker)
                    return
                if lost:
                    await self.vacate_dead_account(
                        worker, lost, respawn=True,
                    )
                    return
                logger.warning(
                    f"Worker {worker_id} failed dialer recovery — scheduling "
                    f"a browser restart to get it back"
                )
                self._worker_states.get(worker_id, {}).update({"state": "failed"})
                self._sync_worker_to_dashboard(worker_id)
                self._schedule_revive(worker, "dialer recovery failed")
                return

        self._in_queue.add(worker_id)
        await self._available_workers.put(worker)
        self._worker_states.get(worker_id, {}).update({"state": "ready"})
        self._sync_worker_to_dashboard(worker_id)
        if not worker.phone_number:
            self._schedule_number_refresh(worker)
        logger.debug(f"Worker {worker_id} released back to pool.")

    async def force_release_checked_out(self, reason: str = "force") -> int:
        """
        Best-effort: hang up + release every checked-out worker.
        Used by /p1stop and agent watchdog so pool cannot stay permanently busy.
        """
        n = 0
        for worker in list(self.workers):
            wid = getattr(worker, "worker_id", -1)
            if wid not in self._checked_out_worker_ids:
                continue
            n += 1
            try:
                for cid in list(getattr(worker, "_active_calls", {})):
                    try:
                        await worker.hangup(cid)
                    except Exception:
                        pass
                getattr(worker, "_active_calls", {}).clear()
            except Exception:
                pass
            try:
                # No recycle here: a hard stop must not wait on a page reload.
                await self.release(worker, recycle=False)
            except Exception as e:
                logger.warning(f"force_release worker {wid} failed: {e}")
                self._checked_out_worker_ids.discard(wid)
                self._checked_out_at.pop(wid, None)
        if n:
            logger.warning(f"force_release_checked_out({reason}): touched {n} worker(s)")
        return n

    async def reclaim_stuck_workers(self) -> int:
        """Reclaim workers checked out longer than GV_WORKER_STUCK_SEC."""
        max_age = max(60, int(getattr(config, "GV_WORKER_STUCK_SEC", 240)))
        now = time.time()
        stuck = [
            wid
            for wid, ts in list(self._checked_out_at.items())
            if now - ts >= max_age
        ]
        if not stuck:
            return 0
        logger.warning(
            f"Watchdog: reclaiming stuck workers {stuck} (>{max_age}s checked out)"
        )
        reclaimed = 0
        for worker in list(self.workers):
            wid = getattr(worker, "worker_id", -1)
            if wid not in stuck:
                continue
            try:
                for cid in list(getattr(worker, "_active_calls", {})):
                    try:
                        await worker.hangup(cid)
                    except Exception:
                        pass
                getattr(worker, "_active_calls", {}).clear()
                # Soft page recover (may fail quickly via hard timeout).
                try:
                    await worker.recover_dialer_state("stuck reclaim")
                except Exception:
                    pass
                if wid in self._checked_out_worker_ids:
                    # Recovery already happened above; keep the watchdog quick.
                    await self.release(worker, recycle=False)
                reclaimed += 1
            except Exception as e:
                logger.warning(f"Watchdog reclaim worker {wid} error: {e}")
                self._checked_out_worker_ids.discard(wid)
                self._checked_out_at.pop(wid, None)
                self._worker_states.get(wid, {}).update(
                    {"state": "failed", "last_error": f"stuck reclaim: {e}"}
                )
                self._sync_worker_to_dashboard(wid)
        return reclaimed

    def get_status_snapshot(self) -> dict:
        """Return live pool and per-worker readiness state."""
        total = len(WORKER_POOL)
        states = list(self._worker_states.values())
        authenticated = sum(1 for s in states if s.get("authenticated"))
        numbered = sum(1 for s in states if s.get("number_fetched"))
        ready = sum(1 for s in states if s.get("state") == "ready")
        restarting = sum(1 for s in states if s.get("state") == "restarting")
        needs_login = sum(1 for s in states if s.get("state") == "needs_login")
        challenge = sum(1 for s in states if s.get("state") == "challenge")
        captcha = sum(1 for s in states if s.get("state") == "captcha")
        logging_in = sum(1 for s in states if s.get("state") == "logging_in")
        init_seconds = None
        if self._init_started_at and self._init_finished_at:
            init_seconds = round(self._init_finished_at - self._init_started_at, 2)

        return {
            "state": self._state,
            "total_workers": total,
            "ready_workers": ready,
            "authenticated_workers": authenticated,
            "numbers_fetched_workers": numbered,
            "checked_out_workers": len(self._checked_out_worker_ids),
            "restarting_workers": restarting,
            "needs_login_workers": needs_login,
            "challenge_workers": challenge,
            "captcha_workers": captcha,
            "logging_in_workers": logging_in,
            "credentials_known": len(self._credentials),
            "oom_events": sum(w.oom_events for w in self.workers),
            "browser_restarts": sum(w.restart_count for w in self.workers),
            "all_workers_ready": (total > 0 and ready == total),
            "all_logged_in_and_numbered": (
                total > 0 and authenticated == total and numbered == total
            ),
            "init_seconds": init_seconds,
            "workers": states,
        }

    def _sync_worker_to_dashboard(self, worker_id: int):
        """Push worker state to the Rich live dashboard."""
        try:
            from console_ui import dashboard
        except Exception:
            return

        state = self._worker_states.get(worker_id)
        if not state:
            return

        st = state["state"]
        if st == "spam":
            status = "🚨 SPAM"
            stt = "Flagged — cookies cleared, sign in a new account"
            phone = state.get("phone_number") or "—"
        elif st == "banned":
            status = "🚫 BANNED"
            stt = state.get("last_error") or "Account banned by Google"
            phone = state.get("phone_number") or "—"
        elif st == "ready":
            status = "GV Ready ✅"
            stt = f"Logged in — {state.get('phone_number') or 'number unknown'}"
            rss = state.get("rss_mb")
            if rss:
                stt += f" | {rss:.0f}MB"
                heap = state.get("heap_pct")
                if heap:
                    stt += f" heap {heap}%"
                served = state.get("calls_served")
                if served:
                    stt += f" | {served} calls"
            phone = state.get("phone_number") or "Unknown"
        elif st == "initializing":
            status = "GV Init…"
            stt = "Launching browser"
            phone = "—"
        elif st == "restarting":
            status = "♻️ Restarting"
            stt = f"Respawning Chrome — {state.get('last_error') or 'memory recycle'}"
            phone = state.get("phone_number") or "—"
        elif st == "logging_in":
            status = "🔐 Signing in"
            stt = f"Google sign-in as {state.get('account_email') or 'unknown account'}"
            phone = "—"
        elif st == "needs_login":
            status = "🔑 Needs login"
            email = state.get("account_email")
            err = state.get("last_error")
            stt = (
                f"Waiting for a password — send /login (account {email})"
                if email else
                (
                    f"{err} — send /login" if err else
                    f"No credentials for {state.get('profile') or 'this profile'} "
                    f"— send /login"
                )
            )
            phone = "—"
        elif st == "challenge":
            status = "🧩 2FA needed"
            stt = state.get("last_error") or "Google wants a human verification"
            phone = "—"
        elif st == "captcha":
            status = "🤖 Captcha"
            stt = state.get("last_error") or "Google showed a robot check"
            phone = "—"
        else:
            status = "GV Failed ❌"
            stt = state.get("last_error") or "Login/number fetch failed"
            phone = "—"

        dashboard.update_worker(worker_id, phone=phone, status=status, stt=stt)
        self._update_pool_status_banner()

    # ──────────────────────────────────────────────────────────────────────────
    # Spam auto-disable
    # ──────────────────────────────────────────────────────────────────────────

    _TRANSIENT_LOSS_MARKERS = (
        "dns_probe",
        "no_internet",
        "no internet",
        "err_internet",
        "err_name_not_resolved",
        "err_network",
        "err_connection",
        "spawn timeout",
        "browser spawn",
        "chromeoptions",
        "cannot reuse",
        "chrome not reachable",
        "page=unknown",
        "chrome://",
        "new-tab-page",
        "about:blank",
        "network-error",
    )

    @classmethod
    def _is_transient_loss(cls, reason: Optional[str]) -> bool:
        """Host/Chrome problems — cookies must stay."""
        s = (reason or "").lower()
        if not s:
            return False
        return any(m in s for m in cls._TRANSIENT_LOSS_MARKERS)

    @staticmethod
    def _slot_already_vacant(worker: PhoneClient) -> bool:
        """Empty /login slot — do not wipe it again."""
        ls = getattr(worker, "login_state", None)
        if ls not in ("needs_login", "expired"):
            return False
        if getattr(worker, "phone_number", None):
            return False
        if getattr(worker, "account_email", None) or getattr(
            worker, "credential_email", None
        ):
            return False
        has = getattr(worker, "has_credentials", None)
        if callable(has):
            try:
                if has():
                    return False
            except Exception:
                pass
        return True

    def _dead_account_reason(self, worker: PhoneClient) -> Optional[str]:
        """Wipe cookies only when Google burned this account.

        A Voice product ban, a disabled Google account, or our own spam
        flag. Everything else (missing number, missing dialer, landing,
        logout, DNS, Chrome crash, spawn timeout, page=unknown) keeps
        cookies and stored credentials — restart / auto_login / /login.
        """
        if getattr(worker, "is_spam_flagged", False):
            number = getattr(worker, "phone_number", None)
            return (
                f"Number {number} flagged as spam" if number
                else "flagged as spam"
            )
        if getattr(worker, "is_banned", False) or getattr(
            worker, "login_state", None
        ) == "banned":
            email = worker.account_email or getattr(worker, "credential_email", None)
            return (
                f"account banned by Google ({email})" if email
                else "account banned by Google"
            )
        if self._slot_already_vacant(worker):
            return None
        offline = getattr(worker, "_page_looks_offline", None)
        if callable(offline):
            try:
                if offline():
                    return None
            except Exception:
                pass

        outcome = getattr(worker, "login_outcome", None)
        if outcome == "disabled":
            return "google account disabled"

        lost = None
        fn = getattr(worker, "session_lost_reason", None)
        if callable(fn):
            try:
                lost = fn()
            except Exception:
                lost = None
        if lost == "banned" or self._is_google_burn(lost):
            email = worker.account_email or getattr(worker, "credential_email", None)
            return (
                f"account banned by Google ({email})" if email
                else (lost or "account banned by Google")
            )
        return None

    _GOOGLE_BURN_MARKERS = (
        "account banned",
        "banned by google",
        "google account disabled",
        "account has been disabled",
        "аккаунт отключен",
        "аккаунт заблокирован",
        "answer/40039",
        "answer/16053145",
        "contact/suspended",
        "flagged as spam",
        "unable to access a google product",
        "unable to access google voice",
        "access to this google product has been suspended",
        "access to google voice has been suspended",
    )

    @classmethod
    def _is_google_burn(cls, reason: Optional[str]) -> bool:
        """True only for Google's own account-death pages / our spam flag."""
        s = (reason or "").lower().strip()
        if not s:
            return False
        if s == "banned":
            return True
        if cls._is_transient_loss(s):
            return False
        return any(m in s for m in cls._GOOGLE_BURN_MARKERS)

    @classmethod
    def _is_ban_lost(cls, lost) -> bool:
        return cls._is_google_burn(lost)

    async def vacate_banned_account(
        self, worker: PhoneClient, reason: Optional[str] = None
    ):
        """Mark the account banned, wipe cookies, restart empty Chrome.

        The slot becomes needs_login so /login can assign a new account.
        The burned email is logged; is_banned is cleared after the wipe
        (wipe_cookies does that) so the profile is not parked forever.
        """
        email = worker.account_email or getattr(worker, "credential_email", None)
        why = reason or (
            f"account banned by Google ({email})" if email
            else "account banned by Google"
        )
        wid = getattr(worker, "worker_id", -1)
        logger.warning(
            f"Worker {wid}: BANNED"
            f"{' (' + email + ')' if email else ''} — wiping cookies and "
            f"restarting the profile so /login can assign a new account"
        )
        await self.vacate_dead_account(worker, why, respawn=True)
        worker.is_banned = False

    async def recycle_captcha_account(
        self, worker: PhoneClient, wait_spawn: bool = False
    ):
        """Restart a captcha'd profile onto a clean login page.

        Does not drop stored credentials (unlike a ban). Cookie wipe + Chrome
        close run immediately after this worker has left the login semaphore,
        so other sign-ins keep going. Chrome respawn uses the UC spawn lock
        in a background task unless `wait_spawn` is set (pool init).
        """
        from telephony import gv_login

        wid = getattr(worker, "worker_id", -1)
        email = worker.account_email or getattr(worker, "credential_email", None)
        logger.warning(
            f"Worker {wid}: CAPTCHA"
            f"{' (' + email + ')' if email else ''} — wiping the sticky "
            f"session and restarting onto the login page"
        )
        loop = asyncio.get_running_loop()
        reset = getattr(worker, "reset_to_login_page", None)
        if callable(reset):
            await loop.run_in_executor(
                None,
                lambda: reset(
                    "reCAPTCHA — restarted onto the login page",
                    True,
                ),
            )
        worker.login_outcome = gv_login.CAPTCHA
        worker.login_state = "needs_login"
        self._worker_states.get(wid, {}).update({
            "state": "needs_login",
            "last_error": "reCAPTCHA — restarted onto the login page",
            "authenticated": False,
            "login_state": "needs_login",
            "login_outcome": gv_login.CAPTCHA,
            "has_credentials": bool(
                getattr(worker, "has_credentials", lambda: False)()
            ),
        })
        self._sync_worker_to_dashboard(wid)

        async def _respawn():
            setup = getattr(worker, "_setup_browser_only", None)
            if not callable(setup):
                return
            startup = max(10, int(getattr(config, "GV_STARTUP_TIMEOUT_SEC", 120)))
            try:
                async with _uc_spawn_lock:
                    spawned = await asyncio.wait_for(
                        loop.run_in_executor(None, setup),
                        timeout=startup,
                    )
            except Exception as e:
                logger.warning(
                    f"Worker {wid}: captcha-slot Chrome spawn failed — "
                    f"/login will retry ({e})"
                )
                spawned = False
            if spawned:
                def _land():
                    try:
                        worker.driver.get(gv_login.LANDING_URL)
                    except Exception:
                        pass
                try:
                    await loop.run_in_executor(None, _land)
                except Exception:
                    pass
                logger.info(
                    f"Worker {wid}: captcha profile back on the login page, "
                    f"waiting for /login"
                )
            worker.login_outcome = gv_login.CAPTCHA
            worker.login_state = "needs_login"

        prev = self._respawn_tasks.get(wid)
        if prev is not None and not prev.done():
            prev.cancel()
        task = asyncio.create_task(_respawn())
        self._respawn_tasks[wid] = task
        if wait_spawn:
            try:
                await task
            except Exception:
                pass

    async def vacate_dead_account(
        self, worker: PhoneClient, reason: str, respawn: bool = True
    ):
        """Wipe a logged-out/broken GV session and leave the profile for /login.

        Cookies and stored credentials go; Chrome is restarted onto the landing
        page so the operator can put a new account on this slot.
        """
        wid = getattr(worker, "worker_id", -1)
        number = worker.phone_number
        email = worker.account_email or getattr(worker, "credential_email", None)
        profile = worker.profile_dir.name if worker.profile_dir else None
        logger.warning(
            f"Worker {wid} SESSION DEAD ({reason}) — clearing cookies"
            f"{' for ' + email if email else ''}"
            f"{' number=' + number if number else ''} and opening /login"
        )

        loop = asyncio.get_running_loop()
        wipe = getattr(worker, "wipe_cookies", None)
        if callable(wipe):
            def _wipe():
                try:
                    wipe(reason)
                except TypeError:
                    wipe()
            await loop.run_in_executor(None, _wipe)

        if profile:
            self._credentials.drop(profile)
            self._credentials.save()

        worker.is_authenticated = False
        worker.is_banned = False
        worker.login_state = "needs_login"
        worker.phone_number = None
        worker.account_email = None
        if hasattr(worker, "clear_credentials"):
            worker.clear_credentials()

        self._worker_states.get(wid, {}).update({
            "state": "needs_login",
            "last_error": f"{reason} — cookies cleared, send /login",
            "authenticated": False,
            "number_fetched": False,
            "phone_number": None,
            "account_email": None,
            "has_credentials": False,
            "login_state": "needs_login",
            "login_outcome": None,
        })
        self._sync_worker_to_dashboard(wid)

        if not respawn:
            return
        setup = getattr(worker, "_setup_browser_only", None)
        if not callable(setup):
            return
        startup = max(10, int(getattr(config, "GV_STARTUP_TIMEOUT_SEC", 120)))
        try:
            async with _uc_spawn_lock:
                spawned = await asyncio.wait_for(
                    loop.run_in_executor(None, setup),
                    timeout=startup,
                )
        except Exception as e:
            logger.warning(
                f"Worker {wid}: vacant-slot Chrome spawn failed — "
                f"/login will retry ({e})"
            )
            abort = getattr(worker, "_abort_inflight_spawn", None)
            if callable(abort):
                try:
                    await loop.run_in_executor(None, abort)
                except Exception:
                    pass
            spawned = False
        worker.login_state = "needs_login"
        worker.is_authenticated = False
        if spawned:
            logger.info(
                f"Worker {wid}: vacant profile restarted, waiting for /login"
            )

    async def disable_worker_spam(self, worker: PhoneClient):
        """Wipe a spam-flagged account and leave the Chrome profile for /login."""
        number = worker.phone_number
        await self.vacate_dead_account(
            worker,
            reason=f"Number {number} flagged as spam",
            respawn=False,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Shutdown
    # ──────────────────────────────────────────────────────────────────────────

    def shutdown(self):
        """Close all browser instances."""
        logger.info("Shutting down Telephony Pool…")
        for task in self._revive_tasks.values():
            if not task.done():
                task.cancel()
        self._revive_tasks = {}
        for worker in self.workers:
            try:
                worker.close()
            except Exception as e:
                logger.error(f"Error shutting down worker: {e}")
        self._state = "stopped"
