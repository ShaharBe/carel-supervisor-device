"""Tests for resolve_node_editor() and annotate_menu_tree()."""

from __future__ import annotations

import pytest

from menu_service import annotate_menu_tree, collect_dashboard_sync_map, resolve_node_editor


# ── helper ───────────────────────────────────────────────────────────────

def _leaf(
    path: str = "99.1",
    kind: str = "leaf",
    family: str | None = None,
    index: int = 0,
    access: str = "R/W",
    range_or_options: str | None = None,
    display_label: str = "Test",
    editor: dict | None = None,
    signed: bool = False,
) -> dict:
    register = None
    if family is not None:
        register = {"family": family, "index": index, "access": access}
        if signed:
            register["signed"] = True
    return {
        "path": path,
        "title": display_label,
        "display_label": display_label,
        "raw_text": display_label,
        "kind": kind,
        "children": [],
        "register": register,
        "range_or_options": range_or_options,
        "editor": editor,
    }


# ── resolve_node_editor — capability flags ───────────────────────────────

class TestResolvedEditorFlags:
    def test_analog_rw_is_modbus_backed_writable(self):
        node = _leaf(family="A", access="R/W")
        result = resolve_node_editor(node)
        assert result["modbus_backed"] is True
        assert result["writable"] is True
        assert result["editable"] is True

    def test_analog_readonly_is_not_editable(self):
        node = _leaf(family="A", access="R")
        result = resolve_node_editor(node)
        assert result["modbus_backed"] is True
        assert result["writable"] is False
        assert result["editable"] is False

    def test_digital_rw_is_editable(self):
        node = _leaf(family="D", access="R/W")
        result = resolve_node_editor(node)
        assert result["modbus_backed"] is True
        assert result["writable"] is True
        assert result["editable"] is True

    def test_no_register_leaf_is_editable_locally(self):
        node = _leaf()
        result = resolve_node_editor(node)
        assert result["modbus_backed"] is False
        assert result["writable"] is False
        # No editor_type derivable → not editable
        assert result["editable"] is False

    def test_caption_is_not_editable(self):
        node = _leaf(kind="caption", family="A", access="R/W")
        result = resolve_node_editor(node)
        assert result["editable"] is False

    def test_menu_is_not_editable(self):
        node = _leaf(kind="menu")
        result = resolve_node_editor(node)
        assert result["editable"] is False


# ── resolve_node_editor — type inference ─────────────────────────────────

class TestResolvedEditorType:
    def test_boolean_from_digital(self):
        node = _leaf(family="D", range_or_options="Auto/Off")
        result = resolve_node_editor(node)
        assert result["type"] == "boolean"

    def test_integer_from_family_i(self):
        node = _leaf(family="I")
        result = resolve_node_editor(node)
        assert result["type"] == "integer"
        assert result["step"] == "1"

    def test_float_from_decimal_hint(self):
        node = _leaf(family="A", range_or_options="2..19.9")
        result = resolve_node_editor(node)
        assert result["type"] == "float"
        assert result["step"] == "any"

    def test_enum_from_choice_tokens(self):
        node = _leaf(family="I", range_or_options="yes,no")
        result = resolve_node_editor(node)
        assert result["type"] == "enum"

    def test_explicit_editor_type(self):
        node = _leaf(
            family="I",
            editor={"type": "enum", "options": [{"value": 0, "label": "Foo"}]},
        )
        result = resolve_node_editor(node)
        assert result["type"] == "enum"
        assert result["options"] == [{"value": 0, "label": "Foo"}]

    def test_no_register_returns_none_type(self):
        node = _leaf()
        result = resolve_node_editor(node)
        assert result["type"] is None


# ── resolve_node_editor — options ────────────────────────────────────────

class TestResolvedEditorOptions:
    def test_boolean_default_labels(self):
        node = _leaf(family="D")
        result = resolve_node_editor(node)
        assert len(result["options"]) == 2
        assert result["options"][0]["value"] is True
        assert result["options"][0]["label"] == "yes"
        assert result["options"][1]["value"] is False
        assert result["options"][1]["label"] == "no"

    def test_boolean_custom_labels(self):
        node = _leaf(family="D", range_or_options="Auto/Off")
        result = resolve_node_editor(node)
        assert result["options"][0]["label"] == "Auto"
        assert result["options"][1]["label"] == "Off"

    def test_enum_options(self):
        node = _leaf(family="I", range_or_options="foo,bar,baz")
        result = resolve_node_editor(node)
        assert result["type"] == "enum"
        assert len(result["options"]) == 3
        assert result["options"][0] == {"value": 0, "label": "foo"}
        assert result["options"][2] == {"value": 2, "label": "baz"}

    def test_numeric_has_empty_options(self):
        node = _leaf(family="I")
        result = resolve_node_editor(node)
        assert result["options"] == []


