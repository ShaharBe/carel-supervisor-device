#!/usr/bin/env python3
"""
Reverse engineering tool: Monitor coils for changes.
Polls a range of coils and reports when any value changes.

Usage (on Pi):
  # First stop the service to free the serial port
  sudo systemctl stop carel-supervisor

  # Run with venv Python (unbuffered for real-time output)
  /opt/carel-supervisor/venv/bin/python -u /opt/carel-supervisor/repo/tools/coil_monitor.py

  # Or with options:
  /opt/carel-supervisor/venv/bin/python -u /opt/carel-supervisor/repo/tools/coil_monitor.py --interval 0.3
    
Interactive control:
  - While running, type a coil address and press Enter to toggle it if it is marked R/W.
  - Accepted formats: 42, D42, D,42
  - Read-only coils are monitored but not written.
  - Unknown addresses outside the default monitored set are still read first,
    then inverted and written back.

  # When done, restart the service
  sudo systemctl start carel-supervisor

Press Ctrl+C to stop.
"""

import argparse
import select
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from pymodbus.client import ModbusSerialClient

@dataclass(frozen=True)
class CoilDef:
    address: int
    writable: bool
    description: str

    @property
    def label(self) -> str:
        return f"D,{self.address}"


COIL_DEFS: Sequence[CoilDef] = (
    CoilDef(7, True, "measure system: 0 = international system, 1 = british system"),
    CoilDef(8, True, "remote on/off from network: 0 = ue disabled, 1 = ue enabled"),
    CoilDef(9, False, "at least 1 alarm is active"),
    CoilDef(10, False, "humidistat status: 0 = open, 1 = closed"),
    CoilDef(11, False, "remote on/off status: 0 = open = ue disabled, 1 = closed = ue enabled"),
    CoilDef(12, False, "high conductivity alarm"),
    CoilDef(13, False, "high conductivity warning"),
    CoilDef(14, False, "cylinder 1: high current alarm"),
    CoilDef(15, False, "cylinder 1: low current alarm"),
    CoilDef(16, False, "cylinder 1: lack of water alarm"),
    CoilDef(17, False, "cylinder 1: low production alarm"),
    CoilDef(18, False, "cylinder 1: drain alarm"),
    CoilDef(19, False, "cylinder 1: full without demand alarm"),
    CoilDef(20, False, "cylinder 1: maintenance-due alarm (timed) (MN alarm)"),
    CoilDef(21, False, "cylinder 1: pre-exhaustion warning (CP alarm)"),
    CoilDef(22, False, "cylinder 1: foam warning"),
    CoilDef(23, False, "cylinder 1: totally exhausted warning (CL alarm)"),
    CoilDef(24, False, "cylinder 1: maintenance-due warning (timed) (CY alarm)"),
    CoilDef(25, False, "cylinder 2: high current alarm"),
    CoilDef(26, False, "cylinder 2: low current alarm"),
    CoilDef(27, False, "cylinder 2: lack of water alarm"),
    CoilDef(28, False, "cylinder 2: low production alarm"),
    CoilDef(29, False, "cylinder 2: drain alarm"),
    CoilDef(30, False, "cylinder 2: full without demand alarm"),
    CoilDef(31, False, "cylinder 2: maintenance-due alarm"),
    CoilDef(32, False, "cylinder 2: pre-exhaustion warning"),
    CoilDef(33, False, "cylinder 2: foam warning"),
    CoilDef(34, False, "cylinder 2: totally exhausted warning"),
    CoilDef(35, False, "cylinder 2: maintenance-due warning"),
    CoilDef(
        36,
        False,
        "warning: room probe/external regulator not properly connected "
        "(only when their signal is 2-10 V or 4-20 mA)",
    ),
    CoilDef(
        37,
        False,
        "warning: limit probe not properly connected "
        "(only when their signal is 2-10 V or 4-20 mA)",
    ),
    CoilDef(38, False, "room high humidity warning"),
    CoilDef(39, False, "room low humidity warning"),
    CoilDef(40, False, "limit probe high humidity warning"),
    CoilDef(41, False, "hour counter alarm (FAULT CLOCK CARD ALARM)"),
    CoilDef(42, True, "drain for strong demand reduction"),
    CoilDef(43, True, "long-inactivity drain"),
    CoilDef(44, True, "total periodical flush"),
    CoilDef(45, True, "dehumidification"),
    CoilDef(46, True, "dilution drain with contactor opened"),
    CoilDef(47, True, "warnings for pre-exhaustion and complete exhaustion"),
    CoilDef(48, True, "cylinders in parallel or series (0=parallel, 1=series)"),
    CoilDef(49, True, "cylinder 1: reset hour counter"),
    CoilDef(50, True, "cylinder 2: reset hour counter"),
    CoilDef(51, True, "alarms reset"),
    CoilDef(52, True, "cylinder 1: manual drain"),
    CoilDef(53, True, "cylinder 2: manual drain"),
    CoilDef(54, True, "cylinder 1: cleaning cycle"),
    CoilDef(55, True, "cylinder 2: cleaning cycle"),
    CoilDef(71, True, "Power Contactor"), # Undocumented
    CoilDef(72, True, "Fill Valve"), # Undocumented
    CoilDef(73, True, "Drain Pump"), # Undocumented
    CoilDef(74, True, "Alarm"), # Undocumented   
    CoilDef(75, True, "Dehumidifier"), # Undocumented
    CoilDef(80, True, "enabling control supervisor"),
    CoilDef(81, True, "enabling ON-OFF from supervisor"),
)

