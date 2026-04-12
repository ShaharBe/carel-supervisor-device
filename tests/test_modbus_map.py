"""Tests for modbus_map.py address conversion functions and derived constants."""

from __future__ import annotations

import pytest

from modbus_map import (
    ALARM_COIL_ALT,
    DEHUMIDIFIER_COIL_ALT,
    DRAIN_PUMP_COIL_ALT,
    FILL_VALVE_COIL_ALT,
    INFO_BLOCK1_START_ADDR,
    INFO_BLOCK2_START_ADDR,
    MANUAL_PROCEDURE_COIL_ALT,
    MAX_PRODUCTION_ADDR,
    POWER_CONTACTOR_COIL_ALT,
    PROP_BAND_ADDR,
    RTC_READ_START_ADDR,
    SETPOINT_ADDR,
    TEMP_ADDR,
    d_to_modbus_coil_addr,
    qmm_to_modbus_addr,
)


# ── qmm_to_modbus_addr ──────────────────────────────────────────────────

class TestQmmToModbusAddr:
    def test_register_1_yields_address_0(self):
        assert qmm_to_modbus_addr(1) == 0

    def test_register_20_yields_address_19(self):
        assert qmm_to_modbus_addr(20) == 19

    def test_register_165_yields_address_164(self):
        assert qmm_to_modbus_addr(165) == 164

    def test_large_register(self):
        assert qmm_to_modbus_addr(1000) == 999

    def test_raises_on_zero(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            qmm_to_modbus_addr(0)

    def test_raises_on_negative(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            qmm_to_modbus_addr(-5)


# ── d_to_modbus_coil_addr ───────────────────────────────────────────────

class TestDToModbusCoilAddr:
    def test_zero_passthrough(self):
        assert d_to_modbus_coil_addr(0) == 0

    def test_positive_passthrough(self):
        assert d_to_modbus_coil_addr(52) == 52

    def test_raises_on_negative(self):
        with pytest.raises(ValueError, match="must be >= 0"):
            d_to_modbus_coil_addr(-1)


# ── Derived constants ────────────────────────────────────────────────────

class TestDerivedConstants:
    def test_temp_addr(self):
        assert TEMP_ADDR == 1

    def test_setpoint_addr(self):
        assert SETPOINT_ADDR == 19

    def test_max_production_addr(self):
        assert MAX_PRODUCTION_ADDR == 14

    def test_prop_band_addr(self):
        assert PROP_BAND_ADDR == 20

    def test_rtc_read_start_addr(self):
        assert RTC_READ_START_ADDR == 153

    def test_info_block1_start_addr(self):
        assert INFO_BLOCK1_START_ADDR == 136

    def test_info_block2_start_addr(self):
        assert INFO_BLOCK2_START_ADDR == 165

    def test_manual_procedure_alt_coils(self):
        assert MANUAL_PROCEDURE_COIL_ALT == 70
        assert POWER_CONTACTOR_COIL_ALT == 71
        assert FILL_VALVE_COIL_ALT == 72
        assert DRAIN_PUMP_COIL_ALT == 73
        assert ALARM_COIL_ALT == 74
        assert DEHUMIDIFIER_COIL_ALT == 75
