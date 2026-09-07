# PondPi

Measures water level with one or more sensors (currently the A02YYUW
waterproof ultrasonic sensor over UART, with the driver layer designed to
support other sensor types too) on a Raspberry Pi, and exposes every
sensor's current reading over a small HTTP API.

## How it works

Nothing is ever pushed. Every `Sensor` and `Signal` (see [Sensor
drivers](#sensor-drivers) and [Signal processing](#signal-processing)
below) keeps a cache of its own last known value, and anything
downstream reads it *on demand* -- a `Signal`'s `read()` pulls
recursively from whatever it names as its `source:` (another signal's
own `read()`, or, for a `sensor`-type signal, its sensor's own
`read()`, passing that signal's own settings -- just its `mode` -- in
as the caller-settings `Sensor.read()` takes), computing lazily the
moment something actually asks and caching the result so a repeat
`read()` with no new upstream data
is cheap and doesn't recompute. The one exception, `rolling_average`,
owns a background thread that proactively samples its `source:` on its
own schedule and writes the cache directly instead of waiting to be
asked, overriding `read()` itself to just return whatever that thread
last wrote -- everything else is lazy. Most `Sensor` drivers (e.g.
`A02YYUWSensor`) also own a background
thread, for the more fundamental reason that *something* has to
actually poll the hardware -- but that thread only ever writes its own
cache (`_record_reading()`), it never reaches into any signal. Either
way, a background thread is each type's own choice, not something the
base class hands it -- a future driver or signal type with no need to
poll wouldn't have to fight any inherited thread machinery to avoid it.
`server.py` just holds onto the fully-built sensors and signals
`load_sensors()` hands it and calls `read()` on whichever one an
incoming HTTP request asks about (`GET /signals/<name>`, plus
per-sensor diagnostic and control routes: `GET /diag`, `POST /reset`).

```
sensor_config.py's load_sensors():
  1. construct every sensor first (a reads_from_sensor signal needs a
     live Sensor object to pull from) -- construction alone starts
     each driver's own background read thread, writing straight into
     its own last_reading() cache as frames arrive
  2. build every signal for those sensors (signal_config.py), each one
     holding either the sensor object or the source signal object it
     reads from -- rolling_average additionally starts its own
     background thread right away, pulling its source's read() on its
     own schedule (its own read() override just returns whatever that
     thread last wrote)

  ┌───────────────────────┐          ┌──────────────────────────┐          ┌───────────────────────────┐
  │ Sensor driver           │          │ sensor-type Signal          │          │ chain Signal (median,        │
  │ own read thread writes    │ <────── │ read({"mode": ...}), lazily    │ <────── │ average, EMA...) pulls          │
  │ last_reading() as frames     │ read() │ on demand -- or, for rolling_ │ read() │ source.read() lazily, on demand,   │
  │ arrive                          │          │ average, on its own timer      │          │ unless its own read() override        │
  └───────────────────────┘          └──────────────────────────┘          │ says otherwise (rolling_average)          │
                                                                              └───────────────────────────┘
                                                                                    ^
                                                                                    │ read()
                                                                              server.py / another chain Signal

Flask app (server.py) -- no threads, just calls read()/last_reading()
on already-running sensors/signals: GET /sensors, GET /health, GET
/diag, POST /reset, GET /signals, GET /signals/<name>, GET
/signals/<name>/diag
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
│   ├── sensors/              # one Sensor subclass per <type>_sensor.py file or <type>_sensor/ dir
│   │   ├── base.py            # Sensor interface
│   │   └── a02yyuw_sensor/    # A02YYUW driver -- multi-file, so it's a package, not a single file
│   │       ├── __init__.py      # A02YYUWSensor + create() -- the discovered entry point
│   │       ├── read_sensor.py   # protocol/hardware layer: checksum, frame parsing, SimulatedSerial
│   │       ├── sensor_mode.py   # drives the mode-select pin
│   │       └── sensor_power.py  # drives the power supply pin
│   ├── sensor_config.py
│   ├── signals/               # one Signal subclass per <type>_signal.py file
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
| `sensors/base.py` | `Sensor` — the interface every driver implements. `read(settings)` returns `{"value": distance_mm, "at": ...}` (distance from the sensor's mount point down to the water surface — different sensor technologies measure fundamentally different native quantities, so each driver converts its own before recording it) for whatever the caller's own `settings` selects. Defines the contract (readings, capability flags, health/reset tracking) but has no notion of *how* a driver obtains a reading -- no polling loop of its own; a driver that polls (like `A02YYUWSensor`) implements that itself and calls `_record_reading()` to participate in health tracking. `supports_reset`/`reset_hardware()` is an optional per-driver capability, not assumed universal. See [Sensor drivers](#sensor-drivers). |
| `sensors/a02yyuw_sensor/` | The A02YYUW driver, as a directory package rather than a single file since its logic naturally splits across several source files — see [Sensor drivers](#sensor-drivers) for how dynamic discovery finds either shape. |
| `sensors/a02yyuw_sensor/__init__.py` | `A02YYUWSensor` — consolidates UART frame reading, hardware raw/processed mode-cycling, and stale-buffer resync (built on this package's own `read_sensor.py`/`sensor_mode.py`/`sensor_power.py`). Reports `"raw"` and `"processed"` named signals. This is the module dynamic discovery imports and scans for the driver's `Sensor` subclass + `create()`. |
| `sensors/a02yyuw_sensor/read_sensor.py` | A02YYUW protocol/hardware layer only: checksum validation, frame parsing, a single instantaneous `read_frame(ser)` call, and `SimulatedSerial` (a fake serial source for local dev). No smoothing, no I/O loop, no knowledge of anything beyond one raw frame. |
| `sensors/a02yyuw_sensor/sensor_mode.py` | Drives the RX/mode-select pin — see [Sensor notes](#sensor-notes). `GpioModeController` (real GPIO via `gpiozero`) and `NullModeController` (no-op, used for `--simulate` and in tests). |
| `sensors/a02yyuw_sensor/sensor_power.py` | Drives the power supply pin for `POST /reset` — see [Sensor notes](#sensor-notes). `GpioPowerController` (real GPIO via `gpiozero`) and `NullPowerController` (no-op, used for `--simulate` and in tests). |
| `sensor_config.py` | `load_sensors()` — reads `config/sensors.yaml` into named sensors, each bundled with its driver instance and its own signal pipeline. Constructs each sensor's driver *before* its signals, since a `reads_from_sensor` signal needs a live `Sensor` object to pull from. |
| `signals/` | `Signal` base class (`base.py`) — owns the thread-safe pull-and-cache `read()` every signal type shares, plus its built-in implementations, one per file, each named `<type>_signal.py` (`sensor_signal.py`, `rolling_median_signal.py`, `rolling_average_signal.py`, `exponential_smoothing_signal.py`) — see [Signal processing](#signal-processing). |
| `signals/utils/` | `RollingMedianFilter` and `RollingAverage` — generic building blocks used internally by some `Signal` classes. Not signals themselves (they don't implement the `Signal` interface), so they live in a subpackage that dynamic discovery ignores — its name doesn't end in `_signal`. |
| `signal_config.py` | `load_signals()`/`build_signals()` — builds named `Signal` instances from `config/sensors.yaml`'s top-level `signals:` list and groups them by which sensor each is ultimately rooted at (tracing `source:` chains back to a `sensor` signal's own `source`). |
| `commit_sha.py` | `read_commit_sha()` — resolves the deployed commit SHA for `/health`. |
| `duration.py` | `format_duration()` — formats a seconds count as `"1d 2h 3m 4s"` for `/health`'s `uptime_human`. |
| `server.py` | Service entrypoint (`pondpi-server`). Builds every sensor and signal (already running their own background threads by the time `load_sensors()` returns) and runs the Flask app -- no thread/loop code of its own. Owns all CLI configuration. |

## API

Two families of routes here. Sensor-scoped routes (`/diag`, `POST
/reset`, and `GET /sensors`) mostly address one sensor by name
(`/sensors/<name>/diag`, `POST /sensors/<name>/reset`); the bare forms
(`/diag`, `POST /reset`) instead span *every* configured sensor at once,
keyed by name in the response, rather than picking one implicitly.

Signal-scoped routes (`GET /signals`, `GET /signals/<name>`, `GET
/signals/<name>/diag`) have no such bare-vs-named split, since signals
aren't nested under a sensor at all (see [Signal
processing](#signal-processing)) — each signal is addressed directly by
its own globally-unique name.

### `GET /sensors`

Lists every configured sensor's name:

```json
{
  "sensors": ["pond_main"]
}
```

### `GET /sensors/<name>/diag`

The config and live output of **every** configured signal for this one
sensor -- the full diagnostic view of its whole signal pipeline in one
call, as opposed to `GET /signals/<name>` (below) which addresses
exactly one signal directly.

```json
{
  "signals": {
    "pond_main_sensor_raw": {
      "config": {"type": "sensor", "source": "pond_main", "settings": {"unit": "cm"}, "emit": true, "unit": "cm", "mode": "raw"},
      "output": {"value": 11.3, "unit": "cm", "at": "2026-09-07T00:28:23.470621+00:00", "sensor": "pond_main", "mode": "raw"}
    },
    "pond_main_sensor_processed": {
      "config": {"type": "sensor", "source": "pond_main", "settings": {"unit": "cm", "mode": "processed"}, "emit": true, "unit": "cm", "mode": "processed"},
      "output": {"value": 11.0, "unit": "cm", "at": "2026-09-07T00:28:22.093268+00:00", "sensor": "pond_main", "mode": "processed"}
    },
    "rolling_median5": {
      "config": {
        "type": "rolling_median",
        "source": "pond_main_sensor_raw",
        "settings": {"window_size": 5},
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
    "rolling_avg": {
      "config": {
        "type": "rolling_average",
        "source": "rolling_median5",
        "settings": {"window_size": 60, "poll_interval_ms": 1000},
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
        "poll_interval_ms": 1000
      }
    }
  }
}
```

Each signal's `config` is its *effective* configuration from
`config/sensors.yaml`'s `signals:` list (defaults filled in, so `emit`
is always present even if the YAML omitted it), and `output` is `value`
plus that signal's own `extra_state()`. Returns `503 {"error": "no
readings yet"}` before this sensor has produced a first value for any
of its signals.

`config.source` and `config.settings` mirror the entry's own top-level
`source:`/`settings:` fields (see [Signal processing](#signal-processing))
-- `source` names whatever this signal reads from (a sensor, for a
`sensor`-type signal; another signal otherwise), and `settings` is
whatever implementation-specific fields that type's own constructor
takes (e.g. `unit`/`mode` for `sensor`, `window_size` for
`rolling_median`) -- empty (`{}`) for a type that needs none. `unit`/
`mode` are additionally reported as their own top-level fields in this
same `config`, alongside `settings`, regardless of whether this signal
declared them itself or inherited them from `source`.

### `GET /diag`

The bare form of the same route, spanning every configured sensor at
once -- each keyed by name, with exactly the same per-sensor `signals`
shape `GET /sensors/<name>/diag` returns for one. A sensor with no
readings yet shows an empty object rather than failing the whole
request, same as bare `POST /reset` below reports per-sensor status
instead of an all-or-nothing error:

```json
{
  "sensors": {
    "pond_main": { "...": "same shape as GET /sensors/pond_main/diag's \"signals\"" }
  }
}
```

Every signal's `config`/`output` includes `unit` — the unit its own
`value` is actually in (e.g. `"cm"`), as declared in `settings.unit` for
a `sensor`-type signal or derived automatically from `source` for every
other type. See [Signal processing](#signal-processing). `value` is
already in that unit by the time server.py sees it -- `SensorSignal`
(the boundary where a sensor's canonical millimeter reading first enters
the signal graph, see [Signal processing](#signal-processing)) converts
it once, there; server.py only rounds for display and does no
unit-specific math of its own. `unit` genuinely describes what `value`
already is, not just a label server.py's own conversion happens to
match.

`output` also includes `at` — an ISO 8601 UTC timestamp of the freshest
underlying sensor reading this `value` reflects (see [Signal
processing](#signal-processing) for exactly how that's tracked through
a chain of signals). `pond_main_sensor_raw` and `pond_main_sensor_processed`
above have visibly different `at` values because they're rooted at
different `mode`s and update on independent cadences — `at` is how to
tell a signal's value apart from stale, without needing to separately
poll `/health`.

### `GET /signals`

Lists every configured signal's name, across every sensor — signals
aren't nested under a sensor in `config/sensors.yaml` (see [Signal
processing](#signal-processing)), so unlike the routes above there's no
bare-vs-sensor-named distinction here; this is the one flat list:

```json
{
  "signals": ["pond_main_sensor_raw", "pond_main_sensor_processed", "rolling_median5", "rolling_avg"]
}
```

### `GET /signals/<name>` / `GET /signals/<name>/diag`

A single named signal's current value, regardless of which sensor it's
rooted at or whether it's marked `emit: false`. This is the endpoint an
external consumer (e.g. Home Assistant's REST sensors) polls directly
for one signal's value -- distance is measured from the sensor down to
the water surface, not a depth/level in absolute terms unless you
subtract it from the sensor's fixed mounting height:

```json
{
  "name": "rolling_avg",
  "sensor": "pond_main",
  "value": 11.2,
  "unit": "cm",
  "at": "2026-09-07T00:28:23.470621+00:00",
  "window_size": 60,
  "samples_in_window": 60,
  "poll_interval_ms": 1000
}
```

`sensor` is which configured sensor this signal is ultimately rooted at
(traced through any `source:` chain back to a `sensor`-type signal's own
`source`). `unit` is this signal's own configured/derived unit, and
`at` is the freshest underlying sensor reading's own timestamp (see
[Signal processing](#signal-processing) for both) — every field past
that is this signal's own `extra_state()` alongside its value, varying
by signal type, same as `/diag`'s `output`.

`/signals/<name>/diag` returns this signal's effective `config` (as
`/diag` would show it) alongside that same `output`, instead of just
the flattened value:

```json
{
  "name": "rolling_avg",
  "sensor": "pond_main",
  "config": {
    "type": "rolling_average",
    "source": "rolling_median5",
    "settings": {"window_size": 60, "poll_interval_ms": 1000},
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
    "poll_interval_ms": 1000
  }
}
```

Both return `404 {"error": "unknown signal '<name>'"}` for a name not
in `config/sensors.yaml`'s `signals:` list, and `503 {"error": "no
readings yet"}` before that signal has produced its first value (a
signal rooted at `mode: processed` can briefly 503 right after startup,
before the sensor's first settled processed-mode reading arrives).

### `POST /reset` / `POST /sensors/<name>/reset`

Resets a sensor — for when it appears wedged/stuck (e.g. a stale,
unchanging reading) and whatever resync a driver attempts internally on
its own (see `/health` below) hasn't resolved it. What "reset" actually
does is entirely up to the driver (`reset_hardware()`, see [Sensor
drivers](#sensor-drivers)) -- the API and server.py have no notion of
the specifics. For the A02YYUW specifically, this drives its power pin
(`power_pin` param, default `24`) low for
`sensor_power.RESET_OFF_DURATION_S` (1s) and back high, so the request
blocks for about that long per sensor reset.

`POST /sensors/<name>/reset` targets exactly one sensor. Not every
sensor driver supports resetting — returns `501 {"error": "sensor does
not support reset"}` if that one doesn't (checked via its
`supports_reset` capability flag; see [Sensor drivers](#sensor-drivers)),
or `404 {"error": "unknown sensor '<name>'"}` for a name not in
`config/sensors.yaml`:

```json
{
  "status": "reset",
  "sensor": "pond_main",
  "reset_at": "2026-09-05T22:05:40.132812+00:00"
}
```

Bare `POST /reset`, like bare `GET /diag` above, spans every configured
sensor at once -- it resets **every** sensor that supports it, one at a
time, reporting each one's outcome individually (`"reset"` or
`"not_supported"`) rather than failing the whole request just because
one sensor lacks the capability:

```json
{
  "sensors": {
    "pond_main": {"status": "reset", "reset_at": "2026-09-05T22:05:40.132812+00:00"},
    "rain_barrel": {"status": "not_supported"}
  }
}
```

Each sensor's `reset_at` (from either route) is also recorded as that
sensor's own entry in `/health`'s `sensors.<name>.last_reset_at` below.
There's no readiness check afterward — a sensor typically resumes
producing valid frames within its normal ~100-300ms response time, same
as at startup.

Resetting a sensor also **cascades**: every signal rooted at it (see
[Signal processing](#signal-processing)) has its own `reset()` called too
-- clearing any accumulated state (a rolling window, an average) back to
empty. A sensor reset happens because something looked wrong (a
stale/unchanging reading, say), so signal state built from readings
around that time is suspect too; a reset gives a clean slate end-to-end
rather than resetting just the sensor while leaving stale-window
averages behind. Concretely: right after a reset, `rolling_avg`'s
`samples_in_window` (see `GET /signals/rolling_avg`) drops back to
climbing from zero, same as right after startup.

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
  "sensors": {
    "pond_main": {
      "status": "ok",
      "last_reading_age_s": 0.1,
      "last_reset_at": null,
      "signals": ["rolling_avg", "pond_main_sensor_raw", "pond_main_sensor_processed"]
    }
  }
}
```

The top-level `status` is `"degraded"` (HTTP 503) if **any** configured
sensor's own `status` is degraded. Each sensor's `status` comes entirely
from that sensor's own `is_healthy()` (`Sensor.STALE_READING_THRESHOLD_S`,
3s by default -- see [Sensor drivers](#sensor-drivers)) -- server.py holds
no threshold of its own and makes no staleness judgment itself, it just
asks. `is_healthy()` returning `False` means no reading has landed in
over that sensor's own threshold, which would otherwise silently leave
its rooted signals (e.g. `pond_main_sensor_raw`, `rolling_avg`) serving
stale data forever with no signal anything was wrong. This also catches
a driver's read thread dying outright (an unhandled exception in
`poll_loop()`, say) within a few seconds of it happening, same as it
catches genuinely stale hardware -- e.g. a wiring disturbance knocking
the A02YYUW driver's byte alignment out of sync in just the wrong way,
so its incremental header-hunting resync never lands on a fresh header.
The driver itself watches for this same condition and forces a
`reset_input_buffer()` once it's crossed, so in practice a stale
reading should self-resolve within a few seconds — `last_reading_age_s`
climbing past the threshold and staying there is the signal that
didn't happen.

`last_reading_age_s` is seconds since that sensor's last valid frame, or
`null` before its first ever reading (not itself a degraded condition —
right after startup, before anything has been read yet, is normal).

`last_reset_at` is when `POST /reset` (or `/sensors/<name>/reset`) last
reset that sensor, or `null` if it's never been called since this
service started (not persisted across restarts).

`signals` is just the list of that sensor's configured signal names, as
a quick "did the config load correctly" signal — see `GET
/signals/<name>` for each one's actual output.

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

Every sensor implements the small `Sensor` interface (`sensors/base.py`):
`read(settings)` returns `{"value": distance_mm, "at": ...}` — distance
from the sensor's mount point down to the water surface, in millimeters
— for whatever the *caller's own* `settings` selects (a driver that
reports more than one named reading, like the A02YYUW's `"raw"`/
`"processed"`, looks for a `mode` key in it; `settings` is optional,
defaulting to whatever the driver considers its primary reading) — and
`close()`. `supports_reset`/`reset_hardware()` is an optional capability
a driver can add if its hardware can actually be power-cycled or
otherwise reset in software; `POST /reset` checks this flag rather than
assuming every sensor has it.

`Sensor` has no notion of *how* a driver actually obtains a reading
-- no polling loop, no thread, nothing background-shaped on the base
class at all, and `read()` itself isn't even required to touch hardware.
`A02YYUWSensor` happens to need a poll loop (it has to keep polling a
UART), so it implements its own `_poll_loop()`/`_begin_polling()` and
starts that thread itself, as the *last* line of its own `__init__` (once
all of its own state -- serial connection, mode controller, whatever it
needs -- is fully set up); that loop calls a driver-private method
(`_read_hardware()`, not `read()`) repeatedly (every `poll_interval_s`)
and, for each `(reading_key, distance_mm)` pair it gets back, caches it
via `self._record_reading()`. `read()` itself is then just a lookup
against that same cache, keyed by whatever mode `settings` asks for --
always instant, never touching the UART or the hardware lock. Nothing
is ever pushed onward from there -- a `reads_from_sensor` signal pulls
its own configured mode's last cached value on its own schedule
instead, passing its own `settings` (specifically, just the `mode` it
cares about) into `read()` (see [Signal
processing](#signal-processing)). A future driver that's push-driven
instead (reacting to an async callback, never looping at all) is just
as valid -- it simply wouldn't implement a poll loop, since the base
class never assumed one; it would just call `_record_reading()`
whenever its callback fires.

Whatever mechanism a driver uses to obtain readings, it calls
`self._record_reading(readings)` (concrete on `Sensor`) each time it
gets one or more, to participate in health tracking (below) *and* to
populate `last_reading()`'s cache -- that's the one thing the base
class asks of every driver. `reset()` is also concrete on the
base class, but only handles the generic part: calling the driver's own
`reset_hardware()` and recording when. A driver that owns a background
loop (like `A02YYUWSensor`) overrides `reset()` to stop that loop, wait
for it to actually exit, call `super().reset()`, then start a fresh
loop -- so there's never a moment where two threads could both be
touching hardware, and a reset always leaves the driver's own loop in a
genuinely fresh state, not just the hardware.

`last_reading_monotonic()`/`last_reset_at()`/`is_healthy()` are also
concrete, and are how `GET /health` (below) gets its per-sensor status --
`STALE_READING_THRESHOLD_S` (a class attribute, default `3.0` seconds,
any driver type free to override) plus `is_healthy()`'s comparison
against it live entirely on `Sensor`, so server.py holds no
threshold and makes no staleness judgment of its own; it just calls
`sensor.is_healthy()` and trusts the answer. `reset()` also records its
own `last_reset_at()` timestamp as part of the same call, and clears
`last_reading()`'s cache -- otherwise a signal pulling from this sensor
would immediately re-read the stale pre-reset value.

Different sensor technologies measure fundamentally different native
quantities with different sign conventions (an ultrasonic sensor's raw
distance vs. a resistive sensor's submerged length, say), so each driver
is responsible for converting its own reading into that shared
millimeters-to-surface unit before returning it — nothing downstream
(signals, the HTTP API) needs to know which sensing technology
produced a given value.

Sensor types are discovered dynamically at startup, the same way
[signal types](#signal-processing) are: each entry in `sensors/` whose
name ends in `_sensor` must define exactly one `Sensor` subclass
*and* a module-level `create(params, simulate)` function, and that
entry's name with the suffix stripped becomes the `type:` string used
in `config/sensors.yaml`. Unlike signals (whose constructors take
simple scalar params directly), most sensor drivers need real hardware
objects — a serial connection, GPIO controllers — assembled around
those params, and build entirely different (simulated) objects under
`--simulate`; `create()` is where a driver type does that assembly, so
`sensor_config.py` never needs to know a given type's own construction
details. Construction alone starts a driver's own background read
thread, if it has one -- what it does from there (e.g. `A02YYUWSensor`
starting its own poll loop) is entirely that driver's own business, not
`Sensor`'s.

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
  of the windows together. The A02YYUW driver's `poll_interval_ms` param
  (`config/sensors.yaml`) defaults to `150` (comfortably above 100 ms) so
  that every sample fed into the filters is an independent look at the
  water surface.
- **Ranging accuracy (±1 cm) sets a noise floor.** Any single reading
  can be off by up to 1 cm even with a perfectly still water surface, so
  don't expect (or chase) sub-centimeter precision out of
  `signals.pond_main_sensor_raw`. That's exactly what `rolling_avg` is
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
`sensors/a02yyuw_sensor/__init__.py`, default 9s raw / 1s processed per
10s cycle), briefly dipping into `processed` just often enough to keep
that reading fresh too. Frames read within `MODE_SETTLE_S` of a mode
switch are discarded rather than cached, since the sensor's response
time means a reading right after a switch can still reflect the
*previous* mode. See `GET /signals/pond_main_sensor_raw` and `GET
/signals/pond_main_sensor_processed` above for how to read each stream.

An optional `read_mode` param (`"raw"` or `"processed"`; omitted keeps
the alternating cycle above) pins the driver permanently in one mode
instead — no cycling, no settling windows after the first frame, and
`_read_hardware()` only ever reports that one key (so `last_reading()`
for the other one never populates, and `read()` -- and any signal
rooted at that other mode -- never has anything to return). This is a
driver-level
override, distinct from (but easy to confuse with) a `sensor` signal's
own `mode` (see [Signal processing](#signal-processing)) —
`read_mode` controls which reading the driver ever *produces*;
`mode` controls which reading a given *signal* pulls. Pinning
`read_mode` to `"processed"` means only signals rooted at `mode:
processed` (e.g. `pond_main_sensor_processed`) ever have anything to
read — the default `raw`-rooted pipeline (`pond_main_sensor_raw`,
`rolling_avg`) never gets data, since `last_reading("raw")` never
populates: the driver never reports a `raw` reading at all. Pinning to
`"raw"` is the inverse: only `raw`-rooted signals get data, and
`pond_main_sensor_processed` never does.

One consequence worth knowing: because the `raw` pipeline (the one
feeding this sensor's `rolling_avg` etc.) only actually gets
sensor data during its ~90% share of each cycle, a plain sample-count
window filled at a fixed poll rate would represent a correspondingly
longer wall-clock span than it would with continuous polling -- at the
defaults, the raw pipeline only gets fresh samples during ~86% of
wall-clock time (9s of `raw` per 10s cycle, minus `MODE_SETTLE_S` lost
right after switching back into it). `rolling_avg` (`type:
rolling_average`) sidesteps this: it owns its own background
thread (see [Signal processing](#signal-processing)) that samples its
`source:` signal's `read()` once every `poll_interval_ms` on its own
timer, rather than recomputing on every one of the sensor's much faster
reads, so `window_size: 60` at `poll_interval_ms: 1000` stays a genuine
~60s window regardless of how the raw pipeline's duty cycle drifts.

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

`POST /reset` runs on a request-handling thread, entirely independent of
the background thread continuously calling `_read_hardware()` -- without
synchronization, a reset landing mid-read can wedge the driver (seen in
practice: an hourly `POST /reset` automation left the sensor stuck for
~53 minutes until the next unrelated service restart happened to clear
it). `A02YYUWSensor` serializes the two internally via a lock around
both `_read_hardware()` and `reset_hardware()` (see `sensors/base.py`'s
`Sensor` docstring for why this is each driver's own responsibility, not
something server.py coordinates) -- a concurrent `_read_hardware()`
simply blocks for the ~1s power-cycle rather than running against the
sensor while it's powered off. The public `read()` (a plain
`last_reading()` cache lookup -- see [Signal processing](#signal-processing))
never contends for this lock at all.

## Signal processing

Signals live in their own top-level `signals:` list in
`config/sensors.yaml`, independent of the `sensors:` list — not nested
under a sensor. Every signal entry names where its data comes from via
a top-level `source:` field — the same field, whatever kind of thing it
names: only a `type: sensor` signal has `source:` name a sensor
directly; every other signal has it name another signal's *live
output* instead. This is how sequential composition (e.g.
median-then-average) is expressed — no dedicated "chain" type needed,
just two flat entries linked by `source:`. `GET /diag` shows the
output of every signal ultimately rooted at a given sensor (traced by
following `source:` chains back to whichever `sensor` signal names that
sensor) side by side. This makes it possible to compare smoothing
approaches against the live sensor stream without a code change or
redeploy — just edit the YAML.

Signal types are discovered dynamically at server startup, not from a
hand-maintained registry: each file in `signals/` whose name ends in
`_signal` must define exactly one `Signal` subclass, and the name
with that suffix stripped becomes the `type:` string used in the YAML.
Files that don't end in `_signal` (`base.py`, or any future non-signal
helper module) are ignored automatically — no hardcoded skip-list to
maintain. Adding a new signal type means writing
`signals/<name>_signal.py` and referencing `type: <name>` in the
`signals:` list — nothing else to edit or register.

Every signal type except `sensor` is genuinely unit-agnostic: `add()`
takes a value in and returns a processed value out, with no notion of
mm/cm baked in anywhere -- a rolling average of centimeters is still in
centimeters, computed the same way regardless of what unit those
centimeters happen to represent. `sensor` is the deliberate one
exception: it's the boundary where a sensor's canonical reading (always
millimeters -- `Sensor.read()`'s fixed contract, see [Sensor
drivers](#sensor-drivers)) first enters the signal graph, so its own
`add()` converts once, there, into its declared `unit`
(`SensorSignal.UNIT_DIVISORS`, currently just `"cm"`) -- `unit: cm`
on `pond_main_sensor_raw` is correct precisely because dividing
millimeters by `UNIT_DIVISORS["cm"]` (10) produces centimeters. Nothing
downstream of that one signal, including every other signal type and
server.py itself, ever needs to think about millimeters again.

`add()` is the pure-computation hook every signal type implements
(median, average, EMA, unit conversion); nothing calls it directly except
`Signal.read()` (concrete, shared by every type), which pulls this
signal's `source:` (its own `read()`, or, for `sensor`-type signals, the
sensor's `last_reading()`) and, if that's newer than what this signal
already incorporated, computes a fresh value via `add()` and
thread-safely caches it alongside that source's own `at`. Calling
`read()` any number of times with no new upstream data is safe and
cheap -- `add()` only reruns when there's genuinely something new, so a
stateful accumulator (a rolling window, an EMA) is never double-fed by
two callers reading in quick succession. This pull-and-cache mechanism
is what lets `rolling_average` sample its `source:` signal directly
(`source_signal.read()`, from its own background thread) rather than
needing anything in server.py to mediate between them -- see [How it
works](#how-it-works).

Every signal entry has four generic top-level fields -- `name`, `type`,
`source`, and (optionally) `emit` -- that `signal_config.py` itself
understands and acts on; everything else a given type's own constructor
needs is opaque to `signal_config.py` and lives nested under that
entry's `settings:`, passed straight through as that constructor's
kwargs. Built-in `Signal` types (`type:` in the YAML) and their
`settings`:

| Type | Settings | Behavior |
|---|---|---|
| `sensor` | `unit`, `mode` | Converts the named sensor's raw millimeter reading into `unit` and passes it through. The only type whose `source:` names a sensor directly -- everything else names another signal. |
| `rolling_median` | `window_size` | Median-filters its input over a rolling window — rejects spikes/outliers. |
| `rolling_average` | `window_size`, `poll_interval_ms` | Averages its input over a rolling window, like `rolling_median` averages instead of filters -- but instead of computing lazily the moment something calls `read()`, it owns its own dedicated background thread that samples its `source:` signal's `read()` once every `poll_interval_ms`, on its own timer, and writes the result directly, overriding `read()` itself to just return that (see below). `window_size * poll_interval_ms` is then the real-world window, independent of the sensor's own poll rate, so it doesn't drift if the underlying pipeline's duty cycle changes (see [RX pin](#rx-pin-raw-vs-processed-hardware-mode) below) and doesn't need a large `window_size` to cover a long span. |
| `exponential_smoothing` | `alpha` | Exponentially-weighted moving average of its input — each new reading is weighted by `alpha` (0-1), with every prior reading's weight decaying geometrically by `(1 - alpha)`. Unlike a rolling window, there's no fixed window size: older readings are never fully dropped, just weighted down forever. Higher `alpha` tracks the latest reading more closely; lower `alpha` smooths more aggressively. |

Every entry requires a top-level `source: <name>` -- for `sensor` it
names a configured sensor; for every other type it names the signal
(defined earlier in the file) whose output feeds it.

`sensor` is the one signal type with `Signal.reads_from_sensor = True`
(same capability-flag pattern as `Sensor.supports_reset`) -- the only
generic thing `signal_config.py` knows about it is that flag itself; it
has no notion of `unit`/`mode` (its two `settings:` fields) or what
values are valid for them, nor that `source:` names a sensor rather
than another signal for this type in particular. All of that --
including validating `source` against the configured sensor names, and
`unit` against `SensorSignal.UNIT_DIVISORS` (currently just
`{"cm": 10.0}`) -- happens inside `SensorSignal`'s own constructor,
which raises if something's wrong; `signal_config.py` just wraps
whatever it raises with file/signal-name context. `unit` (e.g. `"cm"`)
is required, since it's the boundary where a value enters the signal
graph and nothing upstream can tell us what unit to convert into -- an
unsupported unit fails config loading outright rather than silently
mislabeling a number. Every other signal type derives its `unit`
automatically from whichever signal its `source:` names, since none of
them perform any unit conversion -- a rolling average of centimeters is
still in centimeters -- and must not set `unit` in its own `settings:`
(that raises a config error, since it would otherwise just be rejected
generically as an unexpected constructor argument). This is reported on
`/diag` and `/signals/<name>`; see those endpoints above.

A `sensor` signal may also set `mode` in its `settings:` -- `"raw"`
(the default) or `"processed"`, picking which of the sensor's own named
readings feeds it (see `Sensor.read()` in [Sensor
drivers](#sensor-drivers) and the A02YYUW's two hardware modes in
[Sensor notes](#sensor-notes)), validated against
`SensorSignal.VALID_MODES` -- same story as `unit` above,
`signal_config.py` doesn't know this rule exists. Every other signal
type derives `mode` from `source`, same as `unit`, and must not set it
directly either.
Signals rooted at different modes update on genuinely independent
cadences -- see each one's own `at` timestamp (below) rather than
assuming two signals shown together on `/diag` were computed at the
same moment. Any signal type may own its own dedicated background
thread instead of computing lazily on `read()` (currently just
`rolling_average`, as described in the table above) -- there's no
config marker for this, it's purely a property of that type's own
`read()` implementation, and any number of a sensor's signals (zero,
one, or more) may do it.

Every signal's `/diag`/`/signals/<name>` output also includes `at` — an
ISO 8601 UTC timestamp letting a caller tell a signal's freshness apart
from another's without a separate `/health` request. For most signal
types `at` is **propagated from `source:`**, not stamped fresh at
`read()` time -- ultimately tracing back to whichever `sensor`-type
signal's `SensorSignal.read()` pulled it from `Sensor.read()`, i.e.
when the underlying physical reading actually arrived. Stamping
"now" at `read()` time instead would be actively misleading under a
pull model: a signal nobody has queried in 30s would otherwise claim
its value is fresh the instant someone finally asks, masking real
staleness. A type with its own background thread (`rolling_average`)
is the one exception -- it stamps its *own* sampling time instead,
since it deliberately samples its `source:` on its own schedule,
independent of its cadence; "when did I last sample" is the meaningful
timestamp for it specifically.

`exponential_smoothing`'s `alpha` gets applied once per sensor poll
(every `poll_interval_ms`, default 150ms -- see [Sensor drivers](#sensor-drivers))
— not once per reading by
a downstream consumer polling its signal via `GET /signals/<name>`. An
EMA's half-life in *samples* is roughly `ln(0.5) / ln(1 - alpha)`; at a
150ms feed rate, `alpha` values of 0.1-0.9 all decay to a half-life
under a second, which is invisible next to a consumer polling every 30s
(e.g. Home Assistant's default `scan_interval`) — the value it reads
back has already forgotten
everything older than a second regardless of which of those alphas was
configured. To get smoothing that's actually visible at a given polling
cadence, pick `alpha` so the half-life (`0.15 * ln(0.5)/ln(1-alpha)`
seconds, at the default polling interval) lands near that cadence —
roughly `alpha` in the 0.01-0.2 range for a 10-30s cadence.

Example `config/sensors.yaml` (see [Sensor drivers](#sensor-drivers) for
the `sensors:` entry's own `name`/`type`/`settings` fields):

```yaml
sensors:
  - name: pond_main
    type: a02yyuw
    settings:
      serial_port: /dev/serial0
      mode_select_pin: 25
      power_pin: 24

signals:
  - name: pond_main_sensor_raw
    type: sensor
    source: pond_main
    settings:
      unit: cm
  - name: pond_main_sensor_processed
    type: sensor
    source: pond_main
    settings:
      unit: cm
      mode: processed
  - name: rolling_median5
    type: rolling_median
    source: pond_main_sensor_raw
    emit: false
    settings:
      window_size: 5
  - name: rolling_avg
    type: rolling_average
    source: rolling_median5
    settings:
      window_size: 60
      poll_interval_ms: 1000
```

Here `rolling_avg` reads `rolling_median5`'s output, which in
turn reads `pond_main_sensor_raw`'s output (the sensor's raw reading) —
a median-then-average pipeline built entirely from `source:` references,
with each stage its own independently named signal.
`pond_main_sensor_processed` is unrelated to that pipeline: a second,
independent `sensor` signal rooted at the same sensor's `processed`
reading instead, updating on its own cadence (see the A02YYUW's
raw/processed hardware-mode cycling in [Sensor
notes](#sensor-notes)).

`rolling_avg` is `type: rolling_average` (the one type that owns its own
background read loop, see above), reachable like any other signal,
directly at `GET /signals/<name>`. **The deployed Home Assistant integration reads
`pond_main_sensor_raw` and `pond_main_sensor_processed` directly** --
two `rest` sensors in Home Assistant's `configuration.yaml`, each
polling `http://<pi-host>:8080/signals/<name>` every 60s via
`value_json.value` -- so renaming either of those two specific signals
means updating the matching HA sensor's `resource`/`value_template`
too. `signals.pond_main_sensor_raw` is unaffected by other signals —
it's always the raw last-valid reading.

Any signal can also set `emit: false` (default `true`) to mark it as
just an intermediate stage feeding another signal (like
`rolling_median5` above) rather than a meaningful output on its own --
purely descriptive metadata shown in its `config` on `/diag`/
`/signals/<name>/diag`, not something any endpoint currently filters
by.

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
| `--host` | `0.0.0.0` | Address the HTTP server binds to. |
| `--port` | `8080` | Port the HTTP server binds to. |
| `--simulate` | off | Build every configured sensor in simulated mode (e.g. the A02YYUW driver uses `SimulatedSerial` — synthetic sine-wave + noise data — and no-op mode/power controllers) instead of opening real hardware. For local development with no sensor hardware attached. |

How often a sensor is polled for a new reading is a per-sensor
`poll_interval_ms` param in `config/sensors.yaml` now (defaulting to
`150`, same as the A02YYUW driver's own default -- see [Sensor
drivers](#sensor-drivers)), not a global CLI flag, since each driver's
own read thread starts itself the moment it's constructed.

`--sensors-config`'s default (and where `/health`'s `commit_sha` resolves
from) is relative to the working directory, not the installed package's
location — this only works because `WorkingDirectory` is always set
explicitly: `/opt/pondpi` in `deploy/pondpi.service`, and the repo root
by convention for local dev (see below).

Change the deployed configuration by editing `config/sensors.yaml` (sensor
wiring, poll interval, smoothing) or `ExecStart` in `deploy/pondpi.service`
(host/port/`--sensors-config`), e.g.:

```
ExecStart=/opt/pondpi/.venv/bin/pondpi-server --sensors-config /opt/pondpi/config/sensors.yaml --port 8080
```

Don't set a sensor's `poll_interval_ms` below ~100 — see [Sensor
notes](#sensor-notes) above for why.

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
curl http://localhost:8080/signals/rolling_avg
curl http://localhost:8080/health
```

`pip install -e ".[dev]"` is an editable install, so changes to files
under `src/pondpi/` take effect immediately — no reinstall needed. It
also puts the `pondpi-server` command on your `PATH` (equivalently, run
`python -m pondpi.server` directly).

Useful while developing: a small `window_size` on a `rolling_average`/
`rolling_median` signal in `config/sensors.yaml`'s `signals:` list (see
the average react faster) and a larger `poll_interval_ms` on the sensor
itself (slow the stream down to read it by eye). Pass `--sensors-config`
to point at an alternate YAML file without touching the checked-in one.

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
