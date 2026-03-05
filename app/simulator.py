# simulator.py
"""
Simple Modbus simulator for development and testing.
Simulates register reads/writes in memory - no actual Modbus communication.
Mimics the pymodbus ModbusSerialClient interface.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional


@dataclass
class SimulatedResponse:
    """
    Mimics pymodbus response object.
    """
    registers: Optional[List[int]] = None
    _is_error: bool = False
    _error_msg: str = ""

    def isError(self) -> bool:
        return self._is_error

    def __str__(self) -> str:
        if self._is_error:
            return f"SimulatedError: {self._error_msg}"
        return f"SimulatedResponse(registers={self.registers})"


class SimulatorClient:
    """
    Simulates a Modbus device by storing register values in memory.
    Implements the same interface as pymodbus ModbusSerialClient.
    """

    def __init__(self, **kwargs):
        # Accept same kwargs as ModbusSerialClient but ignore them
        self._holding_registers: Dict[int, int] = {}
        self._input_registers: Dict[int, int] = {}
        self._connected = False
        self._initialize_defaults()
        print("[SIMULATOR] Client created (no real hardware)")

    def _initialize_defaults(self):
        """Set up default register values for simulation."""
        # Temperature register (addr 1, 0-based) = 249 -> 24.9°C
        self._holding_registers[1] = 249
        # Setpoint register (addr 19, 0-based) = 280 -> 28.0°C
        self._holding_registers[19] = 280

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

    def read_holding_registers(self, address: int, count: int = 1, slave: int = 1) -> SimulatedResponse:
        """Read holding registers from simulated memory."""
        if not self._connected:
            return SimulatedResponse(_is_error=True, _error_msg="Not connected")

        values = []
        for addr in range(address, address + count):
            values.append(self._holding_registers.get(addr, 0))

        print(f"[SIMULATOR] Read holding registers {address}-{address+count-1}: {values}")
        return SimulatedResponse(registers=values)

    def read_input_registers(self, address: int, count: int = 1, slave: int = 1) -> SimulatedResponse:
        """Read input registers from simulated memory."""
        if not self._connected:
            return SimulatedResponse(_is_error=True, _error_msg="Not connected")

        values = []
        for addr in range(address, address + count):
            values.append(self._input_registers.get(addr, 0))

        print(f"[SIMULATOR] Read input registers {address}-{address+count-1}: {values}")
        return SimulatedResponse(registers=values)

    def write_register(self, address: int, value: int, slave: int = 1) -> SimulatedResponse:
        """Write a single holding register to simulated memory."""
        if not self._connected:
            return SimulatedResponse(_is_error=True, _error_msg="Not connected")

        self._holding_registers[address] = value
        print(f"[SIMULATOR] Write register {address} = {value}")
        return SimulatedResponse(registers=[value])

    def write_registers(self, address: int, values: List[int], slave: int = 1) -> SimulatedResponse:
        """Write multiple holding registers to simulated memory."""
        if not self._connected:
            return SimulatedResponse(_is_error=True, _error_msg="Not connected")

        for i, value in enumerate(values):
            self._holding_registers[address + i] = value

        print(f"[SIMULATOR] Write registers {address}-{address+len(values)-1} = {values}")
        return SimulatedResponse(registers=values)

    # -------------------------
    # Helper methods for testing
    # -------------------------

    def set_input_register(self, address: int, value: int) -> None:
        """Helper to set input register values for testing scenarios."""
        self._input_registers[address] = value

    def set_holding_register(self, address: int, value: int) -> None:
        """Helper to preset holding register values."""
        self._holding_registers[address] = value

    def get_all_registers(self) -> Dict[str, Dict[int, int]]:
        """Debug helper to see all register states."""
        return {
            'holding': dict(self._holding_registers),
            'input': dict(self._input_registers)
        }
