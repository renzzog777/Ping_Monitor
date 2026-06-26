import os
import sys
import json
import time
import logging
import smtplib
import subprocess
from datetime import datetime
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor

# Configuration and Rotational Logs (Max 15MB total)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "ping_monitor.log")
STATUS_RAM_FILE = "/dev/shm/ping_status.json" 

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3)]
)

try:
    with open(os.path.join(BASE_DIR, "config.json"), "r") as f:
        config = json.load(f)
except Exception as e:
    logging.error(f"Error loading config.json: {e}")
    sys.exit(1)

# Initialize states in memory mapping IP to Alias and runtime metrics
ip_states = {
    ip: {
        "alias": alias,
        "status": "UP", 
        "down_since": None, 
        "alert_sent": False, 
        "last_alert_time": None, 
        "last_latency": "N/A"
    }
    for ip, alias in config["ips_to_monitor"].items()
}

def send_email_alert(ip, alias, status, duration=None, is_reminder=False):
    if is_reminder:
        subject = f"[NETWORK ALERT] [REMINDER] {alias} ({ip}) IS STILL DOWN"
        body = f"System: {alias}\nIP Address: {ip}\nStatus: Continues to be unresponsive.\nTotal downtime so far: {duration}.\nLast check: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    elif status == "DOWN":
        subject = f"[NETWORK ALERT] {alias} ({ip}) is DOWN"
        body = f"System: {alias}\nIP Address: {ip}\nStatus: Stopped responding for more than 5 minutes.\nDowntime detected on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    else:
        subject = f"[NETWORK ALERT] {alias} ({ip}) has RECOVERED"
        body = f"System: {alias}\nIP Address: {ip}\nStatus: Successfully restored.\nOffline duration: {duration}.\nRecovery time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config["email_sender"]
    msg["To"] = config["email_receiver"]

    try:
        with smtplib.SMTP(config["smtp_server"], config["smtp_port"]) as server:
            server.starttls()
            server.login(config["email_sender"], config["email_password"])
            server.sendmail(config["email_sender"], config["email_receiver"], msg.as_string())
        logging.info(f"Email alert sent for {alias} ({ip}) [Status: {status}, Reminder: {is_reminder}].")
    except Exception as e:
        logging.error(f"Error sending email alert for {ip}: {e}")

def ping_address(ip):
    try:
        output = subprocess.run(
            ["ping", "-c", "1", "-W", "2", ip],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        if output.returncode == 0:
            for line in output.stdout.split("\n"):
                if "time=" in line:
                    latency = line.split("time=")[1].split(" ")[0] + " ms"
                    return ip, True, latency
            return ip, True, "0 ms"
        return ip, False, "Timeout"
    except Exception:
        return ip, False, "Error"

def check_pool():
    with ThreadPoolExecutor(max_workers=len(ip_states)) as executor:
        results = executor.map(ping_address, ip_states.keys())

    current_time = time.time()

    for ip, is_up, latency in results:
        state = ip_states[ip]
        state["last_latency"] = latency
        alias = state["alias"]

        if is_up:
            if state["status"] == "DOWN":
                logging.info(f"[RECOVERED] {alias} ({ip}) is back online.")
                if state["alert_sent"]:
                    down_duration = str(datetime.now() - datetime.fromtimestamp(state["down_since"])).split('.')[0]
                    send_email_alert(ip, alias, "UP", duration=down_duration)
            state["status"] = "UP"
            state["down_since"] = None
            state["alert_sent"] = False
            state["last_alert_time"] = None
        else:
            if state["status"] == "UP":
                state["status"] = "DOWN"
                state["down_since"] = current_time
                state["last_alert_time"] = None
                logging.warning(f"[DOWN] {alias} ({ip}) stopped responding.")
            
            elif state["status"] == "DOWN":
                total_down_time = current_time - state["down_since"]
                
                if not state["alert_sent"]:
                    if total_down_time >= 300:
                        state["alert_sent"] = True
                        state["last_alert_time"] = current_time
                        send_email_alert(ip, alias, "DOWN")
                else:
                    time_since_last_alert = current_time - state["last_alert_time"]
                    if time_since_last_alert >= 1800: 
                        state["last_alert_time"] = current_time
                        readable_duration = str(datetime.now() - datetime.fromtimestamp(state["down_since"])).split('.')[0]
                        send_email_alert(ip, alias, "DOWN", duration=readable_duration, is_reminder=True)

    try:
        with open(STATUS_RAM_FILE, "w") as f:
            json.dump(ip_states, f)
    except Exception as e:
        logging.error(f"Error writing status payload to RAM: {e}")

if __name__ == "__main__":
    logging.info("Starting Ping Monitoring Core with Alias Support...")
    while True:
        check_pool()
        time.sleep(config["ping_interval_seconds"])
