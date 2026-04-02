"""Tests for RTC date/time helpers in runtime.py."""

from __future__ import annotations

from datetime import datetime

from runtime import (
    build_device_datetime,
    encode_device_year,
    format_device_datetime_local,
    normalize_device_year,
)


# ── normalize_device_year ────────────────────────────────────────────────

class TestNormalizeDeviceYear:
    def test_zero_maps_to_2000(self):
        assert normalize_device_year(0) == 2000

    def test_26_maps_to_2026(self):
        assert normalize_device_year(26) == 2026

    def test_99_maps_to_2099(self):
        assert normalize_device_year(99) == 2099

    def test_four_digit_passthrough(self):
        assert normalize_device_year(2026) == 2026

    def test_100_passthrough(self):
        assert normalize_device_year(100) == 100


# ── encode_device_year ───────────────────────────────────────────────────

class TestEncodeDeviceYear:
    def test_two_digit_encoding(self):
        assert encode_device_year(2027, 26) == 27

    def test_two_digit_encoding_year_2000(self):
        assert encode_device_year(2000, 0) == 0

    def test_four_digit_when_current_raw_is_none(self):
        assert encode_device_year(2027, None) == 2027

    def test_four_digit_when_current_raw_is_four_digit(self):
        assert encode_device_year(2027, 2026) == 2027

    def test_boundary_99_stays_two_digit(self):
        assert encode_device_year(2099, 98) == 99


# ── build_device_datetime ────────────────────────────────────────────────

class TestBuildDeviceDatetime:
    def test_normal_date(self):
        dt = build_device_datetime(14, 30, 2, 4, 26)
        assert dt == datetime(2026, 4, 2, 14, 30)

    def test_year_zero(self):
        dt = build_device_datetime(0, 0, 1, 1, 0)
        assert dt == datetime(2000, 1, 1, 0, 0)

    def test_leap_year_feb_29(self):
        dt = build_device_datetime(12, 0, 29, 2, 24)
        assert dt == datetime(2024, 2, 29, 12, 0)

    def test_four_digit_raw_year(self):
        dt = build_device_datetime(8, 15, 25, 12, 2026)
        assert dt == datetime(2026, 12, 25, 8, 15)


# ── format_device_datetime_local ─────────────────────────────────────────

class TestFormatDeviceDatetimeLocal:
    def test_format(self):
        dt = datetime(2026, 4, 2, 14, 30)
        assert format_device_datetime_local(dt) == "2026-04-02T14:30"

    def test_midnight(self):
        dt = datetime(2026, 1, 1, 0, 0)
        assert format_device_datetime_local(dt) == "2026-01-01T00:00"
