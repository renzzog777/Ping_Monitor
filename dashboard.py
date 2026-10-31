import json
import time
from datetime import datetime
from rich.live import Live
from rich.table import Table
from rich.console import Console

from common import STATUS_RAM_FILE

console = Console()


def format_elapsed(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def generate_table() -> Table:
    table = Table(title=f"24/7 Network Monitor - {datetime.now().strftime('%H:%M:%S')}", min_width=85)
    table.add_column("System / Alias", justify="left", style="yellow", no_wrap=True)
    table.add_column("IP Address", justify="left", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Latency", justify="center")
    table.add_column("Loss", justify="center")
    table.add_column("Jitter", justify="center")
    table.add_column("Downtime", justify="center", style="magenta")
    table.add_column("Email Alert", justify="center")

    try:
        with open(STATUS_RAM_FILE, "r") as f:
            data = json.load(f)
        targets = data["targets"]
    except FileNotFoundError:
        table.add_row("Waiting for service data...", "", "", "", "", "", "", "")
        return table
    except Exception as e:
        table.add_row(f"Error reading data: {e}", "", "", "", "", "", "", "")
        return table

    now = time.time()
    if now - data["updated"] > 3 * data["ping_interval_seconds"]:
        table.caption = f"[bold yellow]Monitor service has not reported for {format_elapsed(now - data['updated'])}[/bold yellow]"
    elif not data["internet_up"]:
        table.caption = f"[bold red]LOCAL INTERNET DOWN for {format_elapsed(now - data['internet_down_since'])} - target failures are not being counted[/bold red]"
    else:
        table.caption = "[green]Local internet OK[/green]"

    for ip, info in targets.items():
        if info["status"] == "DOWN":
            status_str = "[bold red]OFFLINE[/bold red]"
        elif info["consecutive_failures"]:
            status_str = f"[bold yellow]FAILING ({info['consecutive_failures']})[/bold yellow]"
        elif info["status"] == "UP":
            status_str = "[bold green]ONLINE[/bold green]"
        else:
            status_str = "[dim]CHECKING[/dim]"

        loss_str = "--" if info["loss_pct"] is None else f"{info['loss_pct']:.0f}%"
        jitter_str = "--" if info["jitter_ms"] is None else f"{info['jitter_ms']:.1f} ms"
        downtime_str = format_elapsed(now - info["down_since"]) if info["down_since"] else "--"

        alert_str = "[yellow]SENT[/yellow]" if info["alert_sent"] else "No"
        if info["status"] == "DOWN" and not info["alert_sent"]:
            alert_str = f"Pending ({data['alert_after_seconds'] // 60}m)"

        table.add_row(info["alias"], ip, status_str, info["last_latency"], loss_str, jitter_str, downtime_str, alert_str)

    return table


if __name__ == "__main__":
    console.clear()
    with Live(generate_table(), refresh_per_second=1) as live:
        try:
            while True:
                time.sleep(1)
                live.update(generate_table())
        except KeyboardInterrupt:
            console.print("\n[bold yellow]Dashboard closed.[/bold yellow] Monitoring continues to run in the background.")
