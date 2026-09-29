#!/usr/bin/env python3
"""One-off probe: enumerate the APC UPS HID device and dump every readable
feature/input report so we can map usages to NUT-style variable names."""
import hid

VENDOR = 0x051D

for info in hid.enumerate(VENDOR, 0):
    print("=== device ===")
    for k in ("path", "vendor_id", "product_id", "serial_number",
              "manufacturer_string", "product_string", "usage_page", "usage",
              "interface_number", "release_number"):
        print(f"  {k}: {info.get(k)}")

dev = hid.Device(vid=VENDOR, pid=0x0002)
print("\nopened:", dev.manufacturer, "|", dev.product, "|", dev.serial)

print("\n=== feature reports (id: bytes) ===")
for rid in range(1, 256):
    try:
        data = dev.get_feature_report(rid, 64)
    except Exception:
        continue
    if data and len(data) > 1:
        print(f"{rid:3d} ({rid:#04x}): {data.hex(' ')}")

print("\n=== input reports (5s of traffic) ===")
seen = {}
import time
end = time.time() + 5
while time.time() < end:
    try:
        d = dev.read(64, timeout=500)
    except Exception as e:
        print("read err", e)
        break
    if d:
        seen.setdefault(d[0], []).append(bytes(d).hex(" "))
for rid, vals in sorted(seen.items()):
    print(f"{rid:3d}: {vals[0]}  (x{len(vals)})")

dev.close()
