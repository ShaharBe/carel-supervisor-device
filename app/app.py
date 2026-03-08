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

def poll_registers_once() -> None:
  """Read temperature and setpoint holding registers, update cache."""
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
    label { display: inline-block; width: 180px; }
    input { padding: 6px 8px; width: 120px; }
    button { padding: 7px 12px; cursor: pointer; }
    .button-link { display: inline-block; padding: 7px 12px; border: 1px solid #bbb; border-radius: 8px; color: #111; text-decoration: none; }
    .muted { color: #666; font-size: 0.92em; }
    .err { color: #b00020; }
    .ok { color: #0b6b0b; }
    code { background: #f6f6f6; padding: 2px 6px; border-radius: 8px; }

    /* Phone layout (max-width 600px) */
    @media (max-width: 600px) {
      body { margin: 12px; }
      .card { max-width: 100%; padding: 14px; border-radius: 8px; }
      .row { margin: 14px 0; }
      label { display: block; width: 100%; margin-bottom: 6px; font-weight: 500; }
      input { width: calc(100% - 80px); padding: 10px 12px; font-size: 16px; /* prevents iOS zoom */ }
      button { padding: 10px 16px; font-size: 16px; }
      .button-link { padding: 10px 16px; font-size: 16px; }
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

<script>
  async function refresh() {
    try {
      const r = await fetch('api/temp');
      const j = await r.json();

      document.getElementById('tempReg').textContent = j.config.temp_reg_qmm;
      document.getElementById('spReg').textContent = j.config.setpoint_reg_qmm;

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
            "poll_interval_s": POLL_INTERVAL_S,
        },
        **data,
    }
    if not ok:
        resp["error"] = data["last_error"] or "No data yet"
    return jsonify(resp)

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
