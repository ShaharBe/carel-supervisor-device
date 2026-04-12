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
from typing import Any, Callable, Dict, Optional, cast

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
from resource_cache import resource_key


COM_PORT = "/dev/ttyACM0"
BAUDRATE = 19200
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


@dataclass(frozen=True)
class PollBlockResult:
    data: Any = None
    error: Exception | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


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
    resource_values: dict[str, dict[str, Any]] = field(default_factory=dict)


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


TEMP_RESOURCE_KEY = resource_key("A", TEMP_ADDR)
SETPOINT_RESOURCE_KEY = resource_key("A", SETPOINT_ADDR)
MAX_PRODUCTION_RESOURCE_KEY = resource_key("A", MAX_PRODUCTION_ADDR)
PROP_BAND_RESOURCE_KEY = resource_key("A", PROP_BAND_ADDR)
HUMIDIFIER_NETWORK_RESOURCE_KEY = resource_key("D", HUMIDIFIER_REMOTE_ONOFF_COIL)
DRAIN_CYL1_RESOURCE_KEY = resource_key("D", DRAIN_CYL1_COIL)
INFO_HUMIDIFIER_STATUS_RESOURCE_KEY = resource_key("I", HUMIDIFIER_STATUS_ADDR)
INFO_CONDUCTIVITY_RESOURCE_KEY = resource_key("I", 137)
INFO_CYL1_PHASE_RESOURCE_KEY = resource_key("I", 139)
INFO_CYL1_STATUS_RESOURCE_KEY = resource_key("I", 140)
INFO_CYL2_PHASE_RESOURCE_KEY = resource_key("I", 141)
INFO_CYL2_STATUS_RESOURCE_KEY = resource_key("I", 142)
INFO_CYL1_HOURS_RESOURCE_KEY = resource_key("I", INFO_BLOCK2_START_ADDR)
INFO_CYL2_HOURS_RESOURCE_KEY = resource_key("I", INFO_BLOCK2_START_ADDR + 1)
INFO_VOLTAGE_TYPE_RESOURCE_KEY = resource_key("I", INFO_BLOCK2_START_ADDR + 2)


def _sync_legacy_cache_from_resource_locked(key: str, *, raw: Any, value: Any) -> None:
    """Mirror canonical resource updates into the older dashboard fields."""
    if key == TEMP_RESOURCE_KEY:
        cache.temp_raw = int(raw)
        cache.temp_c = float(value)
    elif key == SETPOINT_RESOURCE_KEY:
        cache.last_setpoint_raw = int(raw)
        cache.last_setpoint_c = float(value)
    elif key == MAX_PRODUCTION_RESOURCE_KEY:
        cache.max_production_raw = int(raw)
        cache.max_production_pct = float(value)
    elif key == PROP_BAND_RESOURCE_KEY:
        cache.prop_band_raw = int(raw)
        cache.prop_band_c = float(value)
    elif key == HUMIDIFIER_NETWORK_RESOURCE_KEY:
        cache.humidifier_network_enabled = bool(value)
    elif key == DRAIN_CYL1_RESOURCE_KEY:
        cache.cyl1_drain_on = bool(value)
    elif key == INFO_HUMIDIFIER_STATUS_RESOURCE_KEY:
        cache.info_humidifier_status = int(value)
    elif key == INFO_CONDUCTIVITY_RESOURCE_KEY:
        cache.info_conductivity = int(value)
    elif key == INFO_CYL1_PHASE_RESOURCE_KEY:
        cache.info_cyl1_phase = int(value)
    elif key == INFO_CYL1_STATUS_RESOURCE_KEY:
        cache.info_cyl1_status = int(value)
    elif key == INFO_CYL2_PHASE_RESOURCE_KEY:
        cache.info_cyl2_phase = int(value)
    elif key == INFO_CYL2_STATUS_RESOURCE_KEY:
        cache.info_cyl2_status = int(value)
    elif key == INFO_CYL1_HOURS_RESOURCE_KEY:
        cache.info_cyl1_hours = int(value)
    elif key == INFO_CYL2_HOURS_RESOURCE_KEY:
        cache.info_cyl2_hours = int(value)
    elif key == INFO_VOLTAGE_TYPE_RESOURCE_KEY:
        cache.info_voltage_type = int(value)


