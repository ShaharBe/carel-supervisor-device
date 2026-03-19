#!/usr/bin/env python3
"""
Reverse engineering tool: monitor selected CAREL A/I registers for changes.

The watched register list is defined in this file. The tool polls those
registers, prints the initial values, and then reports only changes.

Usage (on Pi):
  # First stop the service to free the serial port
  sudo systemctl stop carel-supervisor

  # Run with venv Python (unbuffered for real-time output)
  /opt/carel-supervisor/venv/bin/python -u /opt/carel-supervisor/repo/tools/register_monitor.py

  # Or with options:
  /opt/carel-supervisor/venv/bin/python -u /opt/carel-supervisor/repo/tools/register_monitor.py --interval 0.3

Interactive control:
  - Type a register address and press Enter to read it now.
  - Type "<register>=<value>" to write it if it is marked R/W.
  - Accepted address formats: A,15  A15  15  I,143  I143  143
  - For A registers, integer writes are treated as raw register values.
  - For A registers, decimal writes are treated as scaled values using
    --a-scale (default: 10.0). Example: "A,19=24.5" writes raw value 245.
  - For I registers, writes must be integers.

Environment:
  - Set USE_SIMULATOR=1 to reuse the repo's in-memory Modbus simulator.

  # When done, restart the service
  sudo systemctl start carel-supervisor

Press Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from client_factory import create_modbus_client, is_simulator_mode


@dataclass(frozen=True)
class RegisterDef:
    kind: str
    address: int
    writable: bool
    description: str

    @property
    def label(self) -> str:
        return f"{self.kind},{self.address}"


REGISTER_DEFS: Sequence[RegisterDef] = (
    RegisterDef("A", 1, False, "room probe/external regulator: demand"),
    RegisterDef("A", 2, True, "room probe/external regulator: minimum (calibration)"),
    RegisterDef("A", 3, True, "room probe/external regulator: maximum (calibration)"),
    RegisterDef("A", 4, True, "room probe/external regulator: offset (calibration)"),
    RegisterDef("A", 5, False, "current production (kg/h)"),
    RegisterDef("A", 6, False, "limit probe reading"),
    RegisterDef("A", 7, True, "limit probe: minimum (calibration)"),
    RegisterDef("A", 8, True, "limit probe: maximum (calibration)"),
    RegisterDef("A", 9, True, "limit probe: offset (calibration)"),
    RegisterDef("A", 10, False, "nominal production (kg/h)"),
    RegisterDef("A", 11, False, "total actual current (a)"),
    RegisterDef("A", 12, False, "cylinder 1: actual current (a)"),
    RegisterDef("A", 13, False, "cylinder 2: actual current (a)"),
    RegisterDef("A", 14, True, "maximum production (p0)"),
    RegisterDef("A", 15, True, "%rh set point"),
    RegisterDef("A", 16, True, "%rh differential"),
    RegisterDef("A", 17, True, "limit probe set point"),
    RegisterDef("A", 18, True, "limit differential"),
    RegisterDef("A", 19, True, "temperature set point"),
    RegisterDef("A", 20, True, "temperature differential"),
    RegisterDef("A", 21, True, "dehumidification offset"),
    RegisterDef("A", 22, True, "dehumidification differential"),
    RegisterDef("A", 23, True, "room probe: low humidity warning threshold"),
    RegisterDef("A", 24, True, "room probe: high humidity warning threshold"),
    RegisterDef("A", 25, True, "limit probe: high humidity warning threshold"),
    RegisterDef("I", 129, False, "high part sw version"),
    RegisterDef("I", 130, False, "low part sw version"),
    RegisterDef("I", 131, False, "day sw version"),
    RegisterDef("I", 132, False, "month sw version"),
    RegisterDef("I", 133, False, "year sw version"),
    RegisterDef("I", 134, False, "sw release type"),
    RegisterDef("I", 135, False, "sw release number"),
    RegisterDef("I", 136, False, "humidifier status: 0 = on duty; 1 = alarm(s) present; 2 = disabled via network; 3 = disabled by timer; 4 = disabled by remote on/off; 5 = disabled by keyboard; 6 = manual control; 7 = no demand"),
    RegisterDef("I", 137, False, "conductivity reading"),
    RegisterDef("I", 138, True, "manual force conductivity value"),
    RegisterDef("I", 139, False, "cylinder 1: working phase: 0 = not active; 1 = softstart; 2 = start; 3 = production at steady state; 4 = reduced production; 5 = production delayed stop; 6 = full flush; 7 = Fast Start; 8 = Fast Start_FT (Foam Test); 9 = Fast Start_HW (Heating Water - waiting to boil)"),
    RegisterDef("I", 140, False, "cylinder 1: status: 0 = no production; 1 = start of evaporation cycle; 2 = water fill; 3 = steam production in progress; 4 = water drain (decision to open the contactor; drain pump still stopped); 5 = water drain (drain pump running); 6 = water drain (drain pump stopped; contactor closing, if open); 7 = humidifier blocked; 8 = long-term-inactivity full drain; 9 = flushing of the hydraulic circuit; 10 = full drain by manual or network request; 11 = automatic management of lack of supply water; 12 = total periodic drain"),
    RegisterDef("I", 141, False, "cylinder 2: working phase: 0 = not active; 1 = softstart; 2 = start; 3 = production at steady state; 4 = reduced production; 5 = production delayed stop; 6 = full flush"),
    RegisterDef("I", 142, False, "cylinder 2: status: 0 = no production; 1 = start of evaporation cycle; 2 = water fill; 3 = steam production in progress; 4 = water drain (decision to open the contactor; drain pump still stopped); 5 = water drain (drain pump running); 6 = water drain (drain pump stopped; contactor closing, if open); 7 = humidifier blocked; 8 = long-term-inactivity full drain; 9 = flushing of the hydraulic circuit; 10 = full drain by manual or network request; 11 = automatic management of lack of supply water; 12 = total periodic drain"),
    RegisterDef("I", 143, True, "regulation type: 0 = on/off; 1 = slave 0-100%; 2 = slave 0-100% + limit probe; 3 = %rh control with external probe without limit probe; 4 = %rh control with external probe + limit probe; 5 = temperature control"),
    RegisterDef("I", 144, True, "room probe/ext. regulator: type of signal: 0 = 0-1 V; 1 = 0-10 V; 2 = 2-10 V; 3 = 0-20 mA; 4 = 4-20 mA; 5 = NTC CAREL standard"),
    RegisterDef("I", 145, True, "limit probe: type of signal: 0 = 0-1 V; 1 = 0-10 V; 2 = 2-10 V; 3 = 0-20 mA; 4 = 4-20 mA; 5 = NTC CAREL standard"),
    RegisterDef("I", 146, True, "maintenance time-out"),
    RegisterDef("I", 147, True, "periodical flush: period"),
    RegisterDef("I", 148, True, "inactivity drain: time-out"),
    RegisterDef("I", 149, True, "conductivity warning: threshold"),
    RegisterDef("I", 150, True, "conductivity alarm: threshold"),
    RegisterDef("I", 151, True, "tuning of dilution frequency: parameter b8"),
    RegisterDef("I", 152, True, "tuning of dilution duration: parameter b9"),
    RegisterDef("I", 153, False, "system timer: hour"),
    RegisterDef("I", 154, False, "system timer: minute"),
    RegisterDef("I", 155, False, "system timer: day"),
    RegisterDef("I", 156, False, "system timer: month"),
    RegisterDef("I", 157, False, "system timer: year"),
    RegisterDef("I", 158, False, "system timer: week day"),
    RegisterDef("I", 159, True, "system timer: week day (can be edited for updating the sistem timer!): 0 = monday; 1 = tuesday; 2 = wednesday; 3 = thursday; 4 = friday; 5 = saturday; 6 = sunday"),
    RegisterDef("I", 160, True, "system timer: hour (can be edited for updating the sistem timer!)"),
    RegisterDef("I", 161, True, "system timer: minute (can be edited for updating the sistem timer!)"),
    RegisterDef("I", 162, True, "system timer: day (can be edited for updating the sistem timer!)"),
    RegisterDef("I", 163, True, "system timer: month (can be edited for updating the sistem timer!)"),
    RegisterDef("I", 164, True, "system timer: year (can be edited for updating the sistem timer!)"),
    RegisterDef("I", 165, False, "cylinder 1: hour counter"),
    RegisterDef("I", 166, False, "cylinder 2: hour counter"),
    RegisterDef("I", 167, False, "voltage type (V): 0 = 200; 1 = 208; 2 = 230; 3 = 400; 4 = 460; 5 = 575"),
    RegisterDef("I", 168, True, "humidifier type"),
    RegisterDef("I", 180, False, "lista modelli umidificatori"),
    RegisterDef("I", 181, True, "parameter Installer/Supervisor/Supervisor connect/Reg. from BMS: sending analog signal control(0-1000, temper: tenths of deg C/deg F, umid: tenths of rH%)"),
    RegisterDef("I", 182, True, "parameter Installer/Supervisor/Supervisor connect/Offline al. Delay: time delay for alarm SERIAL OFFLINE (seconds)"),
)

REGISTER_BY_KEY: Dict[Tuple[str, int], RegisterDef] = {
    (definition.kind, definition.address): definition for definition in REGISTER_DEFS
}

ADDRESS_RE = re.compile(r"^(?:(?P<kind>[AI])\s*,?\s*)?(?P<addr>-?\d+)$", re.IGNORECASE)


def contiguous_ranges(addresses: Iterable[int]) -> List[Tuple[int, int]]:
    values = sorted(set(addresses))
    if not values:
        return []

    ranges: List[Tuple[int, int]] = []
    start = values[0]
    prev = values[0]
    for value in values[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append((start, prev - start + 1))
        start = value
        prev = value
    ranges.append((start, prev - start + 1))
    return ranges

def to_signed_16(raw_value: int) -> int:
    """Interpret an unsigned 16-bit register as signed two's complement."""
    raw = raw_value & 0xFFFF
    return raw - 0x10000 if raw >= 0x8000 else raw


