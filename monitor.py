import os
import re
import sys
import json
import time
import queue
import signal
import logging
import smtplib
import threading
import subprocess
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor

from common import (
    CONFIG_FILE, LOG_FILE, STATUS_RAM_FILE, LOCAL_OUTAGE_IP,
    load_config, connect, target_stats, outages_since, get_meta, set_meta,
)

# Rotational Logs (Max 15MB total)
RECENT_SAMPLES = 240  # Last ~hour of checks kept in RAM for the dashboards

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3)]
)

SUMMARY_RE = re.compile(r"(\d+) packets transmitted, (\d+) received")
RTT_RE = re.compile(r"= [\d.]+/([\d.]+)/[\d.]+/([\d.]+) ms")

config = None
config_mtime = None
ip_states = {}
recent = {}
internet = {"down_since": None}

# Checks are buffered in RAM and written to the SD card in batches to limit wear
sample_buffer = []
outage_buffer = []
last_flush = time.time()
last_prune_day = None
last_summary_date = None
last_tunnel_check = 0
tunnel_url = None


def format_duration(seconds):
    return str(timedelta(seconds=int(seconds)))


def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ---------------------------------------------------------------- Notifications

def send_email(subject, body):
    if not (config["email_sender"] and config["email_password"] and config["email_receiver"]):
        return None
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config["email_sender"]
    msg["To"] = config["email_receiver"]
    with smtplib.SMTP(config["smtp_server"], config["smtp_port"], timeout=config["smtp_timeout_seconds"]) as server:
        server.starttls()
        server.login(config["email_sender"], config["email_password"])
        server.sendmail(config["email_sender"], config["email_receiver"], msg.as_string())
    return True


def send_telegram(subject, body):
    if not (config["telegram_bot_token"] and config["telegram_chat_id"]):
        return None
    data = urllib.parse.urlencode({"chat_id": config["telegram_chat_id"], "text": f"{subject}\n\n{body}"}).encode()
    url = f"https://api.telegram.org/bot{config['telegram_bot_token']}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=config["smtp_timeout_seconds"]) as response:
        json.load(response)
    return True


CHANNELS = {"email": send_email, "telegram": send_telegram}


class Notifier:
    """Delivers alerts from a background thread so a slow or unreachable server never stalls the ping loop."""
    RETRY_DELAYS = [60, 120, 300, 600, 1200, 1800]

    def __init__(self):
        self.queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, subject, body):
        email_ready = config["email_sender"] and config["email_password"] and config["email_receiver"]
        telegram_ready = config["telegram_bot_token"] and config["telegram_chat_id"]
        if not (email_ready or telegram_ready):
            logging.warning(f"No email or Telegram settings configured, alert not sent: {subject}")
            return
        self.queue.put((subject, body))

    def _run(self):
        while True:
            subject, body = self.queue.get()
            pending = list(CHANNELS)
            for delay in [0] + self.RETRY_DELAYS:
                time.sleep(delay)
                pending = [name for name in pending if not self._deliver(name, subject, body)]
                if not pending:
                    break
            else:
                logging.error(f"Giving up on alert after {len(self.RETRY_DELAYS)} retries via {', '.join(pending)}: {subject}")

    def _deliver(self, name, subject, body):
        try:
            sent = CHANNELS[name](subject, body)
        except Exception as e:
            logging.error(f"Error sending {name} alert '{subject}': {e}")
            return False
        if sent:
            logging.info(f"{name.capitalize()} alert sent: {subject}")
        return True  # A channel that is not configured counts as done


notifier = Notifier()


def send_target_alert(ip, state, status, duration=None, is_reminder=False):
    alias = state["alias"]
    if is_reminder:
        subject = f"[NETWORK ALERT] [REMINDER] {alias} ({ip}) IS STILL DOWN"
        body = f"System: {alias}\nIP Address: {ip}\nStatus: Continues to be unresponsive.\nTotal downtime so far: {duration}.\nLast check: {now_str()}"
    elif status == "DOWN":
        subject = f"[NETWORK ALERT] {alias} ({ip}) is DOWN"
        body = f"System: {alias}\nIP Address: {ip}\nStatus: Stopped responding for more than {format_duration(config['alert_after_seconds'])}.\nDown since: {datetime.fromtimestamp(state['down_since']).strftime('%Y-%m-%d %H:%M:%S')}"
    else:
        subject = f"[NETWORK ALERT] {alias} ({ip}) has RECOVERED"
        body = f"System: {alias}\nIP Address: {ip}\nStatus: Successfully restored.\nOffline duration: {duration}.\nRecovery time: {now_str()}\nCurrent latency: {state['last_latency']}"
    notifier.send(subject, body)


