# app.py
"""
Flask + Modbus RTU (pymodbus) PoC for CAREL controller
- Periodically reads main temperature from register TEMP_REG (QModMaster-style 1-based)
- Allows writing temperature setpoint to register SETPOINT_REG (QModMaster-style 1-based)
- Exposes only 2 web operations: read temp + write setpoint

Run:
  pip install flask pymodbus
  python app.py
Then open:
  http://127.0.0.1:8000
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict

from flask import Flask, Response, jsonify, render_template, request

from alarms import ALARM_CATALOG
from client_factory import is_simulator_mode
from display_menu import load_display_menu
from modbus_map import (
    ALARM_RESET_COIL,
    DRAIN_CYL1_COIL,
    HUMIDIFIER_REMOTE_ONOFF_COIL,
    HUMIDIFIER_SUPERVISOR_ENABLE_COIL,
    MAX_PRODUCTION_ADDR,
    MAX_PRODUCTION_MAX_PCT,
    MAX_PRODUCTION_MIN_PCT,
    MAX_PRODUCTION_REG,
    MAX_PRODUCTION_SCALE,
    POLL_INTERVAL_S,
    PROP_BAND_ADDR,
    PROP_BAND_MAX_C,
    PROP_BAND_MIN_C,
    PROP_BAND_REG,
    PROP_BAND_SCALE,
    RTC_READ_DAY_REG,
    RTC_READ_HOUR_REG,
    RTC_READ_MINUTE_REG,
    RTC_READ_MONTH_REG,
    RTC_READ_WEEKDAY_REG,
    RTC_READ_YEAR_REG,
    RTC_WRITE_DAY_REG,
    RTC_WRITE_HOUR_REG,
    RTC_WRITE_MINUTE_REG,
    RTC_WRITE_MONTH_REG,
    RTC_WRITE_WEEKDAY_REG,
    RTC_WRITE_YEAR_REG,
    SETPOINT_ADDR,
    SETPOINT_MAX_C,
    SETPOINT_MIN_C,
    SETPOINT_REG,
    SETPOINT_SCALE,
    TEMP_REG,
    d_to_modbus_coil_addr,
    qmm_to_modbus_addr,
)
from runtime import (
    BAUDRATE,
    SLAVE_ID,
    USB_MODEL_ID,
    USB_SERIAL_SHORT,
    USB_VENDOR_ID,
    adapter_identity_text,
    cache,
    cache_lock,
    clear_runtime_error,
    format_device_datetime_local,
    get_active_com_port,
    logger,
    modbus_connect_or_raise,
    modbus_lock,
    normalize_modbus_error,
    note_runtime_error,
    now_iso,
    poll_registers_once,
    read_coils,
    read_device_rtc_values,
    read_holding_registers,
    read_log_tail,
    request_interactive_modbus_priority,
    reset_modbus_client,
    start_background_poller,
    write_coil,
    write_device_rtc,
    write_register,
)


def read_runtime_commit_hash() -> str:
  """Return the deployment-injected commit hash for the running app."""
  return os.environ.get("APP_COMMIT_HASH", "").strip() or "unknown"


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


APP_COMMIT_HASH = read_runtime_commit_hash()
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
        normalized.append({
          "value": option.get("value", index),
          "label": option.get("label", str(option.get("value", index))),
        })
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


def cache_menu_value(path: str, *, raw: Any, value: Any, source: str) -> None:
  with cache_lock:
    cache.menu_values[path] = {
      "raw": raw,
      "value": value,
      "source": source,
      "updated_utc": now_iso(),
      "error": None,
    }

    if path == MENU_SETPOINT_PATH:
      cache.last_setpoint_raw = int(raw)
      cache.last_setpoint_c = float(value)
    elif path == MENU_MAX_PRODUCTION_PATH:
      cache.max_production_raw = int(raw)
      cache.max_production_pct = float(value)
    elif path == MENU_PROP_BAND_PATH:
      cache.prop_band_raw = int(raw)
      cache.prop_band_c = float(value)
    elif path == MENU_HUMIDIFIER_PATH:
      cache.humidifier_network_enabled = bool(value)


def cache_menu_error(path: str, error_message: str) -> None:
  with cache_lock:
    previous = cache.menu_values.get(path, {})
    cache.menu_values[path] = {
      **previous,
      "error": error_message,
      "updated_utc": now_iso(),
    }


def get_cached_menu_value(path: str) -> dict[str, Any] | None:
  with cache_lock:
    cached_value = cache.menu_values.get(path)
    if cached_value is None:
      return None
    return dict(cached_value)


def serialize_menu_value(node: dict[str, Any], cached_value: dict[str, Any], *, cached: bool) -> dict[str, Any]:
  return {
    "ok": cached_value.get("error") is None,
    "path": node.get("path"),
    "label": node.get("display_label") or node.get("title") or node.get("path"),
    "writable": is_menu_node_writable(node),
    "modbus_backed": is_menu_node_modbus_backed(node),
    "value": cached_value.get("value"),
    "raw": cached_value.get("raw"),
    "source": cached_value.get("source"),
    "updated_utc": cached_value.get("updated_utc"),
    "cached": cached,
    "error": cached_value.get("error"),
  }


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
      # address = qmm_to_modbus_addr(index)
      address = index
      rr = read_holding_registers(address=address, count=1)
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
          raise RuntimeError(f"Modbus write error (humidifier supervisor enable coil): {enable_wr}")
      wr = write_coil(address=coil_address, value=bool(raw_value))
      if wr.isError():
        raise RuntimeError(f"Modbus write error (coil {index}): {wr}")
    else:
      #wr = write_register(address=qmm_to_modbus_addr(index), value=int(raw_value))
      wr = write_register(address=index, value=int(raw_value))
      if wr.isError():
        raise RuntimeError(f"Modbus write error (register {index}): {wr}")

  cache_menu_value(path, raw=raw_value, value=value, source="write")
  with cache_lock:
    cache.last_write_utc = now_iso()
    cache.last_error = None
    if path == MENU_HUMIDIFIER_PATH and not bool(value):
      cache.info_humidifier_status = 2

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
    with cache_lock:
      cache.last_write_utc = now_iso()
      setattr(cache, field.cache_raw_attr, raw_value)
      setattr(cache, field.cache_scaled_attr, scaled_value)
      cache.last_error = None
    if field.menu_path:
      cache_menu_value(field.menu_path, raw=raw_value, value=scaled_value, source="write")

    logger.info(
      "%s written successfully: %.1f %s on %s",
      field.log_label,
      value,
      field.unit_label,
      get_active_com_port(),
    )
    clear_runtime_error()
    return jsonify({"ok": True, field.response_key: scaled_value, "raw": raw_value})
  except ValueError as e:
    error_message = str(e)
    with cache_lock:
      cache.last_error = error_message
    return jsonify({"ok": False, "error": error_message}), 400
  except Exception as e:
    reset_modbus_client()
    error_message = normalize_modbus_error(e)
    note_runtime_error(error_message)
    with cache_lock:
      cache.last_error = error_message
    return jsonify({"ok": False, "error": cache.last_error}), 400


# ---------------------------
# Flask app
# ---------------------------

app = Flask(__name__)


@app.route("/", methods=["GET"])
def index() -> str:
    ensure_dashboard_config_cache()
    app_title = (
        "CAREL\u2122 Supervisory System [Simulator]"
        if is_simulator_mode()
        else "CAREL\u2122 Supervisory System"
    )
    return render_template(
        "index.html",
        APP_TITLE=app_title,
        APP_COMMIT_HASH=APP_COMMIT_HASH,
        DISPLAY_MENU=load_display_menu(),
    )


@app.route("/logs", methods=["GET"])
def view_logs() -> Response:
  limit = request.args.get("tail", default=200, type=int) or 200
  return Response(read_log_tail(limit), mimetype="text/plain")

@app.route("/api/temp", methods=["GET"])
def api_temp():
    with cache_lock:
        data = {
            "temp_raw": cache.temp_raw,
            "temp_c": cache.temp_c,
            "last_update_utc": cache.last_update_utc,
            "last_error": cache.last_error,
            "last_write_utc": cache.last_write_utc,
            "last_setpoint_raw": cache.last_setpoint_raw,
            "last_setpoint_c": cache.last_setpoint_c,
            "max_production_raw": cache.max_production_raw,
            "max_production_pct": cache.max_production_pct,
            "prop_band_raw": cache.prop_band_raw,
            "prop_band_c": cache.prop_band_c,
            "device_time_iso_local": cache.device_time_iso_local,
            "device_time_display": cache.device_time_display,
            "device_time_weekday": cache.device_time_weekday,
            "device_time_raw_year": cache.device_time_raw_year,
            "last_rtc_update_utc": cache.last_rtc_update_utc,
            "last_rtc_write_utc": cache.last_rtc_write_utc,
            "rtc_error": cache.rtc_error,
            "alarms": {
                "has_active": cache.alarms_has_active,
                "active": cache.alarms_active,
                "skipped_active_count": cache.alarms_skipped_active_count,
                "last_scan_utc": cache.alarms_last_scan_utc,
                "error": cache.alarms_error,
            },
            "info": {
                "humidifier_status": cache.info_humidifier_status,
                "humidifier_network_enabled": cache.humidifier_network_enabled,
                "conductivity": cache.info_conductivity,
                "cyl1_phase": cache.info_cyl1_phase,
                "cyl1_status": cache.info_cyl1_status,
                "cyl2_phase": cache.info_cyl2_phase,
                "cyl2_status": cache.info_cyl2_status,
                "cyl1_hours": cache.info_cyl1_hours,
                "cyl2_hours": cache.info_cyl2_hours,
                "voltage_type": cache.info_voltage_type,
                "error": cache.info_error,
                "cyl1_drain_on": cache.cyl1_drain_on,
            },
        }

    ok = data["temp_c"] is not None and data["last_error"] is None
    resp: Dict[str, Any] = {
        "ok": bool(ok),
        "config": {
          "com_port": get_active_com_port(),
          "adapter_vendor_id": USB_VENDOR_ID,
          "adapter_model_id": USB_MODEL_ID,
          "adapter_serial_short": USB_SERIAL_SHORT,
            "baudrate": BAUDRATE,
            "slave_id": SLAVE_ID,
            "temp_reg_qmm": TEMP_REG,
            "setpoint_reg_qmm": SETPOINT_REG,
            "max_production_reg_qmm": MAX_PRODUCTION_REG,
            "prop_band_reg_qmm": PROP_BAND_REG,
            "rtc_read_regs_qmm": {
              "hour": RTC_READ_HOUR_REG,
              "minute": RTC_READ_MINUTE_REG,
              "day": RTC_READ_DAY_REG,
              "month": RTC_READ_MONTH_REG,
              "year": RTC_READ_YEAR_REG,
              "weekday": RTC_READ_WEEKDAY_REG,
            },
            "rtc_write_regs_qmm": {
              "weekday": RTC_WRITE_WEEKDAY_REG,
              "hour": RTC_WRITE_HOUR_REG,
              "minute": RTC_WRITE_MINUTE_REG,
              "day": RTC_WRITE_DAY_REG,
              "month": RTC_WRITE_MONTH_REG,
              "year": RTC_WRITE_YEAR_REG,
            },
            "alarm_summary_coil": ALARM_CATALOG.summary.address,
            "alarm_scan_start_coil": ALARM_CATALOG.range_start,
            "alarm_scan_count": ALARM_CATALOG.range_count,
            "poll_interval_s": POLL_INTERVAL_S,
        },
        **data,
    }
    if not ok:
        resp["error"] = data["last_error"] or "No data yet"
    return jsonify(resp)


@app.route("/api/menu-value", methods=["GET"])
def api_menu_value_get():
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


@app.route("/api/menu-value", methods=["POST"])
def api_menu_value_post():
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
        error_message = str(exc)
        return jsonify({"ok": False, "error": error_message}), 400
    except Exception as exc:
        reset_modbus_client()
        error_message = normalize_modbus_error(exc)
        path = str((request.get_json(silent=True) or {}).get("path") or "").strip()
        if path:
            cache_menu_error(path, error_message)
        note_runtime_error(error_message)
        return jsonify({"ok": False, "error": error_message}), 400


@app.route("/api/device-datetime", methods=["POST"])
def api_device_datetime():
    try:
        body = request.get_json(force=True, silent=False)
        if not isinstance(body, dict) or "datetime_local" not in body:
            raise ValueError("JSON must include 'datetime_local'")

        raw_value = str(body["datetime_local"]).strip()
        if not raw_value:
            raise ValueError("datetime_local cannot be empty")

        target_datetime = datetime.fromisoformat(raw_value)

        with cache_lock:
            current_raw_year = cache.device_time_raw_year

        with modbus_lock:
            modbus_connect_or_raise()
            write_device_rtc(target_datetime, current_raw_year)

            confirmed_datetime, confirmed_raw_year, confirmed_weekday = read_device_rtc_values()

        with cache_lock:
            cache.device_time_iso_local = format_device_datetime_local(confirmed_datetime)
            cache.device_time_display = confirmed_datetime.strftime("%Y-%m-%d %H:%M")
            cache.device_time_weekday = confirmed_weekday
            cache.device_time_raw_year = confirmed_raw_year
            cache.last_rtc_update_utc = now_iso()
            cache.last_rtc_write_utc = now_iso()
            cache.rtc_error = None

        logger.info(
            "Device RTC written successfully: %s on %s",
            format_device_datetime_local(target_datetime),
            get_active_com_port(),
        )
        return jsonify({
            "ok": True,
            "device_time_iso_local": cache.device_time_iso_local,
            "device_time_display": cache.device_time_display,
            "device_time_weekday": cache.device_time_weekday,
            "device_time_raw_year": cache.device_time_raw_year,
            "last_rtc_write_utc": cache.last_rtc_write_utc,
        })
    except Exception as e:
        reset_modbus_client()
        error_message = normalize_modbus_error(e)
        with cache_lock:
            cache.rtc_error = error_message
        return jsonify({"ok": False, "error": error_message}), 400

@app.route("/api/setpoint", methods=["POST"])
def api_setpoint():
    return write_writable_field(SETPOINT_FIELD)


@app.route("/api/max-production", methods=["POST"])
def api_max_production():
    return write_writable_field(MAX_PRODUCTION_FIELD)


@app.route("/api/prop-band", methods=["POST"])
def api_prop_band():
    return write_writable_field(PROP_BAND_FIELD)


@app.route("/api/humidifier-toggle", methods=["POST"])
def api_humidifier_toggle():
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

        with cache_lock:
            cache.humidifier_network_enabled = target_state
            cache.last_write_utc = now_iso()
            if not target_state:
                cache.info_humidifier_status = 2
            cache.last_error = None
        cache_menu_value(MENU_HUMIDIFIER_PATH, raw=target_state, value=target_state, source="write")

        logger.info(
            "Humidifier remote on/off set to %s on %s",
            "ON" if target_state else "OFF",
            get_active_com_port(),
        )
        return jsonify({"ok": True, "humidifier_network_enabled": target_state})
    except Exception as e:
        reset_modbus_client()
        error_message = normalize_modbus_error(e)
        return jsonify({"ok": False, "error": error_message}), 400


@app.route("/api/cyl1-drain", methods=["POST"])
def api_cyl1_drain():
    """Toggle or set cylinder 1 manual drain pump."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        # If 'on' is provided, use it; otherwise toggle current state
        if "on" in body:
            target_state = bool(body["on"])
        else:
            with cache_lock:
                current = cache.cyl1_drain_on
            target_state = not current if current is not None else True

        with modbus_lock:
            modbus_connect_or_raise()
            wr = write_coil(address=DRAIN_CYL1_COIL, value=target_state)
        if wr.isError():
            raise RuntimeError(f"Modbus write error (drain coil): {wr}")

        with cache_lock:
            cache.cyl1_drain_on = target_state

        logger.info(
            "Cylinder 1 drain %s on %s",
            "ON" if target_state else "OFF",
            get_active_com_port(),
        )
        return jsonify({"ok": True, "cyl1_drain_on": target_state})
    except Exception as e:
        reset_modbus_client()
        error_message = normalize_modbus_error(e)
        return jsonify({"ok": False, "error": error_message}), 400


