"""Tests for the reverse-engineering Modbus address mapper."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.address_mapper import (
    MODBUS_MAX_ADDRESS,
    build_parser,
    discover_valid_addresses,
    representative_addresses,
    validate_args,
)


def make_args(**overrides) -> argparse.Namespace:
    values = {
        "coil_start": 0,
        "coil_end": MODBUS_MAX_ADDRESS,
        "register_start": 0,
        "register_end": MODBUS_MAX_ADDRESS,
        "baudrate": 19200,
        "mode": "both",
        "max_block": 16,
        "window_size": 1024,
        "sample_points": 5,
        "progress_interval": 1000,
        "pause": 0.0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def sparse_probe(valid_addresses: set[int], calls: list[tuple[int, int]]):
    def probe(address: int, count: int) -> tuple[bool, str]:
        calls.append((address, count))
        ok = all(candidate in valid_addresses for candidate in range(address, address + count))
        return ok, "" if ok else "illegal data address"

    return probe


class TestParserDefaults:
    def test_defaults_cover_full_modbus_address_space(self):
        args = build_parser().parse_args([])

        assert args.coil_start == 0
        assert args.coil_end == MODBUS_MAX_ADDRESS
        assert args.register_start == 0
        assert args.register_end == MODBUS_MAX_ADDRESS
        assert args.baudrate == 19200
        assert args.strategy == "adaptive"


class TestRepresentativeAddresses:
    def test_spreads_points_across_window(self):
        assert representative_addresses(0, 63, 5) == [0, 16, 32, 47, 63]

    def test_deduplicates_when_window_is_tiny(self):
        assert representative_addresses(10, 12, 5) == [10, 11, 12]


class TestAdaptiveDiscovery:
    def test_finds_isolated_valid_addresses_even_when_sampling_misses_them(self):
        calls: list[tuple[int, int]] = []
        valid_addresses, probe_count = discover_valid_addresses(
            label="registers",
            start=0,
            end=127,
            max_block=8,
            pause_s=0.0,
            verbose=False,
            strategy="adaptive",
            window_size=64,
            sample_points=3,
            progress_interval=1000,
            probe=sparse_probe({37, 90}, calls),
        )

        assert valid_addresses == [37, 90]
        assert calls[:6] == [(0, 1), (32, 1), (63, 1), (64, 1), (96, 1), (127, 1)]
        assert probe_count == len(calls)

    def test_progress_output_includes_valid_counts_without_verbose(self, capsys: pytest.CaptureFixture[str]):
        discover_valid_addresses(
            label="registers",
            start=0,
            end=7,
            max_block=2,
            pause_s=0.0,
            verbose=False,
            strategy="adaptive",
            window_size=4,
            sample_points=1,
            progress_interval=4,
            probe=sparse_probe({1, 6}, []),
        )

        captured = capsys.readouterr().out
        assert "valid=" in captured
        assert "valid_total=" in captured


class TestValidateArgs:
    def test_rejects_addresses_above_protocol_maximum(self):
        with pytest.raises(ValueError, match="must be <= 65535"):
            validate_args(make_args(coil_end=MODBUS_MAX_ADDRESS + 1))

    def test_rejects_register_block_sizes_above_protocol_limit(self):
        with pytest.raises(ValueError, match="<= 125"):
            validate_args(make_args(mode="registers", max_block=126))

    def test_rejects_non_positive_progress_interval(self):
        with pytest.raises(ValueError, match="progress-interval"):
            validate_args(make_args(progress_interval=0))
