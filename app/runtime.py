from __future__ import annotations

import glob
import logging
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional, cast

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
    MAX_PRODUCTION_ADDR,
    MAX_PRODUCTION_SCALE,
    POLL_INTERVAL_S,
    PROP_BAND_ADDR,
    PROP_BAND_SCALE,
    RTC_LATCH_COILS,
    RTC_LATCH_PULSE_DELAY_S,
    RTC_READ_COUNT,
    RTC_READ_START_ADDR,
    RTC_WRITE_SHADOW_ADDRS,
    SETPOINT_ADDR,
    SETPOINT_SCALE,
    TEMP_ADDR,
    TEMP_SCALE,
)


COM_PORT = "/dev/ttyACM0"
BAUDRATE = 9600
PARITY = "N"
STOPBITS = 1
BYTESIZE = 8
SLAVE_ID = 1
USB_VENDOR_ID = "1a86"
USB_MODEL_ID = "55d3"
USB_SERIAL_SHORT = "586D012821"

LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs"))
LOG_FILE = os.path.join(LOG_DIR, "app.log")
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUP_COUNT = 3


def setup_logging() -> logging.Logger:
    """Configure a small app logger for journald and a rotating file."""
    os.makedirs(LOG_DIR, exist_ok=True)

    app_logger = logging.getLogger("carel_supervisor")
    if app_logger.handlers:
        return app_logger

    app_logger.setLevel(logging.INFO)
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

    app_logger.addHandler(stream_handler)
    app_logger.addHandler(file_handler)
    app_logger.propagate = False

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    return app_logger


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
    menu_values: dict[str, dict[str, Any]] = field(default_factory=dict)


cache = Cache()
cache_lock = threading.Lock()
modbus_lock = threading.Lock()
runtime_state_lock = threading.Lock()
logger = setup_logging()
last_detected_port: Optional[str] = None
last_adapter_missing = False
last_connected_port: Optional[str] = None
last_runtime_error: Optional[str] = None
interactive_modbus_deadline_monotonic = 0.0

active_com_port = COM_PORT
client = build_modbus_client(active_com_port)


def get_active_com_port() -> str:
    return active_com_port


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


def request_interactive_modbus_priority(duration_s: float = 2.0) -> None:
    global interactive_modbus_deadline_monotonic
    deadline = time.monotonic() + max(0.1, duration_s)
    with runtime_state_lock:
        interactive_modbus_deadline_monotonic = max(interactive_modbus_deadline_monotonic, deadline)


def interactive_modbus_priority_active() -> bool:
    with runtime_state_lock:
        return time.monotonic() < interactive_modbus_deadline_monotonic


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

    note_adapter_detected(ports[0])
    return ports[0]


def adapter_identity_text() -> str:
    return f"vendor={USB_VENDOR_ID}, product={USB_MODEL_ID}, serial={USB_SERIAL_SHORT}"


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

    if active_com_port not in available_serial_ports() or any(
        signal in error_text for signal in serial_error_signals
    ):
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
    carel_weekday = value.weekday() + 1
    write_values = [
        carel_weekday,
        value.hour,
        value.minute,
        value.day,
        value.month,
        encode_device_year(value.year, current_raw_year),
    ]
    for index, register_value in enumerate(write_values):
        addr = RTC_WRITE_SHADOW_ADDRS[index]
        wr = write_register(address=addr, value=register_value)
        if wr.isError():
            raise RuntimeError(f"Modbus write error (shadow addr {addr}={register_value}): {wr}")
    return None


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
    set_rtc_edit_latches(False)
    write_device_rtc_values(value, current_raw_year)
    pulse_rtc_edit_latches()
    set_rtc_edit_latches(False)

    logger.info(
        "Wrote RTC shadow block and pulsed D1..D6 latches for %s",
        format_device_datetime_local(value),
    )


def poll_registers_once() -> None:
    """Read live controller data blocks and update the shared cache."""
    try:
        with modbus_lock:
            modbus_connect_or_raise()
            temp_rr = read_holding_registers(address=TEMP_ADDR, count=1)

        if temp_rr.isError():
            raise RuntimeError(f"Modbus read error (temp): {temp_rr}")
        if not temp_rr.registers:
            raise RuntimeError("Modbus read returned no temperature registers")

        temp_raw = int(temp_rr.registers[0])
        temp_c = temp_raw / TEMP_SCALE

        with cache_lock:
            cache.temp_raw = temp_raw
            cache.temp_c = temp_c
            cache.last_update_utc = now_iso()
            cache.last_error = None
        clear_runtime_error()
    except Exception as exc:
        reset_modbus_client()
        error_message = normalize_modbus_error(exc)
        note_runtime_error(error_message)
        with cache_lock:
            cache.last_error = error_message

    if interactive_modbus_priority_active():
        return

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
    except Exception as exc:
        reset_modbus_client()
        error_message = normalize_modbus_error(exc)
        with cache_lock:
            cache.rtc_error = error_message

    if interactive_modbus_priority_active():
        return

    try:
        with modbus_lock:
            modbus_connect_or_raise()
            info1_rr = read_holding_registers(
                address=INFO_BLOCK1_START_ADDR,
                count=INFO_BLOCK1_COUNT,
            )
            info2_rr = read_holding_registers(
                address=INFO_BLOCK2_START_ADDR,
                count=INFO_BLOCK2_COUNT,
            )

        if info1_rr.isError():
            raise RuntimeError(f"Modbus read error (info block 1): {info1_rr}")
        if info2_rr.isError():
            raise RuntimeError(f"Modbus read error (info block 2): {info2_rr}")

        with cache_lock:
            cache.info_conductivity = int(info1_rr.registers[1])
            cache.info_cyl1_phase = int(info1_rr.registers[3])
            cache.info_cyl1_status = int(info1_rr.registers[4])
            cache.info_cyl2_phase = int(info1_rr.registers[5])
            cache.info_cyl2_status = int(info1_rr.registers[6])
            cache.info_cyl1_hours = int(info2_rr.registers[0])
            cache.info_cyl2_hours = int(info2_rr.registers[1])
            cache.info_voltage_type = int(info2_rr.registers[2])
            cache.info_error = None
    except Exception as exc:
        reset_modbus_client()
        error_message = normalize_modbus_error(exc)
        with cache_lock:
            cache.info_error = error_message

    if interactive_modbus_priority_active():
        return

    try:
        with modbus_lock:
            modbus_connect_or_raise()
            humidifier_status_rr = read_holding_registers(address=HUMIDIFIER_STATUS_ADDR, count=1)

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

    if interactive_modbus_priority_active():
        return

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
    except Exception as exc:
        reset_modbus_client()
        error_message = normalize_modbus_error(exc)
        with cache_lock:
            cache.alarms_has_active = None
            cache.alarms_active = []
            cache.alarms_skipped_active_count = 0
            cache.alarms_last_scan_utc = now_iso()
            cache.alarms_error = error_message

    if interactive_modbus_priority_active():
        return

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
        if interactive_modbus_priority_active():
            stop_evt.wait(0.1)
            continue
        poll_registers_once()
        stop_evt.wait(POLL_INTERVAL_S)


stop_event = threading.Event()
poller_thread: Optional[threading.Thread] = None


def start_background_poller() -> None:
    global poller_thread
    with runtime_state_lock:
        if poller_thread is not None and poller_thread.is_alive():
            return
        if stop_event.is_set():
            stop_event.clear()
        poller_thread = threading.Thread(
            target=poller_loop,
            args=(stop_event,),
            daemon=True,
            name="carel-poller",
        )
        poller_thread.start()
