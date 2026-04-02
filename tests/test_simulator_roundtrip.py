"""Tests for Modbus register/coil operations via SimulatorClient."""

from __future__ import annotations

from modbus_map import (
    ALARM_RESET_COIL,
    HUMIDIFIER_REMOTE_ONOFF_COIL,
    HUMIDIFIER_STATUS_ADDR,
    MAX_PRODUCTION_ADDR,
    PROP_BAND_ADDR,
    RTC_READ_ADDRS,
    RTC_WRITE_ADDRS,
    SETPOINT_ADDR,
    TEMP_ADDR,
    TEMP_SCALE,
    SETPOINT_SCALE,
)
from alarms import ALARM_CATALOG


# ── Temperature read ─────────────────────────────────────────────────────

class TestTemperatureRead:
    def test_default_temp_raw(self, simulator_client):
        rr = simulator_client.read_holding_registers(address=TEMP_ADDR, count=1)
        assert not rr.isError()
        assert rr.registers[0] == 249

    def test_default_temp_scaled(self, simulator_client):
        rr = simulator_client.read_holding_registers(address=TEMP_ADDR, count=1)
        assert rr.registers[0] / TEMP_SCALE == 24.9

    def test_preset_temp_read_back(self, simulator_client):
        simulator_client.set_holding_register(TEMP_ADDR, 300)
        rr = simulator_client.read_holding_registers(address=TEMP_ADDR, count=1)
        assert rr.registers[0] == 300


# ── Setpoint write + read-back ───────────────────────────────────────────

class TestSetpointRoundTrip:
    def test_default_setpoint(self, simulator_client):
        rr = simulator_client.read_holding_registers(address=SETPOINT_ADDR, count=1)
        assert rr.registers[0] == 280

    def test_write_and_read_back(self, simulator_client):
        wr = simulator_client.write_register(address=SETPOINT_ADDR, value=350)
        assert not wr.isError()
        rr = simulator_client.read_holding_registers(address=SETPOINT_ADDR, count=1)
        assert rr.registers[0] == 350
        assert rr.registers[0] / SETPOINT_SCALE == 35.0


# ── RTC read ─────────────────────────────────────────────────────────────

class TestRTCRead:
    def test_rtc_registers_populated(self, simulator_client):
        for label, addr in RTC_READ_ADDRS.items():
            rr = simulator_client.read_holding_registers(address=addr, count=1)
            assert not rr.isError(), f"RTC read failed for {label}"
            assert rr.registers[0] >= 0


# ── RTC shadow write + mirror ────────────────────────────────────────────

class TestRTCShadowMirror:
    def test_shadow_write_mirrors_to_read(self, simulator_client):
        simulator_client.write_register(address=RTC_WRITE_ADDRS["hour"], value=23)
        rr = simulator_client.read_holding_registers(address=RTC_READ_ADDRS["hour"], count=1)
        assert rr.registers[0] == 23

    def test_shadow_year_mirror(self, simulator_client):
        simulator_client.write_register(address=RTC_WRITE_ADDRS["year"], value=30)
        rr = simulator_client.read_holding_registers(address=RTC_READ_ADDRS["year"], count=1)
        assert rr.registers[0] == 30


# ── Alarm reset ──────────────────────────────────────────────────────────

class TestAlarmReset:
    def test_alarm_reset_clears_all_alarm_coils(self, simulator_client):
        # Preset: set the summary and one monitored alarm bit
        simulator_client.set_coil(ALARM_CATALOG.summary.address, True)
        first_monitored = ALARM_CATALOG.monitored[0]
        simulator_client.set_coil(first_monitored.address, True)

        # Verify they are set
        summary_rr = simulator_client.read_coils(address=ALARM_CATALOG.summary.address, count=1)
        assert summary_rr.bits[0] is True

        # Pulse alarm reset
        simulator_client.write_coil(address=ALARM_RESET_COIL, value=True)

        # All alarm coils should now be cleared
        summary_rr = simulator_client.read_coils(address=ALARM_CATALOG.summary.address, count=1)
        assert summary_rr.bits[0] is False
        monitored_rr = simulator_client.read_coils(address=first_monitored.address, count=1)
        assert monitored_rr.bits[0] is False


# ── Humidifier coil ──────────────────────────────────────────────────────

class TestHumidifierCoil:
    def test_humidifier_on_sets_status_to_0(self, simulator_client):
        simulator_client.write_coil(address=HUMIDIFIER_REMOTE_ONOFF_COIL, value=True)
        rr = simulator_client.read_holding_registers(address=HUMIDIFIER_STATUS_ADDR, count=1)
        assert rr.registers[0] == 0

    def test_humidifier_off_sets_status_to_2(self, simulator_client):
        simulator_client.write_coil(address=HUMIDIFIER_REMOTE_ONOFF_COIL, value=False)
        rr = simulator_client.read_holding_registers(address=HUMIDIFIER_STATUS_ADDR, count=1)
        assert rr.registers[0] == 2


# ── Coil preset + read ──────────────────────────────────────────────────

class TestCoilPresetAndRead:
    def test_preset_and_read(self, simulator_client):
        simulator_client.set_coil(100, True)
        rr = simulator_client.read_coils(address=100, count=1)
        assert rr.bits[0] is True

    def test_unset_coil_defaults_false(self, simulator_client):
        rr = simulator_client.read_coils(address=200, count=1)
        assert rr.bits[0] is False


# ── Disconnected client ─────────────────────────────────────────────────

class TestDisconnectedClient:
    def test_read_fails_when_disconnected(self, simulator_client):
        simulator_client.close()
        rr = simulator_client.read_holding_registers(address=TEMP_ADDR, count=1)
        assert rr.isError()

    def test_write_fails_when_disconnected(self, simulator_client):
        simulator_client.close()
        wr = simulator_client.write_register(address=SETPOINT_ADDR, value=100)
        assert wr.isError()
