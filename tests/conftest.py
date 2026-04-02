"""Shared pytest fixtures for the carel-supervisor-device test suite."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Make the app/ package importable without installing it.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# Force simulator mode *before* any app module touches the env var.
os.environ["USE_SIMULATOR"] = "1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def simulator_client():
    """Return a fresh, connected SimulatorClient for each test."""
    from simulator import SimulatorClient

    client = SimulatorClient()
    client.connect()
    yield client
    client.close()


@pytest.fixture()
def menu_root():
    """Return the parsed display_menu.json root node."""
    json_path = APP_DIR / "data" / "display_menu.json"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return data


@pytest.fixture()
def alarm_catalog():
    """Return the loaded AlarmCatalog from the CSV."""
    from alarms import load_alarm_catalog

    return load_alarm_catalog()


@pytest.fixture()
def app_client():
    """Return a Flask test client wired to the simulator backend.

    The background poller is *not* started so tests control all Modbus I/O.
    """
    from app import app

    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client
