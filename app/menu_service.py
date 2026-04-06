from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from flask import jsonify, request

from display_menu import load_display_menu
from modbus_map import (
    HUMIDIFIER_REMOTE_ONOFF_COIL,
    HUMIDIFIER_SUPERVISOR_ENABLE_COIL,
    MAX_PRODUCTION_ADDR,
    MAX_PRODUCTION_MAX_PCT,
    MAX_PRODUCTION_MIN_PCT,
    MAX_PRODUCTION_SCALE,
    PROP_BAND_ADDR,
    PROP_BAND_MAX_C,
    PROP_BAND_MIN_C,
    PROP_BAND_SCALE,
    SETPOINT_ADDR,
    SETPOINT_MAX_C,
    SETPOINT_MIN_C,
    SETPOINT_SCALE,
    d_to_modbus_coil_addr,
)
from runtime import (
    cache,
    cache_lock,
    clear_runtime_error,
    get_active_com_port,
    logger,
    modbus_connect_or_raise,
    modbus_lock,
    normalize_modbus_error,
    note_runtime_error,
    now_iso,
    read_coils,
    read_holding_registers,
    request_interactive_modbus_priority,
    reset_modbus_client,
    write_coil,
    write_register,
)


@dataclass(frozen=True)
class WritableField:
    request_key: str
    response_key: str
    address: int
    scale: float
    low_limit: float
    high_limit: float
    cache_raw_attr: str
    cache_scaled_attr: str
    log_label: str
    unit_label: str
    menu_path: str | None = None


MENU_SETPOINT_PATH = "2.1"
MENU_HUMIDIFIER_PATH = "2.2"
MENU_MAX_PRODUCTION_PATH = "2.3"
MENU_PROP_BAND_PATH = "2.4"
NUMERIC_RANGE_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:\.{2,3})\s*(-?\d+(?:\.\d+)?)")
FLOAT_LABEL_RE = re.compile(r"\b(offset|band|setpoint|hyster)\b", re.IGNORECASE)

SETPOINT_FIELD = WritableField(
    request_key="temp_c",
    response_key="temp_c",
    address=SETPOINT_ADDR,
    scale=SETPOINT_SCALE,
    low_limit=SETPOINT_MIN_C,
    high_limit=SETPOINT_MAX_C,
    cache_raw_attr="last_setpoint_raw",
    cache_scaled_attr="last_setpoint_c",
    log_label="Setpoint",
    unit_label="C",
    menu_path=MENU_SETPOINT_PATH,
)
MAX_PRODUCTION_FIELD = WritableField(
    request_key="value_pct",
    response_key="value_pct",
    address=MAX_PRODUCTION_ADDR,
    scale=MAX_PRODUCTION_SCALE,
    low_limit=MAX_PRODUCTION_MIN_PCT,
    high_limit=MAX_PRODUCTION_MAX_PCT,
    cache_raw_attr="max_production_raw",
    cache_scaled_attr="max_production_pct",
    log_label="Max production",
    unit_label="%",
    menu_path=MENU_MAX_PRODUCTION_PATH,
)
PROP_BAND_FIELD = WritableField(
    request_key="value_c",
    response_key="value_c",
    address=PROP_BAND_ADDR,
    scale=PROP_BAND_SCALE,
    low_limit=PROP_BAND_MIN_C,
    high_limit=PROP_BAND_MAX_C,
    cache_raw_attr="prop_band_raw",
    cache_scaled_attr="prop_band_c",
    log_label="Prop. band",
    unit_label="C",
    menu_path=MENU_PROP_BAND_PATH,
)


def format_limit(value: float) -> str:
    return f"{value:g}"


def walk_menu_nodes(node: dict[str, Any]):
    yield node
    for child in node.get("children", []):
        yield from walk_menu_nodes(child)


def find_menu_node(path: str) -> dict[str, Any] | None:
    payload = load_display_menu()
    if not payload.get("ok"):
        return None

    for node in walk_menu_nodes(payload["root"]):
        if node.get("path") == path:
            return node
    return None


