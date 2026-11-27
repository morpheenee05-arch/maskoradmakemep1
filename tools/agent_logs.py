#!/usr/bin/env python3
"""
Fetch live logs from agents over the Redis bus — no RDP/SSH copy-paste.

Agents answer a "logs" job with the tail of their ai-caller.log. Works from
any machine that has this repo + .env with REDIS_URL.

    py -3 tools/agent_logs.py                              # list online agents
    py -3 tools/agent_logs.py --agent w2                   # last 300 lines
    py -3 tools/agent_logs.py --agent 31.77 --lines 500    # match by ip/host/id
    py -3 tools/agent_logs.py --agent all --grep "dtmf|account_banned"
    py -3 tools/agent_logs.py --agent w2 --grep "p1 done" --timeout 40

--grep is a case-insensitive regex (plain substring works too).
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from infra.redis_bus import RedisBus  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def pick_agents(agents: list, selector: str) -> list:
    """Match --agent against id / host / ip (substring, case-insensitive)."""
    selector = (selector or "").strip().lower()
    if selector in ("", "all", "*"):
        return agents
    out = [
        a for a in agents
        if selector in str(a.get("agent_id", "")).lower()
        or selector in str(a.get("host", "")).lower()
        or selector in str(a.get("ip", "")).lower()
    ]
    return out


def fmt_agent(a: dict) -> str:
    bits = [a.get("agent_id", "?")]
    if a.get("host"):
        bits.append(str(a["host"]))
    if a.get("ip"):
        bits.append(str(a["ip"]))
    bits.append(f"state={a.get('state', '?')}")
    bits.append(f"v={a.get('version', '?')}")
    if a.get("p1_running"):
        bits.append("p1=RUNNING")
    return " · ".join(bits)


async def main_async(args) -> int:
    bus = RedisBus()
    try:
        ok = False
        for attempt in range(4):  # the link to Redis blips — retry a bit
            if await bus.ping():
                ok = True
                break
            await asyncio.sleep(1.5)
        if not ok:
            print("Redis недоступен — проверь REDIS_URL в .env")
            return 1

        agents = await bus.list_agents()
        agents = [a for a in agents if a.get("role") in ("agent", "standalone")]
        if not agents:
            print("Нет онлайн-агентов в регистре.")
            return 1

        if args.list or not args.agent:
            print(f"Онлайн-агенты ({len(agents)}):")
            for a in agents:
                age = time.time() - float(a.get("ts") or time.time())
                print(f"  {fmt_agent(a)}  (hb {age:.0f}s ago)")
            print("\nДальше: --agent <id|host|ip|all> [--lines N] [--grep RX]")
            return 0

        targets = pick_agents(agents, args.agent)
        if not targets:
            print(f"Ни один агент не матчится на '{args.agent}'. Онлайн:")
            for a in agents:
                print(f"  {fmt_agent(a)}")
            return 1

        # Create the results group BEFORE dispatching (it reads id="$" — only
        # messages published after creation are visible).
        group = await bus.new_results_group()
        job_ids = {}
        try:
            for a in targets:
                aid = a["agent_id"]
                jid = await bus.dispatch_job(aid, {
                    "type": "logs",
                    "params": {"lines": args.lines, "grep": args.grep},
                })
                job_ids[jid] = aid
                print(f"→ {aid}: logs job {jid} (lines={args.lines}"
                      f"{f' grep={args.grep}' if args.grep else ''})")

            deadline = time.time() + args.timeout
            got: dict = {}
            while time.time() < deadline and len(got) < len(job_ids):
                msgs = await bus.consume_results(group, block_ms=2000)
                for entry_id, r in msgs:
                    try:
                        await bus.ack_result(group, entry_id)
                    except Exception:
                        pass
                    if r.get("kind") != "logs" or r.get("job_id") not in job_ids:
                        continue
                    got[r["job_id"]] = r

            for jid, aid in job_ids.items():
                r = got.get(jid)
                print("\n" + "=" * 78)
                if r is None:
                    print(f"### {aid}: НЕТ ОТВЕТА за {args.timeout}s "
                          f"(агент оффлайн? старый код без logs-job?)")
                    continue
                print(f"### {aid} — {r.get('note', '')}")
                if r.get("error"):
                    print(f"ERROR: {r['error']}")
                print("=" * 78)
                print(r.get("text") or "(пусто)")
            missing = len(job_ids) - len(got)
            if missing:
                print(f"\n[!] {missing} агент(ов) не ответили.")
            return 0 if got else 2
        finally:
            try:
                await bus.destroy_results_group(group)
            except Exception:
                pass
    finally:
        await bus.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", default=None,
                    help="agent id / host / ip substring, or 'all'")
    ap.add_argument("--lines", type=int, default=300, help="tail lines (max 3000)")
    ap.add_argument("--grep", default="", help="case-insensitive regex filter")
    ap.add_argument("--timeout", type=int, default=30, help="wait seconds")
    ap.add_argument("--list", action="store_true", help="just list agents")
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
