24/7 Lightweight Network Ping Monitor

A highly efficient, parallelized ping monitoring tool tailored specifically for resource-constrained hardware like the Raspberry Pi 2 B+. It continuously monitors a pool of up to 20+ IP addresses, sends email and Telegram alerts during downtime, keeps a history of latency, packet loss and outages, and offers both a real-time terminal UI and a web dashboard you can open from any device on the network.
🚀 Key Features

    Parallel Processing: Uses Python's ThreadPoolExecutor to ping all target IPs simultaneously, avoiding timeouts or sequential lag.

    No False Alarms: Each check sends several pings, and a target is only marked DOWN after several failed checks in a row, so a single lost packet does not count as an outage.

    Local Outage Detection: Reference hosts (1.1.1.1, 8.8.8.8, 9.9.9.9 by default) are checked alongside your targets. If none of them respond, the Pi's own internet connection is down, and target failures are not counted against them.

    Latency, Packet Loss and Jitter: Every check measures average latency, packet loss % and jitter (latency variation).

    SD Card Protection (Low Wear): Live status lives in the Raspberry Pi's RAM (/dev/shm). History is buffered in memory and written to a small SQLite database in batches (every 5 minutes by default), and old history is deleted automatically after 90 days.

    Smart Alerting System:

        Triggers a notification only if an IP remains unreachable for more than 5 minutes.

        Prevents spamming by sending reminders every 30 minutes if the target stays down.

        Sends a final Recovery alert tracking total downtime once the node is back online.

        Alerts are delivered from a background thread with timeouts and automatic retries, so a slow mail server never pauses monitoring.

        Optional Telegram alerts in addition to email.

    Daily Summary: A report every morning with availability, packet loss, latency and outages for the last 24 hours.

    Web Dashboard: Live status, availability for the last 24 hours / 7 days / 30 days, latency and packet loss charts, and an outage history, at http://<pi-address>:5000.

    CSV Export: Download ping checks, outages or the event log for any time period and target from the web dashboard, in comma format or semicolon format with decimal commas (for Excel in Spanish and other locales).

    Modern TUI Dashboard: A sleek, live terminal UI powered by the rich library to monitor network health in real-time without the overhead of a desktop environment (GUI).

    Live Config Reload: Edit config.json and changes (targets, intervals, alert settings) apply on the next check, without restarting the service.

    Bounded Log Files: Employs an automated RotatingFileHandler restricted to a maximum of 15MB total storage.

📁 Repository Structure
Plaintext

ping_project/
├── config.example.json  # Template for config.json (copy and fill in)
├── config.json          # Your SMTP/Telegram settings and targets (not committed)
├── common.py            # Shared settings and history database helpers
├── monitor.py           # Background monitoring engine (runs 24/7)
├── dashboard.py         # Real-time console terminal UI
├── web.py               # Web dashboard (runs 24/7)
├── ping-web.service     # systemd unit for the web dashboard
├── ping-tunnel.service  # systemd unit for the public Cloudflare tunnel
├── requirements.txt     # Python dependencies
├── ping_history.db      # History database (created automatically, not committed)
└── ping_monitor.log     # Self-cleaning historical activity logs

🛠️ Installation & Setup
1. Prerequisites

Ensure your Raspberry Pi is running Raspberry Pi OS Lite (32-bit) and you have python3 installed.
Bash

sudo apt update && sudo apt upgrade -y
sudo apt install python3-pip python3-venv git -y

2. Clone and Prepare Environment

Clone the project and spin up a Python virtual environment:
Bash

git clone https://github.com/renzzog777/Ping_Monitor.git ~/ping_project
cd ~/ping_project
python3 -m venv venv
venv/bin/pip install -r requirements.txt

3. Configuration (config.json)

Copy the template and edit it. If using Gmail, you must generate a Google App Password instead of using your standard login credentials. config.json is listed in .gitignore so your passwords are never committed.
Bash

cp config.example.json config.json
nano config.json

