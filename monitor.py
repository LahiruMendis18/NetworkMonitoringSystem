"""
monitor.py  —  Monitoring You background ping worker (Windows-safe)
Run from the same folder as app.py:   python monitor.py
"""
import os, sys, time, platform, subprocess, sqlite3, json, socket
import urllib.request, urllib.error
from datetime import datetime

# ── CONFIG ───────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DB_PATH       = os.path.join(BASE_DIR, "database.db")
API_BASE      = os.environ.get("API_BASE",      "http://127.0.0.1:5000")
MONITOR_TOKEN = os.environ.get("MONITOR_TOKEN", "monitor-secret")
INTERVAL      = int(os.environ.get("POLL_INTERVAL", "30"))
IS_WINDOWS    = platform.system().lower() == "windows"

# ── PING ─────────────────────────────────────────────────────────────────────

def ping(host):
    """Returns (is_online: bool, latency_ms: float|None)"""
    try:
        cmd = ["ping", "-n", "1", "-w", "2000", host] if IS_WINDOWS \
              else ["ping", "-c", "1", "-W", "2", host]

        start  = time.monotonic()
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=6,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        )
        elapsed = (time.monotonic() - start) * 1000
        out = result.stdout.decode(errors="ignore")

        if result.returncode == 0 or (IS_WINDOWS and "TTL=" in out.upper()):
            return True, round(elapsed, 1)
        return False, None

    except subprocess.TimeoutExpired:
        return False, None
    except Exception:
        pass

    # TCP fallback if ping fails
    try:
        start = time.monotonic()
        with socket.create_connection((host, 80), timeout=3):
            pass
        return True, round((time.monotonic() - start) * 1000, 1)
    except ConnectionRefusedError:
        return True, round((time.monotonic() - start) * 1000, 1)
    except OSError:
        return False, None

# ── DB ────────────────────────────────────────────────────────────────────────

def get_devices():
    if not os.path.exists(DB_PATH):
        print(f"  [error] database.db not found at: {DB_PATH}")
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, ip, name FROM devices").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"  [error] DB read failed: {e}")
        return []

# ── PUSH ──────────────────────────────────────────────────────────────────────

def push_result(device_id, status, latency):
    payload = json.dumps({"device_id": device_id, "status": status, "latency": latency}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/api/internal/update",
        data=payload,
        headers={"Content-Type": "application/json", "X-Monitor-Token": MONITOR_TOKEN},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
            return True, None
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        hint = {403: " (token mismatch)", 400: " (bad payload)",
                404: " (old app.py — replace it)"}.get(e.code, "")
        return False, f"HTTP {e.code}{hint}"
    except urllib.error.URLError as e:
        return False, f"Cannot reach {API_BASE} — is app.py running?"
    except Exception as e:
        return False, str(e)

# ── SELF-TEST ─────────────────────────────────────────────────────────────────

def self_test():
    """Confirms token + DB are reachable before polling starts."""
    req = urllib.request.Request(
        f"{API_BASE}/api/ping-test",
        data=b"{}",
        headers={"Content-Type": "application/json", "X-Monitor-Token": MONITOR_TOKEN},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            print(f"  Self-test OK — {data.get('devices_in_db', '?')} device(s) in DB")
            print(f"  DB path: {data.get('db_path', '?')}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        print(f"  [FAIL] HTTP {e.code}: {body[:200]}")
        if e.code == 404:
            print("  Replace app.py with the new version and restart Flask.")
        elif e.code == 403:
            print("  Token mismatch — check MONITOR_TOKEN.")
        return False
    except Exception as e:
        print(f"  [FAIL] {e} — is app.py running?")
        return False

# ── CYCLE ─────────────────────────────────────────────────────────────────────

def run_cycle():
    devices = get_devices()
    if not devices:
        print(f"[{ts()}] No devices in DB. Add one at http://localhost:5000")
        return

    print(f"[{ts()}] Polling {len(devices)} device(s)...")
    for dev in devices:
        is_up, latency = ping(dev["ip"])
        status  = "Online" if is_up else "Offline"
        icon    = "+" if is_up else "x"
        lat_s   = f"{latency:.1f} ms" if latency is not None else "timeout"
        ok, err = push_result(dev["id"], status, latency)
        note    = "-> saved" if ok else f"-> FAILED: {err}"
        print(f"  [{icon}] {dev['name']:<22} {dev['ip']:<17} {status:<8} {lat_s:<12} {note}")

def ts():
    return datetime.now().strftime("%H:%M:%S")

# ── ENTRY ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Monitoring You — Network Monitor")
    print(f"  Interval : {INTERVAL}s")
    print(f"  API      : {API_BASE}")
    print(f"  Token    : {'monitor-secret (default)' if MONITOR_TOKEN == 'monitor-secret' else '(custom)'}")
    print(f"  DB       : {DB_PATH}")
    print(f"  Platform : {platform.system()}")
    print("=" * 60)
    print("Running self-test...")
    self_test()
    print("-" * 60)

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            print("\n[stopped]")
            sys.exit(0)
        except Exception as e:
            print(f"[{ts()}] Unexpected error: {e}")
        time.sleep(INTERVAL)
