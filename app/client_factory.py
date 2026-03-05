# client_factory.py
"""
Factory to create either real ModbusSerialClient or SimulatorClient based on configuration.

Usage:
  Set USE_SIMULATOR=1 environment variable to use simulator mode.
  Otherwise, uses real Modbus hardware.

Examples:
  # Windows CMD
  set USE_SIMULATOR=1
  python app.py

  # PowerShell
  $env:USE_SIMULATOR="1"
  python app.py

  # One-liner (CMD)
  set USE_SIMULATOR=1 && python app.py
"""

import os


def is_simulator_mode() -> bool:
    """Check if simulator mode is enabled via environment variable."""
    return os.environ.get('USE_SIMULATOR', '0') == '1'


def create_modbus_client(
    port: str,
    baudrate: int,
    parity: str,
    stopbits: int,
    bytesize: int,
    timeout: float = 1.0,
    retries: int = 1,
):
    """
    Create and return the appropriate Modbus client based on USE_SIMULATOR env var.

    Parameters are passed to the real ModbusSerialClient when not in simulator mode.
    In simulator mode, parameters are ignored (no real hardware).
    """
    if is_simulator_mode():
        from simulator import SimulatorClient
        print("=" * 50)
        print("*** RUNNING IN SIMULATOR MODE ***")
        print("*** No real hardware communication ***")
        print("=" * 50)
        return SimulatorClient(
            port=port,
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            bytesize=bytesize,
            timeout=timeout,
            retries=retries,
        )
    else:
        from pymodbus.client import ModbusSerialClient
        print("=" * 50)
        print("*** RUNNING WITH REAL HARDWARE ***")
        print(f"*** Port: {port} @ {baudrate} baud ***")
        print("=" * 50)
        return ModbusSerialClient(
            port=port,
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            bytesize=bytesize,
            timeout=timeout,
            retries=retries,
        )
