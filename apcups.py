#!/usr/bin/env python3
"""Low-level driver for APC Back-UPS units over USB HID.

Everything here was derived from the device's own report descriptor
(see report_descriptor.bin / parse_rd.py) and cross-checked against NUT's
drivers/apc-hid.c, so variable names follow NUT conventions. Tested against
an APC Back-UPS BX1500BI-BR; other Back-UPS models sharing the same HID
Power Device usage page are likely compatible but not verified here.

Scaling notes (unit exponents come from the descriptor):
  - Input.Voltage / Input.*VoltageTransfer: unit exponent 7 -> value as-is (volts)
  - Battery.Voltage / Battery.ConfigVoltage: unit exponent 5 -> value / 100 (volts)
  - RunTimeToEmpty / RemainingTimeLimit / DelayBefore*: seconds
"""
from __future__ import annotations

import struct
import threading
import time

VENDOR_ID = 0x051D
PRODUCT_ID = 0x0002

# --- feature report ids (from the parsed descriptor) ---
R_PS_REMAINING_CAPACITY = 12      # %
R_PS_DESIGN_CAPACITY = 13
R_PS_FULL_CHARGE_CAPACITY = 14
R_PS_RUNTIME_TO_EMPTY = 15        # seconds, 16 bit
R_PS_REMAINING_CAPACITY_LIMIT = 17   # %, RW
R_PS_AC_PRESENT = 19
R_PS_BELOW_CAPACITY_LIMIT = 20
R_PS_DELAY_BEFORE_SHUTDOWN = 21   # seconds, RW, -1 = disabled
R_PS_PRESENT_STATUS = 22          # bitfield, 2 bytes
R_PS_REMAINING_TIME_LIMIT = 23    # seconds, RW
R_PS_AUDIBLE_ALARM = 24           # 1=disabled 2=enabled 3=muted, RW

R_BAT_MFR_DATE = 32
R_BAT_TEST = 33                   # RW: write to start self-test; read = result
R_BAT_REMAINING_CAPACITY = 34
R_BAT_RUNTIME_TO_EMPTY = 35
R_BAT_REMAINING_TIME_LIMIT = 36
R_BAT_CONFIG_VOLTAGE = 37         # /100 V
R_BAT_VOLTAGE = 38                # /100 V

R_IN_CONFIG_VOLTAGE = 48          # nominal input volts
R_IN_VOLTAGE = 49                 # volts
R_IN_TRANSFER_LOW = 50            # volts, RW
R_IN_TRANSFER_HIGH = 51           # volts, RW
R_IN_SENSITIVITY = 53             # 0=low 1=medium 2=high, RW
R_IN_LINE_FAIL_CAUSE = 54         # reason of last transfer to battery

R_DELAY_BEFORE_REBOOT = 64        # APCDelayBeforeReboot, seconds, RW
R_OUT_PERCENT_LOAD = 80           # %
R_OUT_CONFIG_ACTIVE_POWER = 82    # watts (nominal)

R_UPS_AUDIBLE_ALARM = 120
R_PANEL_TEST = 121                # APCPanelTest, RW
R_UPS_PRESENT_STATUS = 122        # bitfield, 1 byte
R_UPS_MFR_DATE = 123
R_UPS_SERIAL = 125
R_UPS_MANUFACTURER = 124
R_FIRMWARE_REVISION = 126

# --- PowerSummary.PresentStatus bit layout (report 22) ---
PS_STATUS_BITS = [
    (0, "charging"),
    (1, "discharging"),
    (2, "ac_present"),
    (3, "battery_present"),
    (4, "shutdown_imminent"),
    (5, "remaining_time_limit_expired"),
    (6, "replace_battery"),
    (7, "overload"),
    (8, "voltage_not_regulated"),
]

# --- UPS.PresentStatus bit layout (report 122) ---
UPS_STATUS_BITS = [
    (0, "charging"),
    (1, "discharging"),
    (2, "ac_present"),
    (3, "battery_present"),
    (4, "replace_battery"),
    (5, "voltage_not_regulated"),
    (6, "overload"),
]