# ---------------------------------------------------------------- Configuration

def new_state(alias):
    return {
        "alias": alias,
        "status": "UNKNOWN",
        "consecutive_failures": 0,
        "first_failure": None,
        "down_since": None,
        "alert_sent": False,
        "last_alert_time": None,
        "last_latency": "N/A",
        "avg_ms": None,
        "loss_pct": None,
        "jitter_ms": None,
        "last_check": None,
    }


def reload_config_if_changed():
    global config, config_mtime
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
    except OSError as e:
        if config is None:
            logging.error(f"Error loading config.json: {e}")
            sys.exit(1)
        return
    if mtime == config_mtime:
        return

    try:
        new_config = load_config()
    except Exception as e:
        if config is None:
            logging.error(f"Error loading config.json: {e}")
            sys.exit(1)
        logging.error(f"config.json changed but could not be loaded, keeping previous settings: {e}")
        config_mtime = mtime
        return

    reloaded = config is not None
    config, config_mtime = new_config, mtime

    targets = config["ips_to_monitor"]
    for ip in list(ip_states):
        if ip not in targets:
            logging.info(f"Stopped monitoring {ip_states[ip]['alias']} ({ip}).")
            del ip_states[ip]
            del recent[ip]
    for ip, alias in targets.items():
        if ip in ip_states:
            ip_states[ip]["alias"] = alias
        else:
            ip_states[ip] = new_state(alias)
            recent[ip] = deque(maxlen=RECENT_SAMPLES)
            logging.info(f"Monitoring {alias} ({ip}).")
    if reloaded:
        logging.info("config.json reloaded.")


# ---------------------------------------------------------------- Checks

def ping_address(ip, count):
    timeout = config["ping_timeout_seconds"]
    try:
        output = subprocess.run(
            ["ping", "-n", "-q", "-c", str(count), "-i", "0.2", "-W", str(timeout), ip],
            capture_output=True, text=True, timeout=count * 0.2 + timeout + 5
        )
    except Exception:
        return ip, count, 0, None, None

    summary = SUMMARY_RE.search(output.stdout)
    if not summary:
        return ip, count, 0, None, None
    rtt = RTT_RE.search(output.stdout)
    avg_ms, jitter_ms = (float(rtt.group(1)), float(rtt.group(2))) if rtt else (None, None)
    return ip, int(summary.group(1)), int(summary.group(2)), avg_ms, jitter_ms


def update_internet_state(internet_up, now):
    down_since = internet["down_since"]
    if not internet_up and down_since is None:
        internet["down_since"] = now
        logging.warning("[LOCAL OUTAGE] No reference host responds; this Pi's own internet connection appears to be down.")
    elif internet_up and down_since is not None:
        internet["down_since"] = None
        duration = format_duration(now - down_since)
        logging.info(f"[LOCAL RECOVERED] Internet connection is back after {duration}.")
        outage_buffer.append((LOCAL_OUTAGE_IP, "Local internet", down_since, now, 0))
        if now - down_since >= config["alert_after_seconds"]:
            notifier.send(
                f"[NETWORK ALERT] Local internet connection was down for {duration}",
                f"The monitor's own internet connection was down from "
                f"{datetime.fromtimestamp(down_since).strftime('%Y-%m-%d %H:%M:%S')} to {now_str()} ({duration}).\n"
                f"Target failures during that time were not counted as outages."
            )


