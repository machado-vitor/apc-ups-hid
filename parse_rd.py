#!/usr/bin/env python3
"""Parse a USB HID report descriptor and print every field with its report id,
report type, bit offset/size and full usage path. Used to map the APC UPS."""
import sys

PAGES = {
    0x84: "PowerDevice",
    0x85: "BatterySystem",
}

PD_USAGE = {
    0x01: "iName", 0x02: "PresentStatus", 0x03: "ChangedStatus", 0x04: "UPS",
    0x05: "PowerSupply", 0x10: "BatterySystem", 0x11: "BatterySystemID",
    0x12: "Battery", 0x13: "BatteryID", 0x14: "Charger", 0x15: "ChargerID",
    0x16: "PowerConverter", 0x17: "PowerConverterID", 0x18: "OutletSystem",
    0x19: "OutletSystemID", 0x1A: "Input", 0x1B: "InputID", 0x1C: "Output",
    0x1D: "OutputID", 0x1E: "Flow", 0x1F: "FlowID", 0x20: "Outlet",
    0x21: "OutletID", 0x22: "Gang", 0x23: "GangID", 0x24: "PowerSummary",
    0x25: "PowerSummaryID", 0x30: "Voltage", 0x31: "Current", 0x32: "Frequency",
    0x33: "ApparentPower", 0x34: "ActivePower", 0x35: "PercentLoad",
    0x36: "Temperature", 0x37: "Humidity", 0x38: "BadCount",
    0x40: "ConfigVoltage", 0x41: "ConfigCurrent", 0x42: "ConfigFrequency",
    0x43: "ConfigApparentPower", 0x44: "ConfigActivePower", 0x45: "ConfigPercentLoad",
    0x46: "ConfigTemperature", 0x47: "ConfigHumidity", 0x48: "NominalVoltage",
    0x50: "SwitchOnControl", 0x51: "SwitchOffControl", 0x52: "ToggleControl",
    0x53: "LowVoltageTransfer", 0x54: "HighVoltageTransfer",
    0x55: "DelayBeforeReboot", 0x56: "DelayBeforeStartup", 0x57: "DelayBeforeShutdown",
    0x58: "Test", 0x59: "ModuleReset", 0x5A: "AudibleAlarmControl",
    0x60: "Present", 0x61: "Good", 0x62: "InternalFailure", 0x63: "VoltageOutOfRange",
    0x64: "FrequencyOutOfRange", 0x65: "Overload", 0x66: "OverCharged",
    0x67: "OverTemperature", 0x68: "ShutdownRequested", 0x69: "ShutdownImminent",
    0x6A: "ShutdownDelayed", 0x6B: "Switch On/Off", 0x6C: "Switchable",
    0x6D: "Used", 0x6E: "Boost", 0x6F: "Buck", 0x70: "Initialized",
    0x71: "Tested", 0x72: "AwaitingPower", 0x73: "CommunicationLost",
    0xFD: "iManufacturer", 0xFE: "iProduct", 0xFF: "iSerialNumber",
}

BS_USAGE = {
    0x01: "SMBBatteryMode", 0x02: "SMBBatteryStatus", 0x03: "SMBAlarmWarning",
    0x04: "SMBChargerMode", 0x05: "SMBChargerStatus", 0x06: "SMBChargerSpecInfo",
    0x07: "SMBSelectorState", 0x08: "SMBSelectorPresets", 0x09: "SMBSelectorInfo",
    0x10: "OptionalMfgFunction1", 0x11: "OptionalMfgFunction2",
    0x12: "OptionalMfgFunction3", 0x13: "OptionalMfgFunction4",
    0x14: "OptionalMfgFunction5", 0x15: "ConnectionToSMBus", 0x16: "OutputConnection",
    0x17: "ChargerConnection", 0x18: "BatteryInsertion", 0x19: "Usenext",
    0x1A: "OKToUse", 0x1B: "BatterySupported", 0x1C: "SelectorRevision",
    0x1D: "ChargingIndicator", 0x28: "ManufacturerAccess", 0x29: "RemainingCapacityLimit",
    0x2A: "RemainingTimeLimit", 0x2B: "AtRate", 0x2C: "CapacityMode",
    0x2D: "BroadcastToCharger", 0x2E: "PrimaryBattery", 0x2F: "ChargeController",
    0x40: "TerminateCharge", 0x41: "TerminateDischarge", 0x42: "BelowRemainingCapacityLimit",
    0x43: "RemainingTimeLimitExpired", 0x44: "Charging", 0x45: "Discharging",
    0x46: "FullyCharged", 0x47: "FullyDischarged", 0x48: "ConditioningFlag",
    0x49: "AtRateOK", 0x4A: "SMBErrorCode", 0x4B: "NeedReplacement",
    0x60: "AtRateTimeToFull", 0x61: "AtRateTimeToEmpty", 0x62: "AverageCurrent",
    0x63: "MaxError", 0x64: "RelativeStateOfCharge", 0x65: "AbsoluteStateOfCharge",
    0x66: "RemainingCapacity", 0x67: "FullChargeCapacity", 0x68: "RunTimeToEmpty",
    0x69: "AverageTimeToEmpty", 0x6A: "AverageTimeToFull", 0x6B: "CycleCount",
    0x80: "BattPackModelLevel", 0x81: "InternalChargeController",
    0x82: "PrimaryBatterySupport", 0x83: "DesignCapacity", 0x84: "SpecificationInfo",
    0x85: "ManufacturerDate", 0x86: "SerialNumber", 0x87: "iManufacturerName",
    0x88: "iDeviceName", 0x89: "iDeviceChemistry", 0x8A: "ManufacturerData",
    0x8B: "Rechargeable", 0x8C: "WarningCapacityLimit", 0x8D: "CapacityGranularity1",
    0x8E: "CapacityGranularity2", 0x8F: "iOEMInformation", 0xC0: "InhibitCharge",
    0xC1: "EnablePolling", 0xC2: "ResetToZero", 0xD0: "ACPresent",
    0xD1: "BatteryPresent", 0xD2: "PowerFail", 0xD3: "AlarmInhibited",
    0xD4: "ThermistorUnderRange", 0xD5: "ThermistorHot", 0xD6: "ThermistorCold",
    0xD7: "ThermistorOverRange", 0xD8: "VoltageOutOfRange", 0xD9: "CurrentOutOfRange",
    0xDA: "CurrentNotRegulated", 0xDB: "VoltageNotRegulated", 0xDC: "MasterMode",
    0xF0: "ChargerSelectorSupport", 0xF1: "ChargerSpec", 0xF2: "Level2",
    0xF3: "Level3",
}


