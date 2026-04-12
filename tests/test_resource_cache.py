from __future__ import annotations

import pytest

from menu_service import collect_dashboard_sync_map, walk_menu_nodes
from resource_cache import resource_key, resource_key_for_menu_node, resource_key_for_register


def test_resource_key_formats_supported_families():
    assert resource_key("A", 19) == "A:19"
    assert resource_key("i", 140) == "I:140"
    assert resource_key("D", 54) == "D:54"


def test_resource_key_rejects_unknown_family():
    with pytest.raises(ValueError, match="Unsupported"):
        resource_key("X", 1)


def test_resource_key_for_register_uses_family_and_index():
    register = {"family": "I", "index": 140, "access": "R"}

    assert resource_key_for_register(register) == "I:140"


def test_resource_key_for_menu_node_uses_register_metadata():
    node = {"path": "6.2", "register": {"family": "I", "index": 140, "access": "R"}}

    assert resource_key_for_menu_node(node) == "I:140"


def test_resource_key_for_unmapped_node_returns_none():
    assert resource_key_for_menu_node({"path": "4.4", "register": None}) is None


def test_dashboard_sync_nodes_have_expected_resource_keys(menu_root):
    expected_resource_keys = {
        "2.1": "A:19",
        "2.2": "D:8",
        "2.3": "A:14",
        "2.4": "A:20",
        "4.1": "I:136",
        "4.6": "I:137",
        "5.2": "I:165",
        "5.4": None,
        "6.2": "I:140",
        "6.3": "I:139",
    }
    nodes_by_path = {str(node.get("path")): node for node in walk_menu_nodes(menu_root)}
    sync_map = collect_dashboard_sync_map(menu_root)

    assert set(sync_map) == set(expected_resource_keys)
    for path, expected_key in expected_resource_keys.items():
        assert resource_key_for_menu_node(nodes_by_path[path]) == expected_key
