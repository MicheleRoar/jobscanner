import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module


def test_scan_state_starts_idle():
    app_module.reset_scan_state()

    state = app_module.get_scan_state()

    assert state["running"] is False
    assert state["message"] == ""


def test_scan_state_updates_when_scan_starts():
    app_module.reset_scan_state()

    app_module.set_scan_state(True, "Scan in progress...")

    state = app_module.get_scan_state()
    assert state["running"] is True
    assert "Scan in progress" in state["message"]