def normalize_editor_options(node: dict[str, Any]) -> list[dict[str, Any]]:
    editor = node.get("editor")
    options = editor.get("options") if isinstance(editor, dict) else None

    normalized: list[dict[str, Any]] = []
    if isinstance(options, list):
        for index, option in enumerate(options):
            if isinstance(option, dict):
                normalized.append(
                    {
                        "value": option.get("value", index),
                        "label": option.get("label", str(option.get("value", index))),
                    }
                )
            else:
                normalized.append({"value": index, "label": str(option)})

    if normalized:
        return normalized

    for index, token in enumerate(parse_choice_tokens(str(node.get("range_or_options") or ""))):
        normalized.append({"value": index, "label": token})
    return normalized


def parse_choice_tokens(text: str | None) -> list[str]:
    if not text:
        return []

    separator = "," if "," in text else ("/" if "/" in text else None)
    if separator is None:
        return []

    return [token.strip().strip('"') for token in text.split(separator) if token.strip()]


def infer_menu_editor_type(node: dict[str, Any]) -> str | None:
    editor = node.get("editor")
    if isinstance(editor, dict) and isinstance(editor.get("type"), str):
        return str(editor["type"])

    register = node.get("register") or {}
    family = register.get("family")
    if family == "D":
        return "boolean"

    range_hint = str(node.get("range_or_options") or "")
    if len(parse_choice_tokens(range_hint)) >= 2 and NUMERIC_RANGE_RE.search(range_hint) is None:
        return "enum"

    if family in {"A", "I"}:
        label = str(node.get("display_label") or "")
        if re.search(r"\d+\.\d+", range_hint) or FLOAT_LABEL_RE.search(label):
            return "float"
        return "integer"

    return None


def infer_menu_numeric_scale(node: dict[str, Any], editor_type: str | None) -> float:
    editor = node.get("editor")
    if isinstance(editor, dict) and editor.get("scale") is not None:
        return float(editor["scale"])

    path = str(node.get("path") or "")
    if path == MENU_SETPOINT_PATH:
        return SETPOINT_SCALE
    if path == MENU_MAX_PRODUCTION_PATH:
        return MAX_PRODUCTION_SCALE
    if path == MENU_PROP_BAND_PATH:
        return PROP_BAND_SCALE

    range_hint = str(node.get("range_or_options") or "")
    label = str(node.get("display_label") or "")
    if editor_type == "float" or re.search(r"\d+\.\d+", range_hint) or FLOAT_LABEL_RE.search(label):
        return 10.0
    return 1.0


def infer_menu_numeric_limits(node: dict[str, Any]) -> tuple[float | None, float | None]:
    path = str(node.get("path") or "")
    if path == MENU_SETPOINT_PATH:
        return SETPOINT_MIN_C, SETPOINT_MAX_C
    if path == MENU_MAX_PRODUCTION_PATH:
        return MAX_PRODUCTION_MIN_PCT, MAX_PRODUCTION_MAX_PCT
    if path == MENU_PROP_BAND_PATH:
        return PROP_BAND_MIN_C, PROP_BAND_MAX_C

    hint = str(node.get("range_or_options") or "")
    match = NUMERIC_RANGE_RE.search(hint)
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


def is_menu_node_modbus_backed(node: dict[str, Any]) -> bool:
    register = node.get("register")
    return isinstance(register, dict) and register.get("family") in {"A", "I", "D"}


def is_menu_node_writable(node: dict[str, Any]) -> bool:
    register = node.get("register")
    return isinstance(register, dict) and register.get("access") == "R/W"