def _cache_resource_value_locked(key: str, *, raw: Any, value: Any, source: str) -> None:
    cache.resource_values[key] = {
        "raw": raw,
        "value": value,
        "source": source,
        "updated_utc": now_iso(),
        "error": None,
    }
    _sync_legacy_cache_from_resource_locked(key, raw=raw, value=value)


def cache_resource_value(key: str, *, raw: Any, value: Any, source: str) -> None:
    with cache_lock:
        _cache_resource_value_locked(key, raw=raw, value=value, source=source)


def _cache_resource_error_locked(key: str, error_message: str) -> None:
    previous = cache.resource_values.get(key, {})
    cache.resource_values[key] = {
        "raw": previous.get("raw"),
        "value": previous.get("value"),
        "source": previous.get("source"),
        "updated_utc": now_iso(),
        "error": error_message,
    }


def cache_resource_error(key: str, error_message: str) -> None:
    with cache_lock:
        _cache_resource_error_locked(key, error_message)


def get_cached_resource_value(key: str) -> dict[str, Any] | None:
    with cache_lock:
        cached_value = cache.resource_values.get(key)
        if cached_value is None:
            return None
        return dict(cached_value)


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


def _capture_poll_block(reader: Callable[[], Any]) -> PollBlockResult:
    try:
        return PollBlockResult(data=reader())
    except Exception as exc:
        return PollBlockResult(error=exc)


def read_temp_block() -> PollBlockResult:
    def _read() -> dict[str, Any]:
        with modbus_lock:
            modbus_connect_or_raise()
            temp_rr = read_holding_registers(address=TEMP_ADDR, count=1)

        if temp_rr.isError():
            raise RuntimeError(f"Modbus read error (temp): {temp_rr}")
        if not temp_rr.registers:
            raise RuntimeError("Modbus read returned no temperature registers")

        temp_raw = int(temp_rr.registers[0])
        return {
            "temp_raw": temp_raw,
            "temp_c": temp_raw / TEMP_SCALE,
        }

    return _capture_poll_block(_read)


def read_rtc_block() -> PollBlockResult:
    def _read() -> dict[str, Any]:
        with modbus_lock:
            modbus_connect_or_raise()
            device_time, raw_year, weekday = read_device_rtc_values()

        return {
            "device_time": device_time,
            "raw_year": raw_year,
            "weekday": weekday,
        }

    return _capture_poll_block(_read)


def read_info_block() -> PollBlockResult:
    def _read() -> dict[str, Any]:
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

        return {
            "info_conductivity": int(info1_rr.registers[1]),
            "info_cyl1_phase": int(info1_rr.registers[3]),
            "info_cyl1_status": int(info1_rr.registers[4]),
            "info_cyl2_phase": int(info1_rr.registers[5]),
            "info_cyl2_status": int(info1_rr.registers[6]),
            "info_cyl1_hours": int(info2_rr.registers[0]),
            "info_cyl2_hours": int(info2_rr.registers[1]),
            "info_voltage_type": int(info2_rr.registers[2]),
        }

    return _capture_poll_block(_read)


def read_humidifier_status_block() -> PollBlockResult:
    def _read() -> dict[str, Any]:
        with modbus_lock:
            modbus_connect_or_raise()
            humidifier_status_rr = read_holding_registers(address=HUMIDIFIER_STATUS_ADDR, count=1)

        if humidifier_status_rr.isError():
            raise RuntimeError(f"Modbus read error (humidifier status): {humidifier_status_rr}")
        if not humidifier_status_rr.registers:
            raise RuntimeError("Modbus read returned no humidifier status registers")

        return {
            "info_humidifier_status": int(humidifier_status_rr.registers[0]),
        }

    return _capture_poll_block(_read)


def read_alarms_block() -> PollBlockResult:
    def _read() -> dict[str, Any]:
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

        return {
            "alarms_has_active": alarms_have_active,
            "alarms_active": active_alarms,
            "alarms_skipped_active_count": skipped_alarm_count,
        }

    return _capture_poll_block(_read)


def read_coils_block() -> PollBlockResult:
    def _read() -> dict[str, Any]:
        with modbus_lock:
            modbus_connect_or_raise()
            humidifier_rr = read_coils(address=HUMIDIFIER_REMOTE_ONOFF_COIL, count=1)
            drain_rr = read_coils(address=DRAIN_CYL1_COIL, count=1)

        if humidifier_rr.isError():
            raise RuntimeError(f"Modbus read error (humidifier remote on/off coil): {humidifier_rr}")
        if drain_rr.isError():
            raise RuntimeError(f"Modbus read error (drain coil): {drain_rr}")

        return {
            "humidifier_network_enabled": bool(humidifier_rr.bits[0]) if humidifier_rr.bits else None,
            "cyl1_drain_on": bool(drain_rr.bits[0]) if drain_rr.bits else None,
        }

    return _capture_poll_block(_read)


