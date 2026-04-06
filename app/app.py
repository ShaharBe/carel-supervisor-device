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
import subprocess
import time
from datetime import datetime
from typing import Any, Dict

from flask import Flask, Response, jsonify, render_template, request

from alarms import ALARM_CATALOG
from client_factory import is_simulator_mode
from display_menu import load_display_menu
from menu_service import (
    MAX_PRODUCTION_FIELD,
    PROP_BAND_FIELD,
    SETPOINT_FIELD,
    annotate_menu_tree,
    coerce_menu_write,
    ensure_dashboard_config_cache,
    format_limit,
    handle_humidifier_toggle,
    handle_menu_value_get,
    handle_menu_value_post,
    infer_menu_editor_type,
    infer_menu_numeric_limits,
    infer_menu_numeric_scale,
    is_menu_node_modbus_backed,
    is_menu_node_writable,
    normalize_editor_options,
    parse_choice_tokens,
    parse_menu_boolean_value,
    write_writable_field,
)
from modbus_map import (
    ALARM_RESET_COIL,
    DRAIN_CYL1_COIL,
    MAX_PRODUCTION_REG,
    POLL_INTERVAL_S,
    PROP_BAND_REG,
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
    SETPOINT_REG,
    TEMP_REG,
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
    format_device_datetime_local,
    get_active_com_port,
    logger,
    modbus_connect_or_raise,
    modbus_lock,
    normalize_modbus_error,
    now_iso,
    poll_registers_once,
    read_device_rtc_values,
    read_log_tail,
    reset_modbus_client,
    start_background_poller,
    write_coil,
    write_device_rtc,
)


def read_runtime_commit_hash() -> str:
  """Return the deployment-injected commit hash for the running app."""
  return os.environ.get("APP_COMMIT_HASH", "").strip() or "unknown"


APP_COMMIT_HASH = read_runtime_commit_hash()


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
    menu_payload = load_display_menu()
    if menu_payload.get("ok"):
        annotate_menu_tree(menu_payload["root"])
    return render_template(
        "index.html",
        APP_TITLE=app_title,
        APP_COMMIT_HASH=APP_COMMIT_HASH,
        DISPLAY_MENU=menu_payload,
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
    return handle_menu_value_get()


@app.route("/api/menu-value", methods=["POST"])
def api_menu_value_post():
    return handle_menu_value_post()


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
    return handle_humidifier_toggle()


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
