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

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from flask import Flask, jsonify, request, Response

# Client factory - handles real HW vs simulator toggle
from client_factory import create_modbus_client, is_simulator_mode

# ---------------------------
# Configuration (easy to edit)
# ---------------------------

COM_PORT = "COM3"          # Windows COM port, e.g. COM3
BAUDRATE = 9600            # e.g. 9600
PARITY = "N"               # "N", "E", "O"
STOPBITS = 1               # 1 or 2
BYTESIZE = 8               # usually 8
SLAVE_ID = 1               # Modbus slave address

# QModMaster-style 1-based register numbers (as you see in your tool)
TEMP_REG = 2               # You said: "main temp (addr 1)" earlier, but later confirmed reg 2=249 -> 24.9C
                           # Put the exact register number you read temp from in QModMaster here.
SETPOINT_REG = 20          # Setpoint register (QModMaster-style)

POLL_INTERVAL_S = 1.0      # temperature polling period

# Scaling
TEMP_SCALE = 10.0          # 249 -> 24.9
SETPOINT_SCALE = 10.0      # assume same scaling for setpoint (common on Carel)


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

# Create Modbus client (single owner)
# Toggle between real HW and simulator with USE_SIMULATOR=1 env var
client = create_modbus_client(
    port=COM_PORT,
    baudrate=BAUDRATE,
    parity=PARITY,
    stopbits=STOPBITS,
    bytesize=BYTESIZE,
    timeout=1.0,   # seconds
    retries=1,
)

TEMP_ADDR = qmm_to_modbus_addr(TEMP_REG)
SETPOINT_ADDR = qmm_to_modbus_addr(SETPOINT_REG)


def modbus_connect_or_raise() -> None:
    """Ensure client is connected, raise RuntimeError if not."""
    if client.connected:
        return
    if not client.connect():
        raise RuntimeError(f"Failed to connect Modbus serial on {COM_PORT} @ {BAUDRATE}")

def poll_registers_once() -> None:
    """Read temperature and setpoint holding registers, update cache."""
    try:
        with modbus_lock:
            modbus_connect_or_raise()
            temp_rr = client.read_holding_registers(address=TEMP_ADDR, count=1, slave=SLAVE_ID)
            sp_rr = client.read_holding_registers(address=SETPOINT_ADDR, count=1, slave=SLAVE_ID)

        if temp_rr.isError():
            raise RuntimeError(f"Modbus read error (temp): {temp_rr}")
        if sp_rr.isError():
            raise RuntimeError(f"Modbus read error (setpoint): {sp_rr}")

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
    except Exception as e:
        with cache_lock:
            cache.last_error = str(e)

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
            "com_port": COM_PORT,
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
            wr = client.write_register(address=SETPOINT_ADDR, value=sp_raw, slave=SLAVE_ID)
        if wr.isError():
            raise RuntimeError(f"Modbus write error: {wr}")

        with cache_lock:
            cache.last_write_utc = now_iso()
            cache.last_setpoint_raw = sp_raw
            cache.last_setpoint_c = sp_raw / SETPOINT_SCALE
            cache.last_error = None

        return jsonify({"ok": True, "temp_c": cache.last_setpoint_c, "raw": cache.last_setpoint_raw})
    except Exception as e:
        with cache_lock:
            cache.last_error = str(e)
        return jsonify({"ok": False, "error": str(e)}), 400


def start_background_poller() -> None:
    t = threading.Thread(target=poller_loop, args=(stop_event,), daemon=True)
    t.start()


# Start poller when module loads (works with both `python app.py` and `flask run`)
# Guard against Flask's reloader which spawns two processes - only run in the worker
import os
if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or os.environ.get('FLASK_DEBUG') != '1':
    start_background_poller()


def main() -> None:
    # Preflight read (optional) so you see something quickly
    time.sleep(0.2)
    poll_registers_once()

    # Run Flask dev server (fine for PoC)
    # If you want a steadier Windows server, use waitress:
    #   pip install waitress
    #   waitress-serve --listen=0.0.0.0:8000 app:app
    # app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
    # app.run(host="10.8.0.2", port=5000, debug=False, threaded=True)   
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True) 


if __name__ == "__main__":
    main()
