import sys
import time
from typing import Dict, List, Optional
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich.console import Console
from rich.panel import Panel
from rich import box


class ConsoleDashboard:
    def __init__(self):
        self.workers: Dict[int, dict] = {}
        self._pool_status: str = "⏳ Initializing…"
        from config import WORKER_POOL
        for i in range(len(WORKER_POOL)):
            self.workers[i] = {
                "phone": "—",
                "status": "Waiting…",
                "stt": "",
            }
        self.console = Console()
        self.live: Optional[Live] = None

    def start(self):
        if not self.live:
            self.live = Live(
                self._generate_table(),
                console=self.console,
                refresh_per_second=4,
            )
            self.live.start()

    def stop(self):
        if self.live:
            self.live.stop()
            self.live = None

    def set_pool_status(self, status: str):
        """Update the overall pool status shown in the dashboard header."""
        self._pool_status = status
        if self.live:
            self.live.update(self._generate_table())

    def update_worker(
        self,
        worker_id: int,
        phone: str = None,
        status: str = None,
        stt: str = None,
    ):
        if worker_id not in self.workers:
            return
        if phone is not None:
            self.workers[worker_id]["phone"] = phone
        if status is not None:
            self.workers[worker_id]["status"] = status
        if stt is not None:
            self.workers[worker_id]["stt"] = stt
        if self.live:
            self.live.update(self._generate_table())

    def _generate_table(self) -> Panel:
        table = Table(
            show_header=True,
            header_style="bold cyan",
            expand=True,
            box=box.SIMPLE_HEAVY,
        )
        table.add_column("Worker", style="dim", width=10)
        table.add_column("GV Number", width=16)
        table.add_column("Status", width=22)
        table.add_column("Live STT / Info", width=52)

        for i in sorted(self.workers):
            w = self.workers[i]
            status_val = w["status"]
            # Colour-code the status cell
            if "Ready" in status_val or "✅" in status_val:
                status_text = Text(status_val, style="bold green")
            elif "Failed" in status_val or "❌" in status_val:
                status_text = Text(status_val, style="bold red")
            elif "Init" in status_val or "⏳" in status_val:
                status_text = Text(status_val, style="bold yellow")
            else:
                status_text = Text(status_val)

            table.add_row(
                f"Worker {i + 1}",
                w["phone"],
                status_text,
                w["stt"],
            )

        return Panel(
            table,
            title=f"[bold cyan]AI Caller[/bold cyan]  —  {self._pool_status}",
            border_style="cyan",
        )


dashboard = ConsoleDashboard()


class ControllerDashboard:
    """Live panel for the CONTROLLER node: bot/Redis status + connected agents."""

    def __init__(self):
        self._bot: str = "—"
        self._redis_ok: Optional[bool] = None
        self._agents: List[dict] = []
        self._started_at: float = time.time()
        self.console = Console()
        self.live: Optional[Live] = None

    def start(self):
        if not self.live:
            self.live = Live(
                self._generate(),
                console=self.console,
                refresh_per_second=2,
            )
            self.live.start()

    def stop(self):
        if self.live:
            self.live.stop()
            self.live = None

    def set_header(self, bot_username: str = None, redis_ok: bool = None):
        if bot_username is not None:
            self._bot = bot_username
        if redis_ok is not None:
            self._redis_ok = redis_ok
        if self.live:
            self.live.update(self._generate())

    def update_agents(self, agents: List[dict]):
        self._agents = agents or []
        if self.live:
            self.live.update(self._generate())

    @staticmethod
    def _fmt_uptime(seconds: float) -> str:
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m {sec}s"
        return f"{sec}s"

    def _generate(self) -> Panel:
        now = time.time()
        table = Table(
            show_header=True,
            header_style="bold cyan",
            expand=True,
            box=box.SIMPLE_HEAVY,
        )
        table.add_column("Server", style="bold", width=22)
        table.add_column("Role", width=10)
        table.add_column("State", width=14)
        table.add_column("Workers", width=10, justify="center")
        table.add_column("Busy", width=6, justify="center")
        table.add_column("Numbers", width=8, justify="center")
        table.add_column("Heartbeat", width=12)

        if not self._agents:
            table.add_row(
                "— no agents connected —", "", "", "", "", "", ""
            )
        else:
            for a in sorted(self._agents, key=lambda x: x.get("agent_id", "")):
                state = str(a.get("state", "?"))
                if state in ("ready",):
                    st = Text(state, style="bold green")
                elif state in ("initializing", "not_started", "selftest"):
                    st = Text(state, style="bold yellow")
                elif state in ("degraded",):
                    st = Text(state, style="yellow")
                else:
                    st = Text(state, style="bold red")

                ts = a.get("ts")
                if ts:
                    age = now - float(ts)
                    hb = f"{int(age)}s ago"
                    hb_text = Text(
                        hb, style="green" if age < 30 else "bold red"
                    )
                else:
                    hb_text = Text("—", style="dim")

                ready = a.get("ready_workers", 0)
                total = a.get("total_workers", 0)
                table.add_row(
                    str(a.get("agent_id", "?")),
                    str(a.get("role", "agent")),
                    st,
                    f"{ready}/{total}",
                    str(a.get("checked_out_workers", 0)),
                    str(a.get("numbers_fetched_workers", 0)),
                    hb_text,
                )

        ready_agents = sum(
            1 for a in self._agents if a.get("state") == "ready"
        )
        total_ready_workers = sum(
            a.get("ready_workers", 0) for a in self._agents
        )
        total_workers = sum(
            a.get("total_workers", 0) for a in self._agents
        )

        if self._redis_ok is True:
            redis_txt = "[green]connected[/green]"
        elif self._redis_ok is False:
            redis_txt = "[bold red]DOWN[/bold red]"
        else:
            redis_txt = "[yellow]?[/yellow]"

        subtitle = (
            f"Bot: [cyan]@{self._bot}[/cyan]  •  "
            f"Redis: {redis_txt}  •  "
            f"Agents: [bold]{len(self._agents)}[/bold] "
            f"(ready {ready_agents})  •  "
            f"Workers: [bold]{total_ready_workers}/{total_workers}[/bold]  •  "
            f"Uptime: {self._fmt_uptime(now - self._started_at)}"
        )

        return Panel(
            table,
            title="[bold cyan]AI Caller — Controller[/bold cyan]",
            subtitle=subtitle,
            border_style="cyan",
        )


controller_dashboard = ControllerDashboard()
