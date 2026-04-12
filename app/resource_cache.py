from __future__ import annotations

from typing import Any


RESOURCE_FAMILIES = {"A", "I", "D"}


def resource_key(family: str, index: int) -> str:
    """Return the canonical identity for one CAREL Modbus value."""
    normalized_family = str(family).strip().upper()
    if normalized_family not in RESOURCE_FAMILIES:
        raise ValueError(f"Unsupported resource family: {family!r}")

    normalized_index = int(index)
    if normalized_index < 0:
        raise ValueError("Resource index must be >= 0")

    return f"{normalized_family}:{normalized_index}"


def resource_key_for_register(register: dict[str, Any] | None) -> str | None:
    if not isinstance(register, dict):
        return None

    family = register.get("family")
    index = register.get("index")
    if family is None or index is None:
        return None

    return resource_key(str(family), int(index))


def resource_key_for_menu_node(node: dict[str, Any] | None) -> str | None:
    if not isinstance(node, dict):
        return None

    return resource_key_for_register(node.get("register"))
