"""Start the real connector.py on the fake MetaTrader5 terminal, for backend tests."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "mt5_connector" / "testing" / "run_fake_connector.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_fake(port, *extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MT5_", "FAKE_MT5_"))}
    proc = subprocess.Popen(
        [sys.executable, str(LAUNCHER), "--port", str(port), *extra],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        if proc.poll() is not None:
            pytest.fail(f"fake connector exited early:\n{proc.stdout.read()}")
        try:
            httpx.get(f"{url}/health", timeout=1)
            return proc, url
        except httpx.HTTPError:
            time.sleep(0.5)
    proc.terminate()
    pytest.fail("fake connector did not start")


def stop_fake(proc):
    proc.terminate()
    proc.wait(timeout=10)