def _apply_temp_block(data: dict[str, Any]) -> None:
    with cache_lock:
        cache.temp_raw = data["temp_raw"]
        cache.temp_c = data["temp_c"]
        cache.last_update_utc = now_iso()
        cache.last_error = None
        _cache_resource_value_locked(
            TEMP_RESOURCE_KEY,
            raw=data["temp_raw"],
            value=data["temp_c"],
            source="poll",
        )


def _apply_temp_block_error(error_message: str) -> None:
    with cache_lock:
        cache.last_error = error_message


def _apply_rtc_block(data: dict[str, Any]) -> None:
    device_time = cast(datetime, data["device_time"])
    with cache_lock:
        cache.device_time_iso_local = format_device_datetime_local(device_time)
        cache.device_time_display = device_time.strftime("%Y-%m-%d %H:%M")
        cache.device_time_weekday = cast(int, data["weekday"])
        cache.device_time_raw_year = cast(int, data["raw_year"])
        cache.last_rtc_update_utc = now_iso()
        cache.rtc_error = None


def _apply_rtc_block_error(error_message: str) -> None:
    with cache_lock:
        cache.rtc_error = error_message


def _apply_info_block(data: dict[str, Any]) -> None:
    with cache_lock:
        cache.info_conductivity = data["info_conductivity"]
        cache.info_cyl1_phase = data["info_cyl1_phase"]
        cache.info_cyl1_status = data["info_cyl1_status"]
        cache.info_cyl2_phase = data["info_cyl2_phase"]
        cache.info_cyl2_status = data["info_cyl2_status"]
        cache.info_cyl1_hours = data["info_cyl1_hours"]
        cache.info_cyl2_hours = data["info_cyl2_hours"]
        cache.info_voltage_type = data["info_voltage_type"]
        cache.info_error = None
        _cache_resource_value_locked(
            INFO_CONDUCTIVITY_RESOURCE_KEY,
            raw=data["info_conductivity"],
            value=data["info_conductivity"],
            source="poll",
        )
        _cache_resource_value_locked(
            INFO_CYL1_PHASE_RESOURCE_KEY,
            raw=data["info_cyl1_phase"],
            value=data["info_cyl1_phase"],
            source="poll",
        )
        _cache_resource_value_locked(
            INFO_CYL1_STATUS_RESOURCE_KEY,
            raw=data["info_cyl1_status"],
            value=data["info_cyl1_status"],
            source="poll",
        )
        _cache_resource_value_locked(
            INFO_CYL2_PHASE_RESOURCE_KEY,
            raw=data["info_cyl2_phase"],
            value=data["info_cyl2_phase"],
            source="poll",
        )
        _cache_resource_value_locked(
            INFO_CYL2_STATUS_RESOURCE_KEY,
            raw=data["info_cyl2_status"],
            value=data["info_cyl2_status"],
            source="poll",
        )
        _cache_resource_value_locked(
            INFO_CYL1_HOURS_RESOURCE_KEY,
            raw=data["info_cyl1_hours"],
            value=data["info_cyl1_hours"],
            source="poll",
        )
        _cache_resource_value_locked(
            INFO_CYL2_HOURS_RESOURCE_KEY,
            raw=data["info_cyl2_hours"],
            value=data["info_cyl2_hours"],
            source="poll",
        )
        _cache_resource_value_locked(
            INFO_VOLTAGE_TYPE_RESOURCE_KEY,
            raw=data["info_voltage_type"],
            value=data["info_voltage_type"],
            source="poll",
        )


def _apply_info_block_error(error_message: str) -> None:
    with cache_lock:
        cache.info_error = error_message
        for key in (
            INFO_CONDUCTIVITY_RESOURCE_KEY,
            INFO_CYL1_PHASE_RESOURCE_KEY,
            INFO_CYL1_STATUS_RESOURCE_KEY,
            INFO_CYL2_PHASE_RESOURCE_KEY,
            INFO_CYL2_STATUS_RESOURCE_KEY,
            INFO_CYL1_HOURS_RESOURCE_KEY,
            INFO_CYL2_HOURS_RESOURCE_KEY,
            INFO_VOLTAGE_TYPE_RESOURCE_KEY,
        ):
            _cache_resource_error_locked(key, error_message)