COIL_BY_ADDR: Dict[int, CoilDef] = {definition.address: definition for definition in COIL_DEFS}

# All coil addresses to monitor
COIL_ADDRS = sorted(COIL_BY_ADDR.keys())


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
    definition = COIL_BY_ADDR.get(addr)
    if definition is None:
        return f"D,{addr} = {1 if value else 0}  [unknown]  (unknown)"
    access = "R/W" if definition.writable else "R"
    return f"{definition.label} = {1 if value else 0}  [{access}]  ({definition.description})"


def parse_coil_address(text: str) -> Optional[int]:
    """Parse user-entered coil address (e.g. '42' or 'D,42')."""
    raw = text.strip()
    if not raw:
        return None
    upper = raw.upper().replace(" ", "")
    if upper.startswith("D,"):
        upper = upper[2:]
    elif upper.startswith("D"):
        upper = upper[1:]
    try:
        addr = int(upper)
    except ValueError:
        return None
    return addr if addr >= 0 else None


def toggle_coil(client: ModbusSerialClient, addr: int, slave_id: int) -> Optional[bool]:
    """Read a coil, invert it, and write the new value. Returns new value or None on failure."""
    definition = COIL_BY_ADDR.get(addr)
    if definition is not None and not definition.writable:
        print(f"  [WARN] {definition.label} is read-only; toggle skipped.", file=sys.stderr)
        return None

    state = read_coils(client, [addr], slave_id)
    if addr not in state:
        print(f"  [WARN] Could not read D,{addr}; toggle skipped", file=sys.stderr)
        return None

    new_value = not state[addr]
    try:
        try:
            wr = client.write_coil(address=addr, value=new_value, device_id=slave_id)
        except TypeError:
            wr = client.write_coil(address=addr, value=new_value, slave=slave_id)
        if wr.isError():
            print(f"  [WARN] Write error at D,{addr}: {wr}", file=sys.stderr)
            return None
    except Exception as e:
        print(f"  [WARN] Exception writing D,{addr}: {e}", file=sys.stderr)
        return None

    return new_value


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
    print("Type a coil address and press Enter to toggle it if it is marked R/W (e.g. 42 or D,42).")
    print("Monitored coils show [R/W] or [R]; read-only toggle attempts are skipped.")
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

            # Non-blocking stdin: allow on-demand coil toggles while monitoring.
            if select.select([sys.stdin], [], [], 0)[0]:
                line = sys.stdin.readline()
                if line:
                    addr = parse_coil_address(line)
                    if addr is None:
                        print("  [WARN] Invalid address. Use integer or D,<n> (e.g. 42 or D,42).")
                    else:
                        new_val = toggle_coil(client, addr, args.slave)
                        if new_val is not None:
                            timestamp = time.strftime("%H:%M:%S")
                            print(f"[{timestamp}] TOGGLED: {format_coil(addr, new_val)}")
                            curr_state[addr] = new_val

            prev_state = curr_state
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n\nStopped by user.")
    finally:
        client.close()
        print(f"Total polls: {poll_count}")


if __name__ == "__main__":
    main()
