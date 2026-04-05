#!/usr/bin/env python3
"""
Reverse engineering tool: map valid Modbus coil and holding-register ranges.

This scanner probes configurable address windows and assumes the target device
returns a Modbus error whenever an address does not exist on this model.

By default it scans the full legal 0-based Modbus PDU address span for coils
and holding registers: 0..65535. When run interactively in a terminal, the tool
also lets you confirm or override the scan bounds before it starts. The scan is
intentionally simple: it reads each address one by one so sparse maps do not
pay the extra retry cost of block-read fallback logic.

Usage (on Pi):
  # First stop the service to free the serial port
  sudo systemctl stop carel-supervisor

  # Scan the full legal Modbus address space for coils and holding registers
  /opt/carel-supervisor/venv/bin/python -u /opt/carel-supervisor/repo/tools/address_mapper.py

  # Restrict the scan to a smaller register window with verbose probe output
  /opt/carel-supervisor/venv/bin/python -u /opt/carel-supervisor/repo/tools/address_mapper.py \
      --mode registers --register-start 0 --register-end 511 --verbose

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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from client_factory import create_modbus_client, is_simulator_mode


MODBUS_MIN_ADDRESS = 0
MODBUS_MAX_ADDRESS = 65_535

DEFAULT_COIL_START = MODBUS_MIN_ADDRESS
DEFAULT_COIL_END = MODBUS_MAX_ADDRESS
DEFAULT_REGISTER_START = MODBUS_MIN_ADDRESS
DEFAULT_REGISTER_END = MODBUS_MAX_ADDRESS
DEFAULT_PROGRESS_INTERVAL = 1_000
DEFAULT_ADDRESS_BASE = 0


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
    raw_probe: Callable[[int], Tuple[bool, str]]
    total_addresses: int
    progress_interval: int
    request_count: int = 0
    covered_count: int = 0
    valid_total: int = 0
    next_progress_report: int = 0
    complete_reported: bool = False
    reported_valid_count: int = 0

    def __post_init__(self) -> None:
        self.next_progress_report = min(self.progress_interval, self.total_addresses)

    def _report_progress(self) -> None:
        if self.covered_count >= self.total_addresses and self.complete_reported:
            return

        if self.covered_count < self.next_progress_report and self.covered_count < self.total_addresses:
            return

        percent = (self.covered_count / self.total_addresses) * 100 if self.total_addresses else 100.0
        valid_delta = self.valid_total - self.reported_valid_count
        print(
            f"[{self.label}] progress: {self.covered_count}/{self.total_addresses} addr covered "
            f"({percent:.1f}%), requests={self.request_count}, valid={valid_delta}, valid_total={self.valid_total}"
        )
        self.reported_valid_count = self.valid_total

        if self.covered_count >= self.total_addresses:
            self.complete_reported = True
            self.next_progress_report = self.total_addresses + self.progress_interval
            return

        while (
            self.next_progress_report <= self.covered_count
            and self.next_progress_report < self.total_addresses
        ):
            self.next_progress_report += self.progress_interval

    def probe(self, address: int) -> Tuple[bool, str]:
        self.request_count += 1
        ok, detail = self.raw_probe(address)
        self.covered_count += 1
        if ok:
            self.valid_total += 1

        if self.verbose:
            status = "OK" if ok else "FAIL"
            print(f"[{self.label}] {status:<4} {address} (1 addr)")
            if detail and not ok:
                print(f"  -> {detail}")

        self._report_progress()

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


def probe_coil_address(client, address: int, slave_id: int) -> Tuple[bool, str]:
    try:
        rr = read_coils(client, address=address, count=1, slave_id=slave_id)
    except Exception as exc:
        return False, f"exception: {exc}"

    if rr.isError():
        return False, str(rr)

    bits = getattr(rr, "bits", None)
    if bits is None or len(bits) < 1:
        return False, "incomplete coil read: expected 1, got 0"

    return True, ""


def probe_register_address(client, address: int, slave_id: int) -> Tuple[bool, str]:
    try:
        rr = read_holding_registers(client, address=address, count=1, slave_id=slave_id)
    except Exception as exc:
        return False, f"exception: {exc}"

    if rr.isError():
        return False, str(rr)

    registers = getattr(rr, "registers", None)
    if registers is None or len(registers) < 1:
        return False, "incomplete register read: expected 1, got 0"

    return True, ""


def to_display_address(address: int, address_base: int) -> int:
    return address if address_base == 0 else address + 1


def from_display_address(address: int, address_base: int) -> int:
    return address if address_base == 0 else address - 1


def prompt_for_integer(prompt: str, default: int) -> int:
    while True:
        response = input(f"{prompt} [{default}]: ").strip()
        if not response:
            return default
        try:
            return int(response)
        except ValueError:
            print("Please enter a whole number or press Enter for the default.")


def prompt_for_scan_bounds(args: argparse.Namespace) -> argparse.Namespace:
    print("\nManual scan range entry")
    print("Press Enter to accept each default.")

    address_base = prompt_for_integer("Address entry base (0 or 1)", args.address_base)
    while address_base not in {0, 1}:
        print("Address entry base must be 0 or 1.")
        address_base = prompt_for_integer("Address entry base (0 or 1)", args.address_base)

    args.address_base = address_base

    if args.mode in {"both", "coils"}:
        coil_start_default = to_display_address(args.coil_start, address_base)
        coil_end_default = to_display_address(args.coil_end, address_base)
        args.coil_start = from_display_address(
            prompt_for_integer("Coil scan start", coil_start_default),
            address_base,
        )
        args.coil_end = from_display_address(
            prompt_for_integer("Coil scan end", coil_end_default),
            address_base,
        )

    if args.mode in {"both", "registers"}:
        register_start_default = to_display_address(args.register_start, address_base)
        register_end_default = to_display_address(args.register_end, address_base)
        args.register_start = from_display_address(
            prompt_for_integer("Register scan start", register_start_default),
            address_base,
        )
        args.register_end = from_display_address(
            prompt_for_integer("Register scan end", register_end_default),
            address_base,
        )

    return args


def discover_valid_addresses(
    *,
    label: str,
    start: int,
    end: int,
    pause_s: float,
    verbose: bool,
    progress_interval: int,
    probe: Callable[[int], Tuple[bool, str]],
) -> Tuple[List[int], int]:
    if end < start:
        return [], 0

    probe_runner = ProbeRunner(
        label=label,
        pause_s=pause_s,
        verbose=verbose,
        raw_probe=probe,
        total_addresses=(end - start + 1),
        progress_interval=max(1, min(progress_interval, end - start + 1)),
    )

    valid_addresses: List[int] = []
    for address in range(start, end + 1):
        ok, _detail = probe_runner.probe(address)
        if ok:
            valid_addresses.append(address)

    return valid_addresses, probe_runner.request_count


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
    request_count: int,
) -> None:
    ranges = contiguous_ranges(valid_addresses)
    total_valid = len(valid_addresses)

    print(f"\n{kind.title()} summary")
    print(f"Scanned: {scan_start}..{scan_end}")
    print(f"Requests: {request_count}")
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
    parser.add_argument("--baudrate", type=int, default=19200, help="Baud rate")
    parser.add_argument("--slave", type=int, default=1, help="Modbus slave ID")
    parser.add_argument(
        "--address-base",
        type=int,
        choices=(0, 1),
        default=DEFAULT_ADDRESS_BASE,
        help="Display and manual-entry base for scan bounds: 0-based or 1-based",
    )
    parser.add_argument(
        "--mode",
        choices=("both", "coils", "registers"),
        default="both",
        help="Which address family to scan",
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
        "--progress-interval",
        type=int,
        default=DEFAULT_PROGRESS_INTERVAL,
        help="Print a short progress update after this many additional addresses are covered",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.0,
        help="Optional delay in seconds after each read request to reduce bus load",
    )
    parser.add_argument("--verbose", action="store_true", help="Print every address result and failure detail")
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
    if args.progress_interval < 1:
        raise ValueError("--progress-interval must be >= 1.")
    if args.pause < 0:
        raise ValueError("--pause must be >= 0.")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if sys.stdin.isatty():
        try:
            args = prompt_for_scan_bounds(args)
        except KeyboardInterrupt:
            print("\n\nStopped by user before scan start.")
            sys.exit(130)

    try:
        validate_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    print(f"Connecting to {args.port} @ {args.baudrate} baud, slave {args.slave}")
    print(f"Mode: {args.mode}")
    print(f"Manual address entry base: {args.address_base}-based")
    print("Scan method: sequential single-address reads")
    print(f"Progress interval: {args.progress_interval} addresses")
    if args.pause > 0:
        print(f"Pause between requests: {args.pause}s")
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
            coil_valid, coil_requests = discover_valid_addresses(
                label="coils",
                start=args.coil_start,
                end=args.coil_end,
                pause_s=args.pause,
                verbose=args.verbose,
                progress_interval=args.progress_interval,
                probe=lambda address: probe_coil_address(client, address, args.slave),
            )
            print_summary(
                kind="coils",
                scan_start=args.coil_start,
                scan_end=args.coil_end,
                valid_addresses=coil_valid,
                request_count=coil_requests,
            )

        if args.mode in {"both", "registers"}:
            print(f"\nScanning holding registers {args.register_start}..{args.register_end}...")
            register_valid, register_requests = discover_valid_addresses(
                label="registers",
                start=args.register_start,
                end=args.register_end,
                pause_s=args.pause,
                verbose=args.verbose,
                progress_interval=args.progress_interval,
                probe=lambda address: probe_register_address(client, address, args.slave),
            )
            print_summary(
                kind="registers",
                scan_start=args.register_start,
                scan_end=args.register_end,
                valid_addresses=register_valid,
                request_count=register_requests,
            )

    except KeyboardInterrupt:
        print("\n\nStopped by user.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