def usage_name(page, usage):
    if page == 0x84:
        return "PD." + PD_USAGE.get(usage, f"0x{usage:02X}")
    if page == 0x85:
        return "BS." + BS_USAGE.get(usage, f"0x{usage:02X}")
    return f"0x{page:04X}.0x{usage:02X}"


def signed(val, bits):
    if bits and val >= (1 << (bits - 1)):
        return val - (1 << bits)
    return val


def parse(data):
    i = 0
    g = {}           # global items
    locals_ = {"usages": [], "usage_min": None, "usage_max": None}
    collection = []  # stack of (page, usage)
    offsets = {}     # (report_type, report_id) -> current bit offset
    fields = []

    while i < len(data):
        b = data[i]
        i += 1
        if b == 0xFE:  # long item
            size = data[i]
            i += 2 + size
            continue
        size = b & 0x03
        if size == 3:
            size = 4
        typ = (b >> 2) & 0x03
        tag = (b >> 4) & 0x0F
        val = 0
        for k in range(size):
            val |= data[i + k] << (8 * k)
        i += size

        if typ == 1:  # Global
            names = {0: "usage_page", 1: "logical_min", 2: "logical_max",
                     3: "physical_min", 4: "physical_max", 5: "unit_exp",
                     6: "unit", 7: "report_size", 8: "report_id",
                     9: "report_count"}
            if tag in names:
                if tag in (1, 2, 3, 4, 5):
                    g[names[tag]] = signed(val, size * 8)
                else:
                    g[names[tag]] = val
        elif typ == 2:  # Local
            if tag == 0:
                page = g.get("usage_page", 0)
                if size == 4:
                    locals_["usages"].append((val >> 16, val & 0xFFFF))
                else:
                    locals_["usages"].append((page, val))
            elif tag == 1:
                locals_["usage_min"] = val
            elif tag == 2:
                locals_["usage_max"] = val
        elif typ == 0:  # Main
            if tag in (8, 9, 11):  # Input / Output / Feature
                rtype = {8: "Input", 9: "Output", 11: "Feature"}[tag]
                rid = g.get("report_id", 0)
                rsize = g.get("report_size", 0)
                rcount = g.get("report_count", 0)
                key = (rtype, rid)
                off = offsets.get(key, 0)
                page = g.get("usage_page", 0)
                us = list(locals_["usages"])
                if locals_["usage_min"] is not None and not us:
                    us = [(page, u) for u in range(locals_["usage_min"],
                                                   (locals_["usage_max"] or locals_["usage_min"]) + 1)]
                for n in range(rcount):
                    if us:
                        u = us[n] if n < len(us) else us[-1]
                    else:
                        u = (page, 0)
                    fields.append({
                        "type": rtype, "report_id": rid, "bit_off": off,
                        "bit_size": rsize, "usage": usage_name(*u),
                        "logical_min": g.get("logical_min"),
                        "logical_max": g.get("logical_max"),
                        "unit": g.get("unit"), "unit_exp": g.get("unit_exp"),
                        "flags": val,
                        "path": "/".join(usage_name(*c) for c in collection),
                    })
                    off += rsize
                offsets[key] = off
            elif tag == 10:  # Collection
                page = g.get("usage_page", 0)
                u = locals_["usages"][-1] if locals_["usages"] else (page, 0)
                collection.append(u)
            elif tag == 12:  # End collection
                if collection:
                    collection.pop()
            locals_ = {"usages": [], "usage_min": None, "usage_max": None}
    return fields


if __name__ == "__main__":
    raw = open(sys.argv[1], "rb").read()
    fs = parse(raw)
    print(f"{len(fs)} fields\n")
    for f in fs:
        ro = "" if (f["flags"] & 0x02) else " [CONSTANT/RO]"
        print(f"{f['type']:7s} id={f['report_id']:3d} off={f['bit_off']:3d} "
              f"size={f['bit_size']:2d} lmin={f['logical_min']} lmax={f['logical_max']} "
              f"exp={f['unit_exp']} :: {f['path']}/{f['usage']}{ro}")