def resolve_node_editor(node: dict[str, Any]) -> dict[str, Any]:
    """Build the fully-resolved editor metadata for a menu node.

    Returns a dict with ``type``, ``options``, ``scale``, ``limits``,
    ``step`` plus capability flags ``editable``, ``modbus_backed``, and
    ``writable``.
    """
    editor_type = infer_menu_editor_type(node)
    modbus_backed = is_menu_node_modbus_backed(node)
    writable = is_menu_node_writable(node)
    kind = node.get("kind", "leaf")

    if editor_type is None:
        return {
            "type": None,
            "options": [],
            "scale": 1.0,
            "limits": None,
            "step": None,
            "editable": False,
            "modbus_backed": modbus_backed,
            "writable": writable,
        }

    explicit_editor = node.get("editor") if isinstance(node.get("editor"), dict) else None

    # --- options ---
    if explicit_editor and isinstance(explicit_editor.get("options"), list):
        options = normalize_editor_options(node)
    elif editor_type == "boolean":
        tokens = parse_choice_tokens(str(node.get("range_or_options") or ""))
        labels = tokens[:2] if len(tokens) >= 2 else ["yes", "no"]
        options = [
            {"value": True, "label": labels[0]},
            {"value": False, "label": labels[1]},
        ]
    elif editor_type == "enum":
        options = normalize_editor_options(node)
    else:
        options = []

    # --- scale & limits ---
    scale = infer_menu_numeric_scale(node, editor_type)
    low, high = infer_menu_numeric_limits(node)
    limits = {"low": low, "high": high} if low is not None and high is not None else None

    # --- step ---
    if explicit_editor and explicit_editor.get("step"):
        step = explicit_editor["step"]
    elif editor_type == "float":
        step = "any"
    elif editor_type == "integer":
        step = "1"
    else:
        step = None

    # --- editable ---
    if kind in ("menu", "caption", "page_link"):
        editable = False
    elif modbus_backed:
        editable = writable
    else:
        editable = True

    return {
        "type": editor_type,
        "options": options,
        "scale": scale,
        "limits": limits,
        "step": step,
        "editable": editable,
        "modbus_backed": modbus_backed,
        "writable": writable,
    }


def annotate_menu_tree(root: dict[str, Any]) -> None:
    """Walk the tree and attach ``resolved_editor`` to every node."""
    for node in walk_menu_nodes(root):
        node["resolved_editor"] = resolve_node_editor(node)


def collect_dashboard_sync_map(root: dict[str, Any]) -> dict[str, str]:
    """Build ``{menu_path: dashboard_payload_key}`` from ``dashboard_sync`` annotations."""
    sync_map: dict[str, str] = {}
    for node in walk_menu_nodes(root):
        key = node.get("dashboard_sync")
        if isinstance(key, str) and key:
            sync_map[str(node["path"])] = key
    return sync_map


def _cache_menu_value_locked(path: str, *, raw: Any, value: Any, source: str) -> None:
    cache.menu_values[path] = {
        "raw": raw,
        "value": value,
        "source": source,
        "updated_utc": now_iso(),
        "error": None,
    }


def _sync_dashboard_menu_cache_locked(path: str, *, raw: Any, value: Any) -> bool:
    if path == MENU_SETPOINT_PATH:
        cache.last_setpoint_raw = int(raw)
        cache.last_setpoint_c = float(value)
        return True
    if path == MENU_MAX_PRODUCTION_PATH:
        cache.max_production_raw = int(raw)
        cache.max_production_pct = float(value)
        return True
    if path == MENU_PROP_BAND_PATH:
        cache.prop_band_raw = int(raw)
        cache.prop_band_c = float(value)
        return True
    if path == MENU_HUMIDIFIER_PATH:
        cache.humidifier_network_enabled = bool(value)
        return True
    return False


def cache_menu_value(path: str, *, raw: Any, value: Any, source: str) -> None:
    with cache_lock:
        _cache_menu_value_locked(path, raw=raw, value=value, source=source)
        _sync_dashboard_menu_cache_locked(path, raw=raw, value=value)


def cache_menu_error(path: str, error_message: str) -> None:
    with cache_lock:
        previous = cache.menu_values.get(path, {})
        cache.menu_values[path] = {
            "raw": previous.get("raw"),
            "value": previous.get("value"),
            "source": previous.get("source"),
            "error": error_message,
            "updated_utc": now_iso(),
        }


def get_cached_menu_value(path: str) -> dict[str, Any] | None:
    with cache_lock:
        cached_value = cache.menu_values.get(path)
        if cached_value is None:
            return None
        return dict(cached_value)


