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

from flask import Flask, jsonify, request, Response

# Client factory - handles real HW vs simulator toggle
from alarms import ALARM_CATALOG
from client_factory import create_modbus_client, is_simulator_mode

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

# QModMaster-style 1-based register numbers (as you see in your tool)
TEMP_REG = 2               # You said: "main temp (addr 1)" earlier, but later confirmed reg 2=249 -> 24.9C
                           # Put the exact register number you read temp from in QModMaster here.
SETPOINT_REG = 20          # Setpoint register (QModMaster-style)
RTC_READ_HOUR_REG = 154
RTC_READ_MINUTE_REG = 155
RTC_READ_DAY_REG = 156
RTC_READ_MONTH_REG = 157
RTC_READ_YEAR_REG = 158
RTC_READ_WEEKDAY_REG = 159
# D-bit coil addresses (direct Modbus 0-based addresses, NOT qmm style)
# Verified experimentally: D1=coil addr 1, D2=coil addr 2, etc.
RTC_COIL_HOUR = 1       # D1 -> latches hour
RTC_COIL_MINUTE = 2     # D2 -> latches minute
RTC_COIL_DAY = 3        # D3 -> latches day
RTC_COIL_MONTH = 4      # D4 -> latches month
RTC_COIL_YEAR = 5       # D5 -> latches year
RTC_COIL_WEEKDAY = 6    # D6 -> latches weekday
# Shadow register QMM numbers (1-based) - verified experimentally:
# addr 159=weekday, 160=hour, 161=minute, 162=day, 163=month, 164=year
RTC_WRITE_WEEKDAY_REG = 160   # qmm 160 -> addr 159
RTC_WRITE_HOUR_REG = 161      # qmm 161 -> addr 160
RTC_WRITE_MINUTE_REG = 162    # qmm 162 -> addr 161
RTC_WRITE_DAY_REG = 163       # qmm 163 -> addr 162
RTC_WRITE_MONTH_REG = 164     # qmm 164 -> addr 163
RTC_WRITE_YEAR_REG = 165      # qmm 165 -> addr 164

# Info registers (read-only I-registers for status monitoring)
# Block 1: I,136..I,142 (humidifier status, conductivity, cylinder phases/status)
INFO_BLOCK1_START_REG = 136   # qmm 136 -> addr 135
INFO_BLOCK1_COUNT = 7         # I,136..I,142
# Block 2: I,165..I,167 (hour counters, voltage type)
INFO_BLOCK2_START_REG = 165   # qmm 165 -> addr 164
INFO_BLOCK2_COUNT = 3         # I,165..I,167

# Coil addresses for controls (D addresses are Modbus-aligned per user docs)
DRAIN_CYL1_COIL = 52          # D,52 -> cylinder 1 manual drain
ALARM_RESET_COIL = 51         # D,51 -> alarm reset pulse
# Alarm coils are loaded from app/modbus_alarms.csv and use direct Modbus 0-based coil addresses.

POLL_INTERVAL_S = 1.0      # temperature polling period

# Scaling
TEMP_SCALE = 10.0          # 249 -> 24.9
SETPOINT_SCALE = 10.0      # assume same scaling for setpoint (common on Carel)
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

def qmm_to_modbus_addr(qmm_reg_1_based: int) -> int:
    """Convert QModMaster 1-based register number to Modbus 0-based address."""
    if qmm_reg_1_based < 1:
        raise ValueError("Register must be >= 1 (QModMaster style).")
    return qmm_reg_1_based - 1

@dataclass
class Cache:
    temp_raw: Optional[int] = None
    temp_c: Optional[float] = None
    last_update_utc: Optional[str] = None
    last_error: Optional[str] = None
    last_write_utc: Optional[str] = None
    last_setpoint_raw: Optional[int] = None
    last_setpoint_c: Optional[float] = None
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
    cyl1_drain_on: Optional[bool] = None
    alarms_has_active: Optional[bool] = None
    alarms_active: list[dict[str, Any]] = field(default_factory=list)
    alarms_skipped_active_count: int = 0
    alarms_last_scan_utc: Optional[str] = None
    alarms_error: Optional[str] = None

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