def encode_signed_16(value: int) -> int:
    """Encode a Python int into the 16-bit register word expected by Modbus."""
    if value < -32768 or value > 65535:
        raise ValueError("Value out of 16-bit register range (-32768..65535).")
    return value & 0xFFFF


def effective_address(definition: RegisterDef, a_offset: int, i_offset: int) -> int:
    return definition.address + (a_offset if definition.kind == "A" else i_offset)


def build_effective_ranges(definitions: Sequence[RegisterDef], a_offset: int, i_offset: int) -> List[Tuple[int, int]]:
    return contiguous_ranges(effective_address(definition, a_offset, i_offset) for definition in definitions)


def read_holding_registers(client, address: int, count: int, slave_id: int):
    """Support both current pymodbus (`device_id`) and older/simulated (`slave`) clients."""
    try:
        return client.read_holding_registers(address=address, count=count, device_id=slave_id)
    except TypeError:
        return client.read_holding_registers(address=address, count=count, slave=slave_id)


def write_register(client, address: int, value: int, slave_id: int):
    """Support both current pymodbus (`device_id`) and older/simulated (`slave`) clients."""
    try:
        return client.write_register(address=address, value=value, device_id=slave_id)
    except TypeError:
        return client.write_register(address=address, value=value, slave=slave_id)


