"""
Redis bus — controller/agent coordination.

Phase 1 surface:
  - connect / close / ping
  - agent registry with TTL self-expiry (register_or_heartbeat / list_agents)

Phase 2+ will add job/result streams; the names are reserved here so the
protocol stays stable.

`redis` is imported lazily so `standalone` mode never needs the dependency.
"""
import asyncio
import json
import time
import uuid
from typing import List, Optional, Tuple

from loguru import logger

import config

# ── Reserved key / stream names (stable protocol) ──────────────────────────────
AGENT_KEY_PREFIX = "gv:agent:"          # gv:agent:<agent_id>  (JSON, EX=TTL)
JOB_STREAM_PREFIX = "gv:jobs:"          # gv:jobs:<agent_id>  (one per agent)
JOB_GROUP = "gvjobs"                    # consumer group on a per-agent stream
RESULTS_STREAM = "gv:results"           # agents → controller (single stream)
RESULTS_GROUP = "gvresults"             # controller consumer group
CALL_EVENTS_PREFIX = "gv:call:"         # Phase 3: gv:call:<id>:events / :control
P1_RUN_PREFIX = "gv:p1:"                # gv:p1:<run_id>:queue | :stop
P1_ACTIVE_KEY = "gv:p1:active"          # current run_id pointer
P1_SESSION_PREFIX = "gv:p1:session:"    # gv:p1:session:<run_id> JSON blob
P1_HISTORY_KEY = "gv:p1:history"        # list of recent finished run_ids
SETTINGS_KEY = "gv:settings"            # hash: remote-tunable agent settings
UPDATE_KEY = "gv:update"               # JSON: fleet update signal {ts, ref}
_P1_RUN_TTL_SEC = 86400                 # auto-expire idle run keys after 24h
_P1_HISTORY_TTL_SEC = 7 * 86400         # finished campaigns stay a week
_STREAM_MAXLEN = 2000                   # approx cap so streams don't grow forever