TEMP_ADDR = qmm_to_modbus_addr(TEMP_REG)
SETPOINT_ADDR = qmm_to_modbus_addr(SETPOINT_REG)
RTC_READ_START_ADDR = qmm_to_modbus_addr(RTC_READ_HOUR_REG)
RTC_READ_COUNT = 6
# Coil addresses are direct (not qmm converted) - already 0-indexed Modbus addresses
RTC_COIL_START = RTC_COIL_HOUR  # = 1
RTC_COIL_COUNT = 6
RTC_LATCH_PULSE_DELAY_S = 0.15
# Shadow write block: [weekday, hour, minute, day, month, year]
RTC_WRITE_SHADOW_ADDRS = [
    qmm_to_modbus_addr(RTC_WRITE_WEEKDAY_REG),  # addr 159
    qmm_to_modbus_addr(RTC_WRITE_HOUR_REG),     # addr 160
    qmm_to_modbus_addr(RTC_WRITE_MINUTE_REG),   # addr 161
    qmm_to_modbus_addr(RTC_WRITE_DAY_REG),      # addr 162
    qmm_to_modbus_addr(RTC_WRITE_MONTH_REG),    # addr 163
    qmm_to_modbus_addr(RTC_WRITE_YEAR_REG),     # addr 164
]
# Mapping of field index to coil address for pulsing
# Order: [weekday, hour, minute, day, month, year] -> coils [6, 1, 2, 3, 4, 5]
RTC_FIELD_COILS = [
    RTC_COIL_WEEKDAY,  # 6: for weekday
    RTC_COIL_HOUR,     # 1: for hour
    RTC_COIL_MINUTE,   # 2: for minute
    RTC_COIL_DAY,      # 3: for day
    RTC_COIL_MONTH,    # 4: for month
    RTC_COIL_YEAR,     # 5: for year
]


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

def write_register(address: int, value: int):
  """Support both current pymodbus (`device_id`) and older/simulated (`slave`) clients."""
  writer = cast(Any, client.write_register)
  try:
    return writer(address=address, value=value, device_id=SLAVE_ID)
  except TypeError:
    return writer(address=address, value=value, slave=SLAVE_ID)


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

  for coil_addr in [RTC_COIL_HOUR, RTC_COIL_MINUTE, RTC_COIL_DAY, RTC_COIL_MONTH, RTC_COIL_YEAR, RTC_COIL_WEEKDAY]:
    wr = write_coil(address=coil_addr, value=value)
    if wr.isError():
      state = "set" if value else "clear"
      raise RuntimeError(f"Modbus write error ({state} coil {coil_addr}): {wr}")

def pulse_rtc_edit_latches() -> None:
  """Commit the prepared RTC shadow registers by pulsing D1..D6 one at a time."""
  if is_simulator_mode():
    return

  # Pulse in order: hour(1), minute(2), day(3), month(4), year(5), weekday(6)
  for coil_addr in [RTC_COIL_HOUR, RTC_COIL_MINUTE, RTC_COIL_DAY, RTC_COIL_MONTH, RTC_COIL_YEAR, RTC_COIL_WEEKDAY]:
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
  """Read temperature, setpoint, and device RTC registers, update cache."""
  try:
    with modbus_lock:
      modbus_connect_or_raise()
      temp_rr = read_holding_registers(address=TEMP_ADDR, count=1)
      sp_rr = read_holding_registers(address=SETPOINT_ADDR, count=1)

    if temp_rr.isError():
      raise RuntimeError(f"Modbus read error (temp): {temp_rr}")
    if sp_rr.isError():
      raise RuntimeError(f"Modbus read error (setpoint): {sp_rr}")
    if not temp_rr.registers:
      raise RuntimeError("Modbus read returned no temperature registers")
    if not sp_rr.registers:
      raise RuntimeError("Modbus read returned no setpoint registers")

    temp_raw = int(temp_rr.registers[0])
    temp_c = temp_raw / TEMP_SCALE
    sp_raw = int(sp_rr.registers[0])
    sp_c = sp_raw / SETPOINT_SCALE

    with cache_lock:
      cache.temp_raw = temp_raw
      cache.temp_c = temp_c
      cache.last_setpoint_raw = sp_raw
      cache.last_setpoint_c = sp_c
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
        address=qmm_to_modbus_addr(INFO_BLOCK1_START_REG),
        count=INFO_BLOCK1_COUNT
      )
      info2_rr = read_holding_registers(
        address=qmm_to_modbus_addr(INFO_BLOCK2_START_REG),
        count=INFO_BLOCK2_COUNT
      )

    if info1_rr.isError():
      raise RuntimeError(f"Modbus read error (info block 1): {info1_rr}")
    if info2_rr.isError():
      raise RuntimeError(f"Modbus read error (info block 2): {info2_rr}")

    with cache_lock:
      # Block 1: I,136..I,142 -> humidifier_status, conductivity, (138 skip), cyl1_phase, cyl1_status, cyl2_phase, cyl2_status
      cache.info_humidifier_status = int(info1_rr.registers[0])  # I,136
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

  # Poll drain coil state
  try:
    with modbus_lock:
      modbus_connect_or_raise()
      drain_rr = read_coils(address=DRAIN_CYL1_COIL, count=1)

    if drain_rr.isError():
      raise RuntimeError(f"Modbus read error (drain coil): {drain_rr}")

    with cache_lock:
      cache.cyl1_drain_on = bool(drain_rr.bits[0]) if drain_rr.bits else None
  except Exception as e:
    reset_modbus_client()
    with cache_lock:
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

INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{{APP_TITLE}}</title>
  <style>
    /* Base / Desktop styles */
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 28px; }
    .card { border: 1px solid #ddd; border-radius: 12px; padding: 16px; max-width: 520px; }
    .row { margin: 10px 0; }
    .inline-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .top-strip {
      display: flex;
      align-items: center;
      gap: 10px 18px;
      flex-wrap: wrap;
      margin-bottom: 14px;
      padding-bottom: 12px;
      border-bottom: 1px solid #eee;
    }
    .top-item {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 32px;
      color: #1f1f1f;
    }
    .compact-action-row {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: nowrap;
    }
    .compact-action-row label {
      width: auto;
      margin: 0;
    }
    .top-label { font-weight: 600; color: #484848; }
    .top-value { font-weight: 500; }
    .top-time { font-variant-numeric: tabular-nums; }
    .status-dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: #bdbdbd;
      flex: 0 0 auto;
    }
    .status-dot-live {
      background: #0b8a35;
      box-shadow: 0 0 0 0 rgba(11, 138, 53, 0.35);
      animation: statusPulse 1.2s ease-out infinite;
    }
    .status-dot-dead {
      background: #b00020;
      box-shadow: 0 0 0 1px rgba(176, 0, 32, 0.15);
    }
    .status-dot-idle { background: #bdbdbd; }
    label { display: inline-block; width: 180px; }
    input { padding: 6px 8px; width: 120px; }
    button,
    .button-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 6px 10px;
      cursor: pointer;
      border: 1px solid #d9c3a7;
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.82);
      color: #6b4a20;
      font-size: 0.92rem;
      font-weight: 600;
      line-height: 1.2;
      text-decoration: none;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.6);
    }
    button:hover,
    .button-link:hover { background: #fff7ef; }
    button:disabled {
      opacity: 0.55;
      cursor: default;
    }
    .small-btn { padding: 5px 10px; font-size: 0.88rem; }
    .setpoint-input {
      width: 6.6ch;
      min-width: 6.6ch;
      text-align: center;
      padding-left: 6px;
      padding-right: 6px;
    }
    .setpoint-current {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
      color: #3f3f3f;
    }
    .muted { color: #666; font-size: 0.92em; }
    .err { color: #b00020; }
    .ok { color: #0b6b0b; }
    code { background: #f6f6f6; padding: 2px 6px; border-radius: 8px; }
    .modal-backdrop { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.35); display: none; align-items: center; justify-content: center; padding: 16px; }
    .modal-backdrop.open { display: flex; }
    .modal { background: #fff; border-radius: 14px; width: min(100%, 420px); padding: 18px; box-shadow: 0 18px 48px rgba(0, 0, 0, 0.22); }
    .modal h3 { margin: 0 0 8px; }
    .modal .field { margin: 12px 0; }
    .modal .field label { display: block; width: auto; margin-bottom: 6px; font-weight: 600; }
    .modal .field input { width: 100%; box-sizing: border-box; }
    .modal-actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
    .modal-status { min-height: 1.2em; margin-top: 10px; }

    /* Accordion styles */
    details { margin: 14px 0; }
    details summary { cursor: pointer; font-weight: 600; padding: 8px 0; }
    details summary:hover { color: #0066cc; }
    details[open] summary { margin-bottom: 8px; }
    .info-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 16px; }
    .info-grid .info-label { color: #666; font-size: 0.9em; }
    .info-grid .info-value { font-weight: 500; }
    .alarms-panel {
      margin: 14px 0;
      padding: 12px 13px;
      border-radius: 14px;
      border: 1px solid #e6c9a8;
      background:
        radial-gradient(circle at top right, rgba(255, 230, 196, 0.9), transparent 40%),
        linear-gradient(180deg, #fff6eb 0%, #fffdfa 100%);
    }
    .alarms-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
    .alarms-title { margin: 0; font-size: 1.02rem; }
    .alarms-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .alarm-pill {
      display: inline-flex;
      align-items: center;
      padding: 5px 9px;
      border-radius: 999px;
      font-size: 0.8rem;
      font-weight: 700;
      white-space: nowrap;
    }
    .alarm-pill-neutral { background: #efefef; color: #555; }
    .alarm-pill-clear { background: #e7f7ec; color: #0b6b0b; }
    .alarm-pill-active { background: #fff0f0; color: #b00020; }
    .alarm-clear-btn { font-size: 0.88rem; }
    .alarm-empty {
      margin-top: 8px;
      padding: 9px 11px;
      border-radius: 10px;
      border: 1px dashed #d9c3a7;
      background: rgba(255, 255, 255, 0.8);
      color: #6f5b43;
    }
    .alarm-list { display: grid; gap: 8px; margin-top: 8px; }
    .alarm-card {
      padding: 9px 11px;
      border-radius: 10px;
      border: 1px solid #efc3c8;
      background: #fff;
      box-shadow: 0 8px 18px rgba(176, 0, 32, 0.08);
    }
    .alarm-description { margin-top: 0; font-weight: 600; color: #2a2117; line-height: 1.35; }
    .alarm-hint { margin-top: 8px; min-height: 1.2em; }
    .footer-actions {
      margin-top: 18px;
      padding-top: 16px;
      border-top: 1px solid #eee;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .footer-buttons {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .footer-meta {
      width: 100%;
      margin-top: -4px;
      font-size: 0.72em;
    }
    .danger-btn {
      border: 1px solid #d38b93;
      background: #fff3f4;
      color: #8c1d2c;
    }
    .danger-btn:hover { background: #ffe6e9; }
    @keyframes statusPulse {
      0% { box-shadow: 0 0 0 0 rgba(11, 138, 53, 0.35); }
      70% { box-shadow: 0 0 0 8px rgba(11, 138, 53, 0); }
      100% { box-shadow: 0 0 0 0 rgba(11, 138, 53, 0); }
    }

    /* Phone layout (max-width 600px) */
    @media (max-width: 600px) {
      body { margin: 12px; }
      .card { max-width: 100%; padding: 14px; border-radius: 8px; }
      .row { margin: 14px 0; }
      label { display: block; width: 100%; margin-bottom: 6px; font-weight: 500; }
      input { width: calc(100% - 80px); padding: 10px 12px; font-size: 16px; /* prevents iOS zoom */ }
      button { padding: 10px 16px; font-size: 16px; }
      .button-link { padding: 10px 16px; font-size: 16px; }
      .small-btn { padding: 8px 14px; font-size: 15px; }
      .inline-row { align-items: flex-start; }
      .top-strip { gap: 8px 14px; }
      .top-item { min-height: auto; }
      .compact-action-row label { width: auto; margin-bottom: 0; }
      .setpoint-input {
        width: 4.8ch;
        min-width: 4.8ch;
        padding-left: 8px;
        padding-right: 8px;
      }
      .modal { width: 100%; padding: 16px; }
      .modal-actions { flex-direction: column; }
      .modal .field input { width: 100%; }
      h2 { font-size: 1.4em; }
      .alarms-panel { padding: 12px; }
      .footer-actions { align-items: stretch; }
    }
  </style>
</head>
<body>
  <h2>{{APP_TITLE}}</h2>
  <div class="card">
    <div class="top-strip">
      <div class="top-item">
        <span class="top-label">Status:</span>
        <span id="modbusStatusDot" class="status-dot status-dot-idle" aria-hidden="true"></span>
        <span id="status" class="top-value muted">—</span>
      </div>
      <div class="top-item">
        <span class="top-label">Temperature:</span>
        <span id="temp" class="top-value">—</span>
      </div>
      <div class="top-item">
        <span id="deviceTime" class="top-value top-time">—</span>
        <button id="editRtcBtn" class="small-btn" type="button">Edit</button>
      </div>
    </div>

    <div class="row compact-action-row">
      <label>Setpoint (°C):</label>
      <span class="setpoint-current">        
        <span id="lsp">—</span>
      </span>
      <button id="editSetpointBtn" class="small-btn" type="button" disabled>Edit</button>
    </div>

    <section class="alarms-panel" aria-labelledby="alarmsTitle">
      <div class="alarms-head">
        <div>
          <h3 id="alarmsTitle" class="alarms-title">Alarms</h3>
        </div>
        <div class="alarms-actions">
          <span id="alarmsBadge" class="alarm-pill alarm-pill-neutral">Checking...</span>
          <button id="clearAlarmsBtn" class="small-btn alarm-clear-btn" type="button" disabled>Clear alarms</button>
        </div>
      </div>
      <div id="alarmsEmpty" class="alarm-empty">Waiting for alarm status...</div>
      <div id="alarmsList" class="alarm-list"></div>
      <div id="alarmsHint" class="muted alarm-hint"></div>
    </section>

    <details>
      <summary>Info</summary>
      <div class="info-grid">
        <span class="info-label">Humidifier status:</span>
        <span class="info-value" id="infoHumStatus">—</span>
        <span class="info-label">Conductivity:</span>
        <span class="info-value" id="infoConductivity">—</span>
        <span class="info-label">Cyl 1 phase:</span>
        <span class="info-value" id="infoCyl1Phase">—</span>
        <span class="info-label">Cyl 1 status:</span>
        <span class="info-value" id="infoCyl1Status">—</span>
        <span class="info-label">Cyl 2 phase:</span>
        <span class="info-value" id="infoCyl2Phase">—</span>
        <span class="info-label">Cyl 2 status:</span>
        <span class="info-value" id="infoCyl2Status">—</span>
        <span class="info-label">Cyl 1 hours:</span>
        <span class="info-value" id="infoCyl1Hours">—</span>
        <span class="info-label">Cyl 2 hours:</span>
        <span class="info-value" id="infoCyl2Hours">—</span>
        <span class="info-label">Voltage type:</span>
        <span class="info-value" id="infoVoltage">—</span>
        <span class="info-label">Cyl 1 drain:</span>
        <span class="info-value">
          <button id="drainCyl1Btn" class="small-btn" type="button">—</button>
        </span>
      </div>
      <div id="infoError" class="muted"></div>
    </details>

    <div class="footer-actions">
      <span id="systemStatus" class="muted">System actions</span>
      <div class="footer-buttons">
        <a class="button-link" href="logs" target="_blank" rel="noopener">Open Log</a>
        <button id="rebootBtn" class="danger-btn" type="button">Reboot</button>
      </div>
      <div class="muted footer-meta">Commit: {{APP_COMMIT_HASH}}</div>
    </div>
  </div>

  <div id="rtcModalBackdrop" class="modal-backdrop" aria-hidden="true">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="rtcModalTitle">
      <h3 id="rtcModalTitle">Edit Device Date/Time</h3>
      <div class="muted">Use the device local date/time shown by the controller.</div>

      <div class="field">
        <label for="rtcInput">Device date/time</label>
        <input id="rtcInput" type="datetime-local" step="60" />
      </div>

      <div class="modal-actions">
        <button id="useBrowserTimeBtn" type="button">Use Browser Time</button>
        <button id="saveRtcBtn" type="button">Save</button>
        <button id="cancelRtcBtn" type="button">Cancel</button>
      </div>

      <div id="rtcModalStatus" class="modal-status muted"></div>
    </div>
  </div>

  <div id="setpointModalBackdrop" class="modal-backdrop" aria-hidden="true">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="setpointModalTitle">
      <h3 id="setpointModalTitle">Edit Setpoint</h3>
      <div class="muted">Enter the new temperature setpoint.</div>

      <div class="field">
        <label for="setpointInput">Setpoint (°C)</label>
        <input id="setpointInput" class="setpoint-input" type="number" step="0.1" inputmode="decimal" />
      </div>

      <div class="modal-actions">
        <button id="saveSetpointBtn" type="button">Save</button>
        <button id="cancelSetpointBtn" type="button">Cancel</button>
      </div>

      <div id="setpointModalStatus" class="modal-status muted"></div>
    </div>
  </div>

<script>
  let rtcModalOpen = false;
  let setpointModalOpen = false;
  let lastRtcIsoLocal = null;
  let lastSetpointC = null;
  let lastAlarmState = null;
  let clearAlarmsBusy = false;

  function browserDateTimeLocalValue() {
    const now = new Date();
    const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }

  function openRtcModal() {
    rtcModalOpen = true;
    document.getElementById('rtcModalBackdrop').classList.add('open');
    document.getElementById('rtcModalBackdrop').setAttribute('aria-hidden', 'false');
    document.getElementById('rtcModalStatus').textContent = '';
    document.getElementById('rtcModalStatus').className = 'modal-status muted';
    if (lastRtcIsoLocal) {
      document.getElementById('rtcInput').value = lastRtcIsoLocal;
    }
    document.getElementById('rtcInput').focus();
  }

  function closeRtcModal() {
    rtcModalOpen = false;
    document.getElementById('rtcModalBackdrop').classList.remove('open');
    document.getElementById('rtcModalBackdrop').setAttribute('aria-hidden', 'true');
  }

  function openSetpointModal() {
    if (lastSetpointC === null || lastSetpointC === undefined) {
      return;
    }

    setpointModalOpen = true;
    document.getElementById('setpointModalBackdrop').classList.add('open');
    document.getElementById('setpointModalBackdrop').setAttribute('aria-hidden', 'false');
    document.getElementById('setpointModalStatus').textContent = '';
    document.getElementById('setpointModalStatus').className = 'modal-status muted';
    const input = document.getElementById('setpointInput');
    input.value = lastSetpointC.toFixed(1);
    input.focus();
    input.select();
  }

  function closeSetpointModal() {
    setpointModalOpen = false;
    document.getElementById('setpointModalBackdrop').classList.remove('open');
    document.getElementById('setpointModalBackdrop').setAttribute('aria-hidden', 'true');
  }

  function setAlarmBadge(mode, text) {
    const badge = document.getElementById('alarmsBadge');
    badge.textContent = text;
    badge.className = 'alarm-pill ' + mode;
  }

  function setModbusIndicator(state) {
    const dot = document.getElementById('modbusStatusDot');
    dot.className = 'status-dot ' + state;
  }

  function syncClearAlarmsButton() {
    const clearBtn = document.getElementById('clearAlarmsBtn');
    clearBtn.textContent = clearAlarmsBusy ? 'Clearing...' : 'Clear alarms';
    clearBtn.disabled = clearAlarmsBusy || !(lastAlarmState && lastAlarmState.has_active === true);
  }

  function renderAlarms(alarms) {
    lastAlarmState = alarms;
    const empty = document.getElementById('alarmsEmpty');
    const list = document.getElementById('alarmsList');
    const hint = document.getElementById('alarmsHint');
    list.replaceChildren();
    list.hidden = true;
    empty.hidden = false;
    hint.textContent = '';
    hint.className = 'alarm-hint muted';
    syncClearAlarmsButton();

    if (!alarms) {
      setAlarmBadge('alarm-pill-neutral', 'Unavailable');
      empty.textContent = 'Waiting for alarm status...';
      return;
    }

    if (alarms.error) {
      setAlarmBadge('alarm-pill-neutral', 'Read error');
      empty.textContent = 'Unable to load alarms right now.';
      hint.textContent = alarms.error;
      hint.className = 'alarm-hint err';
      return;
    }

    if (alarms.has_active === true) {
      setAlarmBadge('alarm-pill-active', 'Active');
      if (alarms.active.length > 0) {
        empty.hidden = true;
        list.hidden = false;
        alarms.active.forEach((alarm) => {
          const card = document.createElement('div');
          card.className = 'alarm-card';

          const description = document.createElement('div');
          description.className = 'alarm-description';
          description.textContent = alarm.description;

          card.append(description);
          list.appendChild(card);
        });
      } else if (alarms.skipped_active_count > 0) {
        empty.textContent = 'Alarm summary is active, but only intentionally skipped cylinder 2 alarm bits are set.';
      } else {
        empty.textContent = 'Alarm summary is active, but no monitored alarm bits are currently set.';
      }
    } else if (alarms.has_active === false) {
      setAlarmBadge('alarm-pill-clear', 'Clear');
      empty.textContent = 'No active alarms.';
    } else {
      setAlarmBadge('alarm-pill-neutral', 'Checking...');
      empty.textContent = 'Waiting for alarm status...';
    }

    if (alarms.skipped_active_count > 0) {
      const plural = alarms.skipped_active_count === 1 ? 'bit' : 'bits';
      hint.textContent =
        'Cylinder 2 alarms are intentionally skipped on this unit (' +
        alarms.skipped_active_count + ' skipped ' + plural + ' active).';
      hint.className = 'alarm-hint muted';
    } else {
      hint.className = 'alarm-hint muted';
    }
  }

  async function refresh() {
    try {
      const r = await fetch('api/temp');
      const j = await r.json();

      lastRtcIsoLocal = j.device_time_iso_local || lastRtcIsoLocal;

      if (j.ok) {
        document.getElementById('temp').textContent = j.temp_c.toFixed(1) + ' °C';
        document.getElementById('status').textContent = 'OK';
        document.getElementById('status').className = 'top-value ok';
        document.getElementById('status').title = 'Latest Modbus poll succeeded.';
        setModbusIndicator('status-dot-live');
      } else {
        document.getElementById('temp').textContent = '—';
        document.getElementById('status').textContent = 'Error';
        document.getElementById('status').className = 'top-value err';
        document.getElementById('status').title = j.error || 'No data';
        setModbusIndicator('status-dot-dead');
      }

      if (j.device_time_display) {
        document.getElementById('deviceTime').textContent = j.device_time_display;
        if (!rtcModalOpen && j.device_time_iso_local) {
          document.getElementById('rtcInput').value = j.device_time_iso_local;
        }
      } else {
        document.getElementById('deviceTime').textContent = '—';
      }

      const hasSetpoint = j.last_setpoint_c !== null && j.last_setpoint_c !== undefined;
      lastSetpointC = hasSetpoint ? j.last_setpoint_c : null;
      document.getElementById('editSetpointBtn').disabled = !hasSetpoint;
      if (hasSetpoint) {
        document.getElementById('lsp').textContent = j.last_setpoint_c.toFixed(1) + ' °C';
      } else {
        document.getElementById('lsp').textContent = '—';
      }

      // Info accordion
      if (j.info) {
        const humStatMap = {0:'On duty', 1:'Alarm(s)', 2:'Disabled (network)', 3:'Disabled (timer)', 4:'Disabled (remote)', 5:'Disabled (keyboard)', 6:'Manual', 7:'No demand'};
        const phaseMap = {0:'Not active', 1:'Softstart', 2:'Start', 3:'Steady state', 4:'Reduced', 5:'Delayed stop', 6:'Full flush', 7:'Fast Start', 8:'Fast Start (foam)', 9:'Fast Start (heating)'};
        const statusMap = {0:'No production', 1:'Start evap', 2:'Water fill', 3:'Producing', 4:'Drain (deciding)', 5:'Drain (pump)', 6:'Drain (closing)', 7:'Blocked', 8:'Inactivity drain', 9:'Flushing', 10:'Manual drain', 11:'No supply water', 12:'Periodic drain'};
        const voltMap = {0:'200V', 1:'208V', 2:'230V', 3:'400V', 4:'460V', 5:'575V'};

        document.getElementById('infoHumStatus').textContent = humStatMap[j.info.humidifier_status] ?? j.info.humidifier_status ?? '—';
        document.getElementById('infoConductivity').textContent = j.info.conductivity ?? '—';
        document.getElementById('infoCyl1Phase').textContent = phaseMap[j.info.cyl1_phase] ?? j.info.cyl1_phase ?? '—';
        document.getElementById('infoCyl1Status').textContent = statusMap[j.info.cyl1_status] ?? j.info.cyl1_status ?? '—';
        document.getElementById('infoCyl2Phase').textContent = phaseMap[j.info.cyl2_phase] ?? j.info.cyl2_phase ?? '—';
        document.getElementById('infoCyl2Status').textContent = statusMap[j.info.cyl2_status] ?? j.info.cyl2_status ?? '—';
        document.getElementById('infoCyl1Hours').textContent = j.info.cyl1_hours ?? '—';
        document.getElementById('infoCyl2Hours').textContent = j.info.cyl2_hours ?? '—';
        document.getElementById('infoVoltage').textContent = voltMap[j.info.voltage_type] ?? j.info.voltage_type ?? '—';
        document.getElementById('infoError').textContent = j.info.error || '';
        document.getElementById('infoError').className = j.info.error ? 'muted err' : 'muted';
        // Drain button
        const drainBtn = document.getElementById('drainCyl1Btn');
        if (j.info.cyl1_drain_on === true) {
          drainBtn.textContent = 'ON';
          drainBtn.style.background = '#ffcccc';
        } else if (j.info.cyl1_drain_on === false) {
          drainBtn.textContent = 'OFF';
          drainBtn.style.background = '';
        } else {
          drainBtn.textContent = '—';
          drainBtn.style.background = '';
        }
      }

      renderAlarms(j.alarms);
    } catch (e) {
      document.getElementById('status').textContent = 'UI error';
      document.getElementById('status').className = 'top-value err';
      document.getElementById('status').title = String(e);
      document.getElementById('temp').textContent = '—';
      document.getElementById('deviceTime').textContent = '—';
      setModbusIndicator('status-dot-dead');
      setAlarmBadge('alarm-pill-neutral', 'UI error');
      document.getElementById('alarmsEmpty').textContent = 'Unable to render alarms.';
      document.getElementById('alarmsHint').textContent = String(e);
      document.getElementById('alarmsHint').className = 'alarm-hint err';
    }
  }

  async function saveRtc() {
    const input = document.getElementById('rtcInput');
    const modalStatus = document.getElementById('rtcModalStatus');
    const value = input.value;
    if (!value) {
      modalStatus.textContent = 'Pick a valid date/time first.';
      modalStatus.className = 'modal-status err';
      return;
    }

    modalStatus.textContent = 'Saving...';
    modalStatus.className = 'modal-status muted';

    const r = await fetch('api/device-datetime', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ datetime_local: value })
    });
    const j = await r.json();
    if (!j.ok) {
      modalStatus.textContent = 'Write failed: ' + (j.error || 'unknown');
      modalStatus.className = 'modal-status err';
      return;
    }

    lastRtcIsoLocal = j.device_time_iso_local || value;
    closeRtcModal();
    await refresh();
  }

  async function saveSetpoint() {
    const input = document.getElementById('setpointInput');
    const modalStatus = document.getElementById('setpointModalStatus');
    const saveBtn = document.getElementById('saveSetpointBtn');
    const cancelBtn = document.getElementById('cancelSetpointBtn');
    const v = Number(input.value);
    if (!Number.isFinite(v)) {
      modalStatus.textContent = 'Enter a valid setpoint (°C).';
      modalStatus.className = 'modal-status err';
      return;
    }

    modalStatus.textContent = 'Saving...';
    modalStatus.className = 'modal-status muted';
    input.disabled = true;
    saveBtn.disabled = true;
    cancelBtn.disabled = true;

    try {
      const r = await fetch('api/setpoint', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ temp_c: v })
      });
      const j = await r.json();
      if (!j.ok) {
        modalStatus.textContent = 'Write failed: ' + (j.error || 'unknown');
        modalStatus.className = 'modal-status err';
        input.focus();
        input.select();
        return;
      }
    } catch (e) {
      modalStatus.textContent = 'Write failed: ' + e;
      modalStatus.className = 'modal-status err';
      input.focus();
      input.select();
      return;
    } finally {
      input.disabled = false;
      saveBtn.disabled = false;
      cancelBtn.disabled = false;
    }

    closeSetpointModal();
    await refresh();
  }

  async function clearAlarms() {
    clearAlarmsBusy = true;
    syncClearAlarmsButton();

    try {
      const r = await fetch('api/alarms-reset', { method: 'POST' });
      const j = await r.json();
      if (!j.ok) {
        alert('Alarm reset failed: ' + (j.error || 'unknown'));
        return;
      }
    } catch (e) {
      alert('Alarm reset failed: ' + e);
    } finally {
      clearAlarmsBusy = false;
      await refresh();
    }
  }

  async function rebootDevice() {
    const shouldReboot = window.confirm('Are you sure you want to reboot the device?');
    if (!shouldReboot) return;

    const rebootBtn = document.getElementById('rebootBtn');
    const systemStatus = document.getElementById('systemStatus');
    rebootBtn.disabled = true;
    systemStatus.textContent = 'Sending reboot command...';
    systemStatus.className = 'muted';

    try {
      const r = await fetch('api/reboot', { method: 'POST' });
      const j = await r.json();
      if (!j.ok) {
        systemStatus.textContent = j.error || 'Reboot failed.';
        systemStatus.className = 'err';
        return;
      }
      systemStatus.textContent = j.message || 'Reboot command sent.';
      systemStatus.className = 'muted';
    } catch (e) {
      systemStatus.textContent = 'Reboot failed: ' + e;
      systemStatus.className = 'err';
    } finally {
      rebootBtn.disabled = false;
    }
  }

  document.getElementById('editSetpointBtn').addEventListener('click', openSetpointModal);
  document.getElementById('saveSetpointBtn').addEventListener('click', saveSetpoint);
  document.getElementById('cancelSetpointBtn').addEventListener('click', closeSetpointModal);
  document.getElementById('setpointInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      saveSetpoint();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeSetpointModal();
    }
  });
  document.getElementById('editRtcBtn').addEventListener('click', openRtcModal);
  document.getElementById('cancelRtcBtn').addEventListener('click', closeRtcModal);
  document.getElementById('saveRtcBtn').addEventListener('click', saveRtc);
  document.getElementById('useBrowserTimeBtn').addEventListener('click', () => {
    document.getElementById('rtcInput').value = browserDateTimeLocalValue();
    document.getElementById('rtcModalStatus').textContent = 'Browser time loaded.';
    document.getElementById('rtcModalStatus').className = 'modal-status muted';
  });
  document.getElementById('rtcModalBackdrop').addEventListener('click', (event) => {
    if (event.target.id === 'rtcModalBackdrop') {
      closeRtcModal();
    }
  });
  document.getElementById('setpointModalBackdrop').addEventListener('click', (event) => {
    if (event.target.id === 'setpointModalBackdrop') {
      closeSetpointModal();
    }
  });
  document.getElementById('drainCyl1Btn').addEventListener('click', async () => {
    const btn = document.getElementById('drainCyl1Btn');
    btn.disabled = true;
    try {
      const r = await fetch('api/cyl1-drain', { method: 'POST' });
      const j = await r.json();
      if (!j.ok) alert('Toggle failed: ' + (j.error || 'unknown'));
    } finally {
      btn.disabled = false;
      await refresh();
    }
  });
  document.getElementById('rebootBtn').addEventListener('click', rebootDevice);
  document.getElementById('clearAlarmsBtn').addEventListener('click', clearAlarms);

  refresh();
  setInterval(refresh, 1000);
</script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index() -> Response:
    app_title = "CAREL™ Supervisory System [Simulator]" if is_simulator_mode() else "CAREL™ Supervisory System"
    html = INDEX_HTML.replace("{{APP_TITLE}}", app_title)
    html = html.replace("{{APP_COMMIT_HASH}}", APP_COMMIT_HASH)
    return Response(html, mimetype="text/html")


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
    try:
        body = request.get_json(force=True, silent=False)
        if not isinstance(body, dict) or "temp_c" not in body:
            raise ValueError("JSON must include 'temp_c'")

        sp_c = float(body["temp_c"])
        # Optional: clamp / validate a sane range for your system
        if not (-20.0 <= sp_c <= 100.0):
            raise ValueError("temp_c out of allowed range (-20..100)")

        sp_raw = int(round(sp_c * SETPOINT_SCALE))
        if not (0 <= sp_raw <= 65535):
            raise ValueError("scaled value out of 16-bit range")

        with modbus_lock:
          modbus_connect_or_raise()
          wr = write_register(address=SETPOINT_ADDR, value=sp_raw)
        if wr.isError():
          raise RuntimeError(f"Modbus write error: {wr}")

        with cache_lock:
            cache.last_write_utc = now_iso()
            cache.last_setpoint_raw = sp_raw
            cache.last_setpoint_c = sp_raw / SETPOINT_SCALE
            cache.last_error = None
        logger.info("Setpoint written successfully: %.1f C on %s", sp_c, active_com_port)
        clear_runtime_error()

        return jsonify({"ok": True, "temp_c": cache.last_setpoint_c, "raw": cache.last_setpoint_raw})
    except Exception as e:
        reset_modbus_client()
        error_message = normalize_modbus_error(e)
        note_runtime_error(error_message)
        with cache_lock:
          cache.last_error = error_message
        return jsonify({"ok": False, "error": cache.last_error}), 400


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
