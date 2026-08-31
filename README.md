# PondPi

Measures pond water level with an A02YYUW ultrasonic sensor (UART) on a
Raspberry Pi, and exposes the current reading over a small HTTP API.

## How it works

The sensor sits above the pond and streams distance-to-water-surface
readings continuously over UART. A background thread polls the serial
connection, validates each frame, and runs it through every configured
`LevelSignalProcessor` instance (see [Signal processing](#signal-processing)
below); a Flask server exposes every processor's output on `GET /level`.

```
┌───────────────┐   UART frames    ┌─────────────────┐    add()    ┌───────────────────────────────────┐
│ A02YYUW sensor│ ───────────────> │ poll_sensor()    │ ──────────> │ every configured LevelSignalProcessor │
└───────────────┘                  │ (background      │             │ (signal_processors/), discovered      │
                                    │  thread)         │             │ dynamically at startup                │
                                    └────────┬─────────┘             └──────────────────┬─────────────────┘
                                             │ writes shared state                       │
                                             v                                           v
                                    ┌───────────────────────────────────────────────┐
                                    │ Flask app: GET /level, GET /health, GET /diag  │
                                    └───────────────────────────────────────────────┘
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
│   └── processors.yaml     # signal-processing config, see below
├── src/pondpi/              # the installable package — production code only
│   ├── server.py            # entrypoint (installed as the `pondpi-server` command)
│   ├── read_sensor.py
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
| `read_sensor.py` | Protocol/hardware layer only: checksum validation, frame parsing, a single instantaneous `read_frame(ser)` call, and `SimulatedSerial` (a fake serial source for local dev). No smoothing, no I/O loop. |
| `signal_processors/` | `LevelSignalProcessor` base class (`base.py`) and its built-in implementations, one per file, each named `<type>_processor.py` (`raw_processor.py`, `rolling_median_processor.py`, `rolling_average_processor.py`, `chain_processor.py`) — see [Signal processing](#signal-processing). |
| `signal_processors/utils/` | `RollingMedianFilter` and `RollingAverage` — generic building blocks used internally by some `LevelSignalProcessor` classes. Not signal processors themselves (they don't implement the `LevelSignalProcessor` interface), so they live in a subpackage that dynamic discovery ignores — its name doesn't end in `_processor`. |
| `signal_processor_config.py` | `load_signal_processors()` — reads `config/processors.yaml` into named `LevelSignalProcessor` instances. |
| `commit_sha.py` | `read_commit_sha()` — resolves the deployed commit SHA for `/health`. |
| `duration.py` | `format_duration()` — formats a seconds count as `"1d 2h 3m 4s"` for `/health`'s `uptime_human`. |
| `server.py` | Service entrypoint (`pondpi-server`). Starts the background polling thread and the Flask app. Owns all CLI configuration. |

## API

### `GET /level`

Returns the current instantaneous and smoothed distance readings.

```json
{
  "measure_name": "level",
  "units": "cm",
  "instantaneous_distance_cm": 11.3,
  "rolling_avg_distance_cm": 11.2,
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
| `instantaneous_distance_cm` | **Legacy.** The most recent single valid raw reading (not processed by any processor). Superseded by `signals.instantaneous_raw`, kept because the deployed Home Assistant sensor's `value_template` reads this exact field — see below. |
| `rolling_avg_distance_cm` | **Legacy.** The output of whichever processor is marked `primary: true` in `config/processors.yaml`. Superseded by `primary_signal.value`, kept for the same Home Assistant reason. |
| `polling_interval_ms` | How often the poller checks the serial buffer for a new frame (see `--polling-interval-ms`). This is the poll rate, not necessarily the sensor's own update rate. |
| `primary_signal` | `{value, name}` for whichever processor is marked `primary: true` — `name` is that processor's actual configured name, so this stays correct even if you rename it (unlike `rolling_avg_distance_cm`, whose field name is fixed regardless of what the primary processor is actually called). |
| `signals` | A curated `{name: distance_cm}` view of just the processors meant to be read as final output — every configured processor *except* whichever ones are marked `emit: false` in `config/processors.yaml` (e.g. an intermediate stage that only exists to feed a `chain`). See [Signal processing](#signal-processing). |

`instantaneous_distance_cm`/`rolling_avg_distance_cm` are kept only
because Home Assistant's `configuration.yaml` `value_template`s read
those exact field names (see [Signal processing](#signal-processing)) —
new integrations should use `primary_signal`/`signals` instead. Once
Home Assistant is migrated to the new fields, the legacy ones can be
dropped.

`rolling_median5` (see [Signal processing](#signal-processing)) doesn't
appear here — it's marked `emit: false` since it only exists to feed
`rolling_avg`'s chain, not as a meaningful output on its own. Its full
state is still visible on `/diag`.

Returns `503 {"error": "no readings yet"}` if no valid reading has come in
since the server started.

Distance is measured from the sensor down to the water surface — it's not
a depth/level in absolute terms unless you subtract it from the sensor's
fixed mounting height.

### `GET /diag`

The config and live output of **every** configured signal processor,
regardless of `emit` — the full diagnostic view that `/level`'s `signals`
deliberately leaves out.

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
            {"type": "rolling_average", "params": {"window_size": 40}}
          ]
        },
        "primary": true,
        "emit": true
      },
      "output": {
        "distance_cm": 11.2,
        "steps": [
          {"processor": "rolling_median5", "window_size": 5, "samples_in_window": 5},
          {"processor": "rolling_average", "window_size": 40, "samples_in_window": 40}
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

Each processor's `config` is its *effective* configuration from
`config/processors.yaml` (defaults filled in, so `primary`/`emit` are
always present even if the YAML omitted them), and `output` is the same
shape `/level`'s `signals`/`processors` used to expose — `distance_cm`
plus that processor's own `extra_state()`. Returns `503 {"error": "no
readings yet"}` under the same condition as `/level`.

### `GET /health`

```json
{
  "status": "ok",
  "poller_alive": true,
  "started_at": "2026-08-29T19:31:24.633421+00:00",
  "uptime_seconds": 93780.4,
  "uptime_human": "1d 2h 3m 0s",
  "processors": ["rolling_avg", "instantaneous_raw"],
  "commit_sha": "e1d742a9c2f4b1a0d3e5f6a7b8c9d0e1f2a3b4c5"
}
```

`status` is `"degraded"` (HTTP 503) if the background polling thread has
died — e.g. an unhandled exception in `poll_sensor()` — which otherwise
would silently leave `/level` serving stale data forever with no signal
anything was wrong.

`uptime_human` is `uptime_seconds` formatted as `"1d 2h 3m 4s"`. Units
below the largest non-zero one are always shown (so exactly one hour is
`"1h 0m 0s"`, not `"1h"`); under a minute it's just e.g. `"45s"`.

`processors` is just the list of configured processor names, as a quick
"did the config load correctly" signal — see `/level` for their actual
output.

`commit_sha` is the full commit SHA that's currently deployed. In
production this comes from a `COMMIT_SHA` file written by the deploy
workflow (see `.github/workflows/deploy.yml`) — the SHA can't be read
from git directly on the device since `.git` is excluded from the
rsync'd deploy directory. Falls back to `git rev-parse HEAD` for local
dev checkouts, or `null` if neither is available.

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
  `instantaneous_distance_cm`. That's exactly what
  `rolling_avg_distance_cm` is for — averaging readings down to a
  stabler value — but a rolling window so small that it's dominated by
  one or two ±1 cm outliers will still show that noise. Conversely,
  don't read too much into a rolling average that only moves by a few
  mm between samples; that can be within the sensor's own accuracy
  budget rather than a real water level change.

## Signal processing

Raw readings are run through every signal processor configured in
`config/processors.yaml` (a strategy pattern — one class per algorithm,
in `src/pondpi/signal_processors/`), and every processor's output is
returned side by side on `/level`. This makes it possible to compare
smoothing approaches against the live sensor stream without a code
change or redeploy — just edit the YAML.

Signal processor types are discovered dynamically at server startup, not
from a hand-maintained registry: each file in `signal_processors/` whose
name ends in `_processor` must define exactly one `LevelSignalProcessor`
subclass, and the name with that suffix stripped becomes the `type:`
string used in the YAML. Files that don't end in `_processor` (`base.py`,
or any future non-processor helper module) are ignored automatically —
no hardcoded skip-list to maintain. Adding a new signal processor means
writing `signal_processors/<name>_processor.py` and referencing
`type: <name>` in `config/processors.yaml` — nothing else to edit or
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
| `rolling_average` | `window_size` | Averages the raw reading over a rolling window. |
| `chain` | `steps` | Runs a value through other processors in sequence, feeding each stage's output into the next. See below. |

`chain`'s `steps` is a list where each entry is either `ref: <name>` —
reuses another processor's `type`/`params` to build a fresh, **independent**
instance (a config alias, never the literal same object, so state is
never shared between the two) — or an inline `type:`/`params:`, built
directly (recursively, so a step can itself be a chain). This is how
today's production pipeline (rolling-median-then-rolling-average) is
built, without needing a dedicated hardcoded class for it.

Example `config/processors.yaml`:

```yaml
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
            window_size: 40
  - name: instantaneous_raw
    type: raw
```

Exactly one entry must be marked `primary: true`. Its output backfills the
legacy top-level `rolling_avg_distance_cm` field in `/level` — **the
deployed Home Assistant "Pond Level" sensor reads that exact field**
(`sensor.pond_level`, a `rest` sensor in Home Assistant's
`configuration.yaml` polling `http://pondpi.lan:8080/level` every 30s), so
don't remove or repurpose the `primary` processor without updating that
sensor's `value_template` too. `instantaneous_distance_cm` is unaffected
by processors — it's always the raw last-valid reading — and is what
`sensor.pond_level_instantaneous` reads.

Any entry can also set `emit: false` (default `true`) to keep it out of
`/level`'s `signals` section — the curated "final output values" view —
while it still shows up in full on `/diag`. Use this for a processor
that only exists as an intermediate stage feeding a `chain` (like
`rolling_median5` above) and isn't a meaningful output on its own.

## Configuration

All non-processor configuration is via CLI flags to `pondpi-server`, not
environment variables — a single boolean/numeric mode switch is more
visible this way (shows up in `ps aux` and the systemd unit's `ExecStart`
line), so there's no risk of a stray inherited env var silently changing
behavior. Signal-processing parameters (window sizes, etc.) live in
`config/processors.yaml` instead — see [Signal processing](#signal-processing)
above.

| Flag | Default | Meaning |
|---|---|---|
| `--processors-config` | `config/processors.yaml` (relative to the working directory) | Path to the YAML file configuring level-processing processors. |
| `--polling-interval-ms` | `150` | How often (ms) to check the serial buffer for a new frame. |
| `--host` | `0.0.0.0` | Address the HTTP server binds to. |
| `--port` | `8080` | Port the HTTP server binds to. |
| `--simulate` | off | Use `SimulatedSerial` (synthetic sine-wave + noise data) instead of opening `/dev/serial0`. For local development with no sensor hardware attached. |

`--processors-config`'s default (and where `/health`'s `commit_sha`
resolves from) is relative to the working directory, not the installed
package's location — this only works because `WorkingDirectory` is always
set explicitly: `/opt/pondpi` in `deploy/pondpi.service`, and the repo
root by convention for local dev (see below).

Change the deployed configuration by editing `config/processors.yaml` (for
smoothing) or `ExecStart` in `deploy/pondpi.service` (for everything
else), e.g.:

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

Useful while developing: a small `rolling_window_size` in
`config/processors.yaml` (see the average react faster) and
`--polling-interval-ms 200` (slow the stream down to read it by eye). Pass
`--processors-config` to point at an alternate YAML file without touching
the checked-in one.

## Testing

```bash
pytest -q      # unit tests
ruff check .   # lint
```

Tests live in `tests/`, import from the installed `pondpi` package (e.g.
`from pondpi.signal_processors.utils.rolling_median_filter import RollingMedianFilter`),
and don't need any
hardware or network access — `tests/test_read_sensor.py`,
`tests/test_rolling_average.py`, `tests/test_signal_processors.py`
(including the dynamic-discovery mechanism itself, against both the real
`signal_processors/` package and small synthetic ones built in
`tmp_path`), and `tests/test_signal_processor_config.py` (using
`tmp_path` YAML files) exercise
pure functions directly, and `tests/test_server.py` uses Flask's test
client against `pondpi.server.app` with `pondpi.server._state` set
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
