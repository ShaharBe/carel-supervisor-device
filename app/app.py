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

import logging
import glob
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict, Any, cast

from flask import Flask, Response, jsonify, render_template, request

# Client factory - handles real HW vs simulator toggle
from alarms import ALARM_CATALOG
from client_factory import create_modbus_client, is_simulator_mode
from modbus_map import (
    ALARM_RESET_COIL,
    DRAIN_CYL1_COIL,
    HUMIDIFIER_REMOTE_ONOFF_COIL,
    HUMIDIFIER_STATUS_ADDR,
    HUMIDIFIER_SUPERVISOR_ENABLE_COIL,
    INFO_BLOCK1_COUNT,
    INFO_BLOCK1_START_ADDR,
    INFO_BLOCK2_COUNT,
    INFO_BLOCK2_START_ADDR,
    MAX_PRODUCTION_MAX_PCT,
    MAX_PRODUCTION_MIN_PCT,
    MAX_PRODUCTION_ADDR,
    MAX_PRODUCTION_REG,
    MAX_PRODUCTION_SCALE,
    POLL_INTERVAL_S,
    PROP_BAND_MAX_C,
    PROP_BAND_MIN_C,
    PROP_BAND_ADDR,
    PROP_BAND_REG,
    PROP_BAND_SCALE,
    RTC_LATCH_COILS,
    RTC_LATCH_PULSE_DELAY_S,
    RTC_READ_COUNT,
    RTC_READ_DAY_REG,
    RTC_READ_HOUR_REG,
    RTC_READ_MINUTE_REG,
    RTC_READ_MONTH_REG,
    RTC_READ_START_ADDR,
    RTC_READ_WEEKDAY_REG,
    RTC_READ_YEAR_REG,
    RTC_WRITE_DAY_REG,
    RTC_WRITE_HOUR_REG,
    RTC_WRITE_MINUTE_REG,
    RTC_WRITE_MONTH_REG,
    RTC_WRITE_SHADOW_ADDRS,
    RTC_WRITE_WEEKDAY_REG,
    RTC_WRITE_YEAR_REG,
    SETPOINT_ADDR,
    SETPOINT_MAX_C,
    SETPOINT_MIN_C,
    SETPOINT_REG,
    SETPOINT_SCALE,
    TEMP_ADDR,
    TEMP_REG,
    TEMP_SCALE,
)

# ---------------------------
# Configuration (easy to edit)
# ---------------------------

COM_PORT = "/dev/ttyACM0"  # Linux COM port, "/dev/ttyACM0"
BAUDRATE = 9600            # e.g. 9600
PARITY = "N"               # "N", "E", "O"
STOPBITS = 1               # 1 or 2
BYTESIZE = 8               # usually 8
SLAVE_ID = 1               # Modbus slave address
USB_VENDOR_ID = "1a86"     # QinHeng Electronics
USB_MODEL_ID = "55d3"      # USB Single Serial
USB_SERIAL_SHORT = "586D012821"
LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs"))
LOG_FILE = os.path.join(LOG_DIR, "app.log")
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUP_COUNT = 3


def setup_logging() -> logging.Logger:
  """Configure a small app logger for journald and a rotating file."""
  os.makedirs(LOG_DIR, exist_ok=True)

  logger = logging.getLogger("carel_supervisor")
  if logger.handlers:
    return logger

  logger.setLevel(logging.INFO)
  formatter = logging.Formatter(
    "%(asctime)s %(levelname)s %(message)s",
    "%Y-%m-%d %H:%M:%S",
  )

  stream_handler = logging.StreamHandler()
  stream_handler.setFormatter(formatter)

  file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=LOG_MAX_BYTES,
    backupCount=LOG_BACKUP_COUNT,
    encoding="utf-8",
  )
  file_handler.setFormatter(formatter)

  logger.addHandler(stream_handler)
  logger.addHandler(file_handler)
  logger.propagate = False

  logging.getLogger("werkzeug").setLevel(logging.WARNING)
  return logger


def read_runtime_commit_hash() -> str:
  """Return the deployment-injected commit hash for the running app."""
  return os.environ.get("APP_COMMIT_HASH", "").strip() or "unknown"


