"""Tests for alarms.py — catalog loading and bit interpretation."""

from __future__ import annotations

import pytest

from alarms import (
    ALARM_CATALOG,
    CYLINDER_2_PREFIX,
    NON_ALARM_PREFIXES,
    AlarmCatalog,
    AlarmDefinition,
    load_alarm_catalog,
)


# ── Catalog loading ──────────────────────────────────────────────────────

class TestLoadAlarmCatalog:
    def test_summary_is_present(self):
        catalog = load_alarm_catalog()
        assert catalog.summary is not None
        assert catalog.summary.description == "at least 1 alarm is active"

    def test_summary_address(self):
        assert ALARM_CATALOG.summary.address == 9

    def test_monitored_is_nonempty(self):
        assert len(ALARM_CATALOG.monitored) > 0

    def test_skipped_contains_cylinder_2(self):
        for defn in ALARM_CATALOG.skipped:
            assert defn.description.casefold().startswith(CYLINDER_2_PREFIX)

    def test_range_start_and_count(self):
        assert ALARM_CATALOG.range_start <= ALARM_CATALOG.summary.address
        assert ALARM_CATALOG.range_count >= len(ALARM_CATALOG.monitored)

    def test_non_alarm_entries_excluded(self):
        """Humidistat status and remote on/off must not appear in monitored or skipped."""
        all_descriptions = {d.description.casefold() for d in ALARM_CATALOG.monitored}
        all_descriptions |= {d.description.casefold() for d in ALARM_CATALOG.skipped}
        for prefix in NON_ALARM_PREFIXES:
            for desc in all_descriptions:
                assert not desc.startswith(prefix), f"Non-alarm entry found: {desc}"


# ── bit_is_set ───────────────────────────────────────────────────────────

class TestBitIsSet:
    def _make_catalog(self) -> AlarmCatalog:
        return AlarmCatalog(
            summary=AlarmDefinition(address=10, description="summary"),
            monitored=(AlarmDefinition(address=12, description="alarm A"),),
            skipped=(),
            range_start=10,
            range_count=5,
        )

    def test_bit_set_within_range(self):
        catalog = self._make_catalog()
        bits = [False, False, True, False, False]  # index 2 = address 12
        assert catalog.bit_is_set(bits, 12) is True

    def test_bit_not_set_within_range(self):
        catalog = self._make_catalog()
        bits = [False, False, False, False, False]
        assert catalog.bit_is_set(bits, 12) is False

    def test_out_of_range_returns_false(self):
        catalog = self._make_catalog()
        bits = [True, True, True]
        assert catalog.bit_is_set(bits, 99) is False

    def test_empty_bits_returns_false(self):
        catalog = self._make_catalog()
        assert catalog.bit_is_set([], 10) is False

    def test_address_below_range_start(self):
        catalog = self._make_catalog()
        bits = [True] * 5
        assert catalog.bit_is_set(bits, 5) is False


# ── active_monitored / active_skipped ────────────────────────────────────

class TestActiveFiltering:
    def test_active_monitored_returns_matching(self):
        """Using the real catalog, set the first monitored alarm bit."""
        first = ALARM_CATALOG.monitored[0]
        bits = [False] * ALARM_CATALOG.range_count
        idx = first.address - ALARM_CATALOG.range_start
        bits[idx] = True

        result = ALARM_CATALOG.active_monitored(bits)
        assert len(result) >= 1
        assert first in result

    def test_active_monitored_empty_when_no_bits(self):
        bits = [False] * ALARM_CATALOG.range_count
        assert ALARM_CATALOG.active_monitored(bits) == []

    def test_active_skipped_returns_cylinder_2(self):
        if not ALARM_CATALOG.skipped:
            pytest.skip("No skipped alarms in the catalog")
        first_skipped = ALARM_CATALOG.skipped[0]
        bits = [False] * ALARM_CATALOG.range_count
        idx = first_skipped.address - ALARM_CATALOG.range_start
        bits[idx] = True

        result = ALARM_CATALOG.active_skipped(bits)
        assert first_skipped in result

    def test_active_skipped_empty_when_no_bits(self):
        bits = [False] * ALARM_CATALOG.range_count
        assert ALARM_CATALOG.active_skipped(bits) == []
