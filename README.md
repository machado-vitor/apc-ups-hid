# apc-ups-hid

A USB HID driver and monitoring daemon for APC Back-UPS units, written from
the device's own HID report descriptor. No NUT or apcupsd runtime dependency
is required to read the UPS: on platforms where the kernel keeps exclusive
ownership of the HID device (notably macOS), NUT's `usbhid-ups` driver cannot
open it at all — this project talks to the device directly through
[`hidapi`](https://github.com/libusb/hidapi) instead.

Tested against an **APC Back-UPS BX1500BI-BR**. Other Back-UPS models that
expose the standard USB HID Power Device usage page are likely compatible,
but only this model has been verified.

## What's in here

- `apcups.py` — low-level driver: opens the HID device, decodes feature
  reports into named fields (battery charge, runtime, voltages, load,
  status flags...), and exposes commands (beeper, self-test, sensitivity,
  transfer thresholds, shutdown delays).
- `upsd.py` — long-running supervisor daemon:
  - polls the UPS and keeps live state in memory
  - publishes to MQTT with Home Assistant discovery (sensors, binary
    sensors, selects, a number and two buttons)
  - serves the state as JSON over HTTP (`/state`, `/health`, `/info`,
    `POST /cmd/<name>`)
  - appends a CSV row on an interval so history survives restarts
  - runs an external `notify_command` on power-loss/restore and other
    state transitions worth a human's attention
  - optionally shuts the host down on a long outage, gated by the presence
    of a flag file (`shutdown_flag_path`) so `rm` disables it instantly
  - can optionally shut down dependent peers over SSH, in dependency order,
    before shutting itself down
  - can optionally mirror state into a NUT `dummy-ups` `.dev` file so a NUT
    `upsd` elsewhere in the stack can serve it over the network (port 3493)
    to `upsmon` clients that have no UPS cable of their own
- `upsctl.py` — CLI client for the daemon's HTTP API (`upsctl status`,
  `upsctl watch`, `upsctl beeper ...`, `upsctl set ...`, etc.)
- `parse_rd.py` — generic USB HID report descriptor parser, used to derive
  the report id map in `apcups.py` from `report_descriptor.bin`.
- `probe.py` — one-off tool to enumerate a connected APC HID device and dump
  its raw feature/input reports, useful when adapting this to a different
  model.
- `report_descriptor.bin` — the report descriptor dumped from the tested
  device; a fixture for `parse_rd.py` / for verifying other Back-UPS models.
- `nut/nut-server.sh` — optional helper that starts NUT's `dummy-ups` +
  `upsd` pointed at the `.dev` file this daemon writes, for exposing the
  reading to NUT-compatible network clients.

Reference material used while reverse engineering the report ids (NUT's
`drivers/apc-hid.c` and `docs/net-protocol.txt`, and `upsmon.c`) is not
included here for licensing reasons — see the
[NUT project](https://github.com/networkupstools/nut) itself.

## Installing

```
pip install .
# or, with MQTT/Home Assistant support:
pip install '.[mqtt]'
```

This installs two console scripts, `upsd` and `upsctl`.

## Configuring

`upsd` reads a JSON config file, by default `config.json` next to the
installed package (override with `-c/--config` or the `UPSD_CONFIG`
environment variable). All keys are optional; see `DEFAULTS` in `upsd.py`
for the full set. A minimal example:

```json
{
  "http_port": 8780,
  "http_bind": "127.0.0.1",
  "mqtt_host": "127.0.0.1",
  "notify_command": ["/path/to/your/notify-script.sh"],
  "shutdown_command": "/path/to/your/quiesce-and-sleep.sh",
  "shutdown_flag_path": "/path/to/autoshutdown.enabled",
  "shutdown_on_battery_seconds": 900,
  "shutdown_battery_charge": 30,
  "shutdown_battery_runtime": 420,
  "peers": []
}
```

- `notify_command` is run as `notify_command + [message]` for every alert;
  point it at anything that accepts a single text argument (a wrapper
  around your chat/notification tool of choice, `logger`, `mail`, ...).
- `shutdown_command` is only ever invoked while `shutdown_flag_path` exists
  (i.e. auto-shutdown is armed with `upsctl autoshutdown on`) and one of the
  configured battery thresholds has actually been crossed.
- `nut_dev_path`, if set, enables writing the NUT dummy-ups mirror file.
- `peers` is a list of `{name, host, user, key, command, down_wait,
  enabled}` describing hosts that share this UPS but have no cable of their
  own, shut down over SSH before this host powers off.

## Usage

```
upsctl status            # full current state
upsctl watch             # live view
upsctl status --json     # for scripting

upsctl beeper disabled   # disabled | enabled
upsctl mute              # silence an alarm sounding right now
upsctl test               # battery self-test (costs a little charge)
upsctl test --abort
upsctl set sensitivity medium        # low | medium | high
upsctl set transfer-low 180          # volts
upsctl set battery-charge-low 20     # percent
upsctl autoshutdown on|off|status
```

`upsd` and `upsctl` talk over HTTP; `upsctl` defaults to
`http://127.0.0.1:8780`, overridable with `--url` or `$UPS_URL`.

## Notable quirks discovered along the way

- The UPS applies a feature-report write with roughly a 1s delay; reading
  immediately after returns the old value. `upsd.py` waits 1.5s before
  re-polling after any command.
- Beeper state "muted" (value 3) is not a persistent state: it only takes
  effect while an alarm is actively sounding, and reverts silently if
  written on an idle unit. That's why `mute` is a separate action rather
  than a `beeper` state.
- On at least the tested model, `input.voltage.nominal` always reports
  115V even when running on 220-240V mains — the unit is auto-sensing/bivolt
  and this field just doesn't reflect it. Use `input.voltage` (the live
  reading) instead.
- Some dashboard widgets (e.g. Homepage's `customapi`) split a field name on
  `.` to walk nested JSON, so NUT-style dotted keys (`battery.charge`) are
  unreachable from them. `upsd.py` also publishes flat aliases (`charge`,
  `runtime_human`, `input_voltage`, ...) for exactly that reason.

## License

MIT, see `LICENSE`.
