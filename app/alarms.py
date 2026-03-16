from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


ALARMS_CSV_PATH = Path(__file__).resolve().with_name("modbus_alarms.csv")
SUMMARY_DESCRIPTION = "at least 1 alarm is active"
NON_ALARM_PREFIXES = (
    "humidistat status:",
    "remote on/off status:",
)
CYLINDER_2_PREFIX = "cylinder 2:"


@dataclass(frozen=True)
class AlarmDefinition:
    address: int
    description: str


@dataclass(frozen=True)
class AlarmCatalog:
    summary: AlarmDefinition
    monitored: tuple[AlarmDefinition, ...]
    skipped: tuple[AlarmDefinition, ...]
    range_start: int
    range_count: int

    def bit_is_set(self, bits: Sequence[bool], address: int) -> bool:
        index = address - self.range_start
        return 0 <= index < len(bits) and bool(bits[index])

    def active_monitored(self, bits: Sequence[bool]) -> list[AlarmDefinition]:
        return [
            definition
            for definition in self.monitored
            if self.bit_is_set(bits, definition.address)
        ]

    def active_skipped(self, bits: Sequence[bool]) -> list[AlarmDefinition]:
        return [
            definition
            for definition in self.skipped
            if self.bit_is_set(bits, definition.address)
        ]


def load_alarm_catalog(csv_path: Path = ALARMS_CSV_PATH) -> AlarmCatalog:
    summary: AlarmDefinition | None = None
    monitored: list[AlarmDefinition] = []
    skipped: list[AlarmDefinition] = []
    all_addresses: list[int] = []

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if len(row) < 3:
                continue

            address = int(row[0].strip())
            description = row[2].strip()
            definition = AlarmDefinition(address=address, description=description)
            description_lower = description.casefold()

            all_addresses.append(address)

            if description_lower == SUMMARY_DESCRIPTION:
                summary = definition
                continue

            if any(description_lower.startswith(prefix) for prefix in NON_ALARM_PREFIXES):
                continue

            if description_lower.startswith(CYLINDER_2_PREFIX):
                # This installation does not use cylinder 2, so those coil alarms are
                # intentionally skipped for now even though they share the same alarm bank.
                skipped.append(definition)
                continue

            monitored.append(definition)

    if summary is None:
        raise RuntimeError(
            f"Alarm summary bit '{SUMMARY_DESCRIPTION}' was not found in {csv_path}"
        )
    if not all_addresses:
        raise RuntimeError(f"No alarm coil definitions found in {csv_path}")

    return AlarmCatalog(
        summary=summary,
        monitored=tuple(monitored),
        skipped=tuple(skipped),
        range_start=min(all_addresses),
        range_count=max(all_addresses) - min(all_addresses) + 1,
    )


ALARM_CATALOG = load_alarm_catalog()