def _apply_humidifier_status_block(data: dict[str, Any]) -> None:
    with cache_lock:
        cache.info_humidifier_status = data["info_humidifier_status"]
        _cache_resource_value_locked(
            INFO_HUMIDIFIER_STATUS_RESOURCE_KEY,
            raw=data["info_humidifier_status"],
            value=data["info_humidifier_status"],
            source="poll",
        )


def _apply_humidifier_status_block_error(error_message: str) -> None:
    with cache_lock:
        cache.info_humidifier_status = None
        _cache_resource_error_locked(INFO_HUMIDIFIER_STATUS_RESOURCE_KEY, error_message)


def _apply_alarms_block(data: dict[str, Any]) -> None:
    with cache_lock:
        cache.alarms_has_active = data["alarms_has_active"]
        cache.alarms_active = data["alarms_active"]
        cache.alarms_skipped_active_count = data["alarms_skipped_active_count"]
        cache.alarms_last_scan_utc = now_iso()
        cache.alarms_error = None


def _apply_alarms_block_error(error_message: str) -> None:
    with cache_lock:
        cache.alarms_has_active = None
        cache.alarms_active = []
        cache.alarms_skipped_active_count = 0
        cache.alarms_last_scan_utc = now_iso()
        cache.alarms_error = error_message


def _apply_coils_block(data: dict[str, Any]) -> None:
    with cache_lock:
        cache.humidifier_network_enabled = data["humidifier_network_enabled"]
        cache.cyl1_drain_on = data["cyl1_drain_on"]
        if data["humidifier_network_enabled"] is not None:
            _cache_resource_value_locked(
                HUMIDIFIER_NETWORK_RESOURCE_KEY,
                raw=data["humidifier_network_enabled"],
                value=data["humidifier_network_enabled"],
                source="poll",
            )
        if data["cyl1_drain_on"] is not None:
            _cache_resource_value_locked(
                DRAIN_CYL1_RESOURCE_KEY,
                raw=data["cyl1_drain_on"],
                value=data["cyl1_drain_on"],
                source="poll",
            )


def _apply_coils_block_error(error_message: str) -> None:
    with cache_lock:
        cache.humidifier_network_enabled = None
        cache.cyl1_drain_on = None
        _cache_resource_error_locked(HUMIDIFIER_NETWORK_RESOURCE_KEY, error_message)
        _cache_resource_error_locked(DRAIN_CYL1_RESOURCE_KEY, error_message)


def _run_poll_block(
    reader: Callable[[], PollBlockResult],
    on_success: Callable[[dict[str, Any]], None],
    on_error: Callable[[str], None],
    *,
    note_error: bool = False,
    clear_runtime_on_success: bool = False,
) -> None:
    result = reader()
    if result.ok:
        on_success(cast(dict[str, Any], result.data))
        if clear_runtime_on_success:
            clear_runtime_error()
        return

    reset_modbus_client()
    error_message = normalize_modbus_error(cast(Exception, result.error))
    if note_error:
        note_runtime_error(error_message)
    on_error(error_message)


def poll_registers_once() -> None:
    """Read live controller data blocks and update the shared cache."""
    _run_poll_block(
        read_temp_block,
        _apply_temp_block,
        _apply_temp_block_error,
        note_error=True,
        clear_runtime_on_success=True,
    )

    if interactive_modbus_priority_active():
        return

    _run_poll_block(
        read_rtc_block,
        _apply_rtc_block,
        _apply_rtc_block_error,
    )

    if interactive_modbus_priority_active():
        return

    _run_poll_block(
        read_info_block,
        _apply_info_block,
        _apply_info_block_error,
    )

    if interactive_modbus_priority_active():
        return

    _run_poll_block(
        read_humidifier_status_block,
        _apply_humidifier_status_block,
        _apply_humidifier_status_block_error,
    )

    if interactive_modbus_priority_active():
        return

    _run_poll_block(
        read_alarms_block,
        _apply_alarms_block,
        _apply_alarms_block_error,
    )

    if interactive_modbus_priority_active():
        return

    _run_poll_block(
        read_coils_block,
        _apply_coils_block,
        _apply_coils_block_error,
    )


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
