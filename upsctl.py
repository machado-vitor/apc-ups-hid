#!/usr/bin/env python3
"""upsctl - command line control for an APC Back-UPS.

Talks to the running daemon over HTTP so the USB handle stays with one owner.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8780"


def api(url: str, path: str, method: str = "GET", body: str | None = None, timeout: int = 10):
    req = urllib.request.Request(
        url.rstrip("/") + path, method=method,
        data=body.encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:
            raise SystemExit(f"HTTP {exc.code}: {exc.reason}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"daemon unreachable at {url} ({exc.reason}). "
                         f"Is upsd running?")


def human_time(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "?"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}min"
    if m:
        return f"{m}min{s:02d}s"
    return f"{s}s"


STATUS_WORDS = {
    "OL": "on line power", "OB": "ON BATTERY", "CHRG": "charging",
    "DISCHRG": "discharging", "LB": "LOW BATTERY", "RB": "REPLACE BATTERY",
    "OVER": "OVERLOAD", "TRIM": "regulating voltage", "NOBATT": "NO BATTERY",
}


def cmd_status(args):
    st = api(args.url, "/state")
    if args.json:
        print(json.dumps(st, indent=2, ensure_ascii=False))
        return 0
    if not st.get("ok"):
        print(f"UPS UNAVAILABLE: {st.get('error')}")
        return 1
    words = ", ".join(STATUS_WORDS.get(w, w) for w in st["ups.status"].split())
    print(f"{st.get('device.model', 'UPS')}  ({st.get('device.serial')})")
    print(f"  status          {st['ups.status']}  -> {words}")
    print(f"  battery         {st['battery.charge']}%  "
          f"runtime {human_time(st['battery.runtime'])}  "
          f"{st['battery.voltage']} V (nominal {st['battery.voltage.nominal']} V)")
    print(f"  line            {st['input.voltage']} V  "
          f"(transfers below {st['input.transfer.low']} V / "
          f"above {st['input.transfer.high']} V)")
    print(f"  load            {st['ups.load']}%  ~{st['ups.realpower']} W "
          f"of {st['ups.realpower.nominal']} W")
    print(f"  beeper          {st.get('ups.beeper.status')}")
    print(f"  sensitivity     {st.get('input.sensitivity')}")
    print(f"  last transfer   {st.get('input.transfer.reason')}")
    print(f"  self-test       {st.get('ups.test.result')}")
    print(f"  low battery at  {st['battery.charge.low']}% / "
          f"{human_time(st['battery.runtime.low'])}")
    armed = "ARMED" if st.get("autoshutdown_armed") else "disarmed"
    print(f"  auto-shutdown   {armed}")
    if st.get("on_battery_since"):
        import time
        print(f"  on battery for  {human_time(time.time() - st['on_battery_since'])}")
    return 0


def cmd_watch(args):
    import time
    try:
        while True:
            st = api(args.url, "/state")
            if st.get("ok"):
                line = (f"{time.strftime('%H:%M:%S')}  {st['ups.status']:12s} "
                        f"bat {st['battery.charge']:3d}%  "
                        f"{human_time(st['battery.runtime']):>9s}  "
                        f"line {st['input.voltage']:5.1f}V  "
                        f"load {st['ups.load']:3d}% ({st['ups.realpower']:6.1f}W)")
            else:
                line = f"{time.strftime('%H:%M:%S')}  UNAVAILABLE {st.get('error')}"
            print(line, flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def send_cmd(args, name, value=""):
    res = api(args.url, f"/cmd/{name}", method="POST",
              body=json.dumps({"value": str(value)}))
    if res.get("ok"):
        print(f"ok: {name} {value}".strip())
        return 0
    print(f"failed: {res.get('error')}")
    return 1


def cmd_beeper(args):
    return send_cmd(args, "beeper", args.state)


def cmd_mute(args):
    return send_cmd(args, "mute")


def cmd_test(args):
    if args.abort:
        return send_cmd(args, "abort_test")
    if args.panel:
        return send_cmd(args, "panel_test")
    return send_cmd(args, "self_test")


def cmd_set(args):
    mapping = {
        "sensitivity": "sensitivity",
        "transfer-low": "transfer_low",
        "transfer-high": "transfer_high",
        "battery-charge-low": "battery_charge_low",
        "battery-runtime-low": "battery_runtime_low",
        "delay-shutdown": "delay_shutdown",
        "delay-reboot": "delay_reboot",
    }
    return send_cmd(args, mapping[args.key], args.value)


def cmd_autoshutdown(args):
    flag = os.environ.get("UPSCTL_SHUTDOWN_FLAG") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "autoshutdown.enabled"
    )
    if args.action == "on":
        open(flag, "w").write("armed\n")
        print("auto-shutdown ARMED")
    elif args.action == "off":
        if os.path.exists(flag):
            os.remove(flag)
        print("auto-shutdown disarmed")
    else:
        print("ARMED" if os.path.exists(flag) else "disarmed")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="upsctl", description="APC UPS control")
    p.add_argument("--url", default=os.environ.get("UPS_URL", DEFAULT_URL))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="current state")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("watch", help="follow state live")
    s.add_argument("-n", "--interval", type=float, default=2.0)
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("beeper", help="enable/disable the audible alarm")
    s.add_argument("state", choices=["enabled", "disabled"])
    s.set_defaults(func=cmd_beeper)

    s = sub.add_parser("mute", help="silence an alarm that is sounding right now")
    s.set_defaults(func=cmd_mute)

    s = sub.add_parser("test", help="battery self-test")
    s.add_argument("--abort", action="store_true")
    s.add_argument("--panel", action="store_true", help="panel (lights) test")
    s.set_defaults(func=cmd_test)

    s = sub.add_parser("set", help="adjust a parameter")
    s.add_argument("key", choices=["sensitivity", "transfer-low", "transfer-high",
                                   "battery-charge-low", "battery-runtime-low",
                                   "delay-shutdown", "delay-reboot"])
    s.add_argument("value")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("autoshutdown", help="arm/disarm shutdown-on-long-outage")
    s.add_argument("action", nargs="?", default="status", choices=["on", "off", "status"])
    s.set_defaults(func=cmd_autoshutdown)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
