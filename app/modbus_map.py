from __future__ import annotations


def qmm_to_modbus_addr(qmm_reg_1_based: int) -> int:
    """Convert a QModMaster-style 1-based register number to a 0-based Modbus address."""
    if qmm_reg_1_based < 1:
        raise ValueError("Register must be >= 1 (QModMaster style).")
    return qmm_reg_1_based - 1


def d_to_modbus_coil_addr(d_coil_0_based: int) -> int:
    """Return a D/coil address unchanged after validating it is already 0-based."""
    if d_coil_0_based < 0:
        raise ValueError("Coil address must be >= 0 (direct 0-based Modbus coil address).")
    return d_coil_0_based


# QModMaster-style 1-based register numbers.
TEMP_REG = 2
SETPOINT_REG = 20
MAX_PRODUCTION_REG = 15
PROP_BAND_REG = 21

RTC_READ_HOUR_REG = 154
RTC_READ_MINUTE_REG = 155
RTC_READ_DAY_REG = 156
RTC_READ_MONTH_REG = 157
RTC_READ_YEAR_REG = 158
RTC_READ_WEEKDAY_REG = 159

# Shadow register QMM numbers (1-based).
RTC_WRITE_WEEKDAY_REG = 160
RTC_WRITE_HOUR_REG = 161
RTC_WRITE_MINUTE_REG = 162
RTC_WRITE_DAY_REG = 163
RTC_WRITE_MONTH_REG = 164
RTC_WRITE_YEAR_REG = 165

# Info/status register blocks.
INFO_BLOCK1_START_REG = 136
INFO_BLOCK1_COUNT = 7
INFO_BLOCK2_START_REG = 165
INFO_BLOCK2_COUNT = 3

# Coil addresses are direct 0-based Modbus addresses.
RTC_COIL_HOUR = 1
RTC_COIL_MINUTE = 2
RTC_COIL_DAY = 3
RTC_COIL_MONTH = 4
RTC_COIL_YEAR = 5
RTC_COIL_WEEKDAY = 6

HUMIDIFIER_STATUS_REG = 136
HUMIDIFIER_REMOTE_ONOFF_COIL = 8
HUMIDIFIER_SUPERVISOR_ENABLE_COIL = 81
DRAIN_CYL1_COIL = 52
ALARM_RESET_COIL = 51

POLL_INTERVAL_S = 1.0

# Scaling.
TEMP_SCALE = 10.0
SETPOINT_SCALE = 10.0
MAX_PRODUCTION_SCALE = 1.0
PROP_BAND_SCALE = 10.0

# Writable field limits in engineering units.
SETPOINT_MIN_C = -20.0
SETPOINT_MAX_C = 100.0
MAX_PRODUCTION_MIN_PCT = 0.0
MAX_PRODUCTION_MAX_PCT = 1000.0
PROP_BAND_MIN_C = 0.0
PROP_BAND_MAX_C = 100.0


TEMP_ADDR = qmm_to_modbus_addr(TEMP_REG)
SETPOINT_ADDR = qmm_to_modbus_addr(SETPOINT_REG)
MAX_PRODUCTION_ADDR = qmm_to_modbus_addr(MAX_PRODUCTION_REG)
PROP_BAND_ADDR = qmm_to_modbus_addr(PROP_BAND_REG)
HUMIDIFIER_STATUS_ADDR = HUMIDIFIER_STATUS_REG

RTC_READ_ADDRS = {
    "hour": qmm_to_modbus_addr(RTC_READ_HOUR_REG),
    "minute": qmm_to_modbus_addr(RTC_READ_MINUTE_REG),
    "day": qmm_to_modbus_addr(RTC_READ_DAY_REG),
    "month": qmm_to_modbus_addr(RTC_READ_MONTH_REG),
    "year": qmm_to_modbus_addr(RTC_READ_YEAR_REG),
    "weekday": qmm_to_modbus_addr(RTC_READ_WEEKDAY_REG),
}
RTC_READ_START_ADDR = RTC_READ_ADDRS["hour"]
RTC_READ_COUNT = len(RTC_READ_ADDRS)

RTC_WRITE_ADDRS = {
    "weekday": qmm_to_modbus_addr(RTC_WRITE_WEEKDAY_REG),
    "hour": qmm_to_modbus_addr(RTC_WRITE_HOUR_REG),
    "minute": qmm_to_modbus_addr(RTC_WRITE_MINUTE_REG),
    "day": qmm_to_modbus_addr(RTC_WRITE_DAY_REG),
    "month": qmm_to_modbus_addr(RTC_WRITE_MONTH_REG),
    "year": qmm_to_modbus_addr(RTC_WRITE_YEAR_REG),
}
RTC_WRITE_SHADOW_ADDRS = tuple(RTC_WRITE_ADDRS.values())
RTC_WRITE_TO_READ_ADDR = {
    RTC_WRITE_ADDRS["weekday"]: RTC_READ_ADDRS["weekday"],
    RTC_WRITE_ADDRS["hour"]: RTC_READ_ADDRS["hour"],
    RTC_WRITE_ADDRS["minute"]: RTC_READ_ADDRS["minute"],
    RTC_WRITE_ADDRS["day"]: RTC_READ_ADDRS["day"],
    RTC_WRITE_ADDRS["month"]: RTC_READ_ADDRS["month"],
    RTC_WRITE_ADDRS["year"]: RTC_READ_ADDRS["year"],
}

RTC_LATCH_COILS = (
    RTC_COIL_HOUR,
    RTC_COIL_MINUTE,
    RTC_COIL_DAY,
    RTC_COIL_MONTH,
    RTC_COIL_YEAR,
    RTC_COIL_WEEKDAY,
)
RTC_COIL_START = RTC_LATCH_COILS[0]
RTC_COIL_COUNT = len(RTC_LATCH_COILS)
RTC_LATCH_PULSE_DELAY_S = 0.15

INFO_BLOCK1_START_ADDR = qmm_to_modbus_addr(INFO_BLOCK1_START_REG)
INFO_BLOCK2_START_ADDR = qmm_to_modbus_addr(INFO_BLOCK2_START_REG)