class RedisBus:
    """Thin async wrapper around a Redis connection for cluster coordination."""

    def __init__(self, url: Optional[str] = None):
        self._url = url or config.REDIS_URL
        self._redis = None  # redis.asyncio.Redis

    async def connect(self):
        if self._redis is not None:
            return self._redis
        if not self._url:
            raise RuntimeError("REDIS_URL is not set")
        try:
            # Lazy import: standalone mode must not require the package.
            from redis.asyncio import Redis
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "redis package not installed — run: pip install -r requirements.txt"
            ) from e

        # rediss:// in the URL enables TLS automatically; auth lives in the URL.
        self._redis = Redis.from_url(
            self._url,
            decode_responses=True,
            socket_timeout=10,
            socket_connect_timeout=10,
            health_check_interval=30,
        )
        await self._redis.ping()
        logger.info("RedisBus connected")
        return self._redis

    async def close(self):
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None

    @staticmethod
    def _is_conn_error(exc: Exception) -> bool:
        """True for transient transport failures (reset/timeout/closed/refused).

        These are the RDP<->Redis blips seen in the wild ('Connection closed by
        server', 'Timeout reading', 'Подключение к сети было разорвано', WinError
        10054/10061). They must NOT be treated as terminal (e.g. 'queue empty').
        """
        name = type(exc).__name__.lower()
        if "connection" in name or "timeout" in name:
            return True
        msg = str(exc).lower()
        markers = (
            "connection", "closed by server", "timeout", "reset",
            "разорвано", "отказано", "broken pipe", "10054", "10061",
            "not connected", "reading from",
        )
        return any(m in msg for m in markers)

    async def _reset(self):
        """Drop the cached client so the next connect() builds a fresh pool."""
        r, self._redis = self._redis, None
        if r is not None:
            try:
                await r.aclose()
            except Exception:
                pass

    async def ping(self) -> bool:
        try:
            r = await self.connect()
            await r.ping()
            return True
        except Exception as e:
            logger.warning(f"RedisBus ping failed: {e}")
            return False

    # ── Agent registry ─────────────────────────────────────────────────────────

    async def register_or_heartbeat(self, snapshot: dict) -> None:
        """Write/refresh this agent's record. Auto-expires via TTL so a dead
        agent disappears from the registry without explicit cleanup."""
        r = await self.connect()
        agent_id = config.AGENT_ID
        payload = dict(snapshot)
        payload["agent_id"] = agent_id
        payload["role"] = config.NODE_ROLE
        payload["ts"] = time.time()
        try:
            await r.set(
                f"{AGENT_KEY_PREFIX}{agent_id}",
                json.dumps(payload),
                ex=max(5, config.AGENT_TTL_SEC),
            )
        except Exception as e:
            # Drop the broken socket so the caller's next heartbeat rebuilds it.
            if self._is_conn_error(e):
                await self._reset()
            raise

    async def unregister(self) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(f"{AGENT_KEY_PREFIX}{config.AGENT_ID}")
        except Exception:
            pass

    async def list_agents(self) -> List[dict]:
        """Return all live agents (stale ones already TTL-expired)."""
        r = await self.connect()
        agents: List[dict] = []
        async for key in r.scan_iter(match=f"{AGENT_KEY_PREFIX}*", count=100):
            try:
                raw = await r.get(key)
                if raw:
                    agents.append(json.loads(raw))
            except Exception as e:
                logger.debug(f"list_agents: bad record {key}: {e}")
        agents.sort(key=lambda a: a.get("agent_id", ""))
        return agents

    # ── Phase 2: job dispatch (controller → a specific agent) ──────────────────

    async def _ensure_group(self, stream: str, group: str) -> None:
        r = await self.connect()
        try:
            await r.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def dispatch_job(self, agent_id: str, job: dict) -> str:
        """Controller: queue a job for ONE agent. Returns job_id."""
        r = await self.connect()
        job = dict(job)
        job.setdefault("job_id", uuid.uuid4().hex[:12])
        job["target_agent"] = agent_id
        job["ts"] = time.time()
        await r.xadd(
            f"{JOB_STREAM_PREFIX}{agent_id}",
            {"data": json.dumps(job)},
            maxlen=_STREAM_MAXLEN, approximate=True,
        )
        return job["job_id"]

    async def consume_jobs(
        self, agent_id: str, block_ms: int = 5000
    ) -> List[Tuple[str, dict]]:
        """Agent: read pending jobs for THIS agent (consumer group)."""
        r = await self.connect()
        stream = f"{JOB_STREAM_PREFIX}{agent_id}"
        await self._ensure_group(stream, JOB_GROUP)
        out: List[Tuple[str, dict]] = []

        # Reclaim entries left un-acked by a previous (crashed) run.
        try:
            claimed = await r.xautoclaim(
                stream, JOB_GROUP, agent_id, min_idle_time=60000, count=10
            )
            for entry_id, fields in (claimed[1] or []):
                if fields and fields.get("data"):
                    out.append((entry_id, json.loads(fields["data"])))
        except Exception as e:
            logger.debug(f"xautoclaim skipped: {e}")

        try:
            resp = await r.xreadgroup(
                JOB_GROUP, agent_id, {stream: ">"}, count=1, block=block_ms
            )
            for _stream, entries in (resp or []):
                for entry_id, fields in entries:
                    if fields and fields.get("data"):
                        out.append((entry_id, json.loads(fields["data"])))
        except Exception as e:
            logger.warning(f"consume_jobs error: {e}")
            # NOGROUP self-heals next loop via _ensure_group. Connection blips:
            # drop the socket so the next call rebuilds it. Always back off a
            # little so a persistent error cannot spin the loop at 100% CPU.
            if self._is_conn_error(e):
                await self._reset()
            await asyncio.sleep(1.0)
        return out

    async def ack_job(self, agent_id: str, entry_id: str) -> None:
        r = await self.connect()
        stream = f"{JOB_STREAM_PREFIX}{agent_id}"
        try:
            await r.xack(stream, JOB_GROUP, entry_id)
            await r.xdel(stream, entry_id)
        except Exception as e:
            logger.debug(f"ack_job error: {e}")

    # ── Phase 2: results (agent → controller) ──────────────────────────────────

    async def publish_result(self, result: dict) -> None:
        """Agent: push one result message onto the shared results stream."""
        r = await self.connect()
        await r.xadd(
            RESULTS_STREAM,
            {"data": json.dumps(result)},
            maxlen=_STREAM_MAXLEN, approximate=True,
        )

    async def new_results_group(self) -> str:
        """Create a UNIQUE per-run consumer group (reads only messages
        published after creation). Isolates concurrent distributed runs so
        they never steal/delete each other's results."""
        r = await self.connect()
        group = f"ctl-{uuid.uuid4().hex[:10]}"
        try:
            await r.xgroup_create(RESULTS_STREAM, group, id="$", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise
        return group

    async def destroy_results_group(self, group: str) -> None:
        if self._redis is None or not group:
            return
        try:
            await self._redis.xgroup_destroy(RESULTS_STREAM, group)
        except Exception:
            pass

    async def consume_results(
        self, group: str, block_ms: int = 2000, count: int = 50
    ) -> List[Tuple[str, dict]]:
        """Controller: read result messages for a specific run group.

        Hardened: if the consumer group vanished (Redis restart / stream trim →
        NOGROUP), recreate it instead of throwing on every call. Previously this
        raised NOGROUP synchronously with no backoff, spinning the result loop
        thousands of times/sec (log flood) while the run silently stalled.
        """
        r = await self.connect()
        out: List[Tuple[str, dict]] = []
        try:
            resp = await r.xreadgroup(
                group, "controller", {RESULTS_STREAM: ">"},
                count=count, block=block_ms,
            )
            for _stream, entries in (resp or []):
                for entry_id, fields in entries:
                    if fields and fields.get("data"):
                        out.append((entry_id, json.loads(fields["data"])))
        except Exception as e:
            emsg = str(e)
            if "NOGROUP" in emsg:
                # Group disappeared mid-run — recreate at "$" (only new results)
                # so we don't re-read and double-count this run's own history.
                logger.warning(f"consume_results: group '{group}' missing — recreating")
                try:
                    await r.xgroup_create(RESULTS_STREAM, group, id="$", mkstream=True)
                except Exception as ce:
                    if "BUSYGROUP" not in str(ce):
                        logger.debug(f"consume_results regroup failed: {ce}")
                await asyncio.sleep(0.5)
            else:
                logger.warning(f"consume_results error: {e}")
                if self._is_conn_error(e):
                    await self._reset()
                await asyncio.sleep(1.0)
        return out

    async def ack_result(self, group: str, entry_id: str) -> None:
        # XACK only — never XDEL: the stream is shared by all run groups and
        # bounded by MAXLEN, so deleting would break concurrent runs.
        r = await self.connect()
        try:
            await r.xack(RESULTS_STREAM, group, entry_id)
        except Exception as e:
            logger.debug(f"ack_result error: {e}")

    # ── P1 shared work queue (controller enqueues, agents BRPOP) ───────────────

    def _p1_queue_key(self, run_id: str) -> str:
        return f"{P1_RUN_PREFIX}{run_id}:queue"

    def _p1_stop_key(self, run_id: str) -> str:
        return f"{P1_RUN_PREFIX}{run_id}:stop"

    async def create_p1_run(self, run_id: str, numbers: List[str]) -> int:
        """Controller: seed a cluster-wide P1 run. Returns number count."""
        if not numbers:
            return 0
        r = await self.connect()
        key = self._p1_queue_key(run_id)
        await r.rpush(key, *numbers)
        await r.expire(key, _P1_RUN_TTL_SEC)
        return len(numbers)

    async def claim_p1_number(
        self, run_id: str, block_ms: int = 3000
    ) -> Optional[str]:
        """Agent: atomically take the next phone from the shared run queue."""
        r = await self.connect()
        try:
            resp = await r.brpop(self._p1_queue_key(run_id), timeout=max(1, block_ms // 1000))
        except Exception as e:
            logger.debug(f"claim_p1_number error: {e}")
            if self._is_conn_error(e):
                await self._reset()
            return None
        if not resp:
            return None
        return resp[1]

    async def requeue_p1_number(self, run_id: str, phone: str) -> None:
        """Agent: put back a claimed number that was never dialed (no free
        worker). LPUSH so it is retried after the numbers already waiting."""
        r = await self.connect()
        try:
            await r.lpush(self._p1_queue_key(run_id), phone)
        except Exception as e:
            logger.debug(f"requeue_p1_number error: {e}")
            if self._is_conn_error(e):
                await self._reset()

    async def p1_queue_length(self, run_id: str) -> int:
        """Remaining numbers in the shared run queue.

        Returns -1 on a transport error (NOT 0). Callers must treat 0 as
        'confirmed empty → done' and -1 as 'unknown → retry', so a network blip
        can never make an agent quit a run early with numbers still queued.
        """
        r = await self.connect()
        try:
            return int(await r.llen(self._p1_queue_key(run_id)))
        except Exception as e:
            logger.debug(f"p1_queue_length error: {e}")
            if self._is_conn_error(e):
                await self._reset()
            return -1

    async def set_p1_stop(self, run_id: str) -> None:
        """Controller: signal all agents to stop claiming new numbers."""
        r = await self.connect()
        await r.set(self._p1_stop_key(run_id), "1", ex=_P1_RUN_TTL_SEC)

    async def is_p1_stopped(self, run_id: str) -> bool:
        r = await self.connect()
        try:
            return bool(await r.get(self._p1_stop_key(run_id)))
        except Exception:
            return False

    async def cleanup_p1_run(self, run_id: str) -> None:
        if self._redis is None or not run_id:
            return
        try:
            await self._redis.delete(
                self._p1_queue_key(run_id),
                self._p1_stop_key(run_id),
            )
        except Exception:
            pass

    async def save_p1_session(self, session: dict) -> None:
        """Persist live P1 session stats (readable by any controller user)."""
        run_id = session.get("run_id")
        if not run_id:
            return
        r = await self.connect()
        session = dict(session)
        session["updated_at"] = time.time()
        await r.set(
            f"{P1_SESSION_PREFIX}{run_id}",
            json.dumps(session),
            ex=_P1_RUN_TTL_SEC,
        )
        if session.get("running"):
            await r.set(P1_ACTIVE_KEY, run_id, ex=_P1_RUN_TTL_SEC)

    async def get_p1_session(self, run_id: str) -> Optional[dict]:
        r = await self.connect()
        try:
            raw = await r.get(f"{P1_SESSION_PREFIX}{run_id}")
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def get_p1_active_session(self) -> Optional[dict]:
        r = await self.connect()
        try:
            run_id = await r.get(P1_ACTIVE_KEY)
            if not run_id:
                return None
            return await self.get_p1_session(run_id)
        except Exception:
            return None

    async def get_p1_viewable_session(self) -> Optional[dict]:
        """Active run, or the most recently finished run (for /p1live)."""
        session = await self.get_p1_active_session()
        if session:
            return session
        r = await self.connect()
        try:
            run_id = await r.get("gv:p1:last")
            if run_id:
                return await self.get_p1_session(run_id)
        except Exception:
            pass
        return None

    async def finish_p1_session(self, session: dict) -> None:
        """Mark session complete and clear the active-run pointer."""
        run_id = session.get("run_id")
        if not run_id:
            return
        session = dict(session)
        session["running"] = False
        await self.save_p1_session(session)
        if self._redis is None:
            return
        try:
            # Finished campaigns stay exportable for a week, not 24h.
            await self._redis.expire(
                f"{P1_SESSION_PREFIX}{run_id}", _P1_HISTORY_TTL_SEC
            )
            await self._redis.set("gv:p1:last", run_id, ex=_P1_HISTORY_TTL_SEC)
            active = await self._redis.get(P1_ACTIVE_KEY)
            if active == run_id:
                await self._redis.delete(P1_ACTIVE_KEY)
            await self._redis.lrem(P1_HISTORY_KEY, 0, run_id)
            await self._redis.lpush(P1_HISTORY_KEY, run_id)
            await self._redis.ltrim(P1_HISTORY_KEY, 0, 19)
            await self._redis.expire(P1_HISTORY_KEY, _P1_HISTORY_TTL_SEC)
        except Exception:
            pass

    async def get_p1_history(self, limit: int = 20) -> List[dict]:
        """Recent finished campaigns, newest first (for Campaign results)."""
        r = await self.connect()
        try:
            ids = await r.lrange(P1_HISTORY_KEY, 0, max(0, limit - 1))
        except Exception:
            return []
        out = []
        for rid in ids:
            try:
                session = await self.get_p1_session(
                    rid.decode() if isinstance(rid, bytes) else rid
                )
            except Exception:
                session = None
            if session:
                out.append(session)
        return out

    # ── Remote agent settings (controller writes, agents poll) ──────────────

    async def save_settings(self, values: dict) -> None:
        """Controller: merge key/value pairs into the shared settings hash."""
        r = await self.connect()
        mapping = {str(k): str(v) for k, v in (values or {}).items()}
        if mapping:
            await r.hset(SETTINGS_KEY, mapping=mapping)

    async def get_settings(self) -> dict:
        """All remote settings (name -> raw string). Agents poll this."""
        r = await self.connect()
        try:
            data = await r.hgetall(SETTINGS_KEY)
            return {str(k): str(v) for k, v in (data or {}).items()}
        except Exception as e:
            logger.debug(f"get_settings error: {e}")
            if self._is_conn_error(e):
                await self._reset()
            return {}

    async def delete_setting(self, name: str) -> None:
        r = await self.connect()
        try:
            await r.hdel(SETTINGS_KEY, name)
        except Exception as e:
            logger.debug(f"delete_setting error: {e}")

    # ── Fleet update signal (controller → all agents) ────────────────────────

    async def set_update_signal(self, payload: dict) -> None:
        """Controller: tell every agent to git-pull and restart."""
        r = await self.connect()
        payload = dict(payload)
        payload.setdefault("ts", time.time())
        await r.set(UPDATE_KEY, json.dumps(payload))

    async def get_update_signal(self) -> Optional[dict]:
        """Agents poll this in the watchdog loop."""
        r = await self.connect()
        try:
            raw = await r.get(UPDATE_KEY)
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.debug(f"get_update_signal error: {e}")
            if self._is_conn_error(e):
                await self._reset()
            return None

    # ── Phase 3b: per-call live bridge (controller <-> agent, pub/sub) ─────────

    async def pick_agent(self) -> Optional[str]:
        """Choose the agent with the most ready (free) workers."""
        agents = [
            a for a in await self.list_agents()
            if a.get("role") == "agent"
            and (a.get("ready_workers", 0) - a.get("checked_out_workers", 0)) > 0
        ]
        if not agents:
            return None
        agents.sort(
            key=lambda a: a.get("ready_workers", 0) - a.get("checked_out_workers", 0),
            reverse=True,
        )
        return agents[0]["agent_id"]

    async def publish_call_event(self, call_id: str, event: dict) -> None:
        r = await self.connect()
        await r.publish(
            f"{CALL_EVENTS_PREFIX}{call_id}:events", json.dumps(event)
        )

    async def publish_call_control(self, call_id: str, ctl: dict) -> None:
        r = await self.connect()
        await r.publish(
            f"{CALL_EVENTS_PREFIX}{call_id}:control", json.dumps(ctl)
        )

    async def subscribe(self, channel: str):
        """Return a pubsub object subscribed to `channel`."""
        r = await self.connect()
        ps = r.pubsub()
        await ps.subscribe(channel)
        return ps

    @staticmethod
    async def next_message(pubsub, timeout: float = 1.0) -> Optional[dict]:
        """Get the next JSON message from a pubsub, or None on timeout."""
        try:
            m = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=timeout
            )
        except Exception as e:
            logger.debug(f"pubsub get_message error: {e}")
            return None
        if not m or m.get("type") != "message":
            return None
        try:
            return json.loads(m["data"])
        except Exception:
            return None
