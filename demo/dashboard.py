#!/usr/bin/env python3
"""
Real-time terminal dashboard for the Whisper Network demo.

Connects to all 6 nodes' debug APIs and renders a live table showing:
  - Node status (ALIVE / SUSPECTED / DEAD)
  - Task ledger (aggregated across all nodes — they converge via gossip)
  - Event log

Usage:
    python -m demo.dashboard
    python -m demo.dashboard --ports 8888,8889,8890,8891,8892,8893
"""
import argparse
import time
from typing import Optional

import requests
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

STATUS_STYLE = {
    "alive":     ("● ALIVE",     "bold green"),
    "suspected": ("? SUSPECTED", "bold yellow"),
    "dead":      ("✕ DEAD",      "bold red"),
    None:        ("— OFFLINE",   "dim"),
}


def fetch(port: int) -> Optional[dict]:
    try:
        r = requests.get(f"http://localhost:{port}/state", timeout=1.0)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def node_table(states: list[Optional[dict]], node_names: list[str]) -> Table:
    t = Table(title="Node Status", box=box.SIMPLE_HEAD, expand=True, show_lines=False)
    t.add_column("Node",         style="bold cyan", no_wrap=True)
    t.add_column("Key (short)",  style="dim")
    t.add_column("Shard",        justify="center")
    t.add_column("Status",       no_wrap=True)
    t.add_column("AXL mesh",     justify="center", style="magenta")
    t.add_column("Tasks held",   style="yellow")
    t.add_column("Known peers")

    for name, state in zip(node_names, states):
        if state is None:
            t.add_row(name, "?", "?",
                      Text("— OFFLINE", style="dim"), "—", "—", "—")
            continue

        key_short  = state.get("key_short", "?")
        shard_id   = str(state.get("shard_id", "?"))
        peers      = state.get("peers", {})
        axl        = state.get("axl_mesh", {})
        axl_up     = axl.get("up_peers", "?")
        axl_total  = axl.get("total_peers", "?")
        axl_str    = f"{axl_up}/{axl_total} up"

        my_tasks = [
            tid[:10]
            for tid, task in state.get("tasks", {}).items()
            if task.get("leased_by") == key_short
            and task.get("status") == "in_progress"
        ]

        alive    = sum(1 for p in peers.values() if p["status"] == "alive")
        dead     = sum(1 for p in peers.values() if p["status"] == "dead")
        peer_str = f"{alive} alive" + (f", {dead} dead" if dead else "")

        recovered = state.get("recovered_tasks", 0)
        name_cell = Text(name)
        if recovered:
            name_cell.append(f" [↺{recovered}]", style="bold cyan")

        t.add_row(
            name_cell,
            key_short,
            shard_id,
            Text("● ALIVE", style="bold green"),
            axl_str,
            ", ".join(my_tasks) if my_tasks else "—",
            peer_str,
        )

    return t


def peer_status_table(states: list[Optional[dict]], node_names: list[str]) -> Table:
    """Per-node view of what each node thinks about peers (failure detection)."""
    t = Table(title="Peer View (from node-1)", box=box.SIMPLE_HEAD, expand=True)
    t.add_column("Peer",   style="bold")
    t.add_column("Status", no_wrap=True)
    t.add_column("Last heartbeat")

    # Use state from node-1 (index 0) as the representative view
    representative = next((s for s in states if s), None)
    if not representative:
        t.add_row("(no data)", "—", "—")
        return t

    for short_key, info in representative.get("peers", {}).items():
        status  = info.get("status")
        label, style = STATUS_STYLE.get(status, STATUS_STYLE[None])
        last_seen = info.get("last_seen", 0)
        ago = time.time() - last_seen if last_seen else 0
        t.add_row(
            short_key,
            Text(label, style=style),
            f"{ago:.1f}s ago" if last_seen else "—",
        )

    return t


def task_table(states: list[Optional[dict]]) -> Table:
    # Merge task views from all nodes — completed status wins
    merged: dict[str, dict] = {}
    for state in states:
        if not state:
            continue
        for tid, task in state.get("tasks", {}).items():
            existing = merged.get(tid)
            if existing is None:
                merged[tid] = task
            elif task.get("status") == "completed":
                merged[tid] = task
            elif (task.get("version", 0) or 0) > (existing.get("version", 0) or 0):
                merged[tid] = task

    t = Table(title="Task Ledger (merged)", box=box.SIMPLE_HEAD, expand=True)
    t.add_column("Task ID",     style="dim", no_wrap=True)
    t.add_column("Shard",       justify="center")
    t.add_column("Status",      no_wrap=True)
    t.add_column("Leased by",   style="cyan")
    t.add_column("Expires in",  justify="right")
    t.add_column("Result")

    for tid, task in sorted(merged.items(), key=lambda x: x[1].get("shard_id", 0)):
        status = task.get("status", "?")
        if status == "completed":
            status_text = Text("✓ completed",   style="bold green")
        elif status == "in_progress":
            status_text = Text("⟳ in_progress", style="bold yellow")
        else:
            status_text = Text("○ pending",     style="dim")

        exp    = task.get("lease_expires_in", 0)
        exp_s  = f"{exp:.0f}s" if status == "in_progress" else "—"

        result = task.get("result") or "—"
        if len(result) > 50:
            result = result[:47] + "..."

        t.add_row(
            tid[:14],
            str(task.get("shard_id", "?")),
            status_text,
            task.get("leased_by") or "—",
            exp_s,
            result,
        )

    if not merged:
        t.add_row("(no tasks yet)", "—", Text("—", style="dim"), "—", "—", "—")

    return t


def events_panel(states: list[Optional[dict]]) -> Panel:
    all_events: set[str] = set()
    for state in states:
        if state:
            all_events.update(state.get("events", []))
    lines = sorted(all_events, reverse=True)[:18]
    body  = "\n".join(lines) if lines else "(waiting for events...)"
    return Panel(body, title="[bold]Event Log[/bold]", border_style="blue", padding=(0, 1))


def build_renderable(states, node_names):
    from rich.console import Group
    return Group(
        node_table(states, node_names),
        "",
        peer_status_table(states, node_names),
        "",
        task_table(states),
        "",
        events_panel(states),
    )


def main():
    parser = argparse.ArgumentParser(description="Whisper Network Dashboard")
    parser.add_argument(
        "--ports",
        default="8888,8889,8890,8891,8892,8893",
        help="Comma-separated debug API ports for each node",
    )
    parser.add_argument("--refresh", type=float, default=1.0,
                        help="Refresh interval in seconds")
    args   = parser.parse_args()
    ports  = [int(p) for p in args.ports.split(",")]
    names  = [f"node-{i+1}" for i in range(len(ports))]

    console.print("[bold magenta]Whisper Network[/bold magenta] — live dashboard")
    console.print(f"Connecting to {len(ports)} nodes on ports {args.ports}...\n")

    with Live(console=console, refresh_per_second=int(1 / args.refresh) + 1,
              screen=True) as live:
        while True:
            states = [fetch(p) for p in ports]
            live.update(build_renderable(states, names))
            time.sleep(args.refresh)


if __name__ == "__main__":
    main()
