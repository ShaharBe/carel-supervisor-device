"""Tests for Flask API endpoints using the simulator backend."""

from __future__ import annotations

import json

import pytest


# ── GET /api/temp ────────────────────────────────────────────────────────

class TestApiTemp:
    def test_returns_ok_with_temp(self, app_client):
        # Trigger one poll so the cache has data.
        from runtime import poll_registers_once
        poll_registers_once()

        resp = app_client.get("/api/temp")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert isinstance(data["temp_c"], (int, float))

    def test_config_block_present(self, app_client):
        from runtime import poll_registers_once
        poll_registers_once()

        resp = app_client.get("/api/temp")
        data = resp.get_json()
        assert "config" in data
        assert "com_port" in data["config"]
        assert "baudrate" in data["config"]


# ── GET /api/menu-value ──────────────────────────────────────────────────

class TestApiMenuValueGet:
    def test_read_setpoint(self, app_client):
        resp = app_client.get("/api/menu-value?path=2.1&refresh=1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["path"] == "2.1"
        assert "value" in data

    def test_nonexistent_path_returns_404(self, app_client):
        resp = app_client.get("/api/menu-value?path=99.99.99")
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["ok"] is False

    def test_missing_path_returns_400(self, app_client):
        resp = app_client.get("/api/menu-value?path=")
        assert resp.status_code == 400

    def test_resolved_editor_in_response(self, app_client):
        resp = app_client.get("/api/menu-value?path=2.1&refresh=1")
        assert resp.status_code == 200
        data = resp.get_json()
        editor = data["resolved_editor"]
        assert editor["type"] == "float"
        assert editor["modbus_backed"] is True
        assert editor["writable"] is True
        assert editor["editable"] is True
        assert editor["scale"] == 10.0
        assert editor["limits"]["low"] == -20.0
        assert editor["limits"]["high"] == 100.0

    def test_resolved_editor_boolean(self, app_client):
        resp = app_client.get("/api/menu-value?path=2.2&refresh=1")
        assert resp.status_code == 200
        data = resp.get_json()
        editor = data["resolved_editor"]
        assert editor["type"] == "boolean"
        assert len(editor["options"]) == 2

    def test_signed_probe_offset_decodes_two_complement_word(self, app_client):
        import runtime

        runtime.client.set_holding_register(4, 65535)
        resp = app_client.get("/api/menu-value?path=3.2.2.4&refresh=1")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["raw"] == 65535
        assert abs(data["value"] - -0.1) < 0.01
        assert data["resolved_editor"]["signed"] is True
        assert data["resolved_editor"]["scale"] == 10.0

    def test_signed_probe_min_decodes_integer_word(self, app_client):
        import runtime

        runtime.client.set_holding_register(2, 65535)
        resp = app_client.get("/api/menu-value?path=3.2.2.2&refresh=1")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["raw"] == 65535
        assert data["value"] == -1
        assert data["resolved_editor"]["type"] == "integer"


# ── POST /api/menu-value ─────────────────────────────────────────────────

class TestApiMenuValuePost:
    def test_write_setpoint(self, app_client):
        resp = app_client.post(
            "/api/menu-value",
            data=json.dumps({"path": "2.1", "value": 25.0}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["path"] == "2.1"

    def test_missing_path_returns_400(self, app_client):
        resp = app_client.post(
            "/api/menu-value",
            data=json.dumps({"value": 25.0}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_value_returns_400(self, app_client):
        resp = app_client.post(
            "/api/menu-value",
            data=json.dumps({"path": "2.1"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_nonexistent_path_returns_404(self, app_client):
        resp = app_client.post(
            "/api/menu-value",
            data=json.dumps({"path": "99.99", "value": 1}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_signed_probe_offset_encodes_two_complement_word(self, app_client):
        import runtime

        resp = app_client.post(
            "/api/menu-value",
            data=json.dumps({"path": "3.2.2.4", "value": -1.0}),
            content_type="application/json",
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["raw"] == 65526
        assert abs(data["value"] - -1.0) < 0.01
        assert runtime.client._holding_registers[4] == 65526

    def test_signed_probe_min_encodes_integer_word(self, app_client):
        import runtime

        resp = app_client.post(
            "/api/menu-value",
            data=json.dumps({"path": "3.2.2.2", "value": -1}),
            content_type="application/json",
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["raw"] == 65535
        assert data["value"] == -1
        assert runtime.client._holding_registers[2] == 65535


# ── POST /api/setpoint ───────────────────────────────────────────────────

class TestApiSetpoint:
    def test_valid_setpoint(self, app_client):
        resp = app_client.post(
            "/api/setpoint",
            data=json.dumps({"temp_c": 28.0}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert abs(data["temp_c"] - 28.0) < 0.1

    def test_out_of_range_returns_400(self, app_client):
        resp = app_client.post(
            "/api/setpoint",
            data=json.dumps({"temp_c": 999}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False


# ── POST /api/device-datetime ────────────────────────────────────────────

class TestApiDeviceDatetime:
    def test_write_and_readback(self, app_client):
        resp = app_client.post(
            "/api/device-datetime",
            data=json.dumps({"datetime_local": "2026-04-02T14:30"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "2026-04-02" in data["device_time_iso_local"]

    def test_missing_field_returns_400(self, app_client):
        resp = app_client.post(
            "/api/device-datetime",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400


# ── POST /api/humidifier-toggle ──────────────────────────────────────────

class TestApiHumidifierToggle:
    def test_toggle_returns_ok(self, app_client):
        resp = app_client.post(
            "/api/humidifier-toggle",
            data=json.dumps({"on": False}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["humidifier_network_enabled"] is False


# ── POST /api/alarms-reset ───────────────────────────────────────────────

class TestApiAlarmsReset:
    def test_reset_returns_ok(self, app_client):
        resp = app_client.post("/api/alarms-reset")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True


# ── POST /api/reboot ─────────────────────────────────────────────────────

class TestApiReboot:
    def test_reboot_fails_in_simulator(self, app_client):
        resp = app_client.post("/api/reboot")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert "simulator" in data["error"].lower() or "disabled" in data["error"].lower()


# ── GET / (page payload includes dashboard_sync_map) ─────────────────────

class TestIndexPayload:
    def test_dashboard_sync_map_in_page(self, app_client):
        resp = app_client.get("/")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        # The menu payload is embedded as JSON in a <script> tag.
        import re
        match = re.search(
            r'<script id="displayMenuData"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        assert match, "displayMenuData script tag not found"
        payload = json.loads(match.group(1))
        assert payload["ok"] is True
        sync_map = payload["dashboard_sync_map"]
        assert isinstance(sync_map, dict)
        assert sync_map["2.1"] == "last_setpoint_c"
        assert sync_map["2.2"] == "info.humidifier_network_enabled"
        assert sync_map["4.1"] == "info.humidifier_status"
        assert len(sync_map) == 10
