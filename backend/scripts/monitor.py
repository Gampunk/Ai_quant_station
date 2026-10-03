"""
Outside health monitor: checks the backend, the MT5 connector and the systemd
service, and sends Telegram alerts. Run every 5 minutes by impulse-monitor.timer.

It runs outside the backend on purpose: a crashed backend cannot report itself.
The backend's own heartbeat (core/heartbeat.py) watches the autopilots and prices.

One message when a check starts failing, one when it recovers. The last state is
kept in MONITOR_STATE_FILE so a 5-minute timer does not repeat the same alert.

Settings, from the environment (the systemd unit reads backend/.env):
    MONITOR_BACKEND_URL   default http://127.0.0.1:8002
    MT5_CONNECTOR_URL     required for the connector check; no default address
    MT5_API_TOKEN         sent to the connector, which refuses requests without it
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    MONITOR_STATE_FILE    default /var/lib/impulse-monitor/state.json
    MONITOR_SERVICE_NAME  the systemd service to check, default impulse-analyst
    INSTANCE_LABEL        named in every message, default "Version 2"
"""
import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone

BACKEND_URL = os.getenv("MONITOR_BACKEND_URL", "http://127.0.0.1:8002")
CONNECTOR_URL = os.getenv("MT5_CONNECTOR_URL", "").rstrip("/")
CONNECTOR_TOKEN = os.getenv("MT5_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_FILE = os.getenv("MONITOR_STATE_FILE", "/var/lib/impulse-monitor/state.json")
SERVICE_NAME = os.getenv("MONITOR_SERVICE_NAME", "impulse-analyst")
LABEL = os.getenv("INSTANCE_LABEL", "Version 2")


def _send_alert(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[MONITOR] Telegram not set up. Would send: {text}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        print(f"[MONITOR] Alert sent: {text}")
    except Exception as e:
        print(f"[MONITOR] Failed to send alert: {e}")


def check_backend() -> tuple[bool, str]:
    try:
        r = urllib.request.urlopen(f"{BACKEND_URL}/health", timeout=10)
        return r.status == 200, f"Backend at {BACKEND_URL} is not answering /health"
    except Exception as e:
        return False, f"Backend at {BACKEND_URL} is unreachable: {e}"


def check_connector() -> tuple[bool, str]:
    if not CONNECTOR_URL:
        return True, ""  # not configured here: nothing to check
    req = urllib.request.Request(f"{CONNECTOR_URL}/health",
                                 headers={"Authorization": f"Bearer {CONNECTOR_TOKEN}"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        ok = bool(data.get("mt5_connected") or data.get("mt5_initialized"))
        return ok, "The MT5 connector answers, but its terminal is not connected"
    except Exception as e:
        return False, f"The MT5 connector is unreachable: {e}"


def check_systemd() -> tuple[bool, str]:
    try:
        r = subprocess.run(["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True)
    except FileNotFoundError:
        return True, ""  # not a systemd machine
    return r.stdout.strip() == "active", f"The {SERVICE_NAME} service is not running"


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError as e:
        print(f"[MONITOR] Could not save state to {STATE_FILE}: {e}")


def run(state: dict, checks: dict) -> list[str]:
    """Compare each check with its last state and return the messages to send."""
    messages = []
    for name, (ok, failing) in checks.items():
        was_down = state.get(name) == "down"
        if not ok and not was_down:
            messages.append(f"🔴 {LABEL}: {failing}")
        elif ok and was_down:
            messages.append(f"🟢 {LABEL}: {name} recovered")
        state[name] = "up" if ok else "down"
    return messages


def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    checks = {"service": check_systemd(), "backend": check_backend(), "connector": check_connector()}
    state = _load_state()
    for message in run(state, checks):
        _send_alert(f"{message}\n{ts}")
    _save_state(state)
    print(f"[MONITOR] {ts} | " + " ".join(f"{k}={'ok' if v[0] else 'DOWN'}" for k, v in checks.items()))


if __name__ == "__main__":
    main()