def serialize_menu_value(
    node: dict[str, Any],
    cached_value: dict[str, Any],
    *,
    cached: bool,
) -> dict[str, Any]:
    return {
        "ok": cached_value.get("error") is None,
        "path": node.get("path"),
        "label": node.get("display_label") or node.get("title") or node.get("path"),
        "writable": is_menu_node_writable(node),
        "modbus_backed": is_menu_node_modbus_backed(node),
        "resolved_editor": node.get("resolved_editor") or resolve_node_editor(node),
        "value": cached_value.get("value"),
        "raw": cached_value.get("raw"),
        "source": cached_value.get("source"),
        "updated_utc": cached_value.get("updated_utc"),
        "cached": cached,
        "error": cached_value.get("error"),
    }


def sync_menu_write_success(path: str, *, raw: Any, value: Any) -> None:
    with cache_lock:
        _cache_menu_value_locked(path, raw=raw, value=value, source="write")
        _sync_dashboard_menu_cache_locked(path, raw=raw, value=value)
        cache.last_write_utc = now_iso()
        cache.last_error = None
        if path == MENU_HUMIDIFIER_PATH and not bool(value):
            cache.info_humidifier_status = 2


def sync_writable_field_success(field: WritableField, *, raw_value: int, scaled_value: float) -> None:
    with cache_lock:
        cache.last_write_utc = now_iso()
        cache.last_error = None
        if field.menu_path:
            _cache_menu_value_locked(field.menu_path, raw=raw_value, value=scaled_value, source="write")
            if not _sync_dashboard_menu_cache_locked(field.menu_path, raw=raw_value, value=scaled_value):
                setattr(cache, field.cache_raw_attr, raw_value)
                setattr(cache, field.cache_scaled_attr, scaled_value)
        else:
            setattr(cache, field.cache_raw_attr, raw_value)
            setattr(cache, field.cache_scaled_attr, scaled_value)


def read_menu_value_from_controller(node: dict[str, Any]) -> dict[str, Any]:
    if not is_menu_node_modbus_backed(node):
        raise ValueError("This menu leaf is not mapped to Modbus yet.")

    register = node["register"]
    family = str(register["family"])
    index = int(register["index"])
    editor_type = infer_menu_editor_type(node)
    path = str(node["path"])

    request_interactive_modbus_priority()
    with modbus_lock:
        modbus_connect_or_raise()
        if family == "D":
            rr = read_coils(address=d_to_modbus_coil_addr(index), count=1)
            if rr.isError():
                raise RuntimeError(f"Modbus read error (coil {index}): {rr}")
            raw_value = bool(rr.bits[0]) if rr.bits else False
            value = raw_value
        else:
            rr = read_holding_registers(address=index, count=1)
            if rr.isError():
                raise RuntimeError(f"Modbus read error (register {index}): {rr}")
            if not rr.registers:
                raise RuntimeError(f"Modbus read returned no value for register {index}")
            raw_value = int(rr.registers[0])
            scale = infer_menu_numeric_scale(node, editor_type)
            value = raw_value / scale if editor_type == "float" else raw_value

    cache_menu_value(path, raw=raw_value, value=value, source="modbus")
    return {"path": path, "raw": raw_value, "value": value}


def parse_menu_boolean_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on", "enabled", "auto"}:
            return True
        if lowered in {"false", "0", "no", "off", "disabled"}:
            return False
    raise ValueError("value must be a boolean")