def parse_register_ref(text: str) -> Optional[Tuple[str, int]]:
    raw = text.strip()
    if not raw:
        return None

    match = ADDRESS_RE.fullmatch(raw)
    if not match:
        return None

    addr = int(match.group("addr"))
    if addr < 0:
        return None

    kind = (match.group("kind") or "").upper()
    if kind:
        key = (kind, addr)
        return key if key in REGISTER_BY_KEY else None

    matches = [key for key in REGISTER_BY_KEY if key[1] == addr]
    if len(matches) == 1:
        return matches[0]
    return None


def parse_command(text: str) -> Tuple[Optional[Tuple[str, int]], Optional[str]]:
    raw = text.strip()
    if not raw:
        return None, None

    if "=" not in raw:
        return parse_register_ref(raw), None

    left, right = raw.split("=", 1)
    key = parse_register_ref(left)
    value_text = right.strip()
    return key, value_text if value_text else None


def describe_value(definition: RegisterDef, value: int, a_scale: float) -> str:
    if definition.kind == "A":
        if a_scale != 1.0:
            scaled = value / a_scale
            return f"{value}  (~{scaled:g})"
        return str(value)
    return str(value)


def format_register(definition: RegisterDef, value: int, a_scale: float) -> str:
    access = "R/W" if definition.writable else "R"
    return f"{definition.label} = {describe_value(definition, value, a_scale)}  [{access}]  ({definition.description})"


