import os
import sqlite3
import platform
import time
from datetime import datetime
import smtplib

def send_mail(ip):
    sender = "yourmail@gmail.com"
    password = "your_app_password"
    receiver = "receiver@gmail.com"

    message = f"Subject: ALERT!\n\nDevice {ip} is DOWN!"

    server = smtplib.SMTP("smtp.gmail.com", 587)
    server.starttls()
    server.login(sender, password)
    server.sendmail(sender, receiver, message)
    server.quit()


def ping(host):
    param = "-n 1" if platform.system().lower() == "windows" else "-c 1"
    return os.system(f"ping {param} {host}") == 0


def update_status():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()

    c.execute("SELECT id, ip FROM devices")
    devices = c.fetchall()

    for device_id, ip in devices:
        status = "Online" if ping(ip) else "Offline"
        time_now = datetime.now()

        if status == "Offline":
            send_mail(ip)

        c.execute("""
            UPDATE devices 
            SET status=?, last_checked=? 
            WHERE id=?
        """, (status, time_now, device_id))

    conn.commit()
    conn.close()

    print("Updated:", datetime.now())


while True:
    update_status()
    time.sleep(5)