# simulator.py
"""
Simple Modbus simulator for development and testing.
Simulates register reads/writes in memory - no actual Modbus communication.
Mimics the pymodbus ModbusSerialClient interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from alarms import ALARM_CATALOG
from modbus_map import (
    ALARM_RESET_COIL,
    DRAIN_CYL1_COIL,
    HUMIDIFIER_REMOTE_ONOFF_COIL,
    HUMIDIFIER_STATUS_ADDR,
    HUMIDIFIER_SUPERVISOR_ENABLE_COIL,
    MAX_PRODUCTION_ADDR,
    PROP_BAND_ADDR,
    RTC_READ_ADDRS,
    RTC_WRITE_ADDRS,
    RTC_WRITE_TO_READ_ADDR,
    SETPOINT_ADDR,
    TEMP_ADDR,
)


@dataclass
class SimulatedResponse:
    """Mimics a pymodbus response object."""

    registers: Optional[List[int]] = None
    bits: Optional[List[bool]] = None
    _is_error: bool = False
    _error_msg: str = ""

    def isError(self) -> bool:
        return self._is_error

    def __str__(self) -> str:
        if self._is_error:
            return f"SimulatedError: {self._error_msg}"
        if self.bits is not None:
            return f"SimulatedResponse(bits={self.bits})"
        return f"SimulatedResponse(registers={self.registers})"


class SimulatorClient:
    """
    Simulates a Modbus device by storing register values in memory.
    Implements the same interface as pymodbus ModbusSerialClient.
    """

    def __init__(self, **kwargs):
        # Accept the same kwargs as ModbusSerialClient but ignore them.
        self._holding_registers: Dict[int, int] = {}
        self._input_registers: Dict[int, int] = {}
        self._coils: Dict[int, bool] = {}
        self._connected = False
        self._initialize_defaults()
        print("[SIMULATOR] Client created (no real hardware)")

    def _initialize_defaults(self) -> None:
        """Set up default register values for simulation."""
        self._holding_registers[TEMP_ADDR] = 249
        self._holding_registers[MAX_PRODUCTION_ADDR] = 1000
        self._holding_registers[SETPOINT_ADDR] = 280
        self._holding_registers[PROP_BAND_ADDR] = 20

        self._set_device_rtc(datetime.now())
        self._input_registers[HUMIDIFIER_STATUS_ADDR] = 0
        self._coils[ALARM_CATALOG.summary.address] = False
        self._coils[ALARM_RESET_COIL] = False
        self._coils[HUMIDIFIER_REMOTE_ONOFF_COIL] = True
        self._coils[HUMIDIFIER_SUPERVISOR_ENABLE_COIL] = True
        self._coils[DRAIN_CYL1_COIL] = False

    def _set_device_rtc(self, value: datetime) -> None:
        """Populate both the readable RTC block and the editable shadow block."""
        carel_weekday = value.weekday() + 1
        encoded_year = value.year % 100

        self._holding_registers[RTC_READ_ADDRS["hour"]] = value.hour
        self._holding_registers[RTC_READ_ADDRS["minute"]] = value.minute
        self._holding_registers[RTC_READ_ADDRS["day"]] = value.day
        self._holding_registers[RTC_READ_ADDRS["month"]] = value.month
        self._holding_registers[RTC_READ_ADDRS["year"]] = encoded_year
        self._holding_registers[RTC_READ_ADDRS["weekday"]] = carel_weekday

        self._holding_registers[RTC_WRITE_ADDRS["weekday"]] = carel_weekday
        self._holding_registers[RTC_WRITE_ADDRS["hour"]] = value.hour
        self._holding_registers[RTC_WRITE_ADDRS["minute"]] = value.minute
        self._holding_registers[RTC_WRITE_ADDRS["day"]] = value.day
        self._holding_registers[RTC_WRITE_ADDRS["month"]] = value.month
        self._holding_registers[RTC_WRITE_ADDRS["year"]] = encoded_year

    def _mirror_rtc_shadow_write(self, address: int, value: int) -> None:
        """In simulator mode, shadow writes commit immediately for direct readback."""
        read_address = RTC_WRITE_TO_READ_ADDR.get(address)
        if read_address is not None:
            self._holding_registers[read_address] = value

    def _clear_alarm_coils(self) -> None:
        """Mimic the controller clearing its alarm bank after a reset pulse."""
        for definition in [ALARM_CATALOG.summary, *ALARM_CATALOG.monitored, *ALARM_CATALOG.skipped]:
            self._coils[definition.address] = False
        self._coils[ALARM_RESET_COIL] = False

    @property
    def connected(self) -> bool:
        """Check if client is connected."""
        return self._connected

    def connect(self) -> bool:
        """Simulate connection - always succeeds."""
        self._connected = True
        print("[SIMULATOR] Connected (simulated)")
        return True

    def close(self) -> None:
        """Simulate disconnection."""
        self._connected = False
        print("[SIMULATOR] Disconnected (simulated)")

    def read_holding_registers(
        self,
        address: int,
        count: int = 1,
        slave: int = 1,
        device_id: Optional[int] = None,
    ) -> SimulatedResponse:
        """Read holding registers from simulated memory."""
        if not self._connected:
            return SimulatedResponse(_is_error=True, _error_msg="Not connected")

        values = []
        for addr in range(address, address + count):
            values.append(self._holding_registers.get(addr, 0))

        print(f"[SIMULATOR] Read holding registers {address}-{address + count - 1}: {values}")
        return SimulatedResponse(registers=values)

    def read_input_registers(
        self,
        address: int,
        count: int = 1,
        slave: int = 1,
        device_id: Optional[int] = None,
    ) -> SimulatedResponse:
        """Read input registers from simulated memory."""
        if not self._connected:
            return SimulatedResponse(_is_error=True, _error_msg="Not connected")

        values = []
        for addr in range(address, address + count):
            values.append(self._input_registers.get(addr, 0))

        print(f"[SIMULATOR] Read input registers {address}-{address + count - 1}: {values}")
        return SimulatedResponse(registers=values)

    def read_coils(
        self,
        address: int,
        count: int = 1,
        slave: int = 1,
        device_id: Optional[int] = None,
    ) -> SimulatedResponse:
        """Read coil bits from simulated memory."""
        if not self._connected:
            return SimulatedResponse(_is_error=True, _error_msg="Not connected")

        values = []
        for addr in range(address, address + count):
            values.append(bool(self._coils.get(addr, False)))

        print(f"[SIMULATOR] Read coils {address}-{address + count - 1}: {values}")
        return SimulatedResponse(bits=values)

    def write_register(
        self,
        address: int,
        value: int,
        slave: int = 1,
        device_id: Optional[int] = None,
    ) -> SimulatedResponse:
        """Write a single holding register to simulated memory."""
        if not self._connected:
            return SimulatedResponse(_is_error=True, _error_msg="Not connected")

        self._holding_registers[address] = value
        self._mirror_rtc_shadow_write(address, value)
        print(f"[SIMULATOR] Write register {address} = {value}")
        return SimulatedResponse(registers=[value])

    def write_registers(
        self,
        address: int,
        values: List[int],
        slave: int = 1,
        device_id: Optional[int] = None,
    ) -> SimulatedResponse:
        """Write multiple holding registers to simulated memory."""
        if not self._connected:
            return SimulatedResponse(_is_error=True, _error_msg="Not connected")

        for i, value in enumerate(values):
            register_address = address + i
            self._holding_registers[register_address] = value
            self._mirror_rtc_shadow_write(register_address, value)

        print(f"[SIMULATOR] Write registers {address}-{address + len(values) - 1} = {values}")
        return SimulatedResponse(registers=values)

    def write_coil(
        self,
        address: int,
        value: bool,
        slave: int = 1,
        device_id: Optional[int] = None,
    ) -> SimulatedResponse:
        """Write a single coil bit to simulated memory."""
        if not self._connected:
            return SimulatedResponse(_is_error=True, _error_msg="Not connected")

        self._coils[address] = bool(value)
        if address == HUMIDIFIER_REMOTE_ONOFF_COIL:
            self._input_registers[HUMIDIFIER_STATUS_ADDR] = 0 if bool(value) else 2
        if address == ALARM_RESET_COIL and value:
            self._clear_alarm_coils()
        print(f"[SIMULATOR] Write coil {address} = {bool(value)}")
        return SimulatedResponse(bits=[bool(value)])

    # -------------------------
    # Helper methods for testing
    # -------------------------

    def set_input_register(self, address: int, value: int) -> None:
        """Helper to set input register values for testing scenarios."""
        self._input_registers[address] = value

    def set_holding_register(self, address: int, value: int) -> None:
        """Helper to preset holding register values."""
        self._holding_registers[address] = value

    def set_coil(self, address: int, value: bool) -> None:
        """Helper to preset coil values for simulated alarm/control scenarios."""
        self._coils[address] = bool(value)

    def get_all_registers(self) -> Dict[str, Dict[int, int]]:
        """Debug helper to see all register states."""
        return {
            "holding": dict(self._holding_registers),
            "input": dict(self._input_registers),
            "coils": {addr: int(value) for addr, value in self._coils.items()},
        }
