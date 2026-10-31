import io
import os
import re
import csv
import hmac
import json
import time
from contextlib import closing
from datetime import datetime

from flask import Flask, Response, jsonify, request

from common import (
    CONFIG_FILE, LOG_FILE, STATUS_RAM_FILE, LOCAL_OUTAGE_IP,
    load_config, connect, target_stats, outages_since,
)

# Read-only web dashboard: live status comes from RAM, history from the SQLite database
app = Flask(__name__)

WINDOWS = [("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400)]
CHART_BUCKETS = 150

auth_cache = {"mtime": None, "credentials": None}


def credentials():
    # Re-read config.json only when it changes, so a new password applies without a restart
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
        if mtime != auth_cache["mtime"]:
            config = load_config()
            auth_cache.update(mtime=mtime, credentials=(config["web_username"], config["web_password"]))
    except Exception:
        pass  # Keep the last valid credentials
    return auth_cache["credentials"]


@app.before_request
def require_login():
    creds = credentials()
    if creds is None:
        return Response("Configuration error", 503)
    username, password = creds
    if not password:
        return None
    auth = request.authorization
    if (auth and hmac.compare_digest((auth.username or "").encode(), username.encode())
            and hmac.compare_digest((auth.password or "").encode(), password.encode())):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Ping Monitor"'})


def read_status():
    try:
        with open(STATUS_RAM_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None


def current_targets(status):
    if status:
        return {ip: info["alias"] for ip, info in status["targets"].items()}
    return load_config()["ips_to_monitor"]


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.get("/api/status")
def api_status():
    status = read_status()
    if status:
        for info in status["targets"].values():
            info.pop("recent", None)
    return jsonify(status)


@app.get("/api/summary")
def api_summary():
    status = read_status()
    now = time.time()
    with closing(connect()) as conn:
        targets = {
            ip: {"alias": alias, "windows": {label: target_stats(conn, ip, now - seconds) for label, seconds in WINDOWS}}
            for ip, alias in current_targets(status).items()
        }
        outages = outages_since(conn, now - WINDOWS[-1][1], limit=100)

    # Outages still in progress are not in the database yet
    if status:
        for ip, info in status["targets"].items():
            if info["status"] == "DOWN":
                outages.insert(0, {"ip": ip, "alias": info["alias"], "start": info["down_since"], "end": None, "alerted": info["alert_sent"]})
        if not status["internet_up"]:
            outages.insert(0, {"ip": LOCAL_OUTAGE_IP, "alias": "Local internet", "start": status["internet_down_since"], "end": None, "alerted": False})
    return jsonify(targets=targets, outages=outages)


@app.get("/api/history")
def api_history():
    ip = request.args.get("ip", "")
    hours = min(max(float(request.args.get("hours", 24)), 1), 24 * 90)
    until = time.time()
    since = until - hours * 3600
    bucket = hours * 3600 / CHART_BUCKETS

    # bucket -> [latency sum, latency count, sent, received, checks, successful checks]
    buckets = {}
    last_saved = since
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT CAST((ts - ?) / ? AS INTEGER) AS b, SUM(avg_ms), COUNT(avg_ms), SUM(sent), SUM(received), "
            "COUNT(*), SUM(received > 0), MAX(ts) FROM samples WHERE ip = ? AND ts >= ? GROUP BY b",
            (since, bucket, ip, since),
        )
        for b, lat_sum, lat_n, sent, received, checks, ok, max_ts in rows:
            buckets[b] = [lat_sum or 0.0, lat_n, sent, received, checks, ok]
            last_saved = max(last_saved, max_ts)

    # Add the most recent checks that the monitor has not written to disk yet
    status = read_status()
    recent = status["targets"].get(ip, {}).get("recent", []) if status else []
    for ts, avg_ms, sent, received, *_ in recent:
        if ts <= last_saved:
            continue
        acc = buckets.setdefault(int((ts - since) / bucket), [0.0, 0, 0, 0, 0, 0])
        if avg_ms is not None:
            acc[0] += avg_ms
            acc[1] += 1
        acc[2] += sent
        acc[3] += received
        acc[4] += 1
        acc[5] += received > 0

    points = [
        {
            "ts": since + (b + 0.5) * bucket,
            "avg_ms": lat_sum / lat_n if lat_n else None,
            "loss_pct": 100.0 * (sent - received) / sent if sent else 0.0,
            "availability": 100.0 * ok / checks,
        }
        for b, (lat_sum, lat_n, sent, received, checks, ok) in sorted(buckets.items())
    ]
    return jsonify(since=since, until=until, bucket_seconds=bucket, points=points)


# ---------------------------------------------------------------- CSV export

LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[(\w+)\] (.*)$")
EXPORT_HEADERS = {
    "checks": ["Time", "Target", "IP", "Result", "Pings sent", "Pings received", "Packet loss %", "Avg latency ms", "Jitter ms"],
    "outages": ["Target", "IP", "Start", "End", "Duration seconds", "Duration", "Alert sent"],
    "events": ["Time", "Level", "Message"],
}


def format_time(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts is not None else ""


def format_duration(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def csv_stream(header, rows, sep):
    # Semicolon files use decimal commas, which is what Excel expects in Spanish and other locales
    def cell(value):
        if isinstance(value, float):
            text = f"{value:.3f}".rstrip("0").rstrip(".")
            return text.replace(".", ",") if sep == ";" else text
        return "" if value is None else value

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=sep)
    buffer.write("﻿")  # BOM so Excel opens the file as UTF-8
    writer.writerow(header)
    for i, row in enumerate(rows, 1):
        writer.writerow([cell(value) for value in row])
        if i % 500 == 0:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    yield buffer.getvalue()


def check_rows(ips, start, end, aliases, status):
    query = "SELECT ts, ip, sent, received, avg_ms, jitter_ms FROM samples WHERE ts >= ? AND ts < ?"
    params = [start, end]
    if ips:
        query += f" AND ip IN ({','.join('?' * len(ips))})"
        params += ips
    last_saved = 0
    with closing(connect()) as conn:
        for ts, ip, sent, received, avg_ms, jitter_ms in conn.execute(query + " ORDER BY ts, ip", params):
            last_saved = max(last_saved, ts)
            yield check_row(ts, ip, sent, received, avg_ms, jitter_ms, aliases)

    # Checks from the last few minutes that the monitor has not written to disk yet
    if status:
        recent = sorted(
            (entry[0], ip, *entry[1:])
            for ip, info in status["targets"].items() if not ips or ip in ips
            for entry in info.get("recent", [])
            if last_saved < entry[0] and start <= entry[0] < end
        )
        for ts, ip, avg_ms, sent, received, *rest in recent:
            yield check_row(ts, ip, sent, received, avg_ms, rest[0] if rest else None, aliases)


def check_row(ts, ip, sent, received, avg_ms, jitter_ms, aliases):
    loss = 100.0 * (sent - received) / sent if sent else 100.0
    return [format_time(ts), aliases.get(ip, ip), ip, "OK" if received else "FAIL", sent, received, loss, avg_ms, jitter_ms]


def outage_rows(ips, start, end, status):
    query = "SELECT ip, alias, start, end, alerted FROM outages WHERE end >= ? AND start < ?"
    params = [start, end]
    if ips:
        query += f" AND ip IN ({','.join('?' * len(ips))})"
        params += ips
    now = time.time()
    with closing(connect()) as conn:
        outages = [list(row) for row in conn.execute(query + " ORDER BY start", params)]

    if status:
        ongoing = [[ip, info["alias"], info["down_since"], None, info["alert_sent"]]
                   for ip, info in status["targets"].items() if info["status"] == "DOWN"]
        if not status["internet_up"]:
            ongoing.append([LOCAL_OUTAGE_IP, "Local internet", status["internet_down_since"], None, False])
        outages += [o for o in ongoing if (not ips or o[0] in ips) and o[2] < end]

    for ip, alias, o_start, o_end, alerted in outages:
        duration = (o_end if o_end is not None else now) - o_start
        yield [alias, "" if ip == LOCAL_OUTAGE_IP else ip, format_time(o_start),
               format_time(o_end) if o_end is not None else "ongoing",
               int(duration), format_duration(duration), "Yes" if alerted else "No"]


def event_rows(ips, start, end):
    start_text, end_text = format_time(start), format_time(end)
    files = [f"{LOG_FILE}.{n}" for n in (3, 2, 1)] + [LOG_FILE]
    for path in files:
        if not os.path.exists(path):
            continue
        with open(path, "r", errors="replace") as f:
            for line in f:
                match = LOG_LINE_RE.match(line.strip("\x00\n"))
                if not match:
                    continue
                ts_text, level, message = match.groups()
                if not start_text <= ts_text < end_text:
                    continue
                if ips and not any(f"({ip})" in message for ip in ips):
                    continue
                yield [ts_text, level, message]


@app.get("/api/export")
def api_export():
    kind = request.args.get("type", "checks")
    sep = request.args.get("sep", ",")
    ip = request.args.get("ip", "")
    try:
        start, end = float(request.args["start"]), float(request.args["end"])
    except (KeyError, ValueError):
        return Response("start and end are required", 400)
    if kind not in EXPORT_HEADERS or sep not in (",", ";") or end <= start:
        return Response("Invalid export parameters", 400)

    ips = [ip] if ip else []
    status = read_status()
    if kind == "checks":
        rows = check_rows(ips, start, end, current_targets(status), status)
    elif kind == "outages":
        rows = outage_rows(ips, start, end, status)
    else:
        rows = event_rows(ips, start, end)

    target = f"_{ip}" if ip else ""
    filename = f"ping_{kind}{target}_{datetime.fromtimestamp(start):%Y-%m-%d_%H%M}_to_{datetime.fromtimestamp(end):%Y-%m-%d_%H%M}.csv"
    return Response(csv_stream(EXPORT_HEADERS[kind], rows, sep), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ping Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Outfit:wght@700;800&display=swap">
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#1d2329;--muted:#66707a;--border:#e1e4e8;--up:#1a7f37;--down:#cf222e;--warn:#9a6700;--line:#2563eb;--title:#1e293b;--grid:#eceff2}
@media (prefers-color-scheme: dark){:root{--bg:#0f1216;--card:#181c22;--text:#e6e9ed;--muted:#97a0aa;--border:#2a3038;--up:#3fb950;--down:#f85149;--warn:#d29922;--line:#60a5fa;--title:#e2e8f0;--grid:#252a31}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 40px}
header{display:flex;flex-direction:column;align-items:center;gap:16px;margin:8px 0 22px}
h1{font-family:"Outfit",system-ui,-apple-system,"Segoe UI",sans-serif;font-weight:700;font-size:clamp(34px,7vw,52px);line-height:1.1;letter-spacing:-.01em;text-align:center;margin:0;color:var(--title)}
h2{font-size:17px;margin:28px 0 10px}
.muted{color:var(--muted)}
.banner{padding:10px 14px;border-radius:8px;margin-bottom:14px;font-weight:600;display:none}
.banner.down{display:block;background:color-mix(in srgb,var(--down) 14%,transparent);color:var(--down)}
.banner.warn{display:block;background:color-mix(in srgb,var(--warn) 14%,transparent);color:var(--warn)}
.ranges{display:flex;gap:4px;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:3px}
.ranges button{border:0;background:none;color:var(--muted);font:inherit;font-size:14px;padding:4px 12px;border-radius:6px;cursor:pointer}
.ranges button.active{background:var(--line);color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(330px,100%),1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px;min-width:0}
.card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.alias{font-weight:700;font-size:16px}
.pill{font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px;color:#fff;white-space:nowrap}
.pill.UP{background:var(--up)}.pill.DOWN{background:var(--down)}.pill.PENDING{background:var(--warn)}.pill.UNKNOWN{background:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:12px 0 4px}
.stats.avail{grid-template-columns:repeat(3,1fr);margin:4px 0 12px}
.stat b{display:block;font-size:17px;font-variant-numeric:tabular-nums}
.stat span{font-size:12px;color:var(--muted)}
canvas{width:100%;height:160px;display:block}
.legend{font-size:12px;color:var(--muted);margin-top:6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 10px;vertical-align:-1px}
.table-wrap{overflow-x:auto;background:var(--card);border:1px solid var(--border);border-radius:10px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--border);font-size:14px;white-space:nowrap}
tr:last-child td{border-bottom:0}
th{color:var(--muted);font-weight:600}
.ongoing{color:var(--down);font-weight:600}
.export{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(200px,100%),1fr));gap:12px 14px;align-items:end}
.export label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--muted);min-width:0}
.export select,.export input{font:inherit;font-size:14px;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:7px 8px;width:100%;min-width:0}
.export .presets{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:6px}
.export .presets button{font:inherit;font-size:13px;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:5px 10px;cursor:pointer}
.export .go{font:inherit;font-weight:600;color:#fff;background:var(--line);border:0;border-radius:6px;padding:9px 14px;cursor:pointer}
.export .error{grid-column:1/-1;color:var(--down);font-size:13px;margin:0;display:none}
footer{margin-top:20px;font-size:13px}
</style>
</head>
<body>
<main>
  <header>
    <h1>Ping Monitor</h1>
    <div class="ranges" id="ranges"></div>
  </header>
  <div class="banner" id="banner"></div>
  <div class="grid" id="cards"></div>
  <h2>Outages (last 30 days)</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>Target</th><th>Started</th><th>Duration</th><th>Alert sent</th></tr></thead>
    <tbody id="outages"><tr><td colspan="4" class="muted">Loading...</td></tr></tbody>
  </table></div>
  <h2>Export to CSV</h2>
  <div class="card export">
    <label>Data
      <select id="exType">
        <option value="checks">Ping checks (every check)</option>
        <option value="outages">Outages</option>
        <option value="events">Event log</option>
      </select>
    </label>
    <label>Target
      <select id="exIp"><option value="">All targets</option></select>
    </label>
    <label>From <input type="datetime-local" id="exFrom"></label>
    <label>To <input type="datetime-local" id="exTo"></label>
    <div class="presets" id="exPresets"></div>
    <label>Format
      <select id="exSep">
        <option value=",">Comma separated (1.5)</option>
        <option value=";">Semicolon separated (1,5) - Excel in Spanish</option>
      </select>
    </label>
    <button class="go" id="exGo">Download CSV</button>
    <p class="error" id="exError"></p>
  </div>
  <footer class="muted" id="footer"></footer>
</main>
<script>
const RANGES = {"1h": 1, "24h": 24, "7d": 168, "30d": 720};
let range = "1h";
let status = null, history = {};

const $ = id => document.getElementById(id);
const fmtMs = v => v == null ? "–" : v.toFixed(1) + " ms";
const fmtPct = (v, d = 2) => v == null ? "–" : v.toFixed(d) + "%";
const fmtTime = ts => new Date(ts * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
function fmtDuration(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
}
async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}
function stateOf(t) {
  if (t.status === "DOWN") return ["DOWN", "Offline"];
  if (t.consecutive_failures > 0) return ["PENDING", "Failing"];
  if (t.status === "UP") return ["UP", "Online"];
  return ["UNKNOWN", "Checking"];
}

for (const key of Object.keys(RANGES)) {
  const b = document.createElement("button");
  b.textContent = key;
  b.onclick = () => { range = key; renderRanges(); refreshHistory(); };
  $("ranges").appendChild(b);
}
function renderRanges() {
  for (const b of $("ranges").children) b.classList.toggle("active", b.textContent === range);
}

function card(ip) {
  let el = document.querySelector(`.card[data-ip="${CSS.escape(ip)}"]`);
  if (el) return el;
  el = document.createElement("div");
  el.className = "card";
  el.dataset.ip = ip;
  el.innerHTML = `
    <div class="card-head"><div><div class="alias"></div><div class="muted ip"></div></div><span class="pill"></span></div>
    <div class="stats">
      <div class="stat"><b class="lat"></b><span>Latency</span></div>
      <div class="stat"><b class="loss"></b><span>Packet loss</span></div>
      <div class="stat"><b class="jit"></b><span>Jitter</span></div>
      <div class="stat"><b class="down"></b><span>Downtime</span></div>
    </div>
    <div class="stats avail">
      <div class="stat"><b class="a24h">–</b><span>Availability 24h</span></div>
      <div class="stat"><b class="a7d">–</b><span>7 days</span></div>
      <div class="stat"><b class="a30d">–</b><span>30 days</span></div>
    </div>
    <canvas></canvas>
    <div class="legend"><i style="background:var(--line)"></i>Avg latency<i style="background:var(--down);opacity:.45"></i>Packet loss (bar height = %)</div>`;
  el.querySelector(".ip").textContent = ip;
  $("cards").appendChild(el);
  return el;
}

function renderStatus() {
  const banner = $("banner");
  banner.className = "banner";
  if (!status) {
    banner.className = "banner warn";
    banner.textContent = "Waiting for the monitor service to report...";
    return;
  }
  const now = Date.now() / 1000;
  if (now - status.updated > 3 * status.ping_interval_seconds) {
    banner.className = "banner warn";
    banner.textContent = `The monitor service has not reported for ${fmtDuration(now - status.updated)}. Data below may be stale.`;
  } else if (!status.internet_up) {
    banner.className = "banner down";
    banner.textContent = `This Pi's own internet connection has been down for ${fmtDuration(now - status.internet_down_since)}. Target failures are not being counted.`;
  }

  const ips = Object.keys(status.targets);
  for (const el of [...$("cards").children]) if (!ips.includes(el.dataset.ip)) el.remove();
  for (const ip of ips) {
    const t = status.targets[ip], el = card(ip), [cls, label] = stateOf(t);
    el.querySelector(".alias").textContent = t.alias;
    const pill = el.querySelector(".pill");
    pill.className = "pill " + cls;
    pill.textContent = cls === "PENDING" ? `${label} (${t.consecutive_failures})` : label;
    el.querySelector(".lat").textContent = fmtMs(t.avg_ms);
    el.querySelector(".loss").textContent = t.loss_pct == null ? "–" : t.loss_pct.toFixed(0) + "%";
    el.querySelector(".jit").textContent = fmtMs(t.jitter_ms);
    el.querySelector(".down").textContent = t.down_since ? fmtDuration(now - t.down_since) : "–";
  }
  const select = $("exIp"), chosen = select.value;
  const wanted = ips.map(ip => `${status.targets[ip].alias} (${ip})`);
  if ([...select.options].slice(1).map(o => o.textContent).join() !== wanted.join()) {
    select.length = 1;
    ips.forEach((ip, i) => select.add(new Option(wanted[i], ip)));
    select.value = ips.includes(chosen) ? chosen : "";
  }
  $("footer").textContent = `Last check ${new Date(status.updated * 1000).toLocaleTimeString()} · checks every ${status.ping_interval_seconds}s · alerts after ${fmtDuration(status.alert_after_seconds)} down`
    + (status.public_url ? ` · public address: ${status.public_url}` : "");
}

function renderSummary(summary) {
  for (const [ip, t] of Object.entries(summary.targets)) {
    const el = document.querySelector(`.card[data-ip="${CSS.escape(ip)}"]`);
    if (!el) continue;
    for (const w of ["24h", "7d", "30d"]) {
      const s = t.windows[w];
      el.querySelector(".a" + w).textContent = s ? fmtPct(s.availability) : "–";
    }
  }
  const body = $("outages");
  body.replaceChildren();
  if (!summary.outages.length) {
    body.innerHTML = `<tr><td colspan="4" class="muted">No outages recorded.</td></tr>`;
    return;
  }
  const now = Date.now() / 1000;
  for (const o of summary.outages) {
    const tr = document.createElement("tr");
    const cells = [
      o.ip === "local" ? o.alias : `${o.alias} (${o.ip})`,
      fmtTime(o.start),
      o.end == null ? `${fmtDuration(now - o.start)} (ongoing)` : fmtDuration(o.end - o.start),
      o.alerted ? "Yes" : "No",
    ];
    cells.forEach((text, i) => {
      const td = document.createElement("td");
      td.textContent = text;
      if (i === 2 && o.end == null) td.className = "ongoing";
      tr.appendChild(td);
    });
    body.appendChild(tr);
  }
}

function niceMax(v) {
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function drawChart(canvas, data) {
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement), c = n => css.getPropertyValue(n).trim();
  const L = 46, R = 8, T = 8, B = 20, pw = w - L - R, ph = h - T - B;
  const {since, until, bucket_seconds: bucket, points} = data;
  const lat = points.map(p => p.avg_ms).filter(v => v != null);
  const max = niceMax(Math.max(10, ...lat) * 1.1);
  const x = ts => L + (ts - since) / (until - since) * pw;
  const y = v => T + ph - v / max * ph;

  ctx.font = "11px system-ui, sans-serif";
  ctx.fillStyle = c("--muted");
  ctx.strokeStyle = c("--grid");
  ctx.lineWidth = 1;
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const v = max * i / 4, yy = Math.round(y(v)) + .5;
    ctx.beginPath(); ctx.moveTo(L, yy); ctx.lineTo(w - R, yy); ctx.stroke();
    ctx.fillText(`${+v.toFixed(1)} ms`, L - 6, yy);
  }
  ctx.textBaseline = "top";
  [[since, "left"], [(since + until) / 2, "center"], [until, "right"]].forEach(([ts, align]) => {
    ctx.textAlign = align;
    const d = new Date(ts * 1000);
    const label = until - since > 86400 * 1.5
      ? d.toLocaleDateString([], {month: "short", day: "numeric"})
      : d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
    ctx.fillText(label, x(ts), h - B + 5);
  });

  if (!points.length) {
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("No data for this period yet", L + pw / 2, T + ph / 2);
    return;
  }

  const bw = Math.max(1.5, bucket / (until - since) * pw);
  ctx.fillStyle = c("--down");
  ctx.globalAlpha = .45;
  for (const p of points) {
    if (p.loss_pct > 0) {
      const bh = Math.max(2, p.loss_pct / 100 * ph);
      ctx.fillRect(x(p.ts) - bw / 2, T + ph - bh, bw, bh);
    }
  }
  ctx.globalAlpha = 1;

  ctx.strokeStyle = c("--line");
  ctx.lineWidth = 1.75;
  ctx.lineJoin = "round";
  ctx.beginPath();
  let prev = null;
  for (const p of points) {
    if (p.avg_ms == null) { prev = null; continue; }
    const px = x(p.ts), py = y(p.avg_ms);
    if (prev && p.ts - prev <= bucket * 1.6) ctx.lineTo(px, py); else ctx.moveTo(px, py);
    prev = p.ts;
  }
  ctx.stroke();
}

async function refreshStatus() {
  try { status = await getJSON("api/status"); } catch (e) { status = null; }
  renderStatus();
}
async function refreshHistory() {
  if (!status) return;
  const hours = RANGES[range];
  await Promise.all(Object.keys(status.targets).map(async ip => {
    try {
      history[ip] = await getJSON(`api/history?ip=${encodeURIComponent(ip)}&hours=${hours}`);
      const el = document.querySelector(`.card[data-ip="${CSS.escape(ip)}"]`);
      if (el) drawChart(el.querySelector("canvas"), history[ip]);
    } catch (e) {}
  }));
}
async function refreshSummary() {
  try { renderSummary(await getJSON("api/summary")); } catch (e) {}
}
function redrawCharts() {
  for (const [ip, data] of Object.entries(history)) {
    const el = document.querySelector(`.card[data-ip="${CSS.escape(ip)}"]`);
    if (el) drawChart(el.querySelector("canvas"), data);
  }
}

const toInput = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
function setPeriod(from, to) { $("exFrom").value = toInput(from); $("exTo").value = toInput(to); }
const PRESETS = {
  "Last 24 hours": () => [new Date(Date.now() - 86400e3), new Date()],
  "Last 7 days": () => [new Date(Date.now() - 7 * 86400e3), new Date()],
  "Last 30 days": () => [new Date(Date.now() - 30 * 86400e3), new Date()],
  "This month": () => { const n = new Date(); return [new Date(n.getFullYear(), n.getMonth(), 1), n]; },
  "Last month": () => { const n = new Date(); return [new Date(n.getFullYear(), n.getMonth() - 1, 1), new Date(n.getFullYear(), n.getMonth(), 1)]; },
};
for (const [label, period] of Object.entries(PRESETS)) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  b.onclick = () => setPeriod(...period());
  $("exPresets").appendChild(b);
}
setPeriod(...PRESETS["Last 24 hours"]());
$("exGo").onclick = () => {
  const from = new Date($("exFrom").value), to = new Date($("exTo").value), error = $("exError");
  error.style.display = "none";
  if (isNaN(from) || isNaN(to) || to <= from) {
    error.textContent = "Choose a valid period: \\"To\\" must be after \\"From\\".";
    error.style.display = "block";
    return;
  }
  const params = new URLSearchParams({
    type: $("exType").value, ip: $("exIp").value, sep: $("exSep").value,
    start: from.getTime() / 1000, end: to.getTime() / 1000,
  });
  const a = document.createElement("a");
  a.href = "api/export?" + params;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
};

renderRanges();
(async () => {
  await refreshStatus();
  refreshHistory();
  refreshSummary();
})();
setInterval(refreshStatus, 5000);
setInterval(() => { refreshHistory(); refreshSummary(); }, 60000);
window.addEventListener("resize", redrawCharts);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redrawCharts);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    config = load_config()
    try:
        from waitress import serve
        serve(app, host=config["web_host"], port=config["web_port"], threads=4)
    except ImportError:
        app.run(host=config["web_host"], port=config["web_port"], threaded=True)