BEEPER_STATES = {1: "disabled", 2: "enabled", 3: "muted"}
# Writable states. Value 3 ("mute") is a momentary action that only takes
# effect while an alarm is actually sounding: writing it on an idle UPS is
# accepted and silently discarded, so it must not be offered as a state.
BEEPER_VALUES = {"disabled": 1, "enabled": 2}
BEEPER_MUTE = 3

SENSITIVITY_STATES = {0: "low", 1: "medium", 2: "high"}
SENSITIVITY_VALUES = {v: k for k, v in SENSITIVITY_STATES.items()}

# UPS.Battery.Test, per NUT's test_read_info / test_write_info
TEST_RESULT = {
    1: "done and passed",
    2: "done and warning",
    3: "done and error",
    4: "aborted",
    5: "in progress",
    6: "no test initiated",
}
TEST_START = 1      # write 1 to start a quick self-test
TEST_ABORT = 3

# Reason of the last transfer to battery (APCLineFailCause, from apcupsd)
LINE_FAIL_CAUSE = {
    0: "no transfers since turn on",
    1: "low line voltage",
    2: "high line voltage",
    3: "ripple",
    4: "notch, spike or blackout",
    5: "self test or discharge calibration",
    6: "forced by software",
    7: "input frequency out of range",
    8: "notch or blackout",
    9: "spike or blackout",
    10: "graceful shutdown by accessories",
    11: "test usage invoked",
    12: "front button initiated self test",
    13: "two week self test",
}


def apc_date(value: int) -> str:
    """APC stores dates as hex-as-decimal, e.g. 0x102202 = 2002/10/22."""
    if value == 0:
        return "not set"
    year = (value & 0xF) + 10 * ((value >> 4) & 0xF)
    month = ((value >> 16) & 0xF) + 10 * ((value >> 20) & 0xF)
    day = ((value >> 8) & 0xF) + 10 * ((value >> 12) & 0xF)
    year += 1900 if year >= 70 else 2000
    return f"{year:04d}-{month:02d}-{day:02d}"


def hid_date(value: int) -> str:
    """Standard HID battery date: bits 15-9 year since 1980, 8-5 month, 4-0 day."""
    if value == 0:
        return "not set"
    year = 1980 + ((value >> 9) & 0x7F)
    month = (value >> 5) & 0x0F
    day = value & 0x1F
    return f"{year:04d}-{month:02d}-{day:02d}"


class UPSError(RuntimeError):
    pass


