"""
The outside monitor (scripts/monitor.py): one alert when a check fails, one when it
recovers, never one every 5 minutes, and no connector address written in the code.

Negative control: in run(), drop `and not was_down`. The "only once" test must fail.
"""
import importlib.util
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "monitor.py"
spec = importlib.util.spec_from_file_location("monitor", SCRIPT)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def test_a_failure_is_reported_only_once_and_its_recovery_once():
    state = {}
    down = {"connector": (False, "The MT5 connector is unreachable")}
    assert len(monitor.run(state, down)) == 1
    for _ in range(5):
        assert monitor.run(state, down) == [], "an ongoing failure must not repeat every 5 minutes"
    [recovered] = monitor.run(state, {"connector": (True, "")})
    assert "recovered" in recovered
    assert monitor.run(state, {"connector": (True, "")}) == []


def test_a_healthy_start_sends_nothing():
    assert monitor.run({}, {"backend": (True, ""), "connector": (True, "")}) == []


def test_no_connector_address_is_written_in_the_scripts():
    for script in (SCRIPT, SCRIPT.parent / "generate_master_report.py"):
        text = script.read_text()
        assert not re.search(r'CONNECTOR_URL\s*=\s*(os\.getenv\([^)]*,\s*)?"http', text), script.name


def test_every_message_names_the_instance():
    [message] = monitor.run({}, {"backend": (False, "Backend down")})
    assert monitor.LABEL in message
