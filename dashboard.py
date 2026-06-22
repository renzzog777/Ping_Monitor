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
    table = Table(title=f"Monitor de Red 24/7 - {datetime.now().strftime('%H:%M:%S')}", min_width=70)
    table.add_column("Dirección IP", justify="left", style="cyan", no_wrap=True)
    table.add_column("Estado", justify="center", no_wrap=True)
    table.add_column("Latencia", justify="center")
    table.add_column("Tiempo Caída", justify="center", style="magenta")
    table.add_column("Alerta Email", justify="center")

    try:
        with open(STATUS_RAM_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        table.add_row("Esperando datos del servicio...", "", "", "", "")
        return table
    except Exception as e:
        table.add_row(f"Error leyendo datos: {e}", "", "", "", "")
        return table

    for ip, info in data.items():
        # Formatear Estado
        if info["status"] == "UP":
            status_xml = "[bold green]ONLINE[/bold green]"
        else:
            status_xml = "[bold red]OFFLINE[/bold red]"

        # Calcular tiempo de caída si aplica
        downtime_str = "--"
        if info["down_since"]:
            elapsed = time.time() - info["down_since"]
            # Convertir segundos a formato mm:ss o hh:mm:ss
            m, s = divmod(int(elapsed), 60)
            h, m = divmod(m, 60)
            downtime_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

        # Alerta enviada
        alert_str = "[yellow]ENVIADA[/yellow]" if info["alert_sent"] else "No"
        if info["status"] == "DOWN" and not info["alert_sent"]:
            alert_str = "Pendiente (5m)"

        table.add_row(ip, status_xml, info["last_latency"], downtime_str, alert_str)

    return table

if __name__ == "__main__":
    console.clear()
    with Live(generate_table(), refresh_per_second=1) as live:
        try:
            while True:
                time.sleep(1)
                live.update(generate_table())
        except KeyboardInterrupt:
            console.print("\n[bold yellow]Dashboard cerrado.[/bold yellow] El monitoreo sigue corriendo de fondo.")