class BackUPS:
    """Thread-safe wrapper around the UPS HID device.

    A single instance owns the handle; all access is serialised through a lock
    so the polling loop and command handlers cannot interleave HID transfers.
    """

    def __init__(self, vid: int = VENDOR_ID, pid: int = PRODUCT_ID):
        self.vid = vid
        self.pid = pid
        self._dev = None
        self._lock = threading.RLock()

    # -- connection -------------------------------------------------------
    def open(self) -> None:
        """Open the HID device, retrying while the kernel still holds it.

        After a hard process restart the previous process's handle can
        outlive it by a few seconds; opening immediately then fails with a
        bare "open failed" and every later read returns "read error".
        Retrying beats crashing and being respawned into the same race.
        """
        import hid
        with self._lock:
            if self._dev is not None:
                return
            last = None
            for attempt in range(10):
                dev = hid.device()
                try:
                    dev.open(self.vid, self.pid)
                except (OSError, IOError) as exc:
                    last = exc
                    time.sleep(1.0)
                    continue
                dev.set_nonblocking(1)
                self._dev = dev
                return
            raise OSError(f"could not open UPS after 10 attempts: {last}")

    def close(self) -> None:
        with self._lock:
            if self._dev is not None:
                try:
                    self._dev.close()
                finally:
                    self._dev = None

    def reopen(self) -> None:
        self.close()
        # Give the kernel time to release the handle before grabbing it again;
        # open() retries on top of this.
        time.sleep(1.0)
        self.open()

    @property
    def connected(self) -> bool:
        return self._dev is not None

    # -- raw report access ------------------------------------------------
    def _get(self, report_id: int, length: int) -> bytes:
        with self._lock:
            if self._dev is None:
                raise UPSError("device not open")
            data = self._dev.get_feature_report(report_id, length + 1)
        if not data:
            raise UPSError(f"empty feature report {report_id}")
        if data[0] != report_id:
            raise UPSError(f"report id mismatch: asked {report_id}, got {data[0]}")
        return bytes(data[1:])

    def _set(self, report_id: int, payload: bytes) -> None:
        with self._lock:
            if self._dev is None:
                raise UPSError("device not open")
            written = self._dev.send_feature_report(bytes([report_id]) + payload)
        if written < 0:
            raise UPSError(f"write to report {report_id} failed")

    def read_u8(self, report_id: int) -> int:
        return self._get(report_id, 1)[0]

    def read_u16(self, report_id: int) -> int:
        return struct.unpack("<H", self._get(report_id, 2))[0]

    def read_i16(self, report_id: int) -> int:
        return struct.unpack("<h", self._get(report_id, 2))[0]

    def write_u8(self, report_id: int, value: int) -> None:
        self._set(report_id, struct.pack("<B", value & 0xFF))

    def write_u16(self, report_id: int, value: int) -> None:
        self._set(report_id, struct.pack("<h", value))

    def read_string(self, report_id: int, length: int = 16) -> str:
        raw = self._get(report_id, length)
        return raw.split(b"\x00")[0].decode("latin1").strip()

    # -- decoded reads ----------------------------------------------------
    def status_flags(self) -> dict:
        ps = struct.unpack("<H", self._get(R_PS_PRESENT_STATUS, 2))[0]
        flags = {name: bool(ps >> bit & 1) for bit, name in PS_STATUS_BITS}
        try:
            ups = self.read_u8(R_UPS_PRESENT_STATUS)
            for bit, name in UPS_STATUS_BITS:
                # UPS.PresentStatus corroborates PowerSummary; OR them so a flag
                # raised on either collection is not lost.
                flags[name] = flags.get(name, False) or bool(ups >> bit & 1)
        except UPSError:
            pass
        return flags

    def nut_status(self, flags: dict) -> list[str]:
        """Render NUT-style ups.status tokens from the decoded flags."""
        out = []
        if flags.get("ac_present") and not flags.get("discharging"):
            out.append("OL")
        else:
            out.append("OB")
        if flags.get("charging"):
            out.append("CHRG")
        if flags.get("discharging"):
            out.append("DISCHRG")
        if flags.get("shutdown_imminent") or flags.get("remaining_time_limit_expired"):
            out.append("LB")
        if flags.get("replace_battery"):
            out.append("RB")
        if flags.get("overload"):
            out.append("OVER")
        if flags.get("voltage_not_regulated"):
            out.append("TRIM")
        if not flags.get("battery_present", True):
            out.append("NOBATT")
        return out

    def poll(self) -> dict:
        """Read the full live state. Raises UPSError if the device went away."""
        flags = self.status_flags()
        d: dict = {
            "ts": time.time(),
            "battery.charge": self.read_u8(R_PS_REMAINING_CAPACITY),
            "battery.runtime": self.read_u16(R_PS_RUNTIME_TO_EMPTY),
            "battery.voltage": self.read_u16(R_BAT_VOLTAGE) / 100.0,
            "battery.voltage.nominal": self.read_u16(R_BAT_CONFIG_VOLTAGE) / 100.0,
            "battery.charge.low": self.read_u8(R_PS_REMAINING_CAPACITY_LIMIT),
            "battery.runtime.low": self.read_u16(R_PS_REMAINING_TIME_LIMIT),
            "input.voltage": float(self.read_u16(R_IN_VOLTAGE)),
            "input.voltage.nominal": float(self.read_u8(R_IN_CONFIG_VOLTAGE)),
            "input.transfer.low": float(self.read_u16(R_IN_TRANSFER_LOW)),
            "input.transfer.high": float(self.read_u16(R_IN_TRANSFER_HIGH)),
            "ups.load": self.read_u8(R_OUT_PERCENT_LOAD),
            "ups.realpower.nominal": self.read_u16(R_OUT_CONFIG_ACTIVE_POWER),
            "ups.status": " ".join(self.nut_status(flags)),
            "ups.flags": flags,
        }
        d["ups.realpower"] = round(d["ups.realpower.nominal"] * d["ups.load"] / 100.0, 1)

        # Optional / slow-moving fields: never let one failure kill the poll.
        for key, fn in (
            ("ups.beeper.status", lambda: BEEPER_STATES.get(self.read_u8(R_PS_AUDIBLE_ALARM), "unknown")),
            ("input.sensitivity", lambda: SENSITIVITY_STATES.get(self.read_u8(R_IN_SENSITIVITY), "unknown")),
            ("input.transfer.reason", lambda: LINE_FAIL_CAUSE.get(self.read_u8(R_IN_LINE_FAIL_CAUSE), "unknown")),
            ("ups.test.result", lambda: TEST_RESULT.get(self.read_u8(R_BAT_TEST), "unknown")),
            ("ups.delay.shutdown", lambda: self.read_i16(R_PS_DELAY_BEFORE_SHUTDOWN)),
            ("ups.delay.reboot", lambda: self.read_u8(R_DELAY_BEFORE_REBOOT)),
            ("battery.mfr.date", lambda: hid_date(self.read_u16(R_BAT_MFR_DATE))),
        ):
            try:
                d[key] = fn()
            except UPSError:
                d[key] = None
        return d

    def device_info(self) -> dict:
        info = {
            "device.mfr": "American Power Conversion",
            "device.model": None,
            "device.serial": None,
            "device.type": "ups",
        }
        with self._lock:
            if self._dev is not None:
                try:
                    info["device.model"] = self._dev.get_product_string()
                    info["device.serial"] = self._dev.get_serial_number_string()
                    info["device.mfr"] = self._dev.get_manufacturer_string()
                except Exception:
                    pass
        try:
            info["ups.firmware"] = str(self.read_u8(R_FIRMWARE_REVISION))
        except UPSError:
            info["ups.firmware"] = None
        try:
            info["ups.mfr.date"] = hid_date(self.read_u16(R_UPS_MFR_DATE))
        except UPSError:
            info["ups.mfr.date"] = None
        return info

    # -- commands ---------------------------------------------------------
    def set_beeper(self, state: str) -> None:
        if state not in BEEPER_VALUES:
            raise ValueError(f"beeper state must be one of {sorted(BEEPER_VALUES)}")
        self.write_u8(R_PS_AUDIBLE_ALARM, BEEPER_VALUES[state])

    def mute_beeper(self) -> None:
        """Silence an alarm that is sounding right now.

        Only meaningful during an active alarm; on an idle UPS the write is
        accepted and discarded, which is why this is a separate action instead
        of a beeper state.
        """
        self.write_u8(R_PS_AUDIBLE_ALARM, BEEPER_MUTE)

    def start_self_test(self) -> None:
        self.write_u8(R_BAT_TEST, TEST_START)

    def abort_self_test(self) -> None:
        self.write_u8(R_BAT_TEST, TEST_ABORT)

    def panel_test(self) -> None:
        self.write_u8(R_PANEL_TEST, 1)

    def set_sensitivity(self, level: str) -> None:
        if level not in SENSITIVITY_VALUES:
            raise ValueError(f"sensitivity must be one of {sorted(SENSITIVITY_VALUES)}")
        self.write_u8(R_IN_SENSITIVITY, SENSITIVITY_VALUES[level])

    def set_transfer_low(self, volts: int) -> None:
        self.write_u16(R_IN_TRANSFER_LOW, int(volts))

    def set_transfer_high(self, volts: int) -> None:
        self.write_u16(R_IN_TRANSFER_HIGH, int(volts))

    def set_battery_charge_low(self, percent: int) -> None:
        if not 1 <= percent <= 100:
            raise ValueError("battery.charge.low must be 1..100")
        self.write_u8(R_PS_REMAINING_CAPACITY_LIMIT, percent)

    def set_battery_runtime_low(self, seconds: int) -> None:
        self.write_u16(R_PS_REMAINING_TIME_LIMIT, int(seconds))

    def set_delay_shutdown(self, seconds: int) -> None:
        """Seconds until the UPS cuts its own output. -1 disables the timer."""
        self.write_u16(R_PS_DELAY_BEFORE_SHUTDOWN, int(seconds))

    def set_delay_reboot(self, seconds: int) -> None:
        self.write_u8(R_DELAY_BEFORE_REBOOT, int(seconds))