def update_target(ip, sent, received, avg_ms, jitter_ms, internet_up, now):
    state = ip_states[ip]
    alias = state["alias"]
    is_up = received > 0

    # When the Pi itself is offline, a failed ping says nothing about the target
    if not is_up and not internet_up:
        return

    state["last_check"] = now
    state["avg_ms"] = avg_ms
    state["jitter_ms"] = jitter_ms
    state["loss_pct"] = round(100.0 * (sent - received) / sent, 1) if sent else 100.0
    state["last_latency"] = f"{avg_ms:.1f} ms" if avg_ms is not None else "Timeout"
    sample_buffer.append((now, ip, sent, received, avg_ms, jitter_ms))
    recent[ip].append([now, avg_ms, sent, received, jitter_ms])

    if is_up:
        if state["status"] == "DOWN":
            duration = format_duration(now - state["down_since"])
            logging.info(f"[RECOVERED] {alias} ({ip}) is back online after {duration}.")
            outage_buffer.append((ip, alias, state["down_since"], now, int(state["alert_sent"])))
            if state["alert_sent"]:
                send_target_alert(ip, state, "UP", duration=duration)
        state.update(status="UP", consecutive_failures=0, first_failure=None,
                     down_since=None, alert_sent=False, last_alert_time=None)
        return

    state["consecutive_failures"] += 1
    if state["consecutive_failures"] == 1:
        state["first_failure"] = now

    if state["status"] != "DOWN":
        # A single lost check is usually noise; only declare DOWN after several in a row
        if state["consecutive_failures"] >= config["fail_threshold"]:
            state["status"] = "DOWN"
            state["down_since"] = state["first_failure"]
            logging.warning(f"[DOWN] {alias} ({ip}) stopped responding ({state['consecutive_failures']} failed checks in a row).")
        return

    if not state["alert_sent"]:
        if now - state["down_since"] >= config["alert_after_seconds"]:
            state["alert_sent"] = True
            state["last_alert_time"] = now
            send_target_alert(ip, state, "DOWN")
    elif now - state["last_alert_time"] >= config["reminder_interval_seconds"]:
        state["last_alert_time"] = now
        send_target_alert(ip, state, "DOWN", duration=format_duration(now - state["down_since"]), is_reminder=True)


def check_pool():
    targets = list(ip_states)
    refs = config["reference_hosts"]
    with ThreadPoolExecutor(max_workers=max(1, len(targets) + len(refs))) as executor:
        target_futures = [executor.submit(ping_address, ip, config["ping_count"]) for ip in targets]
        ref_futures = [executor.submit(ping_address, host, 1) for host in refs]
        results = [f.result() for f in target_futures]
        internet_up = not refs or any(f.result()[2] > 0 for f in ref_futures)

    now = time.time()
    update_internet_state(internet_up, now)
    for ip, sent, received, avg_ms, jitter_ms in results:
        update_target(ip, sent, received, avg_ms, jitter_ms, internet_up, now)
    write_status(now)


def write_status(now):
    payload = {
        "updated": now,
        "internet_up": internet["down_since"] is None,
        "internet_down_since": internet["down_since"],
        "ping_interval_seconds": config["ping_interval_seconds"],
        "alert_after_seconds": config["alert_after_seconds"],
        "public_url": tunnel_url,
        "targets": {ip: {**state, "recent": list(recent[ip])} for ip, state in ip_states.items()},
    }
    try:
        tmp_file = STATUS_RAM_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(payload, f)
        os.replace(tmp_file, STATUS_RAM_FILE)  # Readers never see a half-written file
    except Exception as e:
        logging.error(f"Error writing status payload to RAM: {e}")


# ---------------------------------------------------------------- History and daily summary

def flush_history():
    global last_flush
    last_flush = time.time()
    if not sample_buffer and not outage_buffer:
        return
    try:
        conn = connect()
        with conn:
            conn.executemany("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)", sample_buffer)
            conn.executemany("INSERT INTO outages VALUES (?, ?, ?, ?, ?)", outage_buffer)
        conn.close()
        sample_buffer.clear()
        outage_buffer.clear()
    except Exception as e:
        logging.error(f"Error saving history (will retry): {e}")


def prune_history(now):
    global last_prune_day
    today = datetime.fromtimestamp(now).date()
    if last_prune_day == today:
        return
    last_prune_day = today
    cutoff = now - config["history_retention_days"] * 86400
    try:
        conn = connect()
        with conn:
            conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM outages WHERE end < ?", (cutoff,))
        conn.close()
    except Exception as e:
        logging.error(f"Error pruning history: {e}")


