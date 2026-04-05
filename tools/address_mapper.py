#!/usr/bin/env python3
"""
Reverse engineering tool: map valid Modbus coil and holding-register ranges.

This scanner probes configurable address windows and assumes the target device
returns a Modbus error whenever an address does not exist on this model.

By default it scans the full legal 0-based Modbus PDU address span for coils
and holding registers: 0..65535. The default adaptive strategy first samples a
few representative addresses in each window so valid regions are discovered
early, then completes an exhaustive sweep so isolated valid addresses are still
found.

Usage (on Pi):
  # First stop the service to free the serial port
  sudo systemctl stop carel-supervisor

  # Scan the full legal Modbus address space for coils and holding registers
  /opt/carel-supervisor/venv/bin/python -u /opt/carel-supervisor/repo/tools/address_mapper.py

  # Restrict the scan to a smaller register window with verbose probe output
  /opt/carel-supervisor/venv/bin/python -u /opt/carel-supervisor/repo/tools/address_mapper.py \
      --mode registers --register-start 0 --register-end 511 --verbose

  # Use the legacy exhaustive-only sweep
  /opt/carel-supervisor/venv/bin/python -u /opt/carel-supervisor/repo/tools/address_mapper.py \
      --strategy exhaustive --mode coils

Environment:
  - Do not use USE_SIMULATOR=1 for this tool. The simulator returns default
    values for unknown addresses instead of Modbus errors, so the results would
    be misleading.

  # When done, restart the service
  sudo systemctl start carel-supervisor

Press Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from client_factory import create_modbus_client, is_simulator_mode


MODBUS_MIN_ADDRESS = 0
MODBUS_MAX_ADDRESS = 65_535
MODBUS_MAX_COIL_READ_COUNT = 2_000
MODBUS_MAX_REGISTER_READ_COUNT = 125

DEFAULT_COIL_START = MODBUS_MIN_ADDRESS
DEFAULT_COIL_END = MODBUS_MAX_ADDRESS
DEFAULT_REGISTER_START = MODBUS_MIN_ADDRESS
DEFAULT_REGISTER_END = MODBUS_MAX_ADDRESS
DEFAULT_MAX_BLOCK = 16
DEFAULT_STRATEGY = "adaptive"
DEFAULT_WINDOW_SIZE = 1_024
DEFAULT_SAMPLE_POINTS = 5


@dataclass(frozen=True)
class AddressRange:
    start: int
    count: int

    @property
    def end(self) -> int:
        return self.start + self.count - 1


@dataclass
class ProbeRunner:
    label: str
    pause_s: float
    verbose: bool
    raw_probe: Callable[[int, int], Tuple[bool, str]]
    probe_count: int = 0
    cache: Dict[Tuple[int, int], Tuple[bool, str]] = field(default_factory=dict)

    def probe(self, address: int, count: int, *, phase: str) -> Tuple[bool, str]:
        key = (address, count)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        self.probe_count += 1
        ok, detail = self.raw_probe(address, count)
        self.cache[key] = (ok, detail)

        if self.verbose:
            status = "OK" if ok else "FAIL"
            end = address + count - 1
            print(f"[{self.label}] {status:<4} {phase:<10} {address}-{end} ({count} addr)")
            if detail and not ok:
                print(f"  -> {detail}")

        if self.pause_s > 0:
            time.sleep(self.pause_s)

        return ok, detail


def contiguous_ranges(addresses: Iterable[int]) -> List[AddressRange]:
    values = sorted(set(addresses))
    if not values:
        return []

    ranges: List[AddressRange] = []
    start = values[0]
    prev = values[0]
    for value in values[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append(AddressRange(start=start, count=prev - start + 1))
        start = value
        prev = value
    ranges.append(AddressRange(start=start, count=prev - start + 1))
    return ranges


def read_coils(client, address: int, count: int, slave_id: int):
    """Support both current pymodbus (`device_id`) and older/simulated (`slave`) clients."""
    try:
        return client.read_coils(address=address, count=count, device_id=slave_id)
    except TypeError:
        return client.read_coils(address=address, count=count, slave=slave_id)


def read_holding_registers(client, address: int, count: int, slave_id: int):
    """Support both current pymodbus (`device_id`) and older/simulated (`slave`) clients."""
    try:
        return client.read_holding_registers(address=address, count=count, device_id=slave_id)
    except TypeError:
        return client.read_holding_registers(address=address, count=count, slave=slave_id)


def probe_coil_block(client, address: int, count: int, slave_id: int) -> Tuple[bool, str]:
    try:
        rr = read_coils(client, address=address, count=count, slave_id=slave_id)
    except Exception as exc:
        return False, f"exception: {exc}"

    if rr.isError():
        return False, str(rr)

    bits = getattr(rr, "bits", None)
    if bits is None or len(bits) < count:
        return False, f"incomplete coil read: expected {count}, got {len(bits or [])}"

    return True, ""


def probe_register_block(client, address: int, count: int, slave_id: int) -> Tuple[bool, str]:
    try:
        rr = read_holding_registers(client, address=address, count=count, slave_id=slave_id)
    except Exception as exc:
        return False, f"exception: {exc}"

    if rr.isError():
        return False, str(rr)

    registers = getattr(rr, "registers", None)
    if registers is None or len(registers) < count:
        return False, f"incomplete register read: expected {count}, got {len(registers or [])}"

    return True, ""


def iter_windows(start: int, end: int, window_size: int) -> Iterator[AddressRange]:
    current = start
    while current <= end:
        count = min(window_size, end - current + 1)
        yield AddressRange(start=current, count=count)
        current += count


def representative_addresses(start: int, end: int, sample_points: int) -> List[int]:
    if end < start or sample_points < 1:
        return []

    if start == end:
        return [start]

    if sample_points == 1:
        return [start + ((end - start) // 2)]

    span = end - start
    addresses = {
        start + round((span * index) / (sample_points - 1))
        for index in range(sample_points)
    }
    return sorted(addresses)


def exhaustive_scan_range(
    *,
    start: int,
    end: int,
    max_block: int,
    probe_runner: ProbeRunner,
    phase: str,
) -> List[int]:
    if end < start:
        return []

    def scan_block(block_start: int, block_count: int) -> List[int]:
        ok, _detail = probe_runner.probe(block_start, block_count, phase=phase)
        block_end = block_start + block_count - 1

        if ok:
            return list(range(block_start, block_end + 1))

        if block_count == 1:
            return []

        left_count = block_count // 2
        right_count = block_count - left_count
        return scan_block(block_start, left_count) + scan_block(block_start + left_count, right_count)

    valid: List[int] = []
    current = start
    while current <= end:
        block_count = min(max_block, end - current + 1)
        valid.extend(scan_block(current, block_count))
        current += block_count

    return valid


def discover_valid_addresses(
    *,
    label: str,
    start: int,
    end: int,
    max_block: int,
    pause_s: float,
    verbose: bool,
    strategy: str,
    window_size: int,
    sample_points: int,
    probe: Callable[[int, int], Tuple[bool, str]],
) -> Tuple[List[int], int]:
    if end < start:
        return [], 0

    probe_runner = ProbeRunner(
        label=label,
        pause_s=pause_s,
        verbose=verbose,
        raw_probe=probe,
    )

    if strategy == "exhaustive":
        valid = exhaustive_scan_range(
            start=start,
            end=end,
            max_block=max_block,
            probe_runner=probe_runner,
            phase="sweep",
        )
        return valid, probe_runner.probe_count

    valid_addresses = set()
    deferred_windows: List[AddressRange] = []

    for window in iter_windows(start, end, window_size):
        samples = representative_addresses(window.start, window.end, sample_points)
        window_hit = False

        for address in samples:
            ok, _detail = probe_runner.probe(address, 1, phase="sample")
            if ok:
                window_hit = True
                valid_addresses.add(address)

        if window_hit:
            valid_addresses.update(
                exhaustive_scan_range(
                    start=window.start,
                    end=window.end,
                    max_block=max_block,
                    probe_runner=probe_runner,
                    phase="expand",
                )
            )
        else:
            deferred_windows.append(window)

    for window in deferred_windows:
        valid_addresses.update(
            exhaustive_scan_range(
                start=window.start,
                end=window.end,
                max_block=max_block,
                probe_runner=probe_runner,
                phase="sweep",
            )
        )

    return sorted(valid_addresses), probe_runner.probe_count


def format_range(kind: str, address_range: AddressRange) -> str:
    if kind == "coils":
        prefix = "D,"
        if address_range.count == 1:
            return f"{prefix}{address_range.start}"
        return f"{prefix}{address_range.start}..{prefix}{address_range.end}"

    if address_range.count == 1:
        return f"{address_range.start}"
    return f"{address_range.start}..{address_range.end}"


def print_summary(
    *,
    kind: str,
    scan_start: int,
    scan_end: int,
    valid_addresses: Sequence[int],
    probe_count: int,
) -> None:
    ranges = contiguous_ranges(valid_addresses)
    total_valid = len(valid_addresses)

    print(f"\n{kind.title()} summary")
    print(f"Scanned: {scan_start}..{scan_end}")
    print(f"Probes: {probe_count}")
    print(f"Valid addresses: {total_valid}")
    print(f"Valid ranges: {len(ranges)}")

    if not ranges:
        print("  none found")
        return

    for address_range in ranges:
        print(f"  {format_range(kind, address_range)}  ({address_range.count} addr)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Map valid Modbus coil and holding-register address ranges")
    parser.add_argument("--port", default="/dev/ttyACM0", help="Serial port")
    parser.add_argument("--baudrate", type=int, default=9600, help="Baud rate")
    parser.add_argument("--slave", type=int, default=1, help="Modbus slave ID")
    parser.add_argument(
        "--mode",
        choices=("both", "coils", "registers"),
        default="both",
        help="Which address family to scan",
    )
    parser.add_argument(
        "--strategy",
        choices=("adaptive", "exhaustive"),
        default=DEFAULT_STRATEGY,
        help="Adaptive samples each window first, then still completes an exhaustive sweep",
    )
    parser.add_argument("--coil-start", type=int, default=DEFAULT_COIL_START, help="First coil address to scan")
    parser.add_argument("--coil-end", type=int, default=DEFAULT_COIL_END, help="Last coil address to scan")
    parser.add_argument(
        "--register-start",
        type=int,
        default=DEFAULT_REGISTER_START,
        help="First holding-register address to scan",
    )
    parser.add_argument(
        "--register-end",
        type=int,
        default=DEFAULT_REGISTER_END,
        help="Last holding-register address to scan",
    )
    parser.add_argument(
        "--max-block",
        type=int,
        default=DEFAULT_MAX_BLOCK,
        help="Largest block size to probe before recursively splitting failures",
    )
    parser.add_argument(
        "--window-size",
        type=int,
        default=DEFAULT_WINDOW_SIZE,
        help="Adaptive scan window size in addresses before the guaranteed exhaustive sweep",
    )
    parser.add_argument(
        "--sample-points",
        type=int,
        default=DEFAULT_SAMPLE_POINTS,
        help="Representative single-address probes per adaptive window",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.0,
        help="Optional delay in seconds after each probe to reduce bus load",
    )
    parser.add_argument("--verbose", action="store_true", help="Print every probe and failure detail")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.coil_start < MODBUS_MIN_ADDRESS or args.coil_end < MODBUS_MIN_ADDRESS:
        raise ValueError(f"Coil scan bounds must be >= {MODBUS_MIN_ADDRESS}.")
    if args.register_start < MODBUS_MIN_ADDRESS or args.register_end < MODBUS_MIN_ADDRESS:
        raise ValueError(f"Register scan bounds must be >= {MODBUS_MIN_ADDRESS}.")
    if args.coil_start > MODBUS_MAX_ADDRESS or args.coil_end > MODBUS_MAX_ADDRESS:
        raise ValueError(f"Coil scan bounds must be <= {MODBUS_MAX_ADDRESS}.")
    if args.register_start > MODBUS_MAX_ADDRESS or args.register_end > MODBUS_MAX_ADDRESS:
        raise ValueError(f"Register scan bounds must be <= {MODBUS_MAX_ADDRESS}.")
    if args.coil_end < args.coil_start:
        raise ValueError("--coil-end must be >= --coil-start.")
    if args.register_end < args.register_start:
        raise ValueError("--register-end must be >= --register-start.")
    if args.max_block < 1:
        raise ValueError("--max-block must be >= 1.")
    if args.max_block > MODBUS_MAX_COIL_READ_COUNT:
        raise ValueError(f"--max-block must be <= {MODBUS_MAX_COIL_READ_COUNT} for coils.")
    if args.mode in {"both", "registers"} and args.max_block > MODBUS_MAX_REGISTER_READ_COUNT:
        raise ValueError(
            f"--max-block must be <= {MODBUS_MAX_REGISTER_READ_COUNT} when scanning holding registers."
        )
    if args.window_size < 1:
        raise ValueError("--window-size must be >= 1.")
    if args.sample_points < 1:
        raise ValueError("--sample-points must be >= 1.")
    if args.pause < 0:
        raise ValueError("--pause must be >= 0.")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        validate_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    print(f"Connecting to {args.port} @ {args.baudrate} baud, slave {args.slave}")
    print(f"Mode: {args.mode}")
    print(f"Strategy: {args.strategy}")
    print(f"Max block size: {args.max_block}")
    if args.strategy == "adaptive":
        print(f"Adaptive window size: {args.window_size}")
        print(f"Adaptive sample points: {args.sample_points}")
    if args.pause > 0:
        print(f"Pause between probes: {args.pause}s")
    if args.verbose:
        print("Verbose probe logging is enabled.")

    if is_simulator_mode():
        print("ERROR: USE_SIMULATOR=1 is not supported by address_mapper.py.", file=sys.stderr)
        print(
            "The simulator returns default values for unknown addresses instead of Modbus errors, "
            "so it cannot produce a real address map.",
            file=sys.stderr,
        )
        sys.exit(2)

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

    try:
        if args.mode in {"both", "coils"}:
            print(f"\nScanning coils D,{args.coil_start}..D,{args.coil_end}...")
            coil_valid, coil_probes = discover_valid_addresses(
                label="coils",
                start=args.coil_start,
                end=args.coil_end,
                max_block=args.max_block,
                pause_s=args.pause,
                verbose=args.verbose,
                strategy=args.strategy,
                window_size=args.window_size,
                sample_points=args.sample_points,
                probe=lambda address, count: probe_coil_block(client, address, count, args.slave),
            )
            print_summary(
                kind="coils",
                scan_start=args.coil_start,
                scan_end=args.coil_end,
                valid_addresses=coil_valid,
                probe_count=coil_probes,
            )

        if args.mode in {"both", "registers"}:
            print(f"\nScanning holding registers {args.register_start}..{args.register_end}...")
            register_valid, register_probes = discover_valid_addresses(
                label="registers",
                start=args.register_start,
                end=args.register_end,
                max_block=args.max_block,
                pause_s=args.pause,
                verbose=args.verbose,
                strategy=args.strategy,
                window_size=args.window_size,
                sample_points=args.sample_points,
                probe=lambda address, count: probe_register_block(client, address, count, args.slave),
            )
            print_summary(
                kind="registers",
                scan_start=args.register_start,
                scan_end=args.register_end,
                valid_addresses=register_valid,
                probe_count=register_probes,
            )

    except KeyboardInterrupt:
        print("\n\nStopped by user.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