def build_modbus_client(port: str):
  """Create a Modbus client bound to the provided serial port."""
  return create_modbus_client(
    port=port,
    baudrate=BAUDRATE,
    parity=PARITY,
    stopbits=STOPBITS,
    bytesize=BYTESIZE,
    timeout=1.0,
    retries=1,
  )


# ---------------------------
# Helpers / state
# ---------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

@dataclass
class Cache:
    temp_raw: Optional[int] = None
    temp_c: Optional[float] = None
    last_update_utc: Optional[str] = None
    last_error: Optional[str] = None
    last_write_utc: Optional[str] = None
    last_setpoint_raw: Optional[int] = None
    last_setpoint_c: Optional[float] = None
    max_production_raw: Optional[int] = None
    max_production_pct: Optional[float] = None
    prop_band_raw: Optional[int] = None
    prop_band_c: Optional[float] = None
    device_time_iso_local: Optional[str] = None
    device_time_display: Optional[str] = None
    device_time_weekday: Optional[int] = None
    device_time_raw_year: Optional[int] = None
    last_rtc_update_utc: Optional[str] = None
    last_rtc_write_utc: Optional[str] = None
    rtc_error: Optional[str] = None
    # Info panel data
    info_humidifier_status: Optional[int] = None
    info_conductivity: Optional[int] = None
    info_cyl1_phase: Optional[int] = None
    info_cyl1_status: Optional[int] = None
    info_cyl2_phase: Optional[int] = None
    info_cyl2_status: Optional[int] = None
    info_cyl1_hours: Optional[int] = None
    info_cyl2_hours: Optional[int] = None
    info_voltage_type: Optional[int] = None
    info_error: Optional[str] = None
    humidifier_network_enabled: Optional[bool] = None
    cyl1_drain_on: Optional[bool] = None
    alarms_has_active: Optional[bool] = None
    alarms_active: list[dict[str, Any]] = field(default_factory=list)
    alarms_skipped_active_count: int = 0
    alarms_last_scan_utc: Optional[str] = None
    alarms_error: Optional[str] = None


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


cache = Cache()
cache_lock = threading.Lock()
modbus_lock = threading.Lock()
runtime_state_lock = threading.Lock()
logger = setup_logging()
APP_COMMIT_HASH = read_runtime_commit_hash()
last_detected_port: Optional[str] = None
last_adapter_missing = False
last_connected_port: Optional[str] = None
last_runtime_error: Optional[str] = None

# Create Modbus client (single owner)
# Toggle between real HW and simulator with USE_SIMULATOR=1 env var
active_com_port = COM_PORT
client = build_modbus_client(active_com_port)

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
)


