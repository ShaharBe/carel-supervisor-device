"""Tests for the reverse-engineering Modbus address mapper."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.address_mapper import MODBUS_MAX_ADDRESS, build_parser, discover_valid_addresses, validate_args
from tools.address_mapper import prompt_for_scan_bounds


def make_args(**overrides) -> argparse.Namespace:
    values = {
        "coil_start": 0,
        "coil_end": MODBUS_MAX_ADDRESS,
        "register_start": 0,
        "register_end": MODBUS_MAX_ADDRESS,
        "address_base": 0,
        "baudrate": 19200,
        "mode": "both",
        "progress_interval": 1000,
        "pause": 0.0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def sparse_probe(valid_addresses: set[int], calls: list[int]):
    def probe(address: int) -> tuple[bool, str]:
        calls.append(address)
        return (address in valid_addresses), ("", "illegal data address")[address not in valid_addresses]

    return probe


class TestParserDefaults:
    def test_defaults_cover_full_modbus_address_space(self):
        args = build_parser().parse_args([])

        assert args.coil_start == 0
        assert args.coil_end == MODBUS_MAX_ADDRESS
        assert args.register_start == 0
        assert args.register_end == MODBUS_MAX_ADDRESS
        assert args.address_base == 0
        assert args.baudrate == 19200


class TestManualRangePrompt:
    def test_prompt_accepts_enter_defaults_for_zero_based_ranges(self, monkeypatch: pytest.MonkeyPatch):
        args = make_args()
        responses = iter(["", "", "", "", ""])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(responses))

        updated = prompt_for_scan_bounds(args)

        assert updated.address_base == 0
        assert updated.coil_start == 0
        assert updated.coil_end == MODBUS_MAX_ADDRESS
        assert updated.register_start == 0
        assert updated.register_end == MODBUS_MAX_ADDRESS

    def test_prompt_converts_one_based_manual_entry_to_internal_zero_based(self, monkeypatch: pytest.MonkeyPatch):
        args = make_args()
        responses = iter(["1", "1", "100", "201", "300"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(responses))

        updated = prompt_for_scan_bounds(args)

        assert updated.address_base == 1
        assert updated.coil_start == 0
        assert updated.coil_end == 99
        assert updated.register_start == 200
        assert updated.register_end == 299


class TestSequentialDiscovery:
    def test_finds_isolated_valid_addresses_with_single_address_scan(self):
        calls: list[int] = []
        valid_addresses, request_count = discover_valid_addresses(
            label="registers",
            start=0,
            end=127,
            pause_s=0.0,
            verbose=False,
            progress_interval=1000,
            probe=sparse_probe({37, 90}, calls),
        )

        assert valid_addresses == [37, 90]
        assert calls[:6] == [0, 1, 2, 3, 4, 5]
        assert calls[-3:] == [125, 126, 127]
        assert request_count == 128

    def test_progress_output_includes_valid_counts_without_verbose(self, capsys: pytest.CaptureFixture[str]):
        discover_valid_addresses(
            label="registers",
            start=0,
            end=7,
            pause_s=0.0,
            verbose=False,
            progress_interval=4,
            probe=sparse_probe({1, 6}, []),
        )

        captured = capsys.readouterr().out
        assert "valid=" in captured
        assert "valid_total=" in captured
        assert "requests=" in captured


class TestValidateArgs:
    def test_rejects_addresses_above_protocol_maximum(self):
        with pytest.raises(ValueError, match="must be <= 65535"):
            validate_args(make_args(coil_end=MODBUS_MAX_ADDRESS + 1))

    def test_rejects_non_positive_progress_interval(self):
        with pytest.raises(ValueError, match="progress-interval"):
            validate_args(make_args(progress_interval=0))
