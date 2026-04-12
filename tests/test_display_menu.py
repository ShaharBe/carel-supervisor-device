"""Tests for display_menu.py — text parsing, JSON loading, and node helpers."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from display_menu import (
    _build_node,
    _detect_page_direction,
    _empty_menu_root,
    _extract_bracket_hint,
    _extract_display_label,
    _extract_register,
    _load_menu_root_from_json,
    _split_note,
    load_display_menu,
    parse_menu_definition,
)


# ── _split_note ──────────────────────────────────────────────────────────

class TestSplitNote:
    def test_no_note(self):
        body, note = _split_note("Setpoint (A,19,R/W)")
        assert body == "Setpoint (A,19,R/W)"
        assert note is None

    def test_with_note(self):
        body, note = _split_note('Limit: (I,145,R/W) Note: appears only if limit')
        assert body == "Limit: (I,145,R/W)"
        assert note == "appears only if limit"

    def test_empty_note(self):
        body, note = _split_note("Something Note:")
        assert body == "Something"
        assert note is None

    def test_multiple_note_markers(self):
        body, note = _split_note("A Note: first Note: second")
        assert body == "A"
        assert note == "first Note: second"


# ── _extract_display_label ───────────────────────────────────────────────

class TestExtractDisplayLabel:
    def test_plain_label(self):
        assert _extract_display_label("Setpoint", is_caption=False) == "Setpoint"

    def test_label_with_register_suffix(self):
        assert _extract_display_label("Setpoint (A,19,R/W)", is_caption=False) == "Setpoint"

    def test_label_with_bracket_hint(self):
        assert _extract_display_label("Min value [0...250]", is_caption=False) == "Min value"

    def test_label_with_bracket_space(self):
        assert _extract_display_label("Warning [yes,no]", is_caption=False) == "Warning"

    def test_caption_prefix_stripped(self):
        label = _extract_display_label("(Caption) Cylinder lifetime", is_caption=True)
        assert label == "Cylinder lifetime"

    def test_stub_marker(self):
        label = _extract_display_label("Something (Stub)", is_caption=False)
        assert label == "Something"


# ── _extract_register ────────────────────────────────────────────────────

class TestExtractRegister:
    def test_analog_rw(self):
        result = _extract_register("Setpoint (A,19,R/W) (Celsius)")
        assert result == {"family": "A", "index": 19, "access": "R/W"}

    def test_integer_rw(self):
        result = _extract_register("Select Regulation (I,143,R/W)")
        assert result == {"family": "I", "index": 143, "access": "R/W"}

    def test_digital_rw(self):
        result = _extract_register("Humidifier (D,8,R/W)")
        assert result == {"family": "D", "index": 8, "access": "R/W"}

    def test_read_only(self):
        result = _extract_register("Current (A,11,R)")
        assert result == {"family": "A", "index": 11, "access": "R"}

    def test_no_register(self):
        assert _extract_register("Manual procedure") is None

    def test_malformed(self):
        assert _extract_register("Something (X,abc,Z)") is None


# ── _extract_bracket_hint ────────────────────────────────────────────────

class TestExtractBracketHint:
    def test_yes_no(self):
        assert _extract_bracket_hint("Warning [yes,no] (D,47,R/W)") == "yes,no"

    def test_numeric_range(self):
        assert _extract_bracket_hint("Max Prod. [20...100]") == "20...100"

    def test_no_brackets(self):
        assert _extract_bracket_hint("Setpoint (A,19,R/W)") is None

    def test_empty_brackets(self):
        assert _extract_bracket_hint("Something []") is None


# ── _detect_page_direction ───────────────────────────────────────────────

class TestDetectPageDirection:
    def test_next_page(self):
        assert _detect_page_direction("Next page") == "next"

    def test_prev_page(self):
        assert _detect_page_direction("Prev page") == "prev"

    def test_next_page_with_dot(self):
        assert _detect_page_direction("Next page.") == "next"

    def test_unrelated_label(self):
        assert _detect_page_direction("Setpoint") is None

    def test_empty_string(self):
        assert _detect_page_direction("") is None


# ── parse_menu_definition ────────────────────────────────────────────────

class TestParseMenuDefinition:
    SAMPLE = textwrap.dedent("""\
        1. Main Menu
        1.1. Setpoint (A,19,R/W) (Celsius)
        1.2. Max Prod. (A,14,R/W) [20...100]
        2. (Caption) Status Header
    """)

    def test_root_kind(self):
        root = parse_menu_definition(self.SAMPLE)
        assert root["kind"] == "root"

    def test_top_level_children_count(self):
        root = parse_menu_definition(self.SAMPLE)
        assert len(root["children"]) == 2

    def test_submenu_path(self):
        root = parse_menu_definition(self.SAMPLE)
        main = root["children"][0]
        assert main["path"] == "1"
        assert main["kind"] == "menu"

    def test_leaf_path_and_register(self):
        root = parse_menu_definition(self.SAMPLE)
        main = root["children"][0]
        setpoint = main["children"][0]
        assert setpoint["path"] == "1.1"
        assert setpoint["kind"] == "leaf"
        assert setpoint["register"]["family"] == "A"
        assert setpoint["register"]["index"] == 19

    def test_caption_kind(self):
        root = parse_menu_definition(self.SAMPLE)
        caption = root["children"][1]
        assert caption["kind"] == "caption"

    def test_blank_and_root_lines_skipped(self):
        text = "Root\n\n1. Item\n"
        root = parse_menu_definition(text)
        assert len(root["children"]) == 1

    def test_empty_input(self):
        root = parse_menu_definition("")
        assert root["kind"] == "root"
        assert root["children"] == []


# ── load_display_menu (real JSON) ────────────────────────────────────────

class TestLoadDisplayMenu:
    def test_real_json_loads_successfully(self):
        result = load_display_menu()
        assert result["ok"] is True
        assert result["root"]["kind"] == "root"
        assert len(result["root"]["children"]) > 0

    def test_real_json_has_source_path(self):
        result = load_display_menu()
        assert result["source_path"] is not None

    def test_manual_procedure_alt_menu_uses_undocumented_coils(self):
        result = load_display_menu()
        root = result["root"]

        def find_node(node: dict, path: str) -> dict | None:
            if node.get("path") == path:
                return node
            for child in node.get("children", []):
                found = find_node(child, path)
                if found is not None:
                    return found
            return None

        menu = find_node(root, "3.3.4")
        assert menu is not None
        assert menu["title"] == "Manual procedure (alt)"

        leaves = menu["children"]
        assert [leaf["title"] for leaf in leaves] == [
            "Manual procedure",
            "Power contactor",
            "Fill valve",
            "Drain pump",
            "Alarm",
            "Dehumidifier",
        ]
        assert [
            (leaf["register"]["family"], leaf["register"]["index"], leaf["register"]["access"])
            for leaf in leaves
        ] == [
            ("D", 70, "R/W"),
            ("D", 71, "R/W"),
            ("D", 72, "R/W"),
            ("D", 73, "R/W"),
            ("D", 74, "R/W"),
            ("D", 75, "R/W"),
        ]


# ── _load_menu_root_from_json ────────────────────────────────────────────

class TestLoadMenuRootFromJson:
    def test_valid_json(self, tmp_path: Path):
        data = {"path": "", "title": "Root", "children": []}
        path = tmp_path / "menu.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        root = _load_menu_root_from_json(path)
        assert root["children"] == []

    def test_missing_children_raises(self, tmp_path: Path):
        path = tmp_path / "menu.json"
        path.write_text('{"path": ""}', encoding="utf-8")
        with pytest.raises(ValueError, match="children"):
            _load_menu_root_from_json(path)

    def test_non_dict_raises(self, tmp_path: Path):
        path = tmp_path / "menu.json"
        path.write_text("[1,2,3]", encoding="utf-8")
        with pytest.raises(ValueError, match="object"):
            _load_menu_root_from_json(path)
