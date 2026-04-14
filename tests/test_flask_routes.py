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

    def test_network_block_present(self, app_client, monkeypatch):
        import app as app_module
        from runtime import poll_registers_once

        monkeypatch.setattr(
            app_module,
            "get_network_snapshot",
            lambda: {
                "connected": True,
                "ssid": "Plant WiFi",
                "interface": "wlan0",
                "signal_dbm": -58,
                "signal_quality": "Medium",
                "signal_percent": 72,
                "updated_utc": "2026-04-14T00:00:00+00:00",
                "error": None,
            },
        )
        poll_registers_once()

        resp = app_client.get("/api/temp")
        data = resp.get_json()

        assert resp.status_code == 200
        assert data["network"]["connected"] is True
        assert data["network"]["ssid"] == "Plant WiFi"
        assert data["network"]["signal_dbm"] == -58
        assert data["network"]["signal_quality"] == "Medium"

    def test_network_block_serializes_disconnected_wifi(self, app_client, monkeypatch):
        import app as app_module
        from runtime import poll_registers_once

        monkeypatch.setattr(
            app_module,
            "get_network_snapshot",
            lambda: {
                "connected": False,
                "ssid": None,
                "interface": None,
                "signal_dbm": None,
                "signal_quality": None,
                "signal_percent": None,
                "updated_utc": "2026-04-14T00:00:00+00:00",
                "error": None,
            },
        )
        poll_registers_once()

        resp = app_client.get("/api/temp")
        data = resp.get_json()

        assert resp.status_code == 200
        assert data["network"]["connected"] is False
        assert data["network"]["ssid"] is None
        assert data["network"]["error"] is None

    def test_prefers_canonical_resource_over_stale_dashboard_field(self, app_client):
        import runtime

        with runtime.cache_lock:
            runtime.cache.temp_raw = 249
            runtime.cache.temp_c = 24.9
            runtime.cache.last_error = None
            runtime.cache.info_cyl1_status = 2
            runtime.cache.resource_values.pop(runtime.INFO_CYL1_STATUS_RESOURCE_KEY, None)
        runtime.cache_resource_value(
            runtime.INFO_CYL1_STATUS_RESOURCE_KEY,
            raw=3,
            value=3,
            source="modbus",
        )

        resp = app_client.get("/api/temp")
        data = resp.get_json()

        assert resp.status_code == 200
        assert data["info"]["cyl1_status"] == 3

    def test_exposes_resource_freshness_metadata(self, app_client):
        import runtime

        runtime.cache_resource_value(
            runtime.INFO_CYL1_STATUS_RESOURCE_KEY,
            raw=3,
            value=3,
            source="modbus",
        )

        resp = app_client.get("/api/temp")
        data = resp.get_json()
        metadata = data["resources"][runtime.INFO_CYL1_STATUS_RESOURCE_KEY]

        assert resp.status_code == 200
        assert metadata["updated_utc"]
        assert metadata["source"] == "modbus"
        assert metadata["error"] is None

    def test_info_block_error_falls_back_to_legacy_dashboard_value(self, app_client):
        import runtime

        with runtime.cache_lock:
            runtime.cache.temp_raw = 249
            runtime.cache.temp_c = 24.9
            runtime.cache.last_error = None
            runtime.cache.info_cyl1_status = 2
            runtime.cache.resource_values.pop(runtime.INFO_CYL1_STATUS_RESOURCE_KEY, None)
        runtime.cache_resource_value(
            runtime.INFO_CYL1_STATUS_RESOURCE_KEY,
            raw=3,
            value=3,
            source="poll",
        )
        with runtime.cache_lock:
            runtime.cache.info_cyl1_status = 2

        runtime._apply_info_block_error("info poll timeout")

        resp = app_client.get("/api/temp")
        data = resp.get_json()
        metadata = data["resources"][runtime.INFO_CYL1_STATUS_RESOURCE_KEY]

        assert resp.status_code == 200
        assert data["info"]["cyl1_status"] == 2
        assert data["info"]["error"] == "info poll timeout"
        assert metadata["source"] == "poll"
        assert metadata["error"] == "info poll timeout"


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

    def test_read_info_cylinder_status_updates_canonical_resource(self, app_client):
        import runtime

        with runtime.cache_lock:
            runtime.cache.resource_values.pop(runtime.INFO_CYL1_STATUS_RESOURCE_KEY, None)
            runtime.cache.menu_values.pop("6.2", None)
        runtime.client.set_holding_register(140, 3)

        resp = app_client.get("/api/menu-value?path=6.2&refresh=1")
        data = resp.get_json()
        cached = runtime.get_cached_resource_value(runtime.INFO_CYL1_STATUS_RESOURCE_KEY)

        assert resp.status_code == 200
        assert data["resource_key"] == runtime.INFO_CYL1_STATUS_RESOURCE_KEY
        assert data["value"] == 3
        assert cached is not None
        assert cached["value"] == 3
        assert cached["source"] == "modbus"

    def test_i136_aliases_share_canonical_resource_cache(self, app_client):
        import runtime

        with runtime.cache_lock:
            runtime.cache.resource_values.pop(runtime.INFO_HUMIDIFIER_STATUS_RESOURCE_KEY, None)
            runtime.cache.menu_values.pop("4.1", None)
            runtime.cache.menu_values.pop("3.3.1.1", None)
        runtime.client.set_holding_register(136, 6)

        read_resp = app_client.get("/api/menu-value?path=4.1&refresh=1")
        alias_resp = app_client.get("/api/menu-value?path=3.3.1.1")
        read_data = read_resp.get_json()
        alias_data = alias_resp.get_json()

        assert read_resp.status_code == 200
        assert alias_resp.status_code == 200
        assert read_data["resource_key"] == runtime.INFO_HUMIDIFIER_STATUS_RESOURCE_KEY
        assert alias_data["resource_key"] == runtime.INFO_HUMIDIFIER_STATUS_RESOURCE_KEY
        assert alias_data["cached"] is True
        assert alias_data["value"] == 6


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

    def test_manual_procedure_write_updates_i136_dashboard_resource(self, app_client):
        import runtime

        with runtime.cache_lock:
            runtime.cache.resource_values.pop(runtime.INFO_HUMIDIFIER_STATUS_RESOURCE_KEY, None)
            runtime.cache.info_humidifier_status = 2

        resp = app_client.post(
            "/api/menu-value",
            data=json.dumps({"path": "3.3.1.1", "value": 6}),
            content_type="application/json",
        )
        temp_resp = app_client.get("/api/temp")

        assert resp.status_code == 200
        assert resp.get_json()["resource_key"] == runtime.INFO_HUMIDIFIER_STATUS_RESOURCE_KEY
        assert runtime.client._holding_registers[136] == 6
        assert temp_resp.get_json()["info"]["humidifier_status"] == 6


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

    def test_valid_setpoint_updates_canonical_resource_cache(self, app_client):
        import runtime

        with runtime.cache_lock:
            runtime.cache.resource_values.pop(runtime.SETPOINT_RESOURCE_KEY, None)

        resp = app_client.post(
            "/api/setpoint",
            data=json.dumps({"temp_c": 28.0}),
            content_type="application/json",
        )
        cached = runtime.get_cached_resource_value(runtime.SETPOINT_RESOURCE_KEY)

        assert resp.status_code == 200
        assert cached is not None
        assert cached["raw"] == 280
        assert abs(cached["value"] - 28.0) < 0.1
        assert cached["source"] == "write"
        assert cached["error"] is None

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
    def test_footer_includes_network_status_element(self, app_client):
        resp = app_client.get("/")
        html = resp.data.decode("utf-8")

        assert resp.status_code == 200
        assert 'id="networkStatus"' in html

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
