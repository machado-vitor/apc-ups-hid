#!/usr/bin/env python3
"""UPS supervisor daemon for APC Back-UPS units on USB HID.

Responsibilities, in order of importance:
  1. Poll the UPS over USB HID and keep the last known state in memory.
  2. Publish to MQTT with Home Assistant discovery (sensors + controls).
  3. Serve the state as JSON over HTTP (dashboard widgets, uptime monitors,
     scripts).
  4. Append a metrics row to a CSV so history survives restarts.
  5. Notify on state transitions worth a human's attention.
  6. Optionally shut the machine down on a long outage (opt-in flag file).

Design rules:
  - The device handle is owned by this process only; everything else talks to
    it through HTTP or MQTT. Two processes claiming the same HID device fight.
  - Automatic shutdown is armed by the presence of a flag file, so `rm`
    disables it instantly with no restart. Absence of the file is the OFF
    state.
  - Notifications go out through an external `notify_command`, which keeps
    this daemon decoupled from any particular chat/notification stack.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apcups import (  # noqa: E402
    BackUPS,
    UPSError,
    BEEPER_VALUES,
    SENSITIVITY_VALUES,
)

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = sys.stderr

DEFAULT_CONFIG_PATH = os.environ.get(
    "UPSD_CONFIG", os.path.join(BASE, "config.json")
)

DEFAULTS = {
    "poll_interval": 5,
    "http_port": 8780,
    "http_bind": "127.0.0.1",
    "mqtt_host": "127.0.0.1",
    "mqtt_port": 1883,
    "mqtt_prefix": "ups/backups",
    "ha_discovery_prefix": "homeassistant",
    "csv_interval": 60,
    "csv_path": os.path.join(BASE, "ups.csv"),
    "state_path": os.path.join(BASE, "state.json"),
    "notify": True,
    # Command run as `notify_command + [message]`. Must never raise or block
    # for long; failures are only logged. Example: ["notify-send", "-a", "ups"].
    "notify_command": ["logger", "-t", "ups"],
    # Auto shutdown (only acts when autoshutdown.enabled exists):
    "shutdown_on_battery_seconds": 600,   # on battery this long -> shut down
    "shutdown_battery_charge": 25,        # or charge at/below this
    "shutdown_battery_runtime": 300,      # or runtime at/below this (seconds)
    "shutdown_command": os.path.join(BASE, "quiesce-and-sleep.sh"),
    "shutdown_flag_path": os.path.join(BASE, "autoshutdown.enabled"),
    "ups_output_off_delay": 120,          # tell the UPS to cut output N s later
    "nut_dev_path": None,                 # optional dummy-ups .dev file (see write_nut_dev)
    # Machines sharing this UPS that have no UPS cable of their own. They are
    # shut down BEFORE this host (see maybe_shutdown for why the order is not
    # negotiable) and each one is waited for until it stops answering ping.
    "peers": [],
}

CSV_FIELDS = [
    "ts", "iso", "status", "charge", "runtime_s", "battery_v", "input_v",
    "load_pct", "realpower_w", "beeper", "transfer_reason", "test_result",
]


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", file=LOG, flush=True)


def load_config(config_path: str) -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(config_path):
        try:
            with open(config_path) as fh:
                cfg.update(json.load(fh))
        except Exception as exc:
            log(f"config error, using defaults: {exc}")
    return cfg


def notify(cfg: dict, text: str) -> None:
    """Run the configured notify command. Never raises."""
    if not cfg.get("notify"):
        log(f"notify (suppressed): {text}")
        return
    cmd = cfg.get("notify_command") or DEFAULTS["notify_command"]
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    try:
        subprocess.run(
            [*cmd, text],
            check=False, capture_output=True, timeout=60,
        )
        log(f"notified: {text}")
    except Exception as exc:
        log(f"notify failed: {exc}")


# ---------------------------------------------------------------- MQTT ----
class MQTT:
    """Thin optional MQTT layer. If paho is missing the daemon still runs."""

    def __init__(self, cfg: dict, supervisor: "Supervisor"):
        self.cfg = cfg
        self.sup = supervisor
        self.client = None
        self.prefix = cfg["mqtt_prefix"]
        self.connected = False

    def start(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            log("paho-mqtt not installed; MQTT/Home Assistant disabled")
            return
        try:
            self.client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id="apc-ups-hid-daemon"
            )
        except (AttributeError, TypeError):
            self.client = mqtt.Client(client_id="apc-ups-hid-daemon")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.will_set(f"{self.prefix}/availability", "offline", retain=True)
        try:
            self.client.connect(self.cfg["mqtt_host"], int(self.cfg["mqtt_port"]), 60)
        except Exception as exc:
            log(f"mqtt connect failed: {exc}")
            return
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        self.connected = True
        log("mqtt connected")
        client.publish(f"{self.prefix}/availability", "online", retain=True)
        client.subscribe(f"{self.prefix}/cmd/+")
        self.publish_discovery()

    def _on_message(self, client, userdata, msg):
        topic = msg.topic.rsplit("/", 1)[-1]
        payload = msg.payload.decode().strip()
        log(f"mqtt cmd {topic}={payload}")
        try:
            self.sup.command(topic, payload)
        except Exception as exc:
            log(f"command {topic} failed: {exc}")

    def publish(self, topic: str, payload, retain: bool = False) -> None:
        if not self.client or not self.connected:
            return
        if not isinstance(payload, str):
            payload = json.dumps(payload)
        try:
            self.client.publish(f"{self.prefix}/{topic}", payload, retain=retain)
        except Exception as exc:
            log(f"mqtt publish failed: {exc}")

    # Home Assistant MQTT discovery -----------------------------------
    def publish_discovery(self) -> None:
        if not self.client:
            return
        dp = self.cfg["ha_discovery_prefix"]
        info = self.sup.info
        device = {
            "identifiers": ["apc_ups_hid"],
            "manufacturer": info.get("device.mfr", "APC"),
            "model": info.get("device.model", "Back-UPS"),
            "name": "UPS",
            "serial_number": info.get("device.serial"),
            "sw_version": info.get("ups.firmware"),
        }
        avail = [{"topic": f"{self.prefix}/availability"}]
        state_topic = f"{self.prefix}/state"

        def pub(component: str, object_id: str, config: dict) -> None:
            config.update({
                "device": device,
                "availability": avail,
                "unique_id": f"apc_ups_{object_id}",
                "state_topic": state_topic,
            })
            self.client.publish(
                f"{dp}/{component}/apc_ups/{object_id}/config",
                json.dumps(config), retain=True,
            )

        sensors = [
            ("charge", "Battery charge", "battery", "%", "battery.charge", "measurement"),
            ("runtime", "Runtime remaining", "duration", "s", "battery.runtime", "measurement"),
            ("battery_voltage", "Battery voltage", "voltage", "V", "battery.voltage", "measurement"),
            ("input_voltage", "Line voltage", "voltage", "V", "input.voltage", "measurement"),
            ("load", "UPS load", "power_factor", "%", "ups.load", "measurement"),
            ("realpower", "Power draw", "power", "W", "ups.realpower", "measurement"),
        ]
        for oid, name, dev_class, unit, key, state_class in sensors:
            cfg = {
                "name": name,
                "value_template": "{{ value_json['%s'] }}" % key,
                "unit_of_measurement": unit,
                "state_class": state_class,
            }
            if dev_class:
                cfg["device_class"] = dev_class
            pub("sensor", oid, cfg)

        pub("sensor", "status", {
            "name": "UPS status",
            "value_template": "{{ value_json['ups.status'] }}",
            "icon": "mdi:power-plug",
        })
        pub("sensor", "transfer_reason", {
            "name": "Last transfer reason",
            "value_template": "{{ value_json['input.transfer.reason'] }}",
            "icon": "mdi:transmission-tower-off",
            "entity_category": "diagnostic",
        })
        pub("sensor", "test_result", {
            "name": "Self-test result",
            "value_template": "{{ value_json['ups.test.result'] }}",
            "icon": "mdi:clipboard-check",
            "entity_category": "diagnostic",
        })

        binaries = [
            ("online", "On line power", "plug", "ac_present"),
            ("charging", "Charging", "battery_charging", "charging"),
            ("low_battery", "Low battery", "battery", "shutdown_imminent"),
            ("replace_battery", "Replace battery", "problem", "replace_battery"),
            ("overload", "Overload", "problem", "overload"),
        ]
        for oid, name, dev_class, flag in binaries:
            pub("binary_sensor", oid, {
                "name": name,
                "device_class": dev_class,
                "value_template": "{{ 'ON' if value_json['ups.flags']['%s'] else 'OFF' }}" % flag,
                "payload_on": "ON",
                "payload_off": "OFF",
            })

        pub("select", "beeper", {
            "name": "Beeper",
            "options": sorted(BEEPER_VALUES),
            "value_template": "{{ value_json['ups.beeper.status'] }}",
            "command_topic": f"{self.prefix}/cmd/beeper",
            "icon": "mdi:bell",
        })
        pub("select", "sensitivity", {
            "name": "Sensitivity",
            "options": sorted(SENSITIVITY_VALUES),
            "value_template": "{{ value_json['input.sensitivity'] }}",
            "command_topic": f"{self.prefix}/cmd/sensitivity",
            "icon": "mdi:tune",
            "entity_category": "config",
        })
        pub("button", "self_test", {
            "name": "Run self-test",
            "command_topic": f"{self.prefix}/cmd/self_test",
            "payload_press": "press",
            "icon": "mdi:play-circle",
        })
        pub("button", "mute", {
            "name": "Mute alarm",
            "command_topic": f"{self.prefix}/cmd/mute",
            "payload_press": "press",
            "icon": "mdi:bell-off",
        })
        pub("number", "battery_charge_low", {
            "name": "Low battery threshold",
            "command_topic": f"{self.prefix}/cmd/battery_charge_low",
            "value_template": "{{ value_json['battery.charge.low'] }}",
            "min": 1, "max": 100, "step": 1,
            "unit_of_measurement": "%",
            "entity_category": "config",
        })
        log("mqtt discovery published")


# --------------------------------------------------------------- HTTP ----
class Handler(BaseHTTPRequestHandler):
    supervisor: "Supervisor" = None  # set by the server factory

    def log_message(self, fmt, *args):  # silence default stderr spam
        pass

    def _send(self, code: int, body, content_type="application/json"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        sup = Handler.supervisor
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/state"):
            state = sup.snapshot()
            self._send(200 if state.get("ok") else 503, state)
        elif path == "/health":
            ok = sup.healthy()
            self._send(200 if ok else 503, {"ok": ok, "error": sup.last_error})
        elif path == "/info":
            self._send(200, sup.info)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        sup = Handler.supervisor
        path = self.path.split("?")[0].rstrip("/")
        if not path.startswith("/cmd/"):
            self._send(404, {"error": "not found"})
            return
        name = path[len("/cmd/"):]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        value = raw.strip()
        if value.startswith("{"):
            try:
                value = str(json.loads(value).get("value", ""))
            except Exception:
                pass
        try:
            result = sup.command(name, value)
            self._send(200, {"ok": True, "command": name, "result": result})
        except Exception as exc:
            self._send(400, {"ok": False, "command": name, "error": str(exc)})


# ---------------------------------------------------------- Supervisor ----
class Supervisor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ups = BackUPS()
        self.mqtt = MQTT(cfg, self)
        self.state: dict = {}
        self.info: dict = {}
        self.last_error: str | None = None
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.on_battery_since: float | None = None
        self.last_csv = 0.0
        self.shutdown_triggered = False
        self._prev_notify_key: tuple | None = None

    # -- lifecycle ---------------------------------------------------
    def connect(self) -> bool:
        try:
            self.ups.open()
            self.info = self.ups.device_info()
            self.last_error = None
            log(f"connected: {self.info.get('device.model')} sn={self.info.get('device.serial')}")
            return True
        except Exception as exc:
            self.last_error = f"connect: {exc}"
            log(self.last_error)
            return False

    def healthy(self) -> bool:
        with self.lock:
            st = self.state
        return bool(st) and st.get("ok") and (time.time() - st.get("ts", 0)) < 60

    def snapshot(self) -> dict:
        with self.lock:
            st = dict(self.state)
        st.setdefault("ok", False)
        st.setdefault("error", self.last_error)
        # Flat aliases: some dashboard widgets split `field` on "." to walk
        # nested objects, so the NUT-style dotted keys above are unreachable
        # from them. These duplicates are what such widgets bind to.
        for src, alias in (
            ("ups.status", "status"),
            ("battery.charge", "charge"),
            ("battery.runtime", "runtime"),
            ("battery.voltage", "battery_voltage"),
            ("input.voltage", "input_voltage"),
            ("ups.load", "load"),
            ("ups.realpower", "realpower"),
            ("ups.beeper.status", "beeper"),
            ("ups.test.result", "test_result"),
            ("input.transfer.reason", "transfer_reason"),
        ):
            if src in st:
                st[alias] = st[src]
        rt = st.get("battery.runtime")
        if isinstance(rt, (int, float)):
            h, m = divmod(int(rt) // 60, 60)
            st["runtime_human"] = f"{h}h{m:02d}" if h else f"{m} min"
        st.update({k: v for k, v in self.info.items() if k.startswith("device.")})
        if "on_battery_since" not in st:
            st["on_battery_since"] = self.on_battery_since
        st["autoshutdown_armed"] = os.path.exists(self.cfg["shutdown_flag_path"])
        return st

    # -- commands ----------------------------------------------------
    def command(self, name: str, value: str = ""):
        u = self.ups
        if name == "beeper":
            u.set_beeper(value)
        elif name == "mute":
            u.mute_beeper()
        elif name == "self_test":
            u.start_self_test()
        elif name == "abort_test":
            u.abort_self_test()
        elif name == "panel_test":
            u.panel_test()
        elif name == "sensitivity":
            u.set_sensitivity(value)
        elif name == "transfer_low":
            u.set_transfer_low(int(float(value)))
        elif name == "transfer_high":
            u.set_transfer_high(int(float(value)))
        elif name == "battery_charge_low":
            u.set_battery_charge_low(int(float(value)))
        elif name == "battery_runtime_low":
            u.set_battery_runtime_low(int(float(value)))
        elif name == "delay_shutdown":
            u.set_delay_shutdown(int(float(value)))
        elif name == "delay_reboot":
            u.set_delay_reboot(int(float(value)))
        else:
            raise ValueError(f"unknown command '{name}'")
        # Refresh so the UI reflects the change without waiting for the next
        # tick. The UPS applies a feature write asynchronously (~1s measured),
        # so an immediate re-read still returns the previous value.
        time.sleep(1.5)
        self.poll_once()
        self.publish_state()
        return "ok"

    # -- polling -----------------------------------------------------
    def poll_once(self) -> bool:
        try:
            data = self.ups.poll()
        except Exception as exc:
            self.last_error = str(exc)
            with self.lock:
                self.state = {"ok": False, "ts": time.time(), "error": self.last_error}
            return False
        data["ok"] = True
        data["iso"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with self.lock:
            self.state = data
        self.last_error = None
        return True

    def run(self) -> None:
        backoff = 1
        while not self.stopping.is_set():
            if not self.ups.connected and not self.connect():
                self.publish_state()
                self.stopping.wait(min(backoff, 60))
                backoff = min(backoff * 2, 60)
                continue
            backoff = 1
            if not self.poll_once():
                log(f"poll failed: {self.last_error}; reopening")
                self.publish_state()
                try:
                    self.ups.reopen()
                except Exception as exc:
                    self.last_error = f"reopen: {exc}"
                    self.ups.close()
                self.stopping.wait(5)
                continue
            self.publish_state()
            self.evaluate()
            self.append_csv()
            self.stopping.wait(self.cfg["poll_interval"])

    def publish_state(self) -> None:
        st = self.snapshot()
        self.mqtt.publish("state", st, retain=True)
        try:
            path = self.cfg["state_path"]
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(st, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as exc:
            log(f"state write failed: {exc}")
        if self.cfg.get("nut_dev_path"):
            self.write_nut_dev(st)

    def write_nut_dev(self, st: dict) -> None:
        """Mirror the reading as a NUT dummy-ups `.dev` file.

        The NUT usbhid-ups driver cannot open a HID device the kernel already
        holds open on some platforms (notably macOS), so this daemon can act
        as the driver itself: point NUT's dummy-ups at the file written here
        and its upsd can serve it on the standard NUT network port (3493) to
        upsmon clients elsewhere on the network.
        `ups.status` must carry OL/OB/LB for upsmon to act.
        """
        try:
            status = st.get("ups.status") or "OL"
            flags = st.get("ups.flags") or {}
            if (flags.get("shutdown_imminent") or flags.get("remaining_time_limit_expired")
                    or self.shutdown_triggered):
                # Our own thresholds (minutes on battery / charge / runtime) are
                # the policy here; NUT clients only understand LB, so map it.
                status += " LB"
            if flags.get("replace_battery"):
                status += " RB"
            lines = [f"ups.status: {status}"]
            for k in ("battery.charge", "battery.runtime", "battery.voltage",
                      "battery.charge.low", "battery.runtime.low", "input.voltage",
                      "ups.load", "ups.realpower", "ups.realpower.nominal",
                      "ups.beeper.status", "device.mfr", "device.model", "device.serial"):
                v = st.get(k)
                if v is not None and v != "":
                    lines.append(f"{k}: {v}")
            if not st.get("ok"):
                # Stale/unreadable HID: tell clients the data is not fresh.
                lines[0] = "ups.status: OL"
                lines.append("ups.alarm: driver read error")
            path = self.cfg["nut_dev_path"]
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                fh.write("\n".join(lines) + "\n")
            os.replace(tmp, path)
        except Exception as exc:
            log(f"nut dev write failed: {exc}")

    def append_csv(self) -> None:
        now = time.time()
        if now - self.last_csv < self.cfg["csv_interval"]:
            return
        self.last_csv = now
        st = self.snapshot()
        if not st.get("ok"):
            return
        row = {
            "ts": int(st["ts"]), "iso": st.get("iso"),
            "status": st.get("ups.status"), "charge": st.get("battery.charge"),
            "runtime_s": st.get("battery.runtime"), "battery_v": st.get("battery.voltage"),
            "input_v": st.get("input.voltage"), "load_pct": st.get("ups.load"),
            "realpower_w": st.get("ups.realpower"), "beeper": st.get("ups.beeper.status"),
            "transfer_reason": st.get("input.transfer.reason"),
            "test_result": st.get("ups.test.result"),
        }
        try:
            csv_path = self.cfg["csv_path"]
            os.makedirs(os.path.dirname(csv_path), exist_ok=True)
            new = not os.path.exists(csv_path)
            with open(csv_path, "a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
        except Exception as exc:
            log(f"csv write failed: {exc}")

    # -- event logic -------------------------------------------------
    def evaluate(self) -> None:
        st = self.snapshot()
        flags = st.get("ups.flags", {})
        on_battery = not flags.get("ac_present") or flags.get("discharging")
        now = time.time()

        if on_battery and self.on_battery_since is None:
            self.on_battery_since = now
            notify(self.cfg,
                   f"Power lost. UPS on battery: {st.get('battery.charge')}% "
                   f"({round(st.get('battery.runtime', 0) / 60)} min of runtime), "
                   f"load {st.get('ups.load')}% / {st.get('ups.realpower')} W.")
        elif not on_battery and self.on_battery_since is not None:
            dur = round(now - self.on_battery_since)
            self.on_battery_since = None
            self.shutdown_triggered = False
            notify(self.cfg,
                   f"Power restored after {dur // 60}min{dur % 60:02d}s. "
                   f"Battery at {st.get('battery.charge')}%.")

        # Conditions worth one message each time they newly become true.
        key = (
            bool(flags.get("replace_battery")),
            bool(flags.get("overload")),
            bool(flags.get("shutdown_imminent")),
        )
        if self._prev_notify_key is None:
            self._prev_notify_key = key
        elif key != self._prev_notify_key:
            msgs = []
            if key[0] and not self._prev_notify_key[0]:
                msgs.append("UPS requesting BATTERY REPLACEMENT.")
            if key[1] and not self._prev_notify_key[1]:
                msgs.append(f"UPS in OVERLOAD ({st.get('ups.load')}%). Unplug something.")
            if key[2] and not self._prev_notify_key[2]:
                msgs.append(f"Battery critically low ({st.get('battery.charge')}%). "
                            "Shutdown imminent.")
            for m in msgs:
                notify(self.cfg, m)
            self._prev_notify_key = key

        if on_battery:
            self.maybe_shutdown(st)

    def maybe_shutdown(self, st: dict) -> None:
        """Shut the machine down on a long outage. Only when armed by flag file."""
        if self.shutdown_triggered or not os.path.exists(self.cfg["shutdown_flag_path"]):
            return
        cfg = self.cfg
        elapsed = time.time() - (self.on_battery_since or time.time())
        charge = st.get("battery.charge", 100)
        runtime = st.get("battery.runtime", 99999)
        reasons = []
        if elapsed >= cfg["shutdown_on_battery_seconds"]:
            reasons.append(f"{int(elapsed)}s on battery")
        if charge <= cfg["shutdown_battery_charge"]:
            reasons.append(f"battery at {charge}%")
        if runtime <= cfg["shutdown_battery_runtime"]:
            reasons.append(f"runtime of {runtime}s")
        if st.get("ups.flags", {}).get("shutdown_imminent"):
            reasons.append("UPS shutdown-imminent flag")
        if not reasons:
            return

        self.shutdown_triggered = True
        reason = "; ".join(reasons)
        log(f"SHUTDOWN triggered: {reason}")
        # Raise LB (low battery) on the NUT side FIRST, if enabled: upsmon on
        # dependent hosts acts on "OB LB" and shuts itself down. shutdown_peers()
        # below then only waits for it to go silent (HOSTSYNC, as NUT's primary
        # does).
        self.publish_state()
        notify(cfg, f"Shutting the site down now: {reason}. "
                    f"Dependent peers first, then this host. "
                    f"Everything comes back on its own once power returns.")

        # Order matters and it is the opposite of "least important first": the
        # host with the fewest dependents goes last. A typical case is a
        # router/firewall that has no UPS cable of its own and would only
        # learn about the outage over the network - shutting it down last
        # would leave it blind and unreachable, while this daemon needs
        # working DNS/routing to deliver its own alerts. Shutting it down
        # first costs nothing if it holds no state worth flushing.
        #
        # It also has to be finished before the UPS output cut armed below.
        peers_deadline = self.shutdown_peers()

        # Ask the UPS to cut its own output shortly after. What the shutdown
        # command does with that (sleep vs halt) is left entirely to the
        # configured shutdown_command; this daemon only arms the UPS-side
        # timer and invokes the command.
        try:
            delay = int(cfg["ups_output_off_delay"])
            if peers_deadline > delay - 30:
                # Never let the cut land while a peer is still powering down.
                delay = int(peers_deadline) + 30
                log(f"extending UPS output cut to {delay}s to clear peers")
            self.ups.set_delay_shutdown(delay)
        except Exception as exc:
            log(f"could not arm UPS output cut: {exc}")
            # Without a working cut, fall back to a plain halt so at least
            # this host's disks are safe even if it never restarts itself.
            cfg = dict(cfg, shutdown_command="/usr/bin/sudo -n /sbin/shutdown -h now")
        try:
            subprocess.Popen(shlex.split(cfg["shutdown_command"]))
        except Exception as exc:
            log(f"shutdown command failed: {exc}")

    def shutdown_peers(self) -> float:
        """Power down the machines that depend on us, before we go.

        Returns how many seconds were spent, so the caller can keep the UPS
        output cut clear of a peer that is still writing to disk.
        """
        started = time.time()
        for peer in self.cfg.get("peers", []):
            if not peer.get("enabled", True):
                continue
            name = peer.get("name", peer.get("host", "?"))
            cmd = [
                "/usr/bin/ssh", "-i", os.path.expanduser(peer["key"]),
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                "-o", f"ConnectTimeout={peer.get('connect_timeout', 8)}",
                f"{peer.get('user', 'admin')}@{peer['host']}",
                peer.get("command", "/sbin/shutdown -p now"),
            ]
            try:
                subprocess.run(cmd, timeout=peer.get("timeout", 25),
                               capture_output=True)
                log(f"peer {name}: shutdown command sent")
            except Exception as exc:
                # A peer we cannot reach must not block our own shutdown:
                # this machine still has a battery deadline to meet.
                log(f"peer {name}: shutdown FAILED ({exc}) - continuing anyway")
                notify(self.cfg, f"Could not shut down {name}: {exc}. "
                                 f"It will lose power without a clean shutdown.")
                continue
            self.wait_for_peer_down(peer, name)
        return time.time() - started

    def wait_for_peer_down(self, peer: dict, name: str) -> None:
        """Block until the peer stops answering ping, or we run out of patience.

        Sending `shutdown` only proves the command was accepted, not that the
        box finished writing to disk. Waiting for it to go silent is what makes
        the ordering real instead of merely intended.
        """
        deadline = time.time() + peer.get("down_wait", 60)
        while time.time() < deadline:
            time.sleep(2)
            probe = subprocess.run(
                ["/sbin/ping", "-c", "1", "-W", "1000", peer["host"]],
                capture_output=True)
            if probe.returncode != 0:
                log(f"peer {name}: down after "
                    f"{int(peer.get('down_wait', 60) - (deadline - time.time()))}s")
                return
        log(f"peer {name}: still answering after {peer.get('down_wait', 60)}s "
            f"- proceeding with our own shutdown")

    def stop(self, *_a) -> None:
        log("stopping")
        self.stopping.set()


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="upsd", description="APC UPS HID supervisor daemon"
    )
    p.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG_PATH,
        help="path to config.json (default: %(default)s, or $UPSD_CONFIG)",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    sup = Supervisor(cfg)
    Handler.supervisor = sup

    signal.signal(signal.SIGTERM, sup.stop)
    signal.signal(signal.SIGINT, sup.stop)

    sup.connect()
    sup.poll_once()
    sup.mqtt.start()

    server = ThreadingHTTPServer((cfg["http_bind"], int(cfg["http_port"])), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"http listening on {cfg['http_bind']}:{cfg['http_port']}")

    try:
        sup.run()
    finally:
        server.shutdown()
        sup.mqtt.publish("availability", "offline", retain=True)
        sup.ups.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
