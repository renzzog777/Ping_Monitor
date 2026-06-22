24/7 Lightweight Network Ping Monitor

A highly efficient, parallelized ping monitoring tool tailored specifically for resource-constrained hardware like the Raspberry Pi 2 B+. It continuously monitors a pool of up to 20+ IP addresses, triggers automated email notifications during downtime, provides historical log rotations, and offers a beautiful, real-time terminal user interface (TUI).
🚀 Key Features

    Parallel Processing: Uses Python's ThreadPoolExecutor to ping all target IPs simultaneously, avoiding timeouts or sequential lag.

    SD Card Protection (Zero Wear): Instead of continuously writing to the SD card, the monitoring engine writes live status payloads directly to the Raspberry Pi's RAM (/dev/shm), protecting your storage from premature failure.

    Smart Alerting System:

        Triggers an email notification only if an IP remains unreachable for more than 5 minutes.

        Prevents spamming by sending reminders every 30 minutes if the target stays down.

        Sends a final Recovery Email tracking total downtime once the node is back online.

    Bounded Log Files: Employs an automated RotatingFileHandler restricted to a maximum of 15MB total storage, preventing your 8GB SD card from ever filling up.

    Modern TUI Dashboard: A sleek, live terminal UI powered by the rich library to monitor network health in real-time without the overhead of a desktop environment (GUI).

📁 Repository Structure
Plaintext

ping_project/
├── config.json          # SMTP settings, target IPs, and intervals
├── monitor.py           # Background monitoring engine (runs 24/7)
├── dashboard.py         # Real-time console terminal UI
└── ping_monitor.log     # Self-cleaning historical activity logs

🛠️ Installation & Setup
1. Prerequisites

Ensure your Raspberry Pi is running Raspberry Pi OS Lite (32-bit) and you have python3 installed.
Bash

sudo apt update && sudo apt upgrade -y
sudo apt install python3-pip python3-venv -y

2. Clone and Prepare Environment

Navigate to your preferred directory, clone/create the project folder, and spin up a Python virtual environment:
Bash

mkdir -p ~/ping_project
cd ~/ping_project
python3 -m venv venv
source venv/bin/activate
pip install rich

3. Configuration (config.json)

Create a config.json file in the project folder. If using Gmail, you must generate a Google App Password instead of using your standard login credentials.
JSON

{
  "smtp_server": "smtp.gmail.com",
  "smtp_port": 587,
  "email_sender": "your_bot_email@gmail.com",
  "email_password": "your16charapppassword",
  "email_receiver": "your_personal_alerts@gmail.com",
  "ping_interval_seconds": 15,
  "ips_to_monitor": [
    "8.8.8.8",
    "1.1.1.1",
    "192.168.1.1",
    "192.168.1.254"
  ]
}

⚙️ Daemon Deployment (Systemd)

To make sure the monitoring engine runs non-stop 24/7 and survives system restarts, set it up as a system service.

    Create the systemd service file:

Bash

sudo nano /etc/systemd/system/ping-monitor.service

    Paste the following configuration (Replace renzzo with your actual Linux user if different):

Ini, TOML

[Unit]
Description=Servicio de Monitoreo de Ping de Red 24/7
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

    Enable and start the background engine:

Bash

sudo systemctl daemon-reload
sudo systemctl enable ping-monitor.service
sudo systemctl start ping-monitor.service

📊 How to Use
Real-Time Live Dashboard

To look at live metrics, latency speeds, and ongoing downtimes directly from any SSH or local terminal, launch the dashboard component:
Bash

cd ~/ping_project
source venv/bin/activate
python dashboard.py

    💡 Note: You can safely exit the dashboard (Ctrl + C) at any time. The background engine will keep running and tracking independently.

Checking Historical Logs

Logs are kept highly structured and never exceed a memory cap of 15MB. Inspect historical transitions or recent crashes with:
Bash

tail -f ~/ping_project/ping_monitor.log