def coerce_menu_write(node: dict[str, Any], incoming_value: Any) -> tuple[Any, int | bool]:
    editor_type = infer_menu_editor_type(node)
    if editor_type is None:
        raise ValueError("Unable to infer the value type for this menu leaf.")

    if editor_type == "boolean":
        bool_value = parse_menu_boolean_value(incoming_value)
        return bool_value, bool_value

    if editor_type == "enum":
        options = normalize_editor_options(node)
        if options:
            for option in options:
                if str(option["value"]) == str(incoming_value):
                    selected_value = option["value"]
                    break
            else:
                raise ValueError("value must match one of the supported enum options")
            if isinstance(selected_value, bool):
                return selected_value, int(selected_value)
            return int(selected_value), int(selected_value)

    try:
        numeric_value = float(incoming_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be numeric") from exc

    low_limit, high_limit = infer_menu_numeric_limits(node)
    if low_limit is not None and high_limit is not None and not (low_limit <= numeric_value <= high_limit):
        raise ValueError(
            f"value out of allowed range ({format_limit(low_limit)}..{format_limit(high_limit)})"
        )

    if editor_type == "integer":
        integer_value = int(round(numeric_value))
        return integer_value, integer_value

    scale = infer_menu_numeric_scale(node, editor_type)
    raw_value = int(round(numeric_value * scale))
    return raw_value / scale, raw_value


def write_menu_value_to_controller(node: dict[str, Any], incoming_value: Any) -> dict[str, Any]:
    if not is_menu_node_modbus_backed(node) or not is_menu_node_writable(node):
        raise ValueError("This menu leaf is not writable through Modbus yet.")

    register = node["register"]
    family = str(register["family"])
    index = int(register["index"])
    path = str(node["path"])
    value, raw_value = coerce_menu_write(node, incoming_value)

    request_interactive_modbus_priority()
    with modbus_lock:
        modbus_connect_or_raise()
        if family == "D":
            coil_address = d_to_modbus_coil_addr(index)
            if coil_address == HUMIDIFIER_REMOTE_ONOFF_COIL:
                enable_wr = write_coil(address=HUMIDIFIER_SUPERVISOR_ENABLE_COIL, value=True)
                if enable_wr.isError():
                    raise RuntimeError(
                        f"Modbus write error (humidifier supervisor enable coil): {enable_wr}"
                    )
            wr = write_coil(address=coil_address, value=bool(raw_value))
            if wr.isError():
                raise RuntimeError(f"Modbus write error (coil {index}): {wr}")
        else:
            wr = write_register(address=index, value=int(raw_value))
            if wr.isError():
                raise RuntimeError(f"Modbus write error (register {index}): {wr}")

    sync_menu_write_success(path, raw=raw_value, value=value)
    logger.info(
        "Menu value written successfully: %s=%s on %s",
        path,
        value,
        get_active_com_port(),
    )
    return {"path": path, "raw": raw_value, "value": value}


def ensure_dashboard_config_cache() -> None:
    with cache_lock:
        missing_paths = []
        if cache.last_setpoint_c is None:
            missing_paths.append(MENU_SETPOINT_PATH)
        if cache.max_production_pct is None:
            missing_paths.append(MENU_MAX_PRODUCTION_PATH)
        if cache.prop_band_c is None:
            missing_paths.append(MENU_PROP_BAND_PATH)

    for path in missing_paths:
        node = find_menu_node(path)
        if not node:
            continue
        try:
            read_menu_value_from_controller(node)
        except Exception as exc:
            cache_menu_error(path, normalize_modbus_error(exc))


def write_writable_field(field: WritableField):
    """Handle JSON parsing, range validation, Modbus write, and cache updates for a writable value."""
    try:
        body = request.get_json(force=True, silent=False)
        if not isinstance(body, dict) or field.request_key not in body:
            raise ValueError(f"JSON must include '{field.request_key}'")

        try:
            value = float(body[field.request_key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field.request_key} must be a number") from exc

        if not (field.low_limit <= value <= field.high_limit):
            raise ValueError(
                f"{field.request_key} out of allowed range "
                f"({format_limit(field.low_limit)}..{format_limit(field.high_limit)})"
            )

        raw_value = int(round(value * field.scale))
        if not (0 <= raw_value <= 65535):
            raise ValueError("scaled value out of 16-bit range")

        with modbus_lock:
            modbus_connect_or_raise()
            wr = write_register(address=field.address, value=raw_value)
        if wr.isError():
            raise RuntimeError(f"Modbus write error: {wr}")

        scaled_value = raw_value / field.scale
        sync_writable_field_success(field, raw_value=raw_value, scaled_value=scaled_value)
        clear_runtime_error()
        return jsonify({"ok": True, field.response_key: scaled_value, "raw": raw_value})
    except ValueError as exc:
        error_message = str(exc)
        with cache_lock:
            cache.last_error = error_message
        return jsonify({"ok": False, "error": error_message}), 400
    except Exception as exc:
        reset_modbus_client()
        error_message = normalize_modbus_error(exc)
        note_runtime_error(error_message)
        with cache_lock:
            cache.last_error = error_message
        return jsonify({"ok": False, "error": cache.last_error}), 400


def handle_menu_value_get():
    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "Query string must include 'path'."}), 400

    node = find_menu_node(path)
    if not node:
        return jsonify({"ok": False, "error": f"Menu path '{path}' was not found."}), 404
    if not is_menu_node_modbus_backed(node):
        return jsonify({"ok": False, "error": "This menu leaf is not mapped to Modbus yet."}), 400

    refresh_requested = str(request.args.get("refresh", "")).strip().lower() in {"1", "true", "yes"}
    if not refresh_requested:
        cached_value = get_cached_menu_value(path)
        if cached_value is not None and cached_value.get("value") is not None:
            return jsonify(serialize_menu_value(node, cached_value, cached=True))

    try:
        read_menu_value_from_controller(node)
        cached_value = get_cached_menu_value(path)
        if cached_value is None:
            raise RuntimeError("Menu value read succeeded, but no cached value was produced.")
        clear_runtime_error()
        return jsonify(serialize_menu_value(node, cached_value, cached=False))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        reset_modbus_client()
        error_message = normalize_modbus_error(exc)
        note_runtime_error(error_message)
        cache_menu_error(path, error_message)
        return jsonify({"ok": False, "error": error_message, "path": path}), 400