# ── resolve_node_editor — scale & limits ─────────────────────────────────

class TestResolvedEditorScaleLimits:
    def test_setpoint_path(self):
        node = _leaf(path="2.1", family="A", display_label="Setpoint")
        result = resolve_node_editor(node)
        assert result["scale"] == 10.0
        assert result["limits"] is not None
        assert result["limits"]["low"] == -20.0
        assert result["limits"]["high"] == 100.0

    def test_max_production_path(self):
        node = _leaf(path="2.3", family="I")
        result = resolve_node_editor(node)
        assert result["scale"] == 1.0
        assert result["limits"] is not None
        assert result["limits"]["low"] == 0.0
        assert result["limits"]["high"] == 1000.0

    def test_prop_band_path(self):
        node = _leaf(path="2.4", family="A", display_label="Prop. band")
        result = resolve_node_editor(node)
        assert result["scale"] == 10.0
        assert result["limits"] is not None
        assert result["limits"]["low"] == 0.0
        assert result["limits"]["high"] == 100.0

    def test_limits_from_range_hint(self):
        node = _leaf(family="I", range_or_options="0...100")
        result = resolve_node_editor(node)
        assert result["limits"] == {"low": 0.0, "high": 100.0}

    def test_no_limits(self):
        node = _leaf(family="I")
        result = resolve_node_editor(node)
        assert result["limits"] is None

    def test_explicit_editor_scale(self):
        node = _leaf(family="A", editor={"type": "float", "scale": 100})
        result = resolve_node_editor(node)
        assert result["scale"] == 100.0

    def test_explicit_editor_step(self):
        node = _leaf(family="A", editor={"type": "float", "step": 0.01})
        result = resolve_node_editor(node)
        assert result["step"] == 0.01

    def test_signed_register_flag(self):
        node = _leaf(family="A", signed=True, editor={"type": "float", "scale": 10})
        result = resolve_node_editor(node)
        assert result["signed"] is True


# ── annotate_menu_tree ───────────────────────────────────────────────────

class TestAnnotateMenuTree:
    def test_annotates_all_nodes(self, menu_root):
        annotate_menu_tree(menu_root)
        # The fixture is the full display_menu.json root. Walk and check.
        from menu_service import walk_menu_nodes

        for node in walk_menu_nodes(menu_root):
            assert "resolved_editor" in node, f"Node {node.get('path')} missing resolved_editor"
            editor = node["resolved_editor"]
            assert "type" in editor
            assert "modbus_backed" in editor
            assert "writable" in editor
            assert "editable" in editor

    def test_setpoint_node_has_correct_metadata(self, menu_root):
        annotate_menu_tree(menu_root)
        from menu_service import walk_menu_nodes

        setpoint = None
        for node in walk_menu_nodes(menu_root):
            if node.get("path") == "2.1":
                setpoint = node
                break

        assert setpoint is not None
        editor = setpoint["resolved_editor"]
        assert editor["type"] == "float"
        assert editor["modbus_backed"] is True
        assert editor["writable"] is True
        assert editor["editable"] is True
        assert editor["scale"] == 10.0
        assert editor["limits"]["low"] == -20.0
        assert editor["limits"]["high"] == 100.0

    def test_humidifier_node_is_boolean(self, menu_root):
        annotate_menu_tree(menu_root)
        from menu_service import walk_menu_nodes

        humidifier = None
        for node in walk_menu_nodes(menu_root):
            if node.get("path") == "2.2":
                humidifier = node
                break

        assert humidifier is not None
        editor = humidifier["resolved_editor"]
        assert editor["type"] == "boolean"
        assert editor["options"][0]["label"] == "Auto"
        assert editor["options"][1]["label"] == "Off"


# ── collect_dashboard_sync_map ───────────────────────────────────────────