def read_registers(
    client,
    definitions: Sequence[RegisterDef],
    slave_id: int,
    a_offset: int,
    i_offset: int,
) -> Dict[Tuple[str, int], int]:
    result: Dict[Tuple[str, int], int] = {}
    by_address: Dict[int, List[RegisterDef]] = {}
    for definition in definitions:
        by_address.setdefault(effective_address(definition, a_offset, i_offset), []).append(definition)

    for start, count in contiguous_ranges(by_address.keys()):
        try:
            rr = read_holding_registers(client, address=start, count=count, slave_id=slave_id)
            if rr.isError():
                print(f"  [WARN] Read error at regs {start}-{start + count - 1}: {rr}", file=sys.stderr)
                continue
            if not rr.registers or len(rr.registers) < count:
                print(
                    f"  [WARN] Incomplete read at regs {start}-{start + count - 1}: "
                    f"expected {count}, got {len(rr.registers or [])}",
                    file=sys.stderr,
                )
                continue
            for offset, raw_value in enumerate(rr.registers):
                address = start + offset
                decoded = to_signed_16(int(raw_value))
                for definition in by_address.get(address, []):
                    result[(definition.kind, definition.address)] = decoded
        except Exception as exc:
            print(f"  [WARN] Exception reading regs {start}-{start + count - 1}: {exc}", file=sys.stderr)
    return result


def read_one_register(
    client,
    definition: RegisterDef,
    slave_id: int,
    a_offset: int,
    i_offset: int,
) -> Optional[int]:
    state = read_registers(client, [definition], slave_id, a_offset=a_offset, i_offset=i_offset)
    return state.get((definition.kind, definition.address))


def parse_write_value(definition: RegisterDef, value_text: str, a_scale: float) -> int:
    if definition.kind == "I":
        return int(value_text, 10)

    if "." in value_text:
        scaled_value = float(value_text)
        return int(round(scaled_value * a_scale))
    return int(value_text, 10)


def write_one_register(
    client,
    definition: RegisterDef,
    new_value: int,
    slave_id: int,
    a_offset: int,
    i_offset: int,
) -> Optional[int]:
    try:
        raw_to_write = encode_signed_16(new_value)
    except ValueError as exc:
        print(f"  [WARN] {exc}", file=sys.stderr)
        return None

    try:
        wr = write_register(
            client,
            address=effective_address(definition, a_offset, i_offset),
            value=raw_to_write,
            slave_id=slave_id,
        )
        if wr.isError():
            print(f"  [WARN] Write error at {definition.label}: {wr}", file=sys.stderr)
            return None
    except Exception as exc:
        print(f"  [WARN] Exception writing {definition.label}: {exc}", file=sys.stderr)
        return None

    state = read_registers(client, [definition], slave_id, a_offset=a_offset, i_offset=i_offset)
    return state.get((definition.kind, definition.address))


def start_stdin_reader() -> "queue.Queue[str]":
    lines: "queue.Queue[str]" = queue.Queue()

    def _reader() -> None:
        while True:
            try:
                line = sys.stdin.readline()
            except Exception:
                break
            if line == "":
                break
            lines.put(line)

    threading.Thread(target=_reader, name="register-monitor-stdin", daemon=True).start()
    return lines


