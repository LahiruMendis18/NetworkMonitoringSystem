# Monitoring You — Network Monitoring System

A clean, professional network monitoring dashboard built with Flask + SQLite.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the web app
python app.py
# → http://localhost:5000

# 3. In a separate terminal, run the background monitor
MONITOR_TOKEN=monitor-secret python monitor.py
```

Default login: **admin / Admin@1234**

---

## Architecture

```
netmon/
├── app.py          # Flask web server + REST API
├── monitor.py      # Background ping worker (runs separately)
├── database.db     # SQLite — auto-created on first run
├── requirements.txt
└── templates/
    ├── login.html  # Sign-in page
    └── index.html  # Dashboard (pure JS, talks to /api/*)
```

### Data flow

```
monitor.py  ──PING──▶  device IPs
    │
    └─POST /api/internal/update──▶  app.py  ──▶  database.db
                                        │
                            browser ◀── /api/devices, /api/stats, /api/alerts
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/devices` | List all devices |
| POST | `/api/devices` | Add device `{name, ip, group}` |
| PATCH | `/api/devices/<id>` | Update name/group |
| DELETE | `/api/devices/<id>` | Remove device |
| GET | `/api/stats` | Summary counts + avg latency |
| GET | `/api/alerts` | Recent alert log |
| POST | `/api/alerts/read` | Mark all alerts seen |
| GET | `/api/devices/<id>/history` | Ping history for a device |
| POST | `/api/internal/update` | Used by monitor.py (token-protected) |

---

## Security improvements over original

| Original | Monitoring You |
|----------|---------|
| Hardcoded `admin/admin` plain text | SHA-256 hashed password in DB |
| `secret_key = "secret123"` | `os.urandom(32)` on each start |
| No IP validation | Strict IPv4 regex + range check |
| No duplicate IP check | 409 Conflict if IP already exists |
| `send_mail` always fires on offline | Alerts stored in DB, email optional |
| No CSRF protection on forms | JSON API (no form POST from arbitrary sites) |
| Monitor token: none | Shared `X-Monitor-Token` header |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MONITOR_TOKEN` | `monitor-secret` | Shared secret between monitor and app |
| `API_BASE` | `http://127.0.0.1:5000` | Where monitor POSTs results |
| `POLL_INTERVAL` | `30` | Seconds between poll cycles |
