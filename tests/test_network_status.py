from __future__ import annotations

from typing import Sequence

import network_status


def test_parse_iw_link_output_extracts_ssid_and_dbm():
    output = """
Connected to aa:bb:cc:dd:ee:ff (on wlan0)
    SSID: Plant WiFi
    freq: 2412
    signal: -58 dBm
    rx bitrate: 72.2 MBit/s
"""

    info = network_status.parse_iw_link_output(output)

    assert info.connected is True
    assert info.ssid == "Plant WiFi"
    assert info.signal_dbm == -58


def test_not_connected_iw_output_maps_to_disconnected_snapshot():
    def fake_command(args: Sequence[str], timeout_s: float) -> str:
        if args[0] == "nmcli" and "status" in args:
            return "wlan0:wifi:connected:Plant WiFi\n"
        if args[0] == "iw":
            return "Not connected.\n"
        raise AssertionError(f"unexpected command: {args}")

    snapshot = network_status.read_network_snapshot(fake_command)

    assert snapshot.connected is False
    assert snapshot.interface == "wlan0"
    assert snapshot.ssid is None
    assert snapshot.error is None


def test_parse_active_wifi_device_selects_connected_wifi():
    output = "\n".join(
        [
            "eth0:ethernet:connected:Wired",
            "wlan0:wifi:disconnected:--",
            r"wlan1:wifi:connected:Plant\:WiFi",
        ]
    )

    active = network_status.parse_active_wifi_device(output)

    assert active is not None
    assert active.interface == "wlan1"
    assert active.connection == "Plant:WiFi"


def test_classify_signal_quality_uses_dbm_ranges():
    assert network_status.classify_signal_quality(-30) == "High"
    assert network_status.classify_signal_quality(-50) == "High"
    assert network_status.classify_signal_quality(-51) == "Medium"
    assert network_status.classify_signal_quality(-60) == "Medium"
    assert network_status.classify_signal_quality(-61) == "Low"
    assert network_status.classify_signal_quality(-70) == "Low"
    assert network_status.classify_signal_quality(-71) == "Weak"
    assert network_status.classify_signal_quality(-80) == "Weak"
    assert network_status.classify_signal_quality(-81) == "Unusable"
    assert network_status.classify_signal_quality(None) is None


def test_read_network_snapshot_combines_nmcli_and_iw_details():
    def fake_command(args: Sequence[str], timeout_s: float) -> str:
        if args[0] == "nmcli" and "status" in args:
            return "wlan0:wifi:connected:Plant WiFi\n"
        if args[0] == "iw":
            return """
Connected to aa:bb:cc:dd:ee:ff (on wlan0)
    SSID: Plant WiFi
    signal: -58 dBm
"""
        if args[0] == "nmcli" and "wifi" in args:
            return "*:Plant WiFi:72\n"
        raise AssertionError(f"unexpected command: {args}")

    snapshot = network_status.read_network_snapshot(fake_command)

    assert snapshot.connected is True
    assert snapshot.interface == "wlan0"
    assert snapshot.ssid == "Plant WiFi"
    assert snapshot.signal_dbm == -58
    assert snapshot.signal_quality == "Medium"
    assert snapshot.signal_percent == 72
    assert snapshot.error is None


def test_command_failure_returns_error_snapshot():
    def fake_command(args: Sequence[str], timeout_s: float) -> str:
        raise network_status.CommandError("nmcli is not installed.")

    snapshot = network_status.read_network_snapshot(fake_command)

    assert snapshot.connected is False
    assert snapshot.error == "nmcli is not installed."