def build_daily_summary(conn, now):
    since = now - 86400
    outages = outages_since(conn, since)
    lines = [f"Network report for the last 24 hours (until {now_str()})", ""]

    for ip, state in ip_states.items():
        lines.append(f"{state['alias']} ({ip})")
        stats = target_stats(conn, ip, since)
        if stats:
            lines.append(f"  Availability: {stats['availability']:.2f}%   Packet loss: {stats['loss_pct']:.2f}%")
            avg = f"{stats['avg_ms']:.1f} ms" if stats["avg_ms"] is not None else "N/A"
            jitter = f"{stats['jitter_ms']:.1f} ms" if stats["jitter_ms"] is not None else "N/A"
            lines.append(f"  Avg latency: {avg}   Jitter: {jitter}")
        else:
            lines.append("  No data recorded.")
        spans = [(o["start"], o["end"]) for o in outages if o["ip"] == ip]
        if state["status"] == "DOWN":
            spans.append((state["down_since"], now))
        total = sum(min(end, now) - max(start, since) for start, end in spans)
        outage_text = f"{len(spans)} (total {format_duration(total)})" if spans else "none"
        lines.append(f"  Outages: {outage_text}{'  - CURRENTLY DOWN' if state['status'] == 'DOWN' else ''}")
        lines.append("")

    local = [o for o in outages if o["ip"] == LOCAL_OUTAGE_IP]
    if local:
        total = sum(min(o["end"], now) - max(o["start"], since) for o in local)
        lines.append(f"Local internet outages: {len(local)} (total {format_duration(total)})")
    else:
        lines.append("Local internet outages: none")
    return "\n".join(lines)


def maybe_send_daily_summary(now):
    global last_summary_date
    summary_time = config["daily_summary_time"]
    if not summary_time:
        return
    try:
        hour, minute = (int(part) for part in summary_time.split(":"))
    except ValueError:
        return
    current = datetime.fromtimestamp(now)
    today = current.strftime("%Y-%m-%d")
    if (current.hour, current.minute) < (hour, minute) or last_summary_date == today:
        return

    last_summary_date = today
    flush_history()
    try:
        conn = connect()
        with conn:
            set_meta(conn, "last_summary_date", today)
        body = build_daily_summary(conn, now)
        conn.close()
    except Exception as e:
        logging.error(f"Error building daily summary: {e}")
        return
    notifier.send(f"[NETWORK REPORT] Daily summary {today}", body)


# ---------------------------------------------------------------- Public dashboard address

def check_tunnel_url(now):
    """Emails the dashboard's Cloudflare quick tunnel address whenever it changes (it is new after every restart)."""
    global last_tunnel_check, tunnel_url
    metrics_url = config["tunnel_metrics_url"]
    if not metrics_url or now - last_tunnel_check < 60:
        return
    last_tunnel_check = now
    try:
        with urllib.request.urlopen(metrics_url, timeout=3) as response:
            hostname = json.load(response).get("hostname")
    except Exception:
        return  # Tunnel not running (yet)
    if not hostname or f"https://{hostname}" == tunnel_url:
        return

    tunnel_url = f"https://{hostname}"
    logging.info(f"Public dashboard address: {tunnel_url}")
    try:
        conn = connect()
        with conn:
            set_meta(conn, "tunnel_url", tunnel_url)
        conn.close()
    except Exception as e:
        logging.error(f"Error saving public dashboard address: {e}")
    notifier.send(
        "[PING MONITOR] New public dashboard address",
        f"The Ping Monitor dashboard is now available at:\n\n{tunnel_url}\n\n"
        f"Log in with your dashboard username and password.\n"
        f"This address changes whenever the tunnel or the Pi restarts; you will get a new email each time.\n"
        f"Changed at: {now_str()}"
    )


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    reload_config_if_changed()
    logging.info("Starting Ping Monitoring Core...")
    try:
        conn = connect()
        last_summary_date = get_meta(conn, "last_summary_date")
        if last_summary_date is None:
            # First run: start with tomorrow's report instead of one with no data
            last_summary_date = datetime.now().strftime("%Y-%m-%d")
            with conn:
                set_meta(conn, "last_summary_date", last_summary_date)
        tunnel_url = get_meta(conn, "tunnel_url")
        conn.close()
    except Exception as e:
        logging.error(f"Error opening history database: {e}")

    try:
        while True:
            started = time.time()
            reload_config_if_changed()
            check_pool()
            if started - last_flush >= config["history_flush_seconds"]:
                flush_history()
            prune_history(started)
            maybe_send_daily_summary(started)
            check_tunnel_url(started)
            time.sleep(max(0, config["ping_interval_seconds"] - (time.time() - started)))
    finally:
        flush_history()
        logging.info("Ping Monitoring Core stopped.")
