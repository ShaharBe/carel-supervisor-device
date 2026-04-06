"""Tests for the runtime poll block readers."""

from __future__ import annotations

from datetime import datetime

import runtime


class TestReadTempBlock:
    def test_success(self):
        result = runtime.read_temp_block()

        assert result.ok is True
        assert result.error is None
        assert result.data == {"temp_raw": 249, "temp_c": 24.9}

    def test_captures_error(self, monkeypatch):
        monkeypatch.setattr(runtime, "modbus_connect_or_raise", lambda: None)

        def fail_read(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(runtime, "read_holding_registers", fail_read)

        result = runtime.read_temp_block()

        assert result.ok is False
        assert result.data is None
        assert isinstance(result.error, RuntimeError)
        assert "boom" in str(result.error)


class TestReadRtcBlock:
    def test_success(self):
        result = runtime.read_rtc_block()

        assert result.ok is True
        assert result.error is None
        assert isinstance(result.data["device_time"], datetime)
        assert isinstance(result.data["raw_year"], int)
        assert isinstance(result.data["weekday"], int)


class TestReadInfoBlock:
    def test_success(self):
        result = runtime.read_info_block()

        assert result.ok is True
        assert result.error is None
        assert isinstance(result.data["info_conductivity"], int)
        assert result.data["info_conductivity"] >= 0
        assert isinstance(result.data["info_cyl1_hours"], int)
        assert result.data["info_cyl1_hours"] >= 0
        assert isinstance(result.data["info_voltage_type"], int)
        assert result.data["info_voltage_type"] >= 0


class TestReadHumidifierStatusBlock:
    def test_success(self):
        result = runtime.read_humidifier_status_block()

        assert result.ok is True
        assert result.error is None
        assert isinstance(result.data["info_humidifier_status"], int)
        assert result.data["info_humidifier_status"] >= 0


class TestReadAlarmsBlock:
    def test_success(self):
        result = runtime.read_alarms_block()

        assert result.ok is True
        assert result.error is None
        assert result.data["alarms_has_active"] is False
        assert result.data["alarms_active"] == []
        assert result.data["alarms_skipped_active_count"] == 0


class TestReadCoilsBlock:
    def test_success(self):
        result = runtime.read_coils_block()

        assert result.ok is True
        assert result.error is None
        assert isinstance(result.data["humidifier_network_enabled"], bool)
        assert isinstance(result.data["cyl1_drain_on"], bool)