@app.route("/api/alarms-reset", methods=["POST"])
def api_alarms_reset():
    """Pulse the controller alarm reset coil so it can clear active alarms."""
    try:
        with modbus_lock:
            modbus_connect_or_raise()
            wr = write_coil(address=ALARM_RESET_COIL, value=True)
        if wr.isError():
            raise RuntimeError(f"Modbus write error (alarm reset coil): {wr}")

        logger.info("Alarm reset requested on %s", get_active_com_port())
        return jsonify({"ok": True, "message": "Alarm reset command sent."})
    except Exception as e:
        reset_modbus_client()
        error_message = normalize_modbus_error(e)
        return jsonify({"ok": False, "error": error_message}), 400


@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    try:
        if is_simulator_mode():
            raise RuntimeError("Reboot is disabled while running in simulator mode.")
        if os.name != "posix":
            raise RuntimeError("Reboot is only supported on the Linux device.")

        subprocess.Popen(["sudo", "reboot"])
        logger.warning("System reboot requested from the web UI")
        return jsonify({
            "ok": True,
            "message": "Reboot command sent. The device should go offline shortly.",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


def start_runtime_if_needed() -> None:
    """Start background runtime work explicitly during real app startup."""
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or os.environ.get("FLASK_DEBUG") != "1":
        start_background_poller()


def main() -> None:
    logger.info("Carel Supervisor starting")
    logger.info("Expected adapter identity: %s", adapter_identity_text())

    # Preflight read (optional) so you see something quickly
    time.sleep(0.2)
    poll_registers_once()
    ensure_dashboard_config_cache()
    start_runtime_if_needed()

    # Run Flask dev server (fine for PoC)
    # If you want a steadier Windows server, use waitress:
    #   pip install waitress
    #   waitress-serve --listen=0.0.0.0:8000 app:app
    # app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
    # app.run(host="10.8.0.2", port=5000, debug=False, threaded=True)
    logger.info("Flask server starting on 0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
