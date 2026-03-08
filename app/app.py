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
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict, Any, cast

from flask import Flask, jsonify, request, Response

# Client factory - handles real HW vs simulator toggle
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
RTC_ENABLE_HOUR_BIT = 1
RTC_ENABLE_MINUTE_BIT = 2
RTC_ENABLE_DAY_BIT = 3
RTC_ENABLE_MONTH_BIT = 4
RTC_ENABLE_YEAR_BIT = 5
RTC_ENABLE_WEEKDAY_BIT = 6
RTC_WRITE_WEEKDAY_REG = 159
RTC_WRITE_HOUR_REG = 160
RTC_WRITE_MINUTE_REG = 161
RTC_WRITE_DAY_REG = 162
RTC_WRITE_MONTH_REG = 163
RTC_WRITE_YEAR_REG = 164

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

cache = Cache()
cache_lock = threading.Lock()
modbus_lock = threading.Lock()
runtime_state_lock = threading.Lock()
logger = setup_logging()
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
RTC_ENABLE_START_ADDR = qmm_to_modbus_addr(RTC_ENABLE_HOUR_BIT)
RTC_ENABLE_COUNT = 6
RTC_WRITE_START_ADDR = qmm_to_modbus_addr(RTC_WRITE_WEEKDAY_REG)
RTC_WRITE_COUNT = 6
RTC_LATCH_PULSE_DELAY_S = 0.15


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
  """Write the editable CAREL RTC block in weekday,hour,minute,day,month,year order."""
  write_values = [
    value.weekday(),
    value.hour,
    value.minute,
    value.day,
    value.month,
    encode_device_year(value.year, current_raw_year),
  ]
  return write_registers(address=RTC_WRITE_START_ADDR, values=write_values)


def set_rtc_edit_latches(value: bool) -> None:
  """Drive all RTC edit latch bits to a known state."""
  if is_simulator_mode():
    return

  for index in range(RTC_ENABLE_COUNT):
    wr = write_coil(address=RTC_ENABLE_START_ADDR + index, value=value)
    if wr.isError():
      state = "set" if value else "clear"
      raise RuntimeError(f"Modbus write error ({state} rtc latch D{index + 1}): {wr}")

def pulse_rtc_edit_latches() -> None:
  """Commit the prepared RTC shadow registers by pulsing D1..D6 one at a time."""
  if is_simulator_mode():
    return

  for index in range(RTC_ENABLE_COUNT):
    wr = write_coil(address=RTC_ENABLE_START_ADDR + index, value=True)
    if wr.isError():
      raise RuntimeError(f"Modbus write error (set rtc latch D{index + 1}): {wr}")
    time.sleep(RTC_LATCH_PULSE_DELAY_S)
    wr = write_coil(address=RTC_ENABLE_START_ADDR + index, value=False)
    if wr.isError():
      raise RuntimeError(f"Modbus write error (clear rtc latch D{index + 1}): {wr}")

