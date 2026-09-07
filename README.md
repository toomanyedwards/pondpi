# PondPi

Measures water level with one or more sensors (currently the A02YYUW
waterproof ultrasonic sensor over UART, with the driver layer designed to
support other sensor types too) on a Raspberry Pi, and exposes every
sensor's current reading over a small HTTP API.

## How it works

Each configured sensor is driven by its own `LevelSensor` driver instance
(see [Sensor drivers](#sensor-drivers) below) polled on its own background
thread. A driver's readings are run through that sensor's own configured
`LevelSignal` instances (see [Signal processing](#signal-processing));
a Flask server exposes every sensor's output on `GET /level` (the
configured *default* sensor) and `GET /sensors/<name>/level` (any sensor,
by name).

```
┌────────────────┐  read()  ┌──────────────────┐  add()  ┌────────────────────────────┐
│ LevelSensor      │ ───────>│ poll_sensor()      │───────> │ that sensor's configured     │
│ driver (sensors/)│         │ (one thread/sensor)│         │ LevelSignal instances        │
└────────────────┘         └──────────┬─────────┘         └──────────────┬─────────────┘
        ▲ one instance per                │ writes that sensor's own state              │
        │ config/sensors.yaml entry       v                                            v
        └──────────────────────  Flask app: GET /level, GET /sensors, GET /sensors/<name>/level,
                                  GET /health, GET /diag, POST /reset, GET /signals,
                                  GET /signals/<name>, GET /signals/<name>/diag
```

### Project layout

Production code, tests, and config are kept in separate top-level
directories, and `pondpi` is a proper installable Python package (see
[Developing locally](#developing-locally)) rather than a pile of loose
scripts relying on the working directory being on `sys.path`:

```
pondpi/
├── pyproject.toml          # package metadata + dependencies (replaces requirements*.txt)
├── config/
│   └── sensors.yaml         # sensor + signal-processing config, see below
├── src/pondpi/              # the installable package — production code only
│   ├── server.py            # entrypoint (installed as the `pondpi-server` command)
│   ├── sensors/              # one LevelSensor subclass per <type>_sensor.py file or <type>_sensor/ dir
│   │   ├── base.py            # LevelSensor interface
│   │   └── a02yyuw_sensor/    # A02YYUW driver -- multi-file, so it's a package, not a single file
│   │       ├── __init__.py      # A02YYUWSensor + create() -- the discovered entry point
│   │       ├── read_sensor.py   # protocol/hardware layer: checksum, frame parsing, SimulatedSerial
│   │       ├── sensor_mode.py   # drives the mode-select pin
│   │       └── sensor_power.py  # drives the power supply pin
│   ├── sensor_config.py
│   ├── signals/               # one LevelSignal subclass per <type>_signal.py file
│   │   └── utils/             # RollingMedianFilter, RollingAverage -- generic building blocks,
│   │                           # not signals themselves, see below
│   ├── signal_config.py
│   ├── commit_sha.py
│   └── duration.py
├── tests/                   # mirrors src/pondpi/, not shipped/deployed as code
├── deploy/                  # systemd unit + one-time Pi setup docs
└── .github/workflows/       # CI (per-PR) + Deploy (on merge to main)
```

| File | Responsibility |
|---|---|
| `sensors/base.py` | `LevelSensor` — the interface every driver implements. `read()` returns canonical `{signal_name: distance_mm}` readings (distance from the sensor's mount point down to the water surface — different sensor technologies measure fundamentally different native quantities, so each driver converts its own before returning). `supports_reset`/`reset()` is an optional per-driver capability, not assumed universal. See [Sensor drivers](#sensor-drivers). |
| `sensors/a02yyuw_sensor/` | The A02YYUW driver, as a directory package rather than a single file since its logic naturally splits across several source files — see [Sensor drivers](#sensor-drivers) for how dynamic discovery finds either shape. |
| `sensors/a02yyuw_sensor/__init__.py` | `A02YYUWSensor` — consolidates UART frame reading, hardware raw/processed mode-cycling, and stale-buffer resync (built on this package's own `read_sensor.py`/`sensor_mode.py`/`sensor_power.py`). Reports `"raw"` and `"processed"` named signals. This is the module dynamic discovery imports and scans for the driver's `LevelSensor` subclass + `create()`. |
| `sensors/a02yyuw_sensor/read_sensor.py` | A02YYUW protocol/hardware layer only: checksum validation, frame parsing, a single instantaneous `read_frame(ser)` call, and `SimulatedSerial` (a fake serial source for local dev). No smoothing, no I/O loop, no knowledge of anything beyond one raw frame. |
| `sensors/a02yyuw_sensor/sensor_mode.py` | Drives the RX/mode-select pin — see [Sensor notes](#sensor-notes). `GpioModeController` (real GPIO via `gpiozero`) and `NullModeController` (no-op, used for `--simulate` and in tests). |
| `sensors/a02yyuw_sensor/sensor_power.py` | Drives the power supply pin for `POST /reset` — see [Sensor notes](#sensor-notes). `GpioPowerController` (real GPIO via `gpiozero`) and `NullPowerController` (no-op, used for `--simulate` and in tests). |
| `sensor_config.py` | `load_sensors()` — reads `config/sensors.yaml` into named sensors, each bundled with its driver instance and its own signal pipeline. |
| `signals/` | `LevelSignal` base class (`base.py`) and its built-in implementations, one per file, each named `<type>_signal.py` (`sensor_signal.py`, `rolling_median_signal.py`, `rolling_average_signal.py`, `exponential_smoothing_signal.py`) — see [Signal processing](#signal-processing). |
| `signals/utils/` | `RollingMedianFilter` and `RollingAverage` — generic building blocks used internally by some `LevelSignal` classes. Not signals themselves (they don't implement the `LevelSignal` interface), so they live in a subpackage that dynamic discovery ignores — its name doesn't end in `_signal`. |
| `signal_config.py` | `load_signals()`/`build_signals()` — builds named `LevelSignal` instances from `config/sensors.yaml`'s top-level `signals:` list and groups them by which sensor each is ultimately rooted at (tracing `input:` chains back to a `sensor` signal's `params.sensor`). |
| `commit_sha.py` | `read_commit_sha()` — resolves the deployed commit SHA for `/health`. |
| `duration.py` | `format_duration()` — formats a seconds count as `"1d 2h 3m 4s"` for `/health`'s `uptime_human`. |
| `server.py` | Service entrypoint (`pondpi-server`). Starts one background polling thread per configured sensor and the Flask app. Owns all CLI configuration. |

## API

Two families of routes here. Sensor-scoped routes (`/level`, `/diag`,
`POST /reset`, and `GET /sensors`) come in two forms: a bare route
(`/level`, `/diag`, `POST /reset`) that operates on the *default* sensor —
the one entry in `config/sensors.yaml` marked `default: true` — and a
sensor-named route (`/sensors/<name>/level`, `/sensors/<name>/diag`,
`POST /sensors/<name>/reset`) that operates on any configured sensor by
name, default or not. A single-sensor deployment's existing integrations
(e.g. Home Assistant's REST sensors, built against the bare routes) keep
working unchanged as more sensors are added, as long as that original
sensor stays marked default.

Signal-scoped routes (`GET /signals`, `GET /signals/<name>`, `GET
/signals/<name>/diag`) have no such bare-vs-named split, since signals
aren't nested under a sensor at all (see [Signal
processing](#signal-processing)) — each signal is addressed directly by
its own globally-unique name.

### `GET /sensors`

Lists every configured sensor's name and which one is the default:

```json
{
  "sensors": ["pond_main"],
  "default": "pond_main"
}
```

### `GET /level` / `GET /sensors/<name>/level`

Returns the current instantaneous and smoothed distance readings for the
default sensor, or the named one.

```json
{
  "measure_name": "level",
  "units": "cm",
  "mode": "raw",
  "polling_interval_ms": 150,
  "primary_signal": {
    "value": 11.2,
    "name": "polling_rolling_avg"
  },
  "signals": {
    "polling_rolling_avg": 11.2,
    "pond_main_sensor_raw": 11.3,
    "pond_main_sensor_processed": 11.0
  }
}
```

| Field | Meaning |
|---|---|
| `measure_name` | What this endpoint measures — always `"level"`. Self-describing metadata, useful if the response is logged or forwarded without the URL for context. |
| `units` | The unit every `_cm`/`value` field in this response is in — always `"cm"`. |
| `mode` | Which of the sensor's two hardware output modes this response reflects — see `?mode=` below. |
| `polling_interval_ms` | How often the poller checks the serial buffer for a new frame (see `--polling-interval-ms`). This is the poll rate, not necessarily the sensor's own update rate. |
| `primary_signal` | `{value, name}` for whichever signal is marked `primary: true` — `name` is that signal's actual configured name, so this stays correct even if you rename it. |
| `signals` | A curated `{name: value}` view (in cm) of just the signals meant to be read as final output — every signal rooted at this sensor *except* whichever ones are marked `emit: false` in `config/sensors.yaml`'s `signals:` list (e.g. an intermediate stage that only exists to feed another signal). See [Signal processing](#signal-processing). |

`rolling_median5` (see [Signal processing](#signal-processing)) doesn't
appear here — it's marked `emit: false` since it only exists to feed
`polling_rolling_avg` via `input:`, not as a meaningful output on its own. Its
full state is still visible on `/diag`. `pond_main_sensor_processed`
(rooted at `mode: processed`, unlike the other two) does appear here
alongside them — `signals` isn't scoped to one mode, just to this
sensor — but it updates on its own independent cadence; check its `at`
timestamp on `/diag`/`/signals/pond_main_sensor_processed` if you need
to know how fresh it actually is, since this view doesn't show that.

Returns `503 {"error": "no readings yet"}` if no valid reading has come in
since the server started.

Distance is measured from the sensor down to the water surface — it's not
a depth/level in absolute terms unless you subtract it from the sensor's
fixed mounting height.

#### `?mode=raw|processed`

The A02YYUW has two hardware output modes, selected by the level on its
RX pin — see [Sensor notes](#sensor-notes) below. The driver spends
most of its time with the sensor in `raw` mode (so `signals`/
`primary_signal` above keep being fed by a near-continuous stream, same
as before this param existed) and briefly switches to `processed` mode
once per cycle just to keep that reading fresh too, caching it
separately.

`?mode=raw` (the default; also what you get by omitting the param
entirely) is the response shown above. `?mode=processed` returns a
different, much simpler shape instead — there's no `primary_signal` or
`signals` for it, since the sensor's own processed-mode output isn't run
through this sensor's `signals:` pipeline at all (it's already
hardware-smoothed):

```json
{
  "measure_name": "level",
  "units": "cm",
  "mode": "processed",
  "distance_cm": 11.0
}
```

Also returns `503 {"error": "no readings yet"}` if the cycle hasn't
reached a settled `processed`-mode reading yet (e.g. right after
startup).

### `GET /diag` / `GET /sensors/<name>/diag`

The config and live output of **every** configured signal for the
default sensor, or the named one, regardless of `emit` — the full
diagnostic view that `/level`'s `signals` deliberately leaves out.

```json
{
  "signals": {
    "pond_main_sensor_raw": {
      "config": {"type": "sensor", "params": {"sensor": "pond_main", "unit": "cm"}, "primary": false, "emit": true, "unit": "cm", "mode": "raw"},
      "output": {"value": 11.3, "unit": "cm", "at": "2026-09-07T00:28:23.470621+00:00", "sensor": "pond_main", "mode": "raw"}
    },
    "pond_main_sensor_processed": {
      "config": {"type": "sensor", "params": {"sensor": "pond_main", "unit": "cm", "mode": "processed"}, "primary": false, "emit": true, "unit": "cm", "mode": "processed"},
      "output": {"value": 11.0, "unit": "cm", "at": "2026-09-07T00:28:22.093268+00:00", "sensor": "pond_main", "mode": "processed"}
    },
    "rolling_median5": {
      "config": {
        "type": "rolling_median",
        "input": "pond_main_sensor_raw",
        "params": {"window_size": 5},
        "primary": false,
        "emit": false,
        "unit": "cm",
        "mode": "raw"
      },
      "output": {
        "value": 11.2,
        "unit": "cm",
        "at": "2026-09-07T00:28:23.470621+00:00",
        "window_size": 5,
        "samples_in_window": 5
      }
    },
    "polling_rolling_avg": {
      "config": {
        "type": "polling_rolling_average",
        "input": "rolling_median5",
        "params": {"window_size": 60, "poll_interval_s": 1},
        "primary": true,
        "emit": true,
        "unit": "cm",
        "mode": "raw"
      },
      "output": {
        "value": 11.2,
        "unit": "cm",
        "at": "2026-09-07T00:28:23.470621+00:00",
        "window_size": 60,
        "samples_in_window": 60,
        "poll_interval_s": 1
      }
    }
  }
}
```

Each signal's `config` is its *effective* configuration from
`config/sensors.yaml`'s `signals:` list (defaults filled in, so
`primary`/`emit` are always present even if the YAML omitted them; a
non-`sensor` signal's `input` is included too), and `output` is the same
shape `/level`'s `signals` used to expose — `value` plus that
signal's own `extra_state()`. Returns `503 {"error": "no readings
yet"}` under the same condition as `/level`.

Every signal's `config`/`output` includes `unit` — the unit its own
`value` is actually in (e.g. `"cm"`), as declared in `params.unit` for
a `sensor`-type signal or derived automatically from `input` for every
other type. See [Signal processing](#signal-processing). `value` itself
is computed the same way regardless of `unit` (the signal's internal
number divided by 10) -- for `pond_main_sensor_raw`, `params.unit: cm`
is correct precisely because the A02YYUW's millimeter reading divided
by 10 *is* centimeters. A future sensor type whose native reading isn't
in millimeters would need `value`'s conversion itself to become
unit-aware; today `unit` accurately describes every signal's `value`,
but isn't yet wired into computing it.

`output` also includes `at` — an ISO 8601 UTC timestamp of when that
signal's `value` was last computed. `pond_main_sensor_raw` and
`pond_main_sensor_processed` above have visibly different `at` values
because they're rooted at different `mode`s (see [Signal
processing](#signal-processing)) and update on independent cadences —
`at` is how to tell a signal's value apart from stale, without needing
to separately poll `/health`.

### `GET /signals`

Lists every configured signal's name, across every sensor — signals
aren't nested under a sensor in `config/sensors.yaml` (see [Signal
processing](#signal-processing)), so unlike the routes above there's no
bare-vs-sensor-named distinction here; this is the one flat list:

```json
{
  "signals": ["pond_main_sensor_raw", "pond_main_sensor_processed", "rolling_median5", "polling_rolling_avg"]
}
```

### `GET /signals/<name>` / `GET /signals/<name>/diag`

A single named signal's current value, regardless of which sensor it's
rooted at or whether it's marked `emit: false` (unlike `/level`'s
`signals`, which is scoped to one sensor and excludes `emit: false`
entries):

```json
{
  "name": "polling_rolling_avg",
  "sensor": "pond_main",
  "value": 11.2,
  "unit": "cm",
  "at": "2026-09-07T00:28:23.470621+00:00",
  "window_size": 60,
  "samples_in_window": 60,
  "poll_interval_s": 1
}
```

`sensor` is which configured sensor this signal is ultimately rooted at
(traced through any `input:` chain back to a `sensor`-type signal's
`params.sensor`). `unit` is this signal's own configured/derived unit,
and `at` is when this `value` was last computed (see [Signal
processing](#signal-processing) for both) — every field past that is
this signal's own `extra_state()` alongside its value, varying by
signal type, same as `/diag`'s `output`.

`/signals/<name>/diag` returns this signal's effective `config` (as
`/diag` would show it) alongside that same `output`, instead of just
the flattened value:

```json
{
  "name": "polling_rolling_avg",
  "sensor": "pond_main",
  "config": {
    "type": "polling_rolling_average",
    "input": "rolling_median5",
    "params": {"window_size": 60, "poll_interval_s": 1},
    "primary": true,
    "emit": true,
    "unit": "cm",
    "mode": "raw"
  },
  "output": {
    "value": 11.2,
    "unit": "cm",
    "at": "2026-09-07T00:28:23.470621+00:00",
    "window_size": 60,
    "samples_in_window": 60,
    "poll_interval_s": 1
  }
}
```

Both return `404 {"error": "unknown signal '<name>'"}` for a name not
in `config/sensors.yaml`'s `signals:` list, and `503 {"error": "no
readings yet"}` under the same condition as `/level`.

### `POST /reset` / `POST /sensors/<name>/reset`

Power-cycles the default sensor, or the named one, to force a hardware
reset — for when it appears wedged/stuck (e.g. a stale, unchanging
reading) and the automatic serial buffer flush in `poll_sensor()` (see
`/health` below) hasn't resolved it on its own. Not every sensor driver
supports this — returns `501 {"error": "sensor does not support
reset"}` if the target sensor doesn't (checked via its `supports_reset`
capability flag; see [Sensor drivers](#sensor-drivers)). For the
A02YYUW, this drives its power pin (`power_pin` param, default `24`) low
for `sensor_power.RESET_OFF_DURATION_S` (1s) and back high, so the
request blocks for about that long.

```json
{
  "status": "reset",
  "sensor": "pond_main",
  "reset_at": "2026-09-05T22:05:40.132812+00:00"
}
```

`reset_at` is also recorded as that sensor's entry in `/health`'s
`sensors.<name>.last_reset_at` below. There's no readiness check
afterward — the sensor typically resumes producing valid frames within
its normal ~100-300ms response time, same as at startup.

Both the bare and sensor-named routes return `404
{"error": "unknown sensor '<name>'"}` for a name not in `config/sensors.yaml`.

### `GET /health`

Reports every configured sensor's status individually, plus overall
service info:

```json
{
  "status": "ok",
  "started_at": "2026-08-29T19:31:24.633421+00:00",
  "uptime_seconds": 93780.4,
  "uptime_human": "1d 2h 3m 0s",
  "commit_sha": "e1d742a9c2f4b1a0d3e5f6a7b8c9d0e1f2a3b4c5",
  "default_sensor": "pond_main",
  "sensors": {
    "pond_main": {
      "status": "ok",
      "poller_alive": true,
      "last_reading_age_s": 0.1,
      "last_reset_at": null,
      "signals": ["polling_rolling_avg", "pond_main_sensor_raw", "pond_main_sensor_processed"]
    }
  }
}
```

The top-level `status` is `"degraded"` (HTTP 503) if **any** configured
sensor's own `status` is degraded. Each sensor's `status` is degraded
under either of two independent conditions, both of which would
otherwise silently leave that sensor's `/level` serving stale data
forever with no signal anything was wrong:

- that sensor's background polling thread has died (`poller_alive: false`)
  — e.g. an unhandled exception in `poll_sensor()`.
- no valid "raw" reading has landed in over `STALE_READING_THRESHOLD_S`
  (3s, `server.py`) — the thread can be alive and still not be producing
  fresh readings, e.g. if a wiring disturbance knocks the A02YYUW
  driver's byte alignment out of sync in just the wrong way and its
  incremental header-hunting resync never lands on a fresh header. The
  driver itself watches for this same condition and forces a
  `reset_input_buffer()` once it's crossed, so in practice a stale
  reading should self-resolve within a few seconds — `last_reading_age_s`
  climbing past the threshold and staying there is the signal that
  didn't happen.

`last_reading_age_s` is seconds since that sensor's last valid frame, or
`null` before its first ever reading (not itself a degraded condition —
a poller that's alive but just hasn't read anything yet, e.g. right
after startup, is normal).

`last_reset_at` is when `POST /reset` (or `/sensors/<name>/reset`) last
power-cycled that sensor, or `null` if it's never been called since this
service started (not persisted across restarts).

`signals` is just the list of that sensor's configured signal names, as
a quick "did the config load correctly" signal — see `/level` for their
actual output.

`uptime_human` is `uptime_seconds` formatted as `"1d 2h 3m 4s"`. Units
below the largest non-zero one are always shown (so exactly one hour is
`"1h 0m 0s"`, not `"1h"`); under a minute it's just e.g. `"45s"`.

`commit_sha` is the full commit SHA that's currently deployed. In
production this comes from a `COMMIT_SHA` file written by the deploy
workflow (see `.github/workflows/deploy.yml`) — the SHA can't be read
from git directly on the device since `.git` is excluded from the
rsync'd deploy directory. Falls back to `git rev-parse HEAD` for local
dev checkouts, or `null` if neither is available.

## Sensor drivers

Every sensor implements the small `LevelSensor` interface (`sensors/base.py`):
`read()` returns a dict of canonical readings — distance from the
sensor's mount point down to the water surface, in millimeters, keyed by
a driver-defined signal name (e.g. the A02YYUW reports `"raw"` and
sometimes `"processed"`, see below) — and `close()`. `supports_reset`/
`reset()` is an optional capability a driver can add if its hardware can
actually be power-cycled or otherwise reset in software; `POST /reset`
checks this flag rather than assuming every sensor has it.

Different sensor technologies measure fundamentally different native
quantities with different sign conventions (an ultrasonic sensor's raw
distance vs. a resistive sensor's submerged length, say), so each driver
is responsible for converting its own reading into that shared
millimeters-to-surface unit before returning it — nothing downstream
(signals, the HTTP API) needs to know which sensing technology
produced a given value.

Sensor types are discovered dynamically at startup, the same way
[signal types](#signal-processing) are: each entry in `sensors/` whose
name ends in `_sensor` must define exactly one `LevelSensor` subclass
*and* a module-level `create(params, simulate)` function, and that
entry's name with the suffix stripped becomes the `type:` string used
in `config/sensors.yaml`. Unlike signals (whose constructors take
simple scalar params directly), most sensor drivers need real hardware
objects — a serial connection, GPIO controllers — assembled around
those params, and build entirely different (simulated) objects under
`--simulate`; `create()` is where a driver type does that assembly, so
`sensor_config.py` never needs to know a given type's own construction
details.

An entry can be either a single `<name>_sensor.py` file (the class and
`create()` defined directly in it) or a `<name>_sensor/` directory
package, for a driver whose logic is naturally split across several
source files that belong grouped together rather than scattered as
top-level `pondpi` modules (`sensors/a02yyuw_sensor/`'s
`read_sensor.py`/`sensor_mode.py`/`sensor_power.py` are a real
example — all A02YYUW-specific, none used by any other driver). For a
directory package, the class and `create()` can either be defined
directly in its `__init__.py` or defined in one of its own submodules
and re-exported from `__init__.py` — that's the module dynamic
discovery imports and scans either way. A helper submodule inside a
driver's own package (`sensor_mode.py`, say) is never itself mistaken
for a separate driver, the same way `sensors/base.py` isn't — neither
name ends in `_sensor`. Adding a new single-file sensor type means
writing `sensors/<name>_sensor.py` and referencing `type: <name>` in
`config/sensors.yaml`; a multi-file one means a `sensors/<name>_sensor/`
directory instead — nothing else to edit or register either way.

## Sensor notes

### A02YYUW waterproof ultrasonic sensor (UART, 9600 bps)

[DFRobot product page](https://www.dfrobot.com/product-1935.html)

| Spec | Value |
|---|---|
| Response time | ~100 ms |
| Ranging accuracy | ±1 cm |
| Measuring range | 3 cm – 450 cm |
| Blind zone | < 3 cm (`is_valid_reading` in `read_sensor.py` rejects readings ≤ 30 mm) |

These two numbers directly shape the polling and smoothing defaults:

- **Response time (100 ms) sets a polling floor.** The sensor only
  produces a genuinely new measurement every ~100 ms; polling the serial
  buffer faster than that doesn't get you more data, it gets you the
  *same* frame read back multiple times in a row (e.g. polling every
  20 ms would read each frame up to ~5 times). Those duplicate values
  create flat plateaus in the raw signal that distort both the median
  filter and the rolling average — they flatten real step-changes and
  can reintroduce a sawtooth pattern as the duplicates fall in and out
  of the windows together. `--polling-interval-ms` defaults to `150`
  (comfortably above 100 ms) so that every sample fed into the filters
  is an independent look at the water surface.
- **Ranging accuracy (±1 cm) sets a noise floor.** Any single reading
  can be off by up to 1 cm even with a perfectly still water surface, so
  don't expect (or chase) sub-centimeter precision out of
  `signals.pond_main_sensor_raw`. That's exactly what `primary_signal` is
  for — averaging readings down to a
  stabler value — but a rolling window so small that it's dominated by
  one or two ±1 cm outliers will still show that noise. Conversely,
  don't read too much into a rolling average that only moves by a few
  mm between samples; that can be within the sensor's own accuracy
  budget rather than a real water level change.

### RX pin: raw vs. processed hardware mode

The sensor's RX pin isn't a data line here (nothing is ever written to
it over UART) -- it's a mode-select input, per DFRobot's wiki: driven
**low** selects **real-time** ("raw") output, driven **high or left
floating** selects the sensor's own internally-smoothed ("processed")
output, response time ~100-300ms either way. This is a genuine hardware
behavior, not something `read_sensor.py`/`server.py` invent -- see
`sensor_mode.py`.

This deployment wires that pin to a GPIO (`mode_select_pin` param in
`config/sensors.yaml`, default `25`) so the A02YYUW driver can drive it
directly, rather than leaving it hardwired to one mode. It can't read
both modes at once from a single physical sensor, so it alternates:
mostly `raw` (`MODE_CYCLE_INTERVAL_S` / `PROCESSED_MODE_DURATION_S` in
`sensors/a02yyuw_sensor.py`, default 9s raw / 1s processed per 10s
cycle), briefly dipping into `processed` just often enough to keep that
reading fresh too. Frames read within `MODE_SETTLE_S` of a mode switch
are discarded rather than cached, since the sensor's response time means
a reading right after a switch can still reflect the *previous* mode.
See `GET /level`'s `?mode=` param above for how to read each stream.

An optional `read_mode` param (`"raw"` or `"processed"`; omitted keeps
the alternating cycle above) pins the driver permanently in one mode
instead — no cycling, no settling windows after the first frame, and
`read()` only ever reports that one key. This is a driver-level
override, distinct from (but easy to confuse with) a `sensor` signal's
own `params.mode` (see [Signal processing](#signal-processing)) —
`read_mode` controls which reading the driver ever *produces*;
`params.mode` controls which reading a given *signal* consumes. Pinning
`read_mode` to `"processed"` means only signals rooted at `mode:
processed` (e.g. `pond_main_sensor_processed`) ever get fed — the
default `raw`-rooted pipeline (`pond_main_sensor_raw`,
`polling_rolling_avg`, `/level`'s default view) never receives data,
since the driver never reports a `raw` reading at all. Pinning to
`"raw"` is the inverse: only `raw`-rooted signals get fed, and
`pond_main_sensor_processed`/`?mode=processed` never do.

One consequence worth knowing: because the `raw` pipeline (the one
feeding this sensor's `polling_rolling_avg` etc.) only actually gets
sensor data during its ~90% share of each cycle, a plain sample-count
window filled at a fixed poll rate would represent a correspondingly
longer wall-clock span than it would with continuous polling -- at the
defaults, the raw pipeline only gets fresh samples during ~86% of
wall-clock time (9s of `raw` per 10s cycle, minus `MODE_SETTLE_S` lost
right after switching back into it). `polling_rolling_avg` (`type:
polling_rolling_average`) sidesteps this: its `poll_interval_s` gates
on real elapsed time between *accepted* samples rather than a raw
sample count assumed to arrive at a fixed poll rate, so `window_size:
60` at `poll_interval_s: 1` stays a genuine ~60s window regardless of
how the raw pipeline's duty cycle drifts -- see [Signal
processing](#signal-processing).

### Power pin: software-triggered reset

The sensor's power supply is also wired through a GPIO (`power_pin`
param in `config/sensors.yaml`, default `24`) rather than a fixed
always-on rail, so it can be power-cycled from software via `POST /reset`
(above) instead of requiring someone to physically unplug it. That GPIO
is also driven high at boot
via `/boot/firmware/config.txt`'s `gpio=24=op,dh` directive, so the
sensor already has power before this service starts — `sensor_power.py`'s
`GpioPowerController` takes over control of that same pin at startup
(initialized high, matching its already-high boot state, so acquiring it
doesn't itself glitch the sensor's power).

## Signal processing

Signals live in their own top-level `signals:` list in
`config/sensors.yaml`, independent of the `sensors:` list — not nested
under a sensor. Only a `type: sensor` signal reads directly from a
sensor, named by `params.sensor`; every other signal reads another
signal's *live output* instead, named by a top-level `input:` key. This
is how sequential composition (e.g. median-then-average) is expressed —
no dedicated "chain" type needed, just two flat entries linked by
`input:`. Each configured sensor's `/level` shows the output of every
signal ultimately rooted at it (traced by following `input:` chains
back to whichever `sensor` signal names that sensor), side by side. This
makes it possible to compare smoothing approaches against the live
sensor stream without a code change or redeploy — just edit the YAML.

Signal types are discovered dynamically at server startup, not from a
hand-maintained registry: each file in `signals/` whose name ends in
`_signal` must define exactly one `LevelSignal` subclass, and the name
with that suffix stripped becomes the `type:` string used in the YAML.
Files that don't end in `_signal` (`base.py`, or any future non-signal
helper module) are ignored automatically — no hardcoded skip-list to
maintain. Adding a new signal type means writing
`signals/<name>_signal.py` and referencing `type: <name>` in the
`signals:` list — nothing else to edit or register.

Signals are unit-agnostic: `add()` takes a value in and returns a
processed value out, with no notion of mm/cm baked in anywhere. The
A02YYUW's own raw readings are physically in millimeters, and the fixed
conversion in `server.py`'s `_signal_output()` (dividing by 10) always
assumes that -- not inside any signal. A signal's own `unit` (above) is
what that division's *result* should be labeled, not what the sensor
natively reports; `params.unit: cm` on `pond_main_sensor_raw` is
correct precisely because dividing millimeters by 10 produces
centimeters.

Built-in `LevelSignal` types (`type:` in the YAML) and their `params`:

| Type | Params | Behavior |
|---|---|---|
| `sensor` | `sensor`, `unit`, `mode` | Passes the named sensor's reading through unchanged. The only type that connects to a sensor -- everything else uses `input:` instead. |
| `rolling_median` | `window_size` | Median-filters its input over a rolling window — rejects spikes/outliers. |
| `rolling_average` | `window_size` | Averages its input over a rolling window. `window_size` is a *sample* count, filled at the poll rate (`--polling-interval-ms`, default 150ms, shared by every configured sensor) — e.g. `window_size: 200` is a ~30s real-world window, not 200 downstream reads. Same reasoning as `exponential_smoothing` below: size it to the cadence something will actually observe `/level` at, not an arbitrary sample count. |
| `polling_rolling_average` | `window_size`, `poll_interval_s` | Like `rolling_average`, but only admits a new sample into the window once `poll_interval_s` has elapsed since the last one it accepted -- calls in between just return the current average unchanged. `window_size * poll_interval_s` is the real-world window, independent of the sensor's own poll rate, so it doesn't drift if the underlying pipeline's duty cycle changes (see [RX pin](#rx-pin-raw-vs-processed-hardware-mode) below) and doesn't need a large `window_size` to cover a long span. |
| `exponential_smoothing` | `alpha` | Exponentially-weighted moving average of its input — each new reading is weighted by `alpha` (0-1), with every prior reading's weight decaying geometrically by `(1 - alpha)`. Unlike a rolling window, there's no fixed window size: older readings are never fully dropped, just weighted down forever. Higher `alpha` tracks the latest reading more closely; lower `alpha` smooths more aggressively. |

Every type except `sensor` also requires a top-level `input: <name>`,
naming the signal (defined earlier in the file) whose output feeds it.

A `sensor` signal must also set `params.unit` (e.g. `"cm"`) -- the
unit its readings are actually in, required since it's the boundary
where a value enters the signal graph and nothing upstream can tell us
that. Every other signal type derives its `unit` automatically from
whichever signal its `input:` names, since none of them perform any
unit conversion -- a rolling average of centimeters is still in
centimeters -- and must not set `params.unit` itself (that raises a
config error, since it would silently be ignored otherwise). This is
reported on `/diag` and `/signals/<name>`; see those endpoints above.

A `sensor` signal may also set `params.mode` -- `"raw"` (the default)
or `"processed"`, picking which of the sensor's own named readings
feeds it (see `LevelSensor.read()` in [Sensor drivers](#sensor-drivers)
and the A02YYUW's two hardware modes in [Sensor
notes](#sensor-notes)). Every other signal type derives `mode` from
`input`, same as `unit`, and must not set it directly either.
Signals rooted at different modes update on genuinely independent
cadences -- see each one's own `at` timestamp (below) rather than
assuming two signals shown together on `/level` or `/diag` were
computed at the same moment. Exactly one signal per sensor is marked
`primary: true`, and it must be rooted at `mode: "raw"` -- /level's
default view and /health's staleness check are both built around that
pipeline's cadence specifically.

Every signal's `/diag`/`/signals/<name>` output also includes `at` — an
ISO 8601 UTC timestamp of when that signal's `value` was last computed,
letting a caller tell a signal's freshness apart from another's without
a separate `/health` request.

`exponential_smoothing`'s `alpha` gets applied once per sensor poll
(every `--polling-interval-ms`, default 150ms) — not once per reading of
`/level` by a downstream consumer. An EMA's half-life in *samples* is
roughly `ln(0.5) / ln(1 - alpha)`; at a 150ms feed rate, `alpha` values
of 0.1-0.9 all decay to a half-life under a second, which is invisible
next to something polling `/level` every 30s (e.g. Home Assistant's
default `scan_interval`) — the value it reads back has already forgotten
everything older than a second regardless of which of those alphas was
configured. To get smoothing that's actually visible at a given polling
cadence, pick `alpha` so the half-life (`0.15 * ln(0.5)/ln(1-alpha)`
seconds, at the default polling interval) lands near that cadence —
roughly `alpha` in the 0.01-0.2 range for a 10-30s cadence.

Example `config/sensors.yaml` (see [Sensor drivers](#sensor-drivers) for
the `sensors:` entry's own `name`/`type`/`params` fields):

```yaml
sensors:
  - name: pond_main
    type: a02yyuw
    default: true
    params:
      serial_port: /dev/serial0
      mode_select_pin: 25
      power_pin: 24

signals:
  - name: pond_main_sensor_raw
    type: sensor
    params:
      sensor: pond_main
      unit: cm
  - name: pond_main_sensor_processed
    type: sensor
    params:
      sensor: pond_main
      unit: cm
      mode: processed
  - name: rolling_median5
    type: rolling_median
    input: pond_main_sensor_raw
    emit: false
    params:
      window_size: 5
  - name: polling_rolling_avg
    type: polling_rolling_average
    input: rolling_median5
    primary: true
    params:
      window_size: 60
      poll_interval_s: 1
```

Here `polling_rolling_avg` reads `rolling_median5`'s output, which in
turn reads `pond_main_sensor_raw`'s output (the sensor's raw reading) —
a median-then-average pipeline built entirely from `input:` references,
with each stage its own independently named signal.
`pond_main_sensor_processed` is unrelated to that pipeline: a second,
independent `sensor` signal rooted at the same sensor's `processed`
reading instead, updating on its own cadence (see the A02YYUW's
raw/processed hardware-mode cycling in [Sensor
notes](#sensor-notes)).

Exactly one signal rooted at each sensor must be marked `primary: true`.
Its output becomes `primary_signal` in that sensor's `/level`, and it's
also reachable directly at `/signals/<name>` (see above) — **the
deployed Home Assistant "Pond Level Rolling Avg" sensor reads it that
way**, a `rest` sensor in Home Assistant's `configuration.yaml` polling
`http://<pi-host>:8080/signals/polling_rolling_avg` every 60s via
`value_json.value`, so renaming or repurposing the default sensor's
`primary` signal means updating that HA sensor's `resource`/
`value_template` too. `signals.pond_main_sensor_raw` is unaffected by
other signals — it's always the raw last-valid reading — and is what
`sensor.pond_level_sensor_raw` reads (via
`http://<pi-host>:8080/signals/pond_main_sensor_raw`'s own
`value_json.value`).

Any signal can also set `emit: false` (default `true`) to keep it out of
`/level`'s `signals` section — the curated "final output values" view —
while it still shows up in full on `/diag`. Use this for a signal that
only exists as an intermediate stage feeding another signal (like
`rolling_median5` above) and isn't a meaningful output on its own.

## Configuration

Which sensors exist, their hardware wiring (serial port, GPIO pins), and
their signal processing all live in `config/sensors.yaml` — see [Sensor
drivers](#sensor-drivers) and [Signal processing](#signal-processing)
above. Everything else is a CLI flag to `pondpi-server`, not an
environment variable — a single boolean/numeric mode switch is more
visible this way (shows up in `ps aux` and the systemd unit's `ExecStart`
line), so there's no risk of a stray inherited env var silently changing
behavior.

| Flag | Default | Meaning |
|---|---|---|
| `--sensors-config` | `config/sensors.yaml` (relative to the working directory) | Path to the YAML file configuring sensors and their level-processing pipelines. |
| `--polling-interval-ms` | `150` | How often (ms) every configured sensor is checked for a new reading. Shared by all sensors, not per-sensor. |
| `--host` | `0.0.0.0` | Address the HTTP server binds to. |
| `--port` | `8080` | Port the HTTP server binds to. |
| `--simulate` | off | Build every configured sensor in simulated mode (e.g. the A02YYUW driver uses `SimulatedSerial` — synthetic sine-wave + noise data — and no-op mode/power controllers) instead of opening real hardware. For local development with no sensor hardware attached. |

`--sensors-config`'s default (and where `/health`'s `commit_sha` resolves
from) is relative to the working directory, not the installed package's
location — this only works because `WorkingDirectory` is always set
explicitly: `/opt/pondpi` in `deploy/pondpi.service`, and the repo root
by convention for local dev (see below).

Change the deployed configuration by editing `config/sensors.yaml` (for
sensor wiring or smoothing) or `ExecStart` in `deploy/pondpi.service`
(for everything else), e.g.:

```
ExecStart=/opt/pondpi/.venv/bin/pondpi-server --polling-interval-ms 200
```

Don't set `--polling-interval-ms` below ~100 — see [Sensor notes](#sensor-notes)
above for why.

## Developing locally

No Raspberry Pi or sensor hardware required — `--simulate` swaps in a fake
serial source that generates valid, correctly-checksummed A02YYUW frames
with a distance that wanders on a sine wave plus noise, running through
the exact same parsing/averaging code path as production.

Run these from the repo root — `pondpi-server`'s default config/commit-SHA
paths are relative to the working directory (see [Configuration](#configuration)
above).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

pondpi-server --simulate
# in another terminal:
curl http://localhost:8080/level
curl http://localhost:8080/health
```

`pip install -e ".[dev]"` is an editable install, so changes to files
under `src/pondpi/` take effect immediately — no reinstall needed. It
also puts the `pondpi-server` command on your `PATH` (equivalently, run
`python -m pondpi.server` directly).

Useful while developing: a small `window_size` on a `rolling_average`/
`rolling_median` signal in `config/sensors.yaml`'s `signals:` list (see
the average react faster) and `--polling-interval-ms 200` (slow the
stream down to read it by eye). Pass `--sensors-config` to point at an
alternate YAML file without touching the checked-in one.

## Testing

```bash
pytest -q      # unit tests
ruff check .   # lint
```

Tests live in `tests/`, import from the installed `pondpi` package (e.g.
`from pondpi.signals.utils.rolling_median_filter import RollingMedianFilter`),
and don't need any
hardware or network access — `tests/test_read_sensor.py`,
`tests/test_a02yyuw_sensor.py` (the A02YYUW driver, including its
`create()` factory under `--simulate`), `tests/test_rolling_average.py`,
`tests/test_signals.py` (including the dynamic-discovery mechanism
itself, against both the real `signals/` package and small synthetic
ones built in `tmp_path`), `tests/test_signal_config.py` and
`tests/test_sensor_config.py` (both using `tmp_path` YAML files)
exercise pure functions directly, and `tests/test_server.py` uses
Flask's test client against `pondpi.server.app` with
`pondpi.server._state`/`_sensors` (both keyed by sensor name) set
directly.

Bare `pytest` works fine (no `python -m` needed) as long as you've
`pip install -e`'d the package first — `pondpi` resolves via the editable
install, not a `sys.path`/cwd trick.

## CI/CD

- **Every PR** into `main` runs `.github/workflows/ci.yml`: `lint` (ruff)
  and `test` (pytest). Both must pass — `main` is a protected branch
  requiring a PR even for repo admins.
- **On merge to `main`**, `.github/workflows/deploy.yml` runs on a
  self-hosted GitHub Actions runner installed on the Pi itself (works
  behind NAT/firewall, no inbound access needed):
  1. **Staging smoke test** — installs into a separate venv
     (`/opt/pondpi-staging-venv`) and runs `py_compile` + `pytest` against
     the freshly-checked-out code. Since there's only one physical Pi,
     this is the practical equivalent of a staging environment: nothing
     below this step touches the live service unless it passes.
  2. **Sync** the repo into `/opt/pondpi` (excluding `.git`, `.github`,
     `deploy/`, and `.venv`).
  3. **Install dependencies** — an editable install
     (`pip install -e /opt/pondpi`) of the `pondpi` package into
     `/opt/pondpi/.venv`, so `/opt/pondpi/src/pondpi/` stays the literal
     running code (same "sync source, restart service" model as before
     the switch to a proper package).
  4. **Update systemd unit** — copies `deploy/pondpi.service` into
     `/etc/systemd/system/` and reloads systemd, so changes to the unit
     file itself (not just the code) take effect.
  5. **Restart** `pondpi.service`.

## Deployment

See [`deploy/SETUP.md`](deploy/SETUP.md) for the one-time setup required
on a Pi before the pipeline above can deploy to it: creating `/opt/pondpi`
and its venv, installing the systemd unit, the sudoers rule that lets the
runner restart the service without a password, and registering the
self-hosted GitHub Actions runner.
