from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence


COMMAND_TIMEOUT_S = 5.0
MONITOR_RESTART_BACKOFF_S = 5.0

logger = logging.getLogger("carel_supervisor")


class CommandError(RuntimeError):
    """Raised when a network status command cannot provide useful output."""


CommandRunner = Callable[[Sequence[str], float], str]


@dataclass(frozen=True)
class ActiveWifiDevice:
    interface: str
    connection: str | None = None


@dataclass(frozen=True)
class IwLinkInfo:
    connected: bool
    ssid: str | None = None
    signal_dbm: int | None = None


@dataclass(frozen=True)
class NmcliWifiInfo:
    ssid: str | None = None
    signal_percent: int | None = None


@dataclass(frozen=True)
class NetworkSnapshot:
    connected: bool
    ssid: str | None = None
    interface: str | None = None
    signal_dbm: int | None = None
    signal_percent: int | None = None
    updated_utc: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_snapshot = NetworkSnapshot(
    connected=False,
    updated_utc=None,
    error="Network status not initialized.",
)
_snapshot_lock = threading.Lock()
_monitor_thread: Optional[threading.Thread] = None
_monitor_stop_event = threading.Event()


def _store_snapshot(snapshot: NetworkSnapshot) -> None:
    global _snapshot
    with _snapshot_lock:
        _snapshot = snapshot


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_command(args: Sequence[str], timeout_s: float = COMMAND_TIMEOUT_S) -> str:
    try:
        result = subprocess.run(
            list(args),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"{args[0]} is not installed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"{args[0]} timed out.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise CommandError(f"{args[0]} failed: {detail}") from exc

    return result.stdout


def split_nmcli_terse(line: str) -> list[str]:
    """Split nmcli terse output, preserving escaped colons and backslashes."""
    parts: list[str] = []
    current: list[str] = []
    escaped = False

    for char in line.rstrip("\n"):
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == ":":
            parts.append("".join(current))
            current = []
            continue
        current.append(char)

    if escaped:
        current.append("\\")
    parts.append("".join(current))
    return parts


def parse_active_wifi_device(nmcli_device_status: str) -> ActiveWifiDevice | None:
    for line in nmcli_device_status.splitlines():
        if not line.strip():
            continue

        fields = split_nmcli_terse(line)
        if len(fields) < 3:
            continue

        device, device_type, state = fields[:3]
        connection = fields[3] if len(fields) >= 4 else None
        if device_type != "wifi":
            continue
        if not state.startswith("connected"):
            continue
        if connection in ("", "--"):
            connection = None
        return ActiveWifiDevice(interface=device, connection=connection)

    return None


def parse_iw_link_output(iw_link_output: str) -> IwLinkInfo:
    if "Not connected." in iw_link_output:
        return IwLinkInfo(connected=False)

    ssid: str | None = None
    signal_dbm: int | None = None
    for line in iw_link_output.splitlines():
        ssid_match = re.match(r"\s*SSID:\s*(.*)\s*$", line)
        if ssid_match:
            ssid = ssid_match.group(1)
            continue

        signal_match = re.match(r"\s*signal:\s*(-?\d+)\s*dBm\b", line)
        if signal_match:
            signal_dbm = int(signal_match.group(1))

    return IwLinkInfo(connected=True, ssid=ssid, signal_dbm=signal_dbm)


def parse_nmcli_active_wifi(nmcli_wifi_output: str) -> NmcliWifiInfo:
    for line in nmcli_wifi_output.splitlines():
        if not line.strip():
            continue

        fields = split_nmcli_terse(line)
        if len(fields) < 3:
            continue

        in_use, ssid, signal = fields[:3]
        if in_use != "*":
            continue

        signal_percent: int | None = None
        if signal:
            try:
                signal_percent = int(signal)
            except ValueError:
                signal_percent = None
        return NmcliWifiInfo(ssid=ssid or None, signal_percent=signal_percent)

    return NmcliWifiInfo()


def _optional_command(command_runner: CommandRunner, args: Sequence[str]) -> str | None:
    try:
        return command_runner(args, COMMAND_TIMEOUT_S)
    except Exception as exc:
        logger.debug("Optional network status command failed: %s", exc)
        return None


def _error_snapshot(error: object) -> NetworkSnapshot:
    return NetworkSnapshot(
        connected=False,
        updated_utc=utc_now_iso(),
        error=str(error) or "Network status unavailable.",
    )


def read_network_snapshot(
    command_runner: CommandRunner = _run_command,
) -> NetworkSnapshot:
    try:
        device_status = command_runner(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
            COMMAND_TIMEOUT_S,
        )
    except Exception as exc:
        return _error_snapshot(exc)

    active_wifi = parse_active_wifi_device(device_status)
    if active_wifi is None:
        return NetworkSnapshot(connected=False, updated_utc=utc_now_iso(), error=None)

    iw_info = IwLinkInfo(connected=True)
    iw_output = _optional_command(
        command_runner,
        ["iw", "dev", active_wifi.interface, "link"],
    )
    if iw_output is not None:
        iw_info = parse_iw_link_output(iw_output)
        if not iw_info.connected:
            return NetworkSnapshot(
                connected=False,
                interface=active_wifi.interface,
                updated_utc=utc_now_iso(),
                error=None,
            )

    wifi_info = NmcliWifiInfo()
    wifi_output = _optional_command(
        command_runner,
        [
            "nmcli",
            "-t",
            "-f",
            "IN-USE,SSID,SIGNAL",
            "device",
            "wifi",
            "list",
            "ifname",
            active_wifi.interface,
        ],
    )
    if wifi_output is not None:
        wifi_info = parse_nmcli_active_wifi(wifi_output)

    ssid = iw_info.ssid or wifi_info.ssid or active_wifi.connection
    return NetworkSnapshot(
        connected=True,
        ssid=ssid,
        interface=active_wifi.interface,
        signal_dbm=iw_info.signal_dbm,
        signal_percent=wifi_info.signal_percent,
        updated_utc=utc_now_iso(),
        error=None,
    )


def refresh_network_snapshot() -> NetworkSnapshot:
    snapshot = read_network_snapshot()
    _store_snapshot(snapshot)
    return snapshot


def get_network_snapshot() -> dict[str, object]:
    with _snapshot_lock:
        return _snapshot.to_dict()


def _monitor_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        proc: subprocess.Popen[str] | None = None
        try:
            refresh_network_snapshot()
            proc = subprocess.Popen(
                ["nmcli", "monitor"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            if proc.stdout is not None:
                for line in proc.stdout:
                    if stop_event.is_set():
                        break
                    if line.strip():
                        refresh_network_snapshot()

            return_code = proc.wait()
            if not stop_event.is_set():
                logger.warning("nmcli monitor exited with status %s", return_code)
        except FileNotFoundError:
            snapshot = _error_snapshot("nmcli is not installed.")
            _store_snapshot(snapshot)
            logger.warning("Network status monitor unavailable: nmcli is not installed.")
        except Exception as exc:
            snapshot = _error_snapshot(exc)
            _store_snapshot(snapshot)
            logger.warning("Network status monitor failed: %s", exc)
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()

        stop_event.wait(MONITOR_RESTART_BACKOFF_S)


def start_network_status_monitor() -> None:
    global _monitor_thread
    with _snapshot_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return

    refresh_network_snapshot()

    with _snapshot_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
        if _monitor_stop_event.is_set():
            _monitor_stop_event.clear()
        _monitor_thread = threading.Thread(
            target=_monitor_loop,
            args=(_monitor_stop_event,),
            daemon=True,
            name="network-status-monitor",
        )
        _monitor_thread.start()
