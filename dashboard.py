import os
import json
import time
from datetime import datetime
from rich.live import Live
from rich.table import Table
from rich.console import Console

STATUS_RAM_FILE = "/dev/shm/ping_status.json"
console = Console()

def generate_table() -> Table:
    table = Table(title=f"24/7 Network Monitor - {datetime.now().strftime('%H:%M:%S')}", min_width=85)
    table.add_column("System / Alias", justify="left", style="yellow", no_wrap=True)
    table.add_column("IP Address", justify="left", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Latency", justify="center")
    table.add_column("Downtime", justify="center", style="magenta")
    table.add_column("Email Alert", justify="center")

    try:
        with open(STATUS_RAM_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        table.add_row("Waiting for service data...", "", "", "", "", "")
        return table
    except Exception as e:
        table.add_row(f"Error reading data: {e}", "", "", "", "", "")
        return table

    for ip, info in data.items():
        alias_str = info.get("alias", "Unknown Device")
        
        if info["status"] == "UP":
            status_xml = "[bold green]ONLINE[/bold green]"
        else:
            status_xml = "[bold red]OFFLINE[/bold red]"

        downtime_str = "--"
        if info["down_since"]:
            elapsed = time.time() - info["down_since"]
            m, s = divmod(int(elapsed), 60)
            h, m = divmod(m, 60)
            downtime_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

        alert_str = "[yellow]SENT[/yellow]" if info["alert_sent"] else "No"
        if info["status"] == "DOWN" and not info["alert_sent"]:
            alert_str = "Pending (5m)"

        table.add_row(alias_str, ip, status_xml, info["last_latency"], downtime_str, alert_str)

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
