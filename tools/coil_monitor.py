#!/usr/bin/env python3
"""
Reverse engineering tool: Monitor coils for changes.
Polls a range of coils and reports when any value changes.

Usage (on Pi):
  # First stop the service to free the serial port
  sudo systemctl stop carel-supervisor

  # Run with venv Python (unbuffered for real-time output)
  /opt/carel-supervisor/venv/bin/python -u /opt/carel-supervisor/tools/coil_monitor.py

  # Or with options:
  /opt/carel-supervisor/venv/bin/python -u /opt/carel-supervisor/tools/coil_monitor.py --interval 0.3

  # When done, restart the service
  sudo systemctl start carel-supervisor

Press Ctrl+C to stop.
"""

import argparse
import sys
import time
from typing import Dict, List, Optional

from pymodbus.client import ModbusSerialClient

# Coil definitions (D address -> description)
# D addresses are Modbus-aligned per CAREL docs
COIL_DEFS: Dict[int, str] = {
    42: "drain for strong demand reduction",
    43: "long-inactivity drain",
    44: "total periodical flush",
    45: "dehumidification",
    46: "dilution drain with contactor opened",
    47: "warnings for pre-exhaustion and complete exhaustion",
    48: "cylinders in parallel or series (0=parallel, 1=series)",
    49: "cylinder 1: reset hour counter",
    50: "cylinder 2: reset hour counter",
    51: "alarms reset",
    52: "cylinder 1: manual drain",
    53: "cylinder 2: manual drain",
    54: "cylinder 1: cleaning cycle",
    55: "cylinder 2: cleaning cycle",
    80: "enabling control supervisor",
    81: "enabling ON-OFF from supervisor",
}

# All coil addresses to monitor
COIL_ADDRS = sorted(COIL_DEFS.keys())


def read_coils(client: ModbusSerialClient, addrs: List[int], slave_id: int) -> Dict[int, bool]:
    """Read coils at specified addresses, return {addr: value}."""
    result = {}
    # Group into contiguous ranges for efficiency
    if not addrs:
        return result

    # For simplicity, read each address individually (small count)
    # Could optimize with range reads if needed
    for addr in addrs:
        try:
            # Try device_id first (newer pymodbus), fall back to slave
            try:
                rr = client.read_coils(address=addr, count=1, device_id=slave_id)
            except TypeError:
                rr = client.read_coils(address=addr, count=1, slave=slave_id)
            if rr.isError():
                print(f"  [WARN] Read error at D,{addr}: {rr}", file=sys.stderr)
                continue
            result[addr] = bool(rr.bits[0]) if rr.bits else False
        except Exception as e:
            print(f"  [WARN] Exception reading D,{addr}: {e}", file=sys.stderr)
    return result


def format_coil(addr: int, value: bool) -> str:
    """Format coil address and value with description."""
    desc = COIL_DEFS.get(addr, "unknown")
    return f"D,{addr} = {1 if value else 0}  ({desc})"


def main():
    parser = argparse.ArgumentParser(description="Monitor coils for changes")
    parser.add_argument("--port", default="/dev/ttyACM0", help="Serial port")
    parser.add_argument("--baudrate", type=int, default=9600, help="Baud rate")
    parser.add_argument("--slave", type=int, default=1, help="Modbus slave ID")
    parser.add_argument("--interval", type=float, default=0.5, help="Poll interval (seconds)")
    args = parser.parse_args()

    print(f"Connecting to {args.port} @ {args.baudrate} baud, slave {args.slave}")
    print(f"Monitoring {len(COIL_ADDRS)} coils: D,{min(COIL_ADDRS)}..D,{max(COIL_ADDRS)}")
    print(f"Poll interval: {args.interval}s")
    print("-" * 60)

    client = ModbusSerialClient(
        port=args.port,
        baudrate=args.baudrate,
        parity="N",
        stopbits=1,
        bytesize=8,
        timeout=1.0,
    )

    if not client.connect():
        print(f"ERROR: Failed to connect to {args.port}", file=sys.stderr)
        sys.exit(1)

    print("Connected. Reading initial state...")

    prev_state: Optional[Dict[int, bool]] = None
    poll_count = 0

    try:
        while True:
            curr_state = read_coils(client, COIL_ADDRS, args.slave)
            poll_count += 1

            if prev_state is None:
                # First read - print all values
                print(f"\nInitial state (poll #{poll_count}):")
                for addr in COIL_ADDRS:
                    if addr in curr_state:
                        print(f"  {format_coil(addr, curr_state[addr])}")
                print("\nWaiting for changes... (Ctrl+C to stop)")
            else:
                # Compare and report changes
                for addr in COIL_ADDRS:
                    old_val = prev_state.get(addr)
                    new_val = curr_state.get(addr)
                    if old_val is not None and new_val is not None and old_val != new_val:
                        timestamp = time.strftime("%H:%M:%S")
                        print(f"[{timestamp}] CHANGED: {format_coil(addr, new_val)}  (was {1 if old_val else 0})")

            prev_state = curr_state
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n\nStopped by user.")
    finally:
        client.close()
        print(f"Total polls: {poll_count}")


if __name__ == "__main__":
    main()