def write_device_rtc(value: datetime, current_raw_year: Optional[int]) -> None:
  """Write RTC shadow registers, then pulse the D latches to commit them."""
  set_rtc_edit_latches(False)
  wr = write_device_rtc_values(value, current_raw_year)
  if wr.isError():
    raise RuntimeError(f"Modbus write error (device time shadow): {wr}")
  pulse_rtc_edit_latches()
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
  <title>CAREL Modbus PoC</title>
  <style>
    /* Base / Desktop styles */
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 28px; }
    .card { border: 1px solid #ddd; border-radius: 12px; padding: 16px; max-width: 520px; }
    .row { margin: 10px 0; }
    .inline-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    label { display: inline-block; width: 180px; }
    input { padding: 6px 8px; width: 120px; }
    button { padding: 7px 12px; cursor: pointer; }
    .small-btn { padding: 5px 10px; font-size: 0.92rem; }
    .button-link { display: inline-block; padding: 7px 12px; border: 1px solid #bbb; border-radius: 8px; color: #111; text-decoration: none; }
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
      .modal { width: 100%; padding: 16px; }
      .modal-actions { flex-direction: column; }
      .modal .field input { width: 100%; }
      h2 { font-size: 1.4em; }
      .config-info { display: none; } /* hide technical details on phone */
    }
  </style>
</head>
<body>
  <h2>CAREL Modbus PoC</h2>
  <div class="card">
    <div class="row">
      <label>Temperature:</label>
      <span id="temp">—</span>
      <span class="muted" id="temp_raw"></span>
    </div>

    <div class="row">
      <label>Last update (UTC):</label>
      <span class="muted" id="ts">—</span>
    </div>

    <div class="row">
      <label>Status:</label>
      <span id="status" class="muted">—</span>
    </div>

    <div class="row">
      <label>Device date/time:</label>
      <span class="inline-row">
        <span id="deviceTime">—</span>
        <button id="editRtcBtn" class="small-btn" type="button">Edit</button>
      </span>
    </div>

    <div class="row">
      <label>RTC status:</label>
      <span id="rtcStatus" class="muted">—</span>
    </div>

    <hr/>

    <div class="row">
      <label>Setpoint (°C):</label>
      <input id="sp" type="number" step="0.1" placeholder="e.g. 28.0"/>
      <button id="setBtn">Write</button>
    </div>

    <div class="row">
      <label>Last write (UTC):</label>
      <span class="muted" id="wts">—</span>
    </div>

    <div class="row">
      <label>Current setpoint:</label>
      <span id="lsp">—</span>
    </div>

    <div class="row">
      <label>Diagnostics:</label>
      <a class="button-link" href="/logs" target="_blank" rel="noopener">Open Log</a>
    </div>

    <div class="row muted config-info">
      Temp reg (QModMaster): <code id="tempReg"></code>,
      Setpoint reg (QModMaster): <code id="spReg"></code>
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

<script>
  let rtcModalOpen = false;
  let lastRtcIsoLocal = null;

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

  async function refresh() {
    try {
      const r = await fetch('api/temp');
      const j = await r.json();

      document.getElementById('tempReg').textContent = j.config.temp_reg_qmm;
      document.getElementById('spReg').textContent = j.config.setpoint_reg_qmm;
      lastRtcIsoLocal = j.device_time_iso_local || lastRtcIsoLocal;

      if (j.ok) {
        document.getElementById('temp').textContent = j.temp_c.toFixed(1) + ' °C';
        document.getElementById('temp_raw').textContent = '(raw ' + j.temp_raw + ')';
        document.getElementById('ts').textContent = j.last_update_utc || '—';
        document.getElementById('status').textContent = 'OK';
        document.getElementById('status').className = 'ok';
      } else {
        document.getElementById('status').textContent = j.error || 'No data';
        document.getElementById('status').className = 'err';
      }

      if (j.device_time_display) {
        document.getElementById('deviceTime').textContent = j.device_time_display;
        document.getElementById('rtcStatus').textContent = j.last_rtc_write_utc
          ? ('Last RTC write: ' + j.last_rtc_write_utc)
          : (j.last_rtc_update_utc || 'RTC OK');
        document.getElementById('rtcStatus').className = 'muted';
        if (!rtcModalOpen && j.device_time_iso_local) {
          document.getElementById('rtcInput').value = j.device_time_iso_local;
        }
      } else {
        document.getElementById('deviceTime').textContent = '—';
        document.getElementById('rtcStatus').textContent = j.rtc_error || 'No RTC data';
        document.getElementById('rtcStatus').className = 'err';
      }

      // write info
      document.getElementById('wts').textContent = j.last_write_utc || '—';
      if (j.last_setpoint_c !== null && j.last_setpoint_c !== undefined) {
        document.getElementById('lsp').textContent =
          j.last_setpoint_c.toFixed(1) + ' °C (raw ' + j.last_setpoint_raw + ')';
      } else {
        document.getElementById('lsp').textContent = '—';
      }
    } catch (e) {
      document.getElementById('status').textContent = 'UI error: ' + e;
      document.getElementById('status').className = 'err';
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

  async function writeSetpoint() {
    const v = Number(document.getElementById('sp').value);
    if (!Number.isFinite(v)) {
      alert('Enter a valid setpoint (°C).');
      return;
    }
    const r = await fetch('api/setpoint', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ temp_c: v })
    });
    const j = await r.json();
    if (!j.ok) alert('Write failed: ' + (j.error || 'unknown'));
    await refresh();
  }

  document.getElementById('setBtn').addEventListener('click', writeSetpoint);
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

  refresh();
  setInterval(refresh, 1000);
</script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index() -> Response:
    return Response(INDEX_HTML, mimetype="text/html")


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
