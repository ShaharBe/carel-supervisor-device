"""Tests for menu logic in app.py — inference, coercion, formatting, limits."""

from __future__ import annotations

import pytest

from app import (
    coerce_menu_write,
    format_limit,
    infer_menu_editor_type,
    infer_menu_numeric_limits,
    infer_menu_numeric_scale,
    is_menu_node_modbus_backed,
    is_menu_node_writable,
    normalize_editor_options,
    parse_choice_tokens,
    parse_menu_boolean_value,
)


# ── helpers for building synthetic nodes ─────────────────────────────────

def _leaf(
    path: str = "99.1",
    family: str | None = None,
    index: int = 0,
    access: str = "R/W",
    range_or_options: str | None = None,
    display_label: str = "Test",
    editor: dict | None = None,
) -> dict:
    register = None
    if family is not None:
        register = {"family": family, "index": index, "access": access}
    return {
        "path": path,
        "title": display_label,
        "display_label": display_label,
        "raw_text": display_label,
        "kind": "leaf",
        "children": [],
        "register": register,
        "range_or_options": range_or_options,
        "editor": editor,
    }


# ── parse_choice_tokens ─────────────────────────────────────────────────

class TestParseChoiceTokens:
    def test_comma_separated(self):
        assert parse_choice_tokens("yes,no") == ["yes", "no"]

    def test_slash_separated(self):
        assert parse_choice_tokens("Auto/Off") == ["Auto", "Off"]

    def test_comma_with_spaces(self):
        assert parse_choice_tokens(" a , b , c ") == ["a", "b", "c"]

    def test_quoted_tokens(self):
        assert parse_choice_tokens('"N.O.","N.C."') == ["N.O.", "N.C."]

    def test_no_separator(self):
        assert parse_choice_tokens("single") == []

    def test_empty_string(self):
        assert parse_choice_tokens("") == []

    def test_none(self):
        assert parse_choice_tokens(None) == []


# ── infer_menu_editor_type ───────────────────────────────────────────────

class TestInferMenuEditorType:
    def test_family_d_is_boolean(self):
        node = _leaf(family="D")
        assert infer_menu_editor_type(node) == "boolean"

    def test_family_a_with_decimal_hint_is_float(self):
        node = _leaf(family="A", range_or_options="2..19.9")
        assert infer_menu_editor_type(node) == "float"

    def test_family_a_with_float_label_is_float(self):
        node = _leaf(family="A", display_label="Prop. band")
        assert infer_menu_editor_type(node) == "float"

    def test_family_i_plain_is_integer(self):
        node = _leaf(family="I")
        assert infer_menu_editor_type(node) == "integer"

    def test_choice_tokens_enum(self):
        # A node with range_or_options that parses to >=2 choices and no numeric range
        node = _leaf(family="I", range_or_options="yes,no")
        assert infer_menu_editor_type(node) == "enum"

    def test_explicit_editor_type(self):
        node = _leaf(family="I", editor={"type": "enum", "options": []})
        assert infer_menu_editor_type(node) == "enum"

    def test_no_register_returns_none(self):
        node = _leaf()
        assert infer_menu_editor_type(node) is None


# ── infer_menu_numeric_scale ─────────────────────────────────────────────

class TestInferMenuNumericScale:
    def test_setpoint_path(self):
        node = _leaf(path="2.1", family="A")
        assert infer_menu_numeric_scale(node, "float") == 10.0

    def test_max_production_path(self):
        node = _leaf(path="2.3", family="A")
        assert infer_menu_numeric_scale(node, "integer") == 1.0

    def test_prop_band_path(self):
        node = _leaf(path="2.4", family="A")
        assert infer_menu_numeric_scale(node, "float") == 10.0

    def test_explicit_editor_scale(self):
        node = _leaf(family="A", editor={"type": "float", "scale": 5})
        assert infer_menu_numeric_scale(node, "float") == 5.0

    def test_float_fallback(self):
        node = _leaf(path="99.1", family="A", range_or_options="0.5..10.0")
        assert infer_menu_numeric_scale(node, "float") == 10.0

    def test_integer_fallback(self):
        node = _leaf(path="99.1", family="I")
        assert infer_menu_numeric_scale(node, "integer") == 1.0


# ── infer_menu_numeric_limits ────────────────────────────────────────────

class TestInferMenuNumericLimits:
    def test_setpoint_path(self):
        node = _leaf(path="2.1")
        assert infer_menu_numeric_limits(node) == (-20.0, 100.0)

    def test_max_production_path(self):
        node = _leaf(path="2.3")
        assert infer_menu_numeric_limits(node) == (0.0, 1000.0)

    def test_prop_band_path(self):
        node = _leaf(path="2.4")
        assert infer_menu_numeric_limits(node) == (0.0, 100.0)

    def test_regex_from_triple_dot_hint(self):
        node = _leaf(range_or_options="20...100")
        assert infer_menu_numeric_limits(node) == (20.0, 100.0)

    def test_regex_from_double_dot_hint(self):
        node = _leaf(range_or_options="2..19.9")
        assert infer_menu_numeric_limits(node) == (2.0, 19.9)

    def test_no_hint_returns_none(self):
        node = _leaf()
        assert infer_menu_numeric_limits(node) == (None, None)

    def test_non_numeric_hint_returns_none(self):
        node = _leaf(range_or_options="yes,no")
        assert infer_menu_numeric_limits(node) == (None, None)


