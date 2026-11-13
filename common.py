import os
import json
import sqlite3

# Shared paths, settings and history database helpers for monitor.py, dashboard.py and web.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DB_FILE = os.path.join(BASE_DIR, "ping_history.db")
LOG_FILE = os.path.join(BASE_DIR, "ping_monitor.log")
STATUS_RAM_FILE = "/dev/shm/ping_status.json"

# Any key missing from config.json falls back to these values
DEFAULTS = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_timeout_seconds": 20,
    "email_sender": "",
    "email_password": "",
    "email_receiver": "",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "ping_interval_seconds": 15,
    "ping_count": 3,
    "ping_timeout_seconds": 2,
    "fail_threshold": 2,
    "alert_after_seconds": 300,
    "reminder_interval_seconds": 1800,
    "reference_hosts": ["1.1.1.1", "8.8.8.8", "9.9.9.9"],
    "history_flush_seconds": 300,
    "history_retention_days": 90,
    "daily_summary_time": "08:00",
    "web_host": "0.0.0.0",
    "web_port": 5000,
    "web_username": "admin",
    "web_password": "",
    "tunnel_metrics_url": "http://127.0.0.1:20241/quicktunnel",
    "other_tunnels": {},
    "ips_to_monitor": {},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (ts REAL, ip TEXT, sent INTEGER, received INTEGER, avg_ms REAL, jitter_ms REAL);
CREATE INDEX IF NOT EXISTS idx_samples_ip_ts ON samples (ip, ts);
CREATE TABLE IF NOT EXISTS outages (ip TEXT, alias TEXT, start REAL, end REAL, alerted INTEGER);
CREATE INDEX IF NOT EXISTS idx_outages_end ON outages (end);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

LOCAL_OUTAGE_IP = "local"


def load_config():
    with open(CONFIG_FILE, "r") as f:
        config = {**DEFAULTS, **json.load(f)}
    # Older configs listed plain IPs without aliases
    if isinstance(config["ips_to_monitor"], list):
        config["ips_to_monitor"] = {ip: ip for ip in config["ips_to_monitor"]}
    return config


def connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.executescript(SCHEMA)
    return conn


def target_stats(conn, ip, since):
    checks, ok, sent, received, avg_ms, jitter_ms = conn.execute(
        "SELECT COUNT(*), SUM(received > 0), SUM(sent), SUM(received), AVG(avg_ms), AVG(jitter_ms) "
        "FROM samples WHERE ip = ? AND ts >= ?",
        (ip, since),
    ).fetchone()
    if not checks:
        return None
    return {
        "checks": checks,
        "availability": 100.0 * ok / checks,
        "loss_pct": 100.0 * (sent - received) / sent if sent else 0.0,
        "avg_ms": avg_ms,
        "jitter_ms": jitter_ms,
    }


def outages_since(conn, since, limit=None):
    query = "SELECT ip, alias, start, end, alerted FROM outages WHERE end >= ? ORDER BY start DESC"
    params = [since]
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    return [
        {"ip": ip, "alias": alias, "start": start, "end": end, "alerted": bool(alerted)}
        for ip, alias, start, end, alerted in conn.execute(query, params)
    ]


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