The main settings:

    ips_to_monitor: The targets, as "IP": "Alias" pairs.

    email_*, smtp_*: Email alert settings. Leave email_sender empty to disable email.

    telegram_bot_token, telegram_chat_id: Optional Telegram alerts. Create a bot with @BotFather, send it a message, then get your chat ID from https://api.telegram.org/bot<token>/getUpdates.

    ping_interval_seconds (15), ping_count (3), fail_threshold (2): How often to check, how many pings per check, and how many failed checks in a row mark a target DOWN.

    alert_after_seconds (300), reminder_interval_seconds (1800): When to send the first alert and reminders.

    reference_hosts: Hosts used to detect when the Pi's own internet is down. Use [] to disable.

    history_flush_seconds (300), history_retention_days (90): How often history is written to the SD card and how long it is kept.

    daily_summary_time ("08:00"): When to send the daily report. Use "" to disable.

    web_host, web_port (5000): Where the web dashboard listens.

    web_username, web_password: Login for the web dashboard. Leave web_password empty to disable the login (only do this if the dashboard is not published on the internet).

    tunnel_metrics_url: Where the monitor reads the Cloudflare quick tunnel address from, to email you when it changes. Use "" to disable.

Any setting left out of config.json uses the default shown in config.example.json.

⚙️ Daemon Deployment (Systemd)

To make sure the monitoring engine and web dashboard run non-stop 24/7 and survive system restarts, set them up as system services.

    Create the monitoring service file:

Bash

sudo nano /etc/systemd/system/ping-monitor.service

    Paste the following configuration (Replace renzzo with your actual Linux user if different):

Ini, TOML

[Unit]
Description=24/7 Network Ping Monitoring Service
After=network.target

[Service]
Type=simple
User=renzzo
WorkingDirectory=/home/renzzo/ping_project
ExecStart=/home/renzzo/ping_project/venv/bin/python monitor.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target

    Install the web dashboard service (edit the user in the file first if it is not renzzo):

Bash

sudo cp ping-web.service /etc/systemd/system/

    Enable and start both services:

Bash

sudo systemctl daemon-reload
sudo systemctl enable --now ping-monitor.service ping-web.service

📊 How to Use
Web Dashboard

Open http://<pi-address>:5000 from any phone or computer on the same network. It refreshes on its own: live status every 5 seconds, charts and history every minute.

    💡 Note: Set web_username and web_password in config.json to require a login. Do not forward port 5000 on your router; use the Cloudflare tunnel below to reach it from outside.

Public Access (Cloudflare Quick Tunnel)

The dashboard can be reached from anywhere through a free Cloudflare quick tunnel, without opening ports on your router. The public address is a random https://<words>.trycloudflare.com that changes every time the tunnel or the Pi restarts; the monitor emails you the new address each time it changes. Always set a web_password before enabling the tunnel.

    Install cloudflared (32-bit Raspberry Pi OS; check the release page for the current version and its checksum):

Bash

curl -L -o cloudflared https://github.com/cloudflare/cloudflared/releases/download/2026.10.1/cloudflared-linux-arm
sudo install -m 755 cloudflared /usr/local/bin/cloudflared

    Install and start the tunnel service:

Bash

sudo cp ping-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ping-tunnel.service

    💡 Note: Quick tunnels are intended for testing and have no uptime guarantee. For a permanent address with Cloudflare's own login page, use a named tunnel with a Cloudflare account and Cloudflare Access.

Real-Time Terminal Dashboard

To look at live metrics, latency speeds, and ongoing downtimes directly from any SSH or local terminal, launch the dashboard component:
Bash

cd ~/ping_project
venv/bin/python dashboard.py

    💡 Note: You can safely exit the dashboard (Ctrl + C) at any time. The background engine will keep running and tracking independently.

Checking Historical Logs

Logs are kept highly structured and never exceed a memory cap of 15MB. Inspect historical transitions or recent crashes with:
Bash

tail -f ~/ping_project/ping_monitor.log

Running Headless

The monitor does not need a desktop. On a Pi that is only used over SSH, booting to the console frees roughly 200MB of RAM:
Bash

sudo systemctl set-default multi-user.target    # To go back: sudo systemctl set-default graphical.target