def print_help(a_scale: float) -> None:
    print("Commands:")
    print("  A,15           Read register A,15 now")
    print("  I,143=5        Write integer register I,143")
    print(f"  A,19=24.5      Write scaled A register value using --a-scale ({a_scale:g})")
    print("  A,19=245       Write raw register value 245")
    print("  help           Show this help")


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor selected CAREL A/I registers for changes")
    parser.add_argument("--port", default="/dev/ttyACM0", help="Serial port")
    parser.add_argument("--baudrate", type=int, default=9600, help="Baud rate")
    parser.add_argument("--slave", type=int, default=1, help="Modbus slave ID")
    parser.add_argument("--interval", type=float, default=0.5, help="Poll interval (seconds)")
    parser.add_argument(
        "--a-scale",
        type=float,
        default=10.0,
        help="Scale used when displaying decimal hints for A registers and when writing decimal A values",
    )
    parser.add_argument(
        "--a-offset",
        type=int,
        default=0,
        help="Offset added to A register numbers before Modbus reads/writes",
    )
    parser.add_argument(
        "--i-offset",
        type=int,
        default=0,
        help="Offset added to I register numbers before Modbus reads/writes",
    )
    args = parser.parse_args()

    effective_ranges = build_effective_ranges(REGISTER_DEFS, args.a_offset, args.i_offset)

    print(f"Connecting to {args.port} @ {args.baudrate} baud, slave {args.slave}")
    print(
        f"Monitoring {len(REGISTER_DEFS)} registers in {len(effective_ranges)} blocks: "
        + ", ".join(f"{start}-{start + count - 1}" for start, count in effective_ranges)
    )
    print(f"Address offsets: A {args.a_offset:+d}, I {args.i_offset:+d}")
    if is_simulator_mode():
        print("Simulator mode is enabled via USE_SIMULATOR=1.")
    print(f"Poll interval: {args.interval}s")
    print(
        "Type a register to read it, or '<register>=<value>' to write it "
        "(e.g. A,19 or I,143=5)."
    )
    print_help(args.a_scale)
    print("-" * 60)

    client = create_modbus_client(
        port=args.port,
        baudrate=args.baudrate,
        parity="N",
        stopbits=1,
        bytesize=8,
        timeout=1.0,
        retries=1,
    )

    if not client.connect():
        print(f"ERROR: Failed to connect to {args.port}", file=sys.stderr)
        sys.exit(1)

    print("Connected. Reading initial state...")

    input_lines = start_stdin_reader()
    prev_state: Optional[Dict[Tuple[str, int], int]] = None
    poll_count = 0

    try:
        while True:
            curr_state = read_registers(
                client,
                REGISTER_DEFS,
                args.slave,
                a_offset=args.a_offset,
                i_offset=args.i_offset,
            )
            poll_count += 1

            if prev_state is None:
                print(f"\nInitial state (poll #{poll_count}):")
                for definition in REGISTER_DEFS:
                    key = (definition.kind, definition.address)
                    if key in curr_state:
                        print(f"  {format_register(definition, curr_state[key], args.a_scale)}")
                print("\nWaiting for changes... (Ctrl+C to stop)")
            else:
                for definition in REGISTER_DEFS:
                    key = (definition.kind, definition.address)
                    old_value = prev_state.get(key)
                    new_value = curr_state.get(key)
                    if old_value is not None and new_value is not None and old_value != new_value:
                        timestamp = time.strftime("%H:%M:%S")
                        print(
                            f"[{timestamp}] CHANGED: "
                            f"{format_register(definition, new_value, args.a_scale)}  "
                            f"(was {describe_value(definition, old_value, args.a_scale)})"
                        )

            try:
                line = input_lines.get_nowait()
            except queue.Empty:
                line = None

            if line:
                stripped = line.strip()
                if stripped.lower() in {"help", "?", "h"}:
                    print_help(args.a_scale)
                else:
                    key, value_text = parse_command(line)
                    if key is None:
                        print("  [WARN] Invalid register. Use A,15 / I,143 or A,19=24.5.")
                    else:
                        definition = REGISTER_BY_KEY[key]
                        if value_text is None:
                            current = read_one_register(
                                client,
                                definition,
                                args.slave,
                                a_offset=args.a_offset,
                                i_offset=args.i_offset,
                            )
                            if current is None:
                                print(f"  [WARN] Could not read {definition.label}.", file=sys.stderr)
                            else:
                                timestamp = time.strftime("%H:%M:%S")
                                print(f"[{timestamp}] READ: {format_register(definition, current, args.a_scale)}")
                                curr_state[key] = current
                        else:
                            if not definition.writable:
                                print(f"  [WARN] {definition.label} is read-only; write skipped.")
                            else:
                                try:
                                    parsed_value = parse_write_value(definition, value_text, args.a_scale)
                                except ValueError:
                                    if definition.kind == "A":
                                        print(
                                            "  [WARN] Invalid value. Use an integer raw value "
                                            "or a decimal scaled value for A registers."
                                        )
                                    else:
                                        print("  [WARN] Invalid value. I-register writes must be integers.")
                                    parsed_value = None

                                if parsed_value is not None:
                                    confirmed = write_one_register(
                                        client,
                                        definition,
                                        parsed_value,
                                        args.slave,
                                        a_offset=args.a_offset,
                                        i_offset=args.i_offset,
                                    )
                                    if confirmed is not None:
                                        timestamp = time.strftime("%H:%M:%S")
                                        print(
                                            f"[{timestamp}] WROTE: {format_register(definition, confirmed, args.a_scale)}"
                                        )
                                        curr_state[key] = confirmed

            prev_state = curr_state
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n\nStopped by user.")
    finally:
        client.close()
        print(f"Total polls: {poll_count}")


if __name__ == "__main__":
    main()
