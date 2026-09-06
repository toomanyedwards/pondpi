# PondPi

Measures water level with one or more sensors (currently the A02YYUW
waterproof ultrasonic sensor over UART, with the driver layer designed to
support other sensor types too) on a Raspberry Pi, and exposes every
sensor's current reading over a small HTTP API.

## How it works

Each configured sensor is driven by its own `LevelSensor` driver instance
(see [Sensor drivers](#sensor-drivers) below) polled on its own background
thread. A driver's readings are run through that sensor's own configured
`LevelSignalProcessor` instances (see [Signal processing](#signal-processing));
a Flask server exposes every sensor's output on `GET /level` (the
configured *default* sensor) and `GET /sensors/<name>/level` (any sensor,
by name).

```
┌────────────────┐  read()  ┌──────────────────┐  add()  ┌────────────────────────────┐
│ LevelSensor      │ ───────>│ poll_sensor()      │───────> │ that sensor's configured     │
│ driver (sensors/)│         │ (one thread/sensor)│         │ LevelSignalProcessor instances│
└────────────────┘         └──────────┬─────────┘         └──────────────┬─────────────┘
        ▲ one instance per                │ writes that sensor's own state              │
        │ config/sensors.yaml entry       v                                            v
        └──────────────────────  Flask app: GET /level, GET /sensors, GET /sensors/<name>/level,
                                  GET /health, GET /diag, POST /reset
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
│   ├── sensors/              # one LevelSensor subclass per <type>_sensor.py file
│   │   ├── base.py            # LevelSensor interface
│   │   └── a02yyuw_sensor.py  # A02YYUW driver -- wraps read_sensor.py + sensor_mode.py + sensor_power.py
│   ├── sensor_config.py
│   ├── read_sensor.py
│   ├── sensor_mode.py
│   ├── sensor_power.py
│   ├── signal_processors/    # one LevelSignalProcessor subclass per <type>_processor.py file
│   │   └── utils/             # RollingMedianFilter, RollingAverage -- generic building blocks,
│   │                           # not signal processors themselves, see below
│   ├── signal_processor_config.py
│   ├── commit_sha.py
│   └── duration.py
├── tests/                   # mirrors src/pondpi/, not shipped/deployed as code
├── deploy/                  # systemd unit + one-time Pi setup docs
└── .github/workflows/       # CI (per-PR) + Deploy (on merge to main)
```

| File | Responsibility |
|---|---|
| `sensors/base.py` | `LevelSensor` — the interface every driver implements. `read()` returns canonical `{signal_name: distance_mm}` readings (distance from the sensor's mount point down to the water surface — different sensor technologies measure fundamentally different native quantities, so each driver converts its own before returning). `supports_reset`/`reset()` is an optional per-driver capability, not assumed universal. See [Sensor drivers](#sensor-drivers). |
| `sensors/a02yyuw_sensor.py` | `A02YYUWSensor` — the A02YYUW driver. Consolidates UART frame reading, hardware raw/processed mode-cycling, and stale-buffer resync (built on `read_sensor.py`/`sensor_mode.py`/`sensor_power.py`). Reports `"raw"` and `"processed"` named signals. Discovered dynamically like signal processors — see [Sensor drivers](#sensor-drivers). |
| `sensor_config.py` | `load_sensors()` — reads `config/sensors.yaml` into named sensors, each bundled with its driver instance and its own signal processor pipeline. |
| `read_sensor.py` | A02YYUW protocol/hardware layer only: checksum validation, frame parsing, a single instantaneous `read_frame(ser)` call, and `SimulatedSerial` (a fake serial source for local dev). No smoothing, no I/O loop, no knowledge of anything beyond one raw frame. |
| `sensor_mode.py` | Drives the A02YYUW's RX/mode-select pin — see [Sensor notes](#sensor-notes). `GpioModeController` (real GPIO via `gpiozero`) and `NullModeController` (no-op, used for `--simulate` and in tests). |
| `sensor_power.py` | Drives the A02YYUW's power supply pin for `POST /reset` — see [Sensor notes](#sensor-notes). `GpioPowerController` (real GPIO via `gpiozero`) and `NullPowerController` (no-op, used for `--simulate` and in tests). |
| `signal_processors/` | `LevelSignalProcessor` base class (`base.py`) and its built-in implementations, one per file, each named `<type>_processor.py` (`raw_processor.py`, `rolling_median_processor.py`, `rolling_average_processor.py`, `exponential_smoothing_processor.py`, `chain_processor.py`) — see [Signal processing](#signal-processing). |
| `signal_processors/utils/` | `RollingMedianFilter` and `RollingAverage` — generic building blocks used internally by some `LevelSignalProcessor` classes. Not signal processors themselves (they don't implement the `LevelSignalProcessor` interface), so they live in a subpackage that dynamic discovery ignores — its name doesn't end in `_processor`. |
| `signal_processor_config.py` | `load_signal_processors()`/`build_processors()` — builds named `LevelSignalProcessor` instances from a `processors:` list (a standalone file, or nested under a sensor in `config/sensors.yaml`). |
| `commit_sha.py` | `read_commit_sha()` — resolves the deployed commit SHA for `/health`. |
| `duration.py` | `format_duration()` — formats a seconds count as `"1d 2h 3m 4s"` for `/health`'s `uptime_human`. |
| `server.py` | Service entrypoint (`pondpi-server`). Starts one background polling thread per configured sensor and the Flask app. Owns all CLI configuration. |

## API

Every endpoint below except `GET /sensors` has two forms: a bare route
(`/level`, `/diag`, `POST /reset`) that operates on the *default* sensor —
the one entry in `config/sensors.yaml` marked `default: true` — and a
sensor-named route (`/sensors/<name>/level`, `/sensors/<name>/diag`,
`POST /sensors/<name>/reset`) that operates on any configured sensor by
name, default or not. A single-sensor deployment's existing integrations
(e.g. Home Assistant's REST sensors, built against the bare routes) keep
working unchanged as more sensors are added, as long as that original
sensor stays marked default.

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
    "name": "rolling_avg"
  },
  "signals": {
    "rolling_avg": 11.2,
    "instantaneous_raw": 11.3
  }
}
```

| Field | Meaning |
|---|---|
| `measure_name` | What this endpoint measures — always `"level"`. Self-describing metadata, useful if the response is logged or forwarded without the URL for context. |
| `units` | The unit every `_cm`/`value` field in this response is in — always `"cm"`. |
| `mode` | Which of the sensor's two hardware output modes this response reflects — see `?mode=` below. |
| `polling_interval_ms` | How often the poller checks the serial buffer for a new frame (see `--polling-interval-ms`). This is the poll rate, not necessarily the sensor's own update rate. |
| `primary_signal` | `{value, name}` for whichever processor is marked `primary: true` — `name` is that processor's actual configured name, so this stays correct even if you rename it. |
| `signals` | A curated `{name: distance_cm}` view of just the processors meant to be read as final output — every configured processor *except* whichever ones are marked `emit: false` in this sensor's `processors:` list in `config/sensors.yaml` (e.g. an intermediate stage that only exists to feed a `chain`). See [Signal processing](#signal-processing). |

`rolling_median5` (see [Signal processing](#signal-processing)) doesn't
appear here — it's marked `emit: false` since it only exists to feed
`rolling_avg`'s chain, not as a meaningful output on its own. Its full
state is still visible on `/diag`.

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
through this sensor's `processors:` pipeline at all (it's already
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

The config and live output of **every** configured signal processor for
the default sensor, or the named one, regardless of `emit` — the full
diagnostic view that `/level`'s `signals` deliberately leaves out.

```json
{
  "processors": {
    "rolling_median5": {
      "config": {
        "type": "rolling_median",
        "params": {"window_size": 5},
        "primary": false,
        "emit": false
      },
      "output": {
        "distance_cm": 11.2,
        "window_size": 5,
        "samples_in_window": 5
      }
    },
    "rolling_avg": {
      "config": {
        "type": "chain",
        "params": {
          "steps": [
            {"ref": "rolling_median5"},
            {"type": "rolling_average", "params": {"window_size": 200}}
          ]
        },
        "primary": true,
        "emit": true
      },
      "output": {
        "distance_cm": 11.2,
        "steps": [
          {"processor": "rolling_median5", "window_size": 5, "samples_in_window": 5},
          {"processor": "rolling_average", "window_size": 200, "samples_in_window": 200}
        ]
      }
    },
    "instantaneous_raw": {
      "config": {"type": "raw", "params": {}, "primary": false, "emit": true},
      "output": {"distance_cm": 11.3}
    }
  }
}
```

Each processor's `config` is its *effective* configuration from this
sensor's `processors:` list in `config/sensors.yaml` (defaults filled in,
so `primary`/`emit` are always present even if the YAML omitted them),
and `output` is the same shape `/level`'s `signals`/`processors` used to
expose — `distance_cm` plus that processor's own `extra_state()`.
Returns `503 {"error": "no readings yet"}` under the same condition as
`/level`.

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
      "processors": ["rolling_avg", "instantaneous_raw"]
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

`processors` is just the list of that sensor's configured processor
names, as a quick "did the config load correctly" signal — see `/level`
for their actual output.

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
(signal processors, the HTTP API) needs to know which sensing technology
produced a given value.

Sensor types are discovered dynamically at startup, the same way
[signal processor types](#signal-processing) are: each file in
`sensors/` whose name ends in `_sensor` must define exactly one
`LevelSensor` subclass *and* a module-level `create(params, simulate)`
function, and the filename with that suffix stripped becomes the
`type:` string used in `config/sensors.yaml`. Unlike signal processors
(whose constructors take simple scalar params directly), most sensor
drivers need real hardware objects — a serial connection, GPIO
controllers — assembled around those params, and build entirely
different (simulated) objects under `--simulate`; `create()` is where a
driver type does that assembly, so `sensor_config.py` never needs to
know a given type's own construction details. Adding a new sensor type
means writing `sensors/<name>_sensor.py` and referencing `type: <name>`
in `config/sensors.yaml` — nothing else to edit or register.

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
  `signals.instantaneous_raw`. That's exactly what `primary_signal` is
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

One consequence worth knowing: because the `raw` pipeline (the one
feeding this sensor's `rolling_avg` etc.) only actually gets sensor data
during its ~90% share of each cycle, a `rolling_avg` window sized in
samples at the poll rate (see above) now represents a correspondingly
longer wall-clock span than it would with continuous polling -- at the
defaults, the raw pipeline only gets fresh samples during ~86% of
wall-clock time (9s of `raw` per 10s cycle, minus `MODE_SETTLE_S` lost
right after switching back into it), so a "~60s window" (`rolling_avg`'s
`window_size: 400` in `config/sensors.yaml`) is closer to ~70s in
practice. Not large enough to bother retuning, but worth remembering if
that math is ever redone.

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

Each sensor's raw readings are run through every signal processor
configured in its own `processors:` list in `config/sensors.yaml` (a
strategy pattern — one class per algorithm, in
`src/pondpi/signal_processors/`), and every processor's output is
returned side by side on that sensor's `/level`. This makes it possible
to compare smoothing approaches against the live sensor stream without a
code change or redeploy — just edit the YAML.

Signal processor types are discovered dynamically at server startup, not
from a hand-maintained registry: each file in `signal_processors/` whose
name ends in `_processor` must define exactly one `LevelSignalProcessor`
subclass, and the name with that suffix stripped becomes the `type:`
string used in the YAML. Files that don't end in `_processor` (`base.py`,
or any future non-processor helper module) are ignored automatically —
no hardcoded skip-list to maintain. Adding a new signal processor means
writing `signal_processors/<name>_processor.py` and referencing
`type: <name>` in a sensor's `processors:` list — nothing else to edit or
register.

Signal processors are unit-agnostic: `add()` takes a raw value in and
returns a processed value out, with no notion of mm/cm baked in anywhere.
Millimeter readings from the sensor go in, and whatever comes out is only
interpreted as millimeters (and converted to cm) at the HTTP layer in
`server.py`'s `/level` route — not inside any signal processor.

Built-in `LevelSignalProcessor` types (`type:` in the YAML) and their `params`:

| Type | Params | Behavior |
|---|---|---|
| `raw` | none | Passes the raw reading through unchanged. |
| `rolling_median` | `window_size` | Median-filters the raw reading over a rolling window — rejects spikes/outliers. |
| `rolling_average` | `window_size` | Averages the raw reading over a rolling window. `window_size` is a *sample* count, filled at the poll rate (`--polling-interval-ms`, default 150ms, shared by every configured sensor) — e.g. `window_size: 200` is a ~30s real-world window, not 200 downstream reads. Same reasoning as `exponential_smoothing` below: size it to the cadence something will actually observe `/level` at, not an arbitrary sample count. |
| `exponential_smoothing` | `alpha` | Exponentially-weighted moving average — each new reading is weighted by `alpha` (0-1), with every prior reading's weight decaying geometrically by `(1 - alpha)`. Unlike a rolling window, there's no fixed window size: older readings are never fully dropped, just weighted down forever. Higher `alpha` tracks the latest reading more closely; lower `alpha` smooths more aggressively. |
| `chain` | `steps` | Runs a value through other processors in sequence, feeding each stage's output into the next. See below. |

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

`chain`'s `steps` is a list where each entry is either `ref: <name>` —
reuses another processor's `type`/`params` to build a fresh, **independent**
instance (a config alias, never the literal same object, so state is
never shared between the two) — or an inline `type:`/`params:`, built
directly (recursively, so a step can itself be a chain). This is how
today's production pipeline (rolling-median-then-rolling-average) is
built, without needing a dedicated hardcoded class for it.

Example sensor entry in `config/sensors.yaml` (see [Sensor
drivers](#sensor-drivers) for the `name`/`type`/`params` fields
surrounding `processors:`):

```yaml
sensors:
  - name: pond_main
    type: a02yyuw
    default: true
    params:
      serial_port: /dev/serial0
      mode_select_pin: 25
      power_pin: 24
    processors:
      - name: rolling_median5
        type: rolling_median
        emit: false
        params:
          window_size: 5
      - name: rolling_avg
        type: chain
        primary: true
        params:
          steps:
            - ref: rolling_median5
            - type: rolling_average
              params:
                window_size: 200
      - name: instantaneous_raw
        type: raw
```

Exactly one entry per sensor must be marked `primary: true`. Its output
becomes `primary_signal` in that sensor's `/level` — **the deployed Home
Assistant "Pond Level" sensor reads that field**
(`sensor.pond_level`, a `rest` sensor in Home Assistant's
`configuration.yaml` polling `http://pondpi.lan:8080/level` every 30s via
`value_json.primary_signal.value`), so don't remove or repurpose the
default sensor's `primary` processor without updating that HA sensor's
`value_template` too. `signals.instantaneous_raw` is unaffected by
processors — it's always the raw last-valid reading — and is what
`sensor.pond_level_sensor_raw` reads (via
`value_json.signals.instantaneous_raw`).

Any entry can also set `emit: false` (default `true`) to keep it out of
`/level`'s `signals` section — the curated "final output values" view —
while it still shows up in full on `/diag`. Use this for a processor
that only exists as an intermediate stage feeding a `chain` (like
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

Useful while developing: a small `rolling_window_size` in a sensor's
`processors:` list in `config/sensors.yaml` (see the average react
faster) and `--polling-interval-ms 200` (slow the stream down to read it
by eye). Pass `--sensors-config` to point at an alternate YAML file
without touching the checked-in one.

## Testing

```bash
pytest -q      # unit tests
ruff check .   # lint
```

Tests live in `tests/`, import from the installed `pondpi` package (e.g.
`from pondpi.signal_processors.utils.rolling_median_filter import RollingMedianFilter`),
and don't need any
hardware or network access — `tests/test_read_sensor.py`,
`tests/test_a02yyuw_sensor.py` (the A02YYUW driver, including its
`create()` factory under `--simulate`), `tests/test_rolling_average.py`,
`tests/test_signal_processors.py` (including the dynamic-discovery
mechanism itself, against both the real `signal_processors/` package and
small synthetic ones built in `tmp_path`), `tests/test_signal_processor_config.py`
and `tests/test_sensor_config.py` (both using `tmp_path` YAML files)
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