def test_probe_config_signed_fields(menu_root):
    annotate_menu_tree(menu_root)
    from menu_service import walk_menu_nodes

    nodes = {node.get("path"): node for node in walk_menu_nodes(menu_root)}
    integer_paths = ["3.2.2.2", "3.2.2.3", "3.2.2.6", "3.2.2.7"]
    float_paths = ["3.2.2.4", "3.2.2.8"]

    for path in integer_paths:
        editor = nodes[path]["resolved_editor"]
        assert nodes[path]["register"]["signed"] is True
        assert editor["signed"] is True
        assert editor["type"] == "integer"
        assert editor["scale"] == 1.0
        assert editor["limits"] == {"low": -250.0, "high": 250.0}

    for path in float_paths:
        editor = nodes[path]["resolved_editor"]
        assert nodes[path]["register"]["signed"] is True
        assert editor["signed"] is True
        assert editor["type"] == "float"
        assert editor["scale"] == 10.0
        assert editor["limits"] == {"low": -250.0, "high": 250.0}


class TestCollectDashboardSyncMap:
    def test_collects_all_annotated_nodes(self, menu_root):
        sync_map = collect_dashboard_sync_map(menu_root)
        assert isinstance(sync_map, dict)
        assert len(sync_map) == 10

    def test_setpoint_mapping(self, menu_root):
        sync_map = collect_dashboard_sync_map(menu_root)
        assert sync_map["2.1"] == "last_setpoint_c"

    def test_humidifier_mapping(self, menu_root):
        sync_map = collect_dashboard_sync_map(menu_root)
        assert sync_map["2.2"] == "info.humidifier_network_enabled"

    def test_nested_info_mappings(self, menu_root):
        sync_map = collect_dashboard_sync_map(menu_root)
        assert sync_map["4.1"] == "info.humidifier_status"
        assert sync_map["4.6"] == "info.conductivity"
        assert sync_map["5.2"] == "info.cyl1_hours"
        assert sync_map["6.2"] == "info.cyl1_status"
        assert sync_map["6.3"] == "info.cyl1_phase"

    def test_device_time_mapping(self, menu_root):
        sync_map = collect_dashboard_sync_map(menu_root)
        assert sync_map["5.4"] == "device_time_display"

    def test_empty_tree_returns_empty(self):
        root = {"path": "", "kind": "root", "children": []}
        assert collect_dashboard_sync_map(root) == {}

    def test_ignores_nodes_without_annotation(self):
        root = {
            "path": "",
            "kind": "root",
            "children": [
                {"path": "1.1", "kind": "leaf", "children": []},
                {"path": "1.2", "kind": "leaf", "children": [], "dashboard_sync": "some_key"},
            ],
        }
        sync_map = collect_dashboard_sync_map(root)
        assert sync_map == {"1.2": "some_key"}


# ── collect_dashboard_sync_map ───────────────────────────────────────────

class TestCollectDashboardSyncMap:
    def test_collects_all_annotated_nodes(self, menu_root):
        sync_map = collect_dashboard_sync_map(menu_root)
        assert isinstance(sync_map, dict)
        assert len(sync_map) == 10

    def test_setpoint_mapping(self, menu_root):
        sync_map = collect_dashboard_sync_map(menu_root)
        assert sync_map["2.1"] == "last_setpoint_c"

    def test_humidifier_mapping(self, menu_root):
        sync_map = collect_dashboard_sync_map(menu_root)
        assert sync_map["2.2"] == "info.humidifier_network_enabled"

    def test_nested_info_mappings(self, menu_root):
        sync_map = collect_dashboard_sync_map(menu_root)
        assert sync_map["4.1"] == "info.humidifier_status"
        assert sync_map["4.6"] == "info.conductivity"
        assert sync_map["5.2"] == "info.cyl1_hours"
        assert sync_map["6.2"] == "info.cyl1_status"
        assert sync_map["6.3"] == "info.cyl1_phase"

    def test_device_time_mapping(self, menu_root):
        sync_map = collect_dashboard_sync_map(menu_root)
        assert sync_map["5.4"] == "device_time_display"

    def test_empty_tree_returns_empty(self):
        root = {"path": "", "kind": "root", "children": []}
        assert collect_dashboard_sync_map(root) == {}

    def test_ignores_nodes_without_annotation(self):
        root = {
            "path": "",
            "kind": "root",
            "children": [
                {"path": "1.1", "kind": "leaf", "children": []},
                {"path": "1.2", "kind": "leaf", "children": [], "dashboard_sync": "some_key"},
            ],
        }
        sync_map = collect_dashboard_sync_map(root)
        assert sync_map == {"1.2": "some_key"}
