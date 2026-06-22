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

# Initialize states in memory with a tracking timestamp for reminders
ip_states = {
    ip: {
        "status": "UP", 
        "down_since": None, 
        "alert_sent": False, 
        "last_alert_time": None, # Tracks when the last email went out
        "last_latency": "N/A"
    }
    for ip in config["ips_to_monitor"]
}

def send_email_alert(ip, status, duration=None, is_reminder=False):
    # Differentiate subject line if it's a 30-minute reminder
    if is_reminder:
        subject = f"[ALERTA RED] [RECORDATORIO] La IP {ip} SIGUE CAÍDA"
        body = f"La dirección IP {ip} continúa sin responder.\nTiempo total caído hasta ahora: {duration}.\nÚltima comprobación: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    elif status == "DOWN":
        subject = f"[ALERTA RED] La IP {ip} está DOWN"
        body = f"La dirección IP {ip} ha dejado de responder por más de 5 minutos.\nCaída detectada el: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    else:
        subject = f"[ALERTA RED] La IP {ip} se ha RECUPERADO"
        body = f"La dirección IP {ip} se ha restablecido con éxito.\nEstuvo fuera de servicio por: {duration}.\nHora de recuperación: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config["email_sender"]
    msg["To"] = config["email_receiver"]

    try:
        with smtplib.SMTP(config["smtp_server"], config["smtp_port"]) as server:
            server.starttls()
            server.login(config["email_sender"], config["email_password"])
            server.sendmail(config["email_sender"], config["email_receiver"], msg.as_string())
        logging.info(f"Email notification sent for {ip} (Status: {status}, Reminder: {is_reminder}).")
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

        if is_up:
            if state["status"] == "DOWN":
                logging.info(f"[RECUPERADO] {ip} is back online.")
                if state["alert_sent"]:
                    # Calculate total downtime duration
                    down_duration = str(datetime.now() - datetime.fromtimestamp(state["down_since"])).split('.')[0]
                    send_email_alert(ip, "UP", duration=down_duration)
            state["status"] = "UP"
            state["down_since"] = None
            state["alert_sent"] = False
            state["last_alert_time"] = None
        else:
            if state["status"] == "UP":
                state["status"] = "DOWN"
                state["down_since"] = current_time
                state["last_alert_time"] = None
                logging.warning(f"[CAÍDA] {ip} stopped responding.")
            
            # Handle Down States and Reminders
            elif state["status"] == "DOWN":
                total_down_time = current_time - state["down_since"]
                
                # 1. Initial Alert after 5 minutes (300 seconds)
                if not state["alert_sent"]:
                    if total_down_time >= 300:
                        state["alert_sent"] = True
                        state["last_alert_time"] = current_time
                        send_email_alert(ip, "DOWN")
                
                # 2. Sequential Reminders every 30 minutes (1800 seconds)
                else:
                    time_since_last_alert = current_time - state["last_alert_time"]
                    if time_since_last_alert >= 1800: # 30 minutes
                        state["last_alert_time"] = current_time
                        readable_duration = str(datetime.now() - datetime.fromtimestamp(state["down_since"])).split('.')[0]
                        send_email_alert(ip, "DOWN", duration=readable_duration, is_reminder=True)

    # Push to RAM layout for Dashboard.py
    try:
        with open(STATUS_RAM_FILE, "w") as f:
            json.dump(ip_states, f)
    except Exception as e:
        logging.error(f"Error writing status payload to RAM: {e}")

if __name__ == "__main__":
    logging.info("Starting Ping Monitoring Core 24/7 with 30-min reminders...")
    while True:
        check_pool()
        time.sleep(config["ping_interval_seconds"])
