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

    def test_apply_temp_block_updates_resource_cache(self):
        with runtime.cache_lock:
            runtime.cache.resource_values.pop(runtime.TEMP_RESOURCE_KEY, None)

        runtime._apply_temp_block({"temp_raw": 236, "temp_c": 23.6})
        cached = runtime.get_cached_resource_value(runtime.TEMP_RESOURCE_KEY)

        assert cached is not None
        assert cached["raw"] == 236
        assert cached["value"] == 23.6
        assert cached["source"] == "poll"
        assert cached["error"] is None


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

    def test_maps_documented_info_registers_without_off_by_one_shift(self):
        client = runtime.client
        original_registers = dict(client._holding_registers)

        try:
            # Block 1 starts at documented I,136 and block 2 at I,165.
            client._holding_registers[136] = 2
            client._holding_registers[137] = 321
            client._holding_registers[138] = 100
            client._holding_registers[139] = 0
            client._holding_registers[140] = 11
            client._holding_registers[141] = 6
            client._holding_registers[142] = 12
            client._holding_registers[165] = 123
            client._holding_registers[166] = 456
            client._holding_registers[167] = 4

            result = runtime.read_info_block()

            assert result.ok is True
            assert result.data["info_conductivity"] == 321
            assert result.data["info_cyl1_phase"] == 0
            assert result.data["info_cyl1_status"] == 11
            assert result.data["info_cyl2_phase"] == 6
            assert result.data["info_cyl2_status"] == 12
            assert result.data["info_cyl1_hours"] == 123
            assert result.data["info_cyl2_hours"] == 456
            assert result.data["info_voltage_type"] == 4
        finally:
            client._holding_registers.clear()
            client._holding_registers.update(original_registers)

    def test_apply_info_block_updates_resource_cache_and_clears_error(self):
        runtime.cache_resource_error(runtime.INFO_CYL1_STATUS_RESOURCE_KEY, "old error")

        runtime._apply_info_block(
            {
                "info_conductivity": 321,
                "info_cyl1_phase": 0,
                "info_cyl1_status": 11,
                "info_cyl2_phase": 6,
                "info_cyl2_status": 12,
                "info_cyl1_hours": 123,
                "info_cyl2_hours": 456,
                "info_voltage_type": 4,
            }
        )

        cached = runtime.get_cached_resource_value(runtime.INFO_CYL1_STATUS_RESOURCE_KEY)

        assert cached is not None
        assert cached["value"] == 11
        assert cached["error"] is None
        assert cached["source"] == "poll"


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