def handle_menu_value_post():
    try:
        body = request.get_json(force=True, silent=False)
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object.")

        path = str(body.get("path", "")).strip()
        if not path:
            raise ValueError("JSON must include 'path'.")
        if "value" not in body:
            raise ValueError("JSON must include 'value'.")

        node = find_menu_node(path)
        if not node:
            return jsonify({"ok": False, "error": f"Menu path '{path}' was not found."}), 404

        write_menu_value_to_controller(node, body["value"])
        cached_value = get_cached_menu_value(path)
        if cached_value is None:
            raise RuntimeError("Menu value write succeeded, but no cached value was produced.")
        clear_runtime_error()
        return jsonify(serialize_menu_value(node, cached_value, cached=False))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        reset_modbus_client()
        error_message = normalize_modbus_error(exc)
        path = str((request.get_json(silent=True) or {}).get("path") or "").strip()
        if path:
            cache_menu_error(path, error_message)
        note_runtime_error(error_message)
        return jsonify({"ok": False, "error": error_message}), 400


def handle_humidifier_toggle():
    """Toggle the humidifier network on/off coil after enabling supervisor control."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        if "on" in body:
            target_state = bool(body["on"])
        else:
            with cache_lock:
                current = cache.humidifier_network_enabled
            target_state = not current if current is not None else True

        with modbus_lock:
            modbus_connect_or_raise()
            enable_wr = write_coil(address=HUMIDIFIER_SUPERVISOR_ENABLE_COIL, value=True)
            if enable_wr.isError():
                raise RuntimeError(f"Modbus write error (humidifier supervisor enable coil): {enable_wr}")
            onoff_wr = write_coil(address=HUMIDIFIER_REMOTE_ONOFF_COIL, value=target_state)
        if onoff_wr.isError():
            raise RuntimeError(f"Modbus write error (humidifier remote on/off coil): {onoff_wr}")

        sync_menu_write_success(MENU_HUMIDIFIER_PATH, raw=target_state, value=target_state)
        logger.info(
            "Humidifier remote on/off set to %s on %s",
            "ON" if target_state else "OFF",
            get_active_com_port(),
        )
        return jsonify({"ok": True, "humidifier_network_enabled": target_state})
    except Exception as exc:
        reset_modbus_client()
        error_message = normalize_modbus_error(exc)
        return jsonify({"ok": False, "error": error_message}), 400