def available_serial_ports() -> list[str]:
  """Return serial device candidates that commonly host USB RS485 adapters."""
  return sorted(set(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")))


def note_adapter_detected(port: str) -> None:
  global last_detected_port, last_adapter_missing
  with runtime_state_lock:
    if last_detected_port != port or last_adapter_missing:
      logger.info("RS485 adapter detected on %s (%s)", port, adapter_identity_text())
    last_detected_port = port
    last_adapter_missing = False


def note_adapter_missing() -> None:
  global last_detected_port, last_adapter_missing, last_connected_port
  with runtime_state_lock:
    if not last_adapter_missing:
      logger.warning("RS485 adapter unavailable (%s)", adapter_identity_text())
    last_detected_port = None
    last_connected_port = None
    last_adapter_missing = True


def note_client_connected(port: str) -> None:
  global last_connected_port
  with runtime_state_lock:
    if last_connected_port != port:
      logger.info("Modbus client connected on %s", port)
    last_connected_port = port


def note_runtime_error(message: str) -> None:
  global last_runtime_error
  with runtime_state_lock:
    if last_runtime_error != message:
      logger.warning("%s", message)
    last_runtime_error = message


def clear_runtime_error() -> None:
  global last_runtime_error
  with runtime_state_lock:
    if last_runtime_error is not None:
      logger.info("Modbus communication recovered on %s", active_com_port)
    last_runtime_error = None


def read_log_tail(limit: int = 200) -> str:
  """Return the last log lines for browser viewing."""
  if not os.path.exists(LOG_FILE):
    return "Log file not created yet."

  safe_limit = max(1, min(limit, 1000))
  with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as handle:
    return "".join(deque(handle, maxlen=safe_limit))


def read_udev_properties(port: str) -> Dict[str, str]:
  """Read udev properties for a tty device on Linux."""
  try:
    result = subprocess.run(
      ["udevadm", "info", "-q", "property", "-n", port],
      check=True,
      capture_output=True,
      text=True,
    )
  except (FileNotFoundError, subprocess.CalledProcessError):
    return {}

  props: Dict[str, str] = {}
  for line in result.stdout.splitlines():
    if "=" not in line:
      continue
    key, value = line.split("=", 1)
    props[key] = value
  return props


def is_target_adapter(port: str) -> bool:
  """Return True when the tty belongs to the expected RS485 USB adapter."""
  props = read_udev_properties(port)
  if not props:
    return False

  vendor_ok = props.get("ID_VENDOR_ID") == USB_VENDOR_ID
  model_ok = props.get("ID_MODEL_ID") == USB_MODEL_ID
  serial_ok = props.get("ID_SERIAL_SHORT") == USB_SERIAL_SHORT
  return vendor_ok and model_ok and serial_ok


def detect_modbus_port() -> Optional[str]:
  """Find the tty device that belongs to the expected USB RS485 adapter."""
  if is_simulator_mode():
    return COM_PORT

  ports = available_serial_ports()
  if os.path.exists(COM_PORT) and is_target_adapter(COM_PORT):
    note_adapter_detected(COM_PORT)
    return COM_PORT

  for port in ports:
    if is_target_adapter(port):
      note_adapter_detected(port)
      return port

  if not ports:
    note_adapter_missing()
    return None

  # Fallback keeps the service usable if udevadm is unavailable unexpectedly.
  note_adapter_detected(ports[0])
  return ports[0]


def adapter_identity_text() -> str:
  return (
    f"vendor={USB_VENDOR_ID}, product={USB_MODEL_ID}, serial={USB_SERIAL_SHORT}"
  )


def available_matching_ports() -> list[str]:
  return [port for port in available_serial_ports() if is_target_adapter(port)]


def serial_port_hint() -> str:
  """Return a user-facing message for a missing or detached RS485 adapter."""
  available_ports = available_matching_ports()
  message = (
    "The expected RS485 USB adapter is currently unavailable. "
    f"Expected adapter identity: {adapter_identity_text()}. "
    "Check that the USB-to-RS485 cable is connected securely and recognized by the OS."
  )
  if available_ports:
    message += f" Detected serial ports: {', '.join(available_ports)}."
  return message


def reset_modbus_client() -> None:
  """Close the current client so the next poll can establish a fresh connection."""
  try:
    client.close()
  except Exception:
    pass


def ensure_modbus_client_port(port: str) -> None:
  """Recreate the client when the adapter reappears on a different device path."""
  global client, active_com_port
  if port == active_com_port:
    return
  reset_modbus_client()
  logger.info("Switching serial port from %s to %s", active_com_port, port)
  client = build_modbus_client(port)
  active_com_port = port


def normalize_modbus_error(exc: Exception) -> str:
  """Translate low-level serial failures into a stable user-facing message."""
  if is_simulator_mode():
    return str(exc)

  error_text = str(exc).lower()
  serial_error_signals = (
    "no such file",
    "could not open port",
    "input/output error",
    "returned no data",
    "device disconnected",
    "device reports readiness",
  )

  if active_com_port not in available_serial_ports() or any(signal in error_text for signal in serial_error_signals):
    return serial_port_hint()
  return str(exc)


def modbus_connect_or_raise() -> None:
  """Ensure client is connected, raise RuntimeError if not."""
  resolved_port = detect_modbus_port()
  if resolved_port is None:
    reset_modbus_client()
    raise RuntimeError(serial_port_hint())

  ensure_modbus_client_port(resolved_port)
  if client.connected:
    return

  if not client.connect():
    reset_modbus_client()
    raise RuntimeError(f"Failed to connect Modbus serial on {active_com_port} @ {BAUDRATE}")
  note_client_connected(active_com_port)


def normalize_device_year(raw_year: int) -> int:
  """Convert device year register into a 4-digit year when needed."""
  if 0 <= raw_year <= 99:
    return 2000 + raw_year
  return raw_year


def encode_device_year(target_year: int, current_raw_year: Optional[int]) -> int:
  """Write the year back using the same 2-digit/4-digit shape already reported by the device."""
  if current_raw_year is not None and 0 <= current_raw_year <= 99:
    return target_year % 100
  return target_year


def build_device_datetime(hour: int, minute: int, day: int, month: int, raw_year: int) -> datetime:
  """Build a Python datetime from device RTC register values."""
  return datetime(normalize_device_year(raw_year), month, day, hour, minute)


def format_device_datetime_local(value: datetime) -> str:
  return value.strftime("%Y-%m-%dT%H:%M")


def write_registers(address: int, values: list[int]):
  """Support both current pymodbus (`device_id`) and older/simulated (`slave`) clients."""
  writer = cast(Any, client.write_registers)
  try:
    return writer(address=address, values=values, device_id=SLAVE_ID)
  except TypeError:
    return writer(address=address, values=values, slave=SLAVE_ID)


def read_coils(address: int, count: int):
  """Support both current pymodbus (`device_id`) and older/simulated (`slave`) clients."""
  reader = cast(Any, client).read_coils
  try:
    return reader(address=address, count=count, device_id=SLAVE_ID)
  except TypeError:
    return reader(address=address, count=count, slave=SLAVE_ID)


def write_coil(address: int, value: bool):
  """Support both current pymodbus (`device_id`) and older/simulated (`slave`) clients."""
  writer = cast(Any, client).write_coil
  try:
    return writer(address=address, value=value, device_id=SLAVE_ID)
  except TypeError:
    return writer(address=address, value=value, slave=SLAVE_ID)

def read_holding_registers(address: int, count: int):
  """Support both current pymodbus (`device_id`) and older/simulated (`slave`) clients."""
  reader = cast(Any, client.read_holding_registers)
  try:
    return reader(address=address, count=count, device_id=SLAVE_ID)
  except TypeError:
    return reader(address=address, count=count, slave=SLAVE_ID)

def read_input_registers(address: int, count: int):
  """Support both current pymodbus (`device_id`) and older/simulated (`slave`) clients."""
  reader = cast(Any, client.read_input_registers)
  try:
    return reader(address=address, count=count, device_id=SLAVE_ID)
  except TypeError:
    return reader(address=address, count=count, slave=SLAVE_ID)

def write_register(address: int, value: int):
  """Support both current pymodbus (`device_id`) and older/simulated (`slave`) clients."""
  writer = cast(Any, client.write_register)
  try:
    return writer(address=address, value=value, device_id=SLAVE_ID)
  except TypeError:
    return writer(address=address, value=value, slave=SLAVE_ID)


def format_limit(value: float) -> str:
  return f"{value:g}"


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

    logger.info(
      "%s written successfully: %.1f %s on %s",
      field.log_label,
      value,
      field.unit_label,
      active_com_port,
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


def read_device_rtc_values() -> tuple[datetime, int, int]:
  """Read the device RTC block and return the parsed datetime plus raw year/weekday."""
  rtc_rr = read_holding_registers(address=RTC_READ_START_ADDR, count=RTC_READ_COUNT)
  if rtc_rr.isError():
    raise RuntimeError(f"Modbus read error (device time): {rtc_rr}")
  if not rtc_rr.registers or len(rtc_rr.registers) < RTC_READ_COUNT:
    raise RuntimeError("Modbus read returned incomplete device time registers")

  hour = int(rtc_rr.registers[0])
  minute = int(rtc_rr.registers[1])
  day = int(rtc_rr.registers[2])
  month = int(rtc_rr.registers[3])
  raw_year = int(rtc_rr.registers[4])
  weekday = int(rtc_rr.registers[5])
  return build_device_datetime(hour, minute, day, month, raw_year), raw_year, weekday


def write_device_rtc_values(value: datetime, current_raw_year: Optional[int]):
  """Write the editable CAREL RTC shadow registers for weekday,hour,minute,day,month,year."""
  # CAREL weekday: 1=Monday..7=Sunday; Python: 0=Monday..6=Sunday
  carel_weekday = value.weekday() + 1
  write_values = [
    carel_weekday,
    value.hour,
    value.minute,
    value.day,
    value.month,
    encode_device_year(value.year, current_raw_year),
  ]
  # Write each shadow register individually
  for i, val in enumerate(write_values):
    addr = RTC_WRITE_SHADOW_ADDRS[i]
    wr = write_register(address=addr, value=val)
    if wr.isError():
      raise RuntimeError(f"Modbus write error (shadow addr {addr}={val}): {wr}")
  return None  # Success


def set_rtc_edit_latches(value: bool) -> None:
  """Drive all RTC edit latch bits (D1..D6) to a known state."""
  if is_simulator_mode():
    return

  for coil_addr in RTC_LATCH_COILS:
    wr = write_coil(address=coil_addr, value=value)
    if wr.isError():
      state = "set" if value else "clear"
      raise RuntimeError(f"Modbus write error ({state} coil {coil_addr}): {wr}")

def pulse_rtc_edit_latches() -> None:
  """Commit the prepared RTC shadow registers by pulsing D1..D6 one at a time."""
  if is_simulator_mode():
    return

  # Pulse in order: hour(1), minute(2), day(3), month(4), year(5), weekday(6)
  for coil_addr in RTC_LATCH_COILS:
    wr = write_coil(address=coil_addr, value=True)
    if wr.isError():
      raise RuntimeError(f"Modbus write error (set coil {coil_addr}): {wr}")
    time.sleep(RTC_LATCH_PULSE_DELAY_S)
    wr = write_coil(address=coil_addr, value=False)
    if wr.isError():
      raise RuntimeError(f"Modbus write error (clear coil {coil_addr}): {wr}")

def write_device_rtc(value: datetime, current_raw_year: Optional[int]) -> None:
  """Write RTC shadow registers, then pulse the D latches to commit them."""
  # Clear all latches first
  set_rtc_edit_latches(False)
  # Write all shadow registers
  write_device_rtc_values(value, current_raw_year)
  # Pulse each latch to commit
  pulse_rtc_edit_latches()
  # Clear latches again
  set_rtc_edit_latches(False)

  logger.info(
    "Wrote RTC shadow block and pulsed D1..D6 latches for %s",
    format_device_datetime_local(value),
  )

def poll_registers_once() -> None:
  """Read temperature, writable analog values, and device RTC registers, update cache."""
  try:
    with modbus_lock:
      modbus_connect_or_raise()
      temp_rr = read_holding_registers(address=TEMP_ADDR, count=1)
      max_production_rr = read_holding_registers(address=MAX_PRODUCTION_ADDR, count=1)
      sp_rr = read_holding_registers(address=SETPOINT_ADDR, count=1)
      prop_band_rr = read_holding_registers(address=PROP_BAND_ADDR, count=1)

    if temp_rr.isError():
      raise RuntimeError(f"Modbus read error (temp): {temp_rr}")
    if max_production_rr.isError():
      raise RuntimeError(f"Modbus read error (max production): {max_production_rr}")
    if sp_rr.isError():
      raise RuntimeError(f"Modbus read error (setpoint): {sp_rr}")
    if prop_band_rr.isError():
      raise RuntimeError(f"Modbus read error (prop. band): {prop_band_rr}")
    if not temp_rr.registers:
      raise RuntimeError("Modbus read returned no temperature registers")
    if not max_production_rr.registers:
      raise RuntimeError("Modbus read returned no max production registers")
    if not sp_rr.registers:
      raise RuntimeError("Modbus read returned no setpoint registers")
    if not prop_band_rr.registers:
      raise RuntimeError("Modbus read returned no prop. band registers")

    temp_raw = int(temp_rr.registers[0])
    temp_c = temp_raw / TEMP_SCALE
    max_production_raw = int(max_production_rr.registers[0])
    max_production_pct = max_production_raw / MAX_PRODUCTION_SCALE
    sp_raw = int(sp_rr.registers[0])
    sp_c = sp_raw / SETPOINT_SCALE
    prop_band_raw = int(prop_band_rr.registers[0])
    prop_band_c = prop_band_raw / PROP_BAND_SCALE

    with cache_lock:
      cache.temp_raw = temp_raw
      cache.temp_c = temp_c
      cache.max_production_raw = max_production_raw
      cache.max_production_pct = max_production_pct
      cache.last_setpoint_raw = sp_raw
      cache.last_setpoint_c = sp_c
      cache.prop_band_raw = prop_band_raw
      cache.prop_band_c = prop_band_c
      cache.last_update_utc = now_iso()
      cache.last_error = None
    clear_runtime_error()
  except Exception as e:
    reset_modbus_client()
    error_message = normalize_modbus_error(e)
    note_runtime_error(error_message)
    with cache_lock:
      cache.last_error = error_message

  try:
    with modbus_lock:
      modbus_connect_or_raise()
      device_time, raw_year, weekday = read_device_rtc_values()

    with cache_lock:
      cache.device_time_iso_local = format_device_datetime_local(device_time)
      cache.device_time_display = device_time.strftime("%Y-%m-%d %H:%M")
      cache.device_time_weekday = weekday
      cache.device_time_raw_year = raw_year
      cache.last_rtc_update_utc = now_iso()
      cache.rtc_error = None
  except Exception as e:
    reset_modbus_client()
    error_message = normalize_modbus_error(e)
    with cache_lock:
      cache.rtc_error = error_message

  # Poll info registers
  try:
    with modbus_lock:
      modbus_connect_or_raise()
      info1_rr = read_holding_registers(
        address=INFO_BLOCK1_START_ADDR,
        count=INFO_BLOCK1_COUNT
      )
      info2_rr = read_holding_registers(
        address=INFO_BLOCK2_START_ADDR,
        count=INFO_BLOCK2_COUNT
      )

    if info1_rr.isError():
      raise RuntimeError(f"Modbus read error (info block 1): {info1_rr}")
    if info2_rr.isError():
      raise RuntimeError(f"Modbus read error (info block 2): {info2_rr}")

    with cache_lock:
      # Block 1: I,136..I,142 -> (I,136 sampled separately from the input register), conductivity, (138 skip), cyl1_phase, cyl1_status, cyl2_phase, cyl2_status
      cache.info_conductivity = int(info1_rr.registers[1])       # I,137
      # info1_rr.registers[2] is I,138 (manual conductivity) - skip
      cache.info_cyl1_phase = int(info1_rr.registers[3])         # I,139
      cache.info_cyl1_status = int(info1_rr.registers[4])        # I,140
      cache.info_cyl2_phase = int(info1_rr.registers[5])         # I,141
      cache.info_cyl2_status = int(info1_rr.registers[6])        # I,142
      # Block 2: I,165..I,167 -> cyl1_hours, cyl2_hours, voltage_type
      cache.info_cyl1_hours = int(info2_rr.registers[0])         # I,165
      cache.info_cyl2_hours = int(info2_rr.registers[1])         # I,166
      cache.info_voltage_type = int(info2_rr.registers[2])       # I,167
      cache.info_error = None
  except Exception as e:
    reset_modbus_client()
    error_message = normalize_modbus_error(e)
    with cache_lock:
      cache.info_error = error_message

  # Poll humidifier status separately from the live input register because it can change outside the UI.
  try:
    with modbus_lock:
      modbus_connect_or_raise()
      humidifier_status_rr = read_input_registers(address=HUMIDIFIER_STATUS_ADDR, count=1)

    if humidifier_status_rr.isError():
      raise RuntimeError(f"Modbus read error (humidifier status): {humidifier_status_rr}")
    if not humidifier_status_rr.registers:
      raise RuntimeError("Modbus read returned no humidifier status registers")

    with cache_lock:
      cache.info_humidifier_status = int(humidifier_status_rr.registers[0])
  except Exception:
    reset_modbus_client()
    with cache_lock:
      cache.info_humidifier_status = None

  # Poll alarm summary coil first, then scan the full alarm bank only when active.
  try:
    active_alarms: list[dict[str, Any]] = []
    skipped_alarm_count = 0

    with modbus_lock:
      modbus_connect_or_raise()
      alarm_summary_rr = read_coils(address=ALARM_CATALOG.summary.address, count=1)
      if alarm_summary_rr.isError():
        raise RuntimeError(f"Modbus read error (alarm summary coil): {alarm_summary_rr}")

      alarms_have_active = bool(alarm_summary_rr.bits[0]) if alarm_summary_rr.bits else False
      if alarms_have_active:
        alarm_bank_rr = read_coils(
          address=ALARM_CATALOG.range_start,
          count=ALARM_CATALOG.range_count,
        )
        if alarm_bank_rr.isError():
          raise RuntimeError(f"Modbus read error (alarm bank): {alarm_bank_rr}")

        alarm_bits = alarm_bank_rr.bits or []
        active_alarms = [
          {"address": definition.address, "description": definition.description}
          for definition in ALARM_CATALOG.active_monitored(alarm_bits)
        ]
        skipped_alarm_count = len(ALARM_CATALOG.active_skipped(alarm_bits))

    with cache_lock:
      cache.alarms_has_active = alarms_have_active
      cache.alarms_active = active_alarms
      cache.alarms_skipped_active_count = skipped_alarm_count
      cache.alarms_last_scan_utc = now_iso()
      cache.alarms_error = None
  except Exception as e:
    reset_modbus_client()
    error_message = normalize_modbus_error(e)
    with cache_lock:
      cache.alarms_has_active = None
      cache.alarms_active = []
      cache.alarms_skipped_active_count = 0
      cache.alarms_last_scan_utc = now_iso()
      cache.alarms_error = error_message

  # Poll humidifier remote on/off and drain coil states.
  try:
    with modbus_lock:
      modbus_connect_or_raise()
      humidifier_rr = read_coils(address=HUMIDIFIER_REMOTE_ONOFF_COIL, count=1)
      drain_rr = read_coils(address=DRAIN_CYL1_COIL, count=1)

    if humidifier_rr.isError():
      raise RuntimeError(f"Modbus read error (humidifier remote on/off coil): {humidifier_rr}")
    if drain_rr.isError():
      raise RuntimeError(f"Modbus read error (drain coil): {drain_rr}")

    with cache_lock:
      cache.humidifier_network_enabled = bool(humidifier_rr.bits[0]) if humidifier_rr.bits else None
      cache.cyl1_drain_on = bool(drain_rr.bits[0]) if drain_rr.bits else None
  except Exception:
    reset_modbus_client()
    with cache_lock:
      cache.humidifier_network_enabled = None
      cache.cyl1_drain_on = None

def poller_loop(stop_evt: threading.Event) -> None:
    while not stop_evt.is_set():
        poll_registers_once()
        stop_evt.wait(POLL_INTERVAL_S)

stop_event = threading.Event()


# ---------------------------
# Flask app
# ---------------------------

app = Flask(__name__)


@app.route("/", methods=["GET"])
def index() -> str:
    app_title = (
        "CAREL\u2122 Supervisory System [Simulator]"
        if is_simulator_mode()
        else "CAREL\u2122 Supervisory System"
    )
    return render_template(
        "index.html",
        APP_TITLE=app_title,
        APP_COMMIT_HASH=APP_COMMIT_HASH,
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
          "com_port": active_com_port,
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

        logger.info("Device RTC written successfully: %s on %s", format_device_datetime_local(target_datetime), active_com_port)
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
            if not target_state:
                cache.info_humidifier_status = 2

        logger.info("Humidifier remote on/off set to %s on %s", "ON" if target_state else "OFF", active_com_port)
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

        logger.info("Cylinder 1 drain %s on %s", "ON" if target_state else "OFF", active_com_port)
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

        logger.info("Alarm reset requested on %s", active_com_port)
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


def start_background_poller() -> None:
    t = threading.Thread(target=poller_loop, args=(stop_event,), daemon=True)
    t.start()


# Start poller when module loads (works with both `python app.py` and `flask run`)
# Guard against Flask's reloader which spawns two processes - only run in the worker
if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or os.environ.get('FLASK_DEBUG') != '1':
    start_background_poller()


def main() -> None:
    logger.info("Carel Supervisor starting")
    logger.info("Expected adapter identity: %s", adapter_identity_text())

    # Preflight read (optional) so you see something quickly
    time.sleep(0.2)
    poll_registers_once()

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