# ── parse_menu_boolean_value ─────────────────────────────────────────────

class TestParseMenuBooleanValue:
    @pytest.mark.parametrize("value", [True, 1, 1.0, "true", "True", "1", "yes", "on", "enabled", "auto"])
    def test_truthy_values(self, value):
        assert parse_menu_boolean_value(value) is True

    @pytest.mark.parametrize("value", [False, 0, 0.0, "false", "False", "0", "no", "off", "disabled"])
    def test_falsy_values(self, value):
        assert parse_menu_boolean_value(value) is False

    def test_garbage_raises(self):
        with pytest.raises(ValueError, match="boolean"):
            parse_menu_boolean_value("maybe")

    def test_none_raises(self):
        with pytest.raises(ValueError):
            parse_menu_boolean_value(None)


# ── normalize_editor_options ─────────────────────────────────────────────

class TestNormalizeEditorOptions:
    def test_explicit_editor_options(self):
        node = _leaf(
            family="I",
            editor={
                "type": "enum",
                "options": [
                    {"value": 0, "label": "Off"},
                    {"value": 1, "label": "On"},
                ],
            },
        )
        result = normalize_editor_options(node)
        assert result == [{"value": 0, "label": "Off"}, {"value": 1, "label": "On"}]

    def test_fallback_to_choice_tokens(self):
        node = _leaf(family="I", range_or_options="Alpha,Beta,Gamma")
        result = normalize_editor_options(node)
        assert len(result) == 3
        assert result[0] == {"value": 0, "label": "Alpha"}
        assert result[2] == {"value": 2, "label": "Gamma"}

    def test_no_options_returns_empty(self):
        node = _leaf(family="I")
        result = normalize_editor_options(node)
        assert result == []


# ── is_menu_node_modbus_backed / is_menu_node_writable ───────────────────

class TestMenuNodePredicates:
    def test_backed_with_analog(self):
        assert is_menu_node_modbus_backed(_leaf(family="A")) is True

    def test_backed_with_integer(self):
        assert is_menu_node_modbus_backed(_leaf(family="I")) is True

    def test_backed_with_digital(self):
        assert is_menu_node_modbus_backed(_leaf(family="D")) is True

    def test_not_backed_without_register(self):
        assert is_menu_node_modbus_backed(_leaf()) is False

    def test_writable_rw(self):
        assert is_menu_node_writable(_leaf(family="A", access="R/W")) is True

    def test_not_writable_r(self):
        assert is_menu_node_writable(_leaf(family="A", access="R")) is False

    def test_not_writable_without_register(self):
        assert is_menu_node_writable(_leaf()) is False


# ── format_limit ─────────────────────────────────────────────────────────

class TestFormatLimit:
    def test_integer_like_float(self):
        assert format_limit(1.0) == "1"

    def test_half(self):
        assert format_limit(0.5) == "0.5"

    def test_hundred(self):
        assert format_limit(100.0) == "100"

    def test_negative(self):
        assert format_limit(-20.0) == "-20"


# ── coerce_menu_write ────────────────────────────────────────────────────

class TestCoerceMenuWrite:
    def test_boolean_node(self):
        node = _leaf(family="D", range_or_options="yes,no")
        value, raw = coerce_menu_write(node, "yes")
        assert value is True
        assert raw is True

    def test_boolean_node_false(self):
        node = _leaf(family="D", range_or_options="off,on")
        value, raw = coerce_menu_write(node, "off")
        assert value is False
        assert raw is False

    def test_enum_valid_option(self):
        node = _leaf(
            family="I",
            editor={
                "type": "enum",
                "options": [
                    {"value": 0, "label": "Off"},
                    {"value": 5, "label": "Temperature probe"},
                ],
            },
        )
        value, raw = coerce_menu_write(node, "5")
        assert value == 5
        assert raw == 5

    def test_enum_invalid_option_raises(self):
        node = _leaf(
            family="I",
            editor={
                "type": "enum",
                "options": [{"value": 0, "label": "Off"}],
            },
        )
        with pytest.raises(ValueError, match="enum options"):
            coerce_menu_write(node, "99")

    def test_integer_node(self):
        node = _leaf(path="99.1", family="I", range_or_options="0...2000")
        value, raw = coerce_menu_write(node, "500")
        assert value == 500
        assert raw == 500

    def test_float_node_with_scaling(self):
        node = _leaf(path="2.1", family="A", range_or_options="-20..100", display_label="Setpoint")
        value, raw = coerce_menu_write(node, "28.0")
        assert raw == 280
        assert abs(value - 28.0) < 0.01

    def test_out_of_range_raises(self):
        node = _leaf(path="99.1", family="I", range_or_options="0...100")
        with pytest.raises(ValueError, match="out of allowed range"):
            coerce_menu_write(node, "200")

    def test_exact_boundary_accepted(self):
        node = _leaf(path="99.1", family="I", range_or_options="0...100")
        value, raw = coerce_menu_write(node, "100")
        assert value == 100

    def test_no_register_raises(self):
        node = _leaf()
        with pytest.raises(ValueError, match="Unable to infer"):
            coerce_menu_write(node, "anything")
