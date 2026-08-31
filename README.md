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
                                    ┌─────────────────────────────────────────┐
                                    │ Flask app: GET /level, GET /health       │
                                    └─────────────────────────────────────────┘
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
│   ├── median_filter.py
│   ├── rolling_average.py
│   ├── signal_processors/    # one LevelSignalProcessor subclass per file, see below
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
| `median_filter.py` | `MedianFilter` — tracks the median of the last N values added; a building block used by some `LevelSignalProcessor` classes. |
| `rolling_average.py` | `RollingAverage` — tracks the average of the last N values added; a building block used by some `LevelSignalProcessor` classes. |
| `signal_processors/` | `LevelSignalProcessor` base class (`base.py`) and its built-in implementations, one per file, each named `<type>_processor.py` (`raw_processor.py`, `median_processor.py`, `rolling_average_processor.py`, `chain_processor.py`) — see [Signal processing](#signal-processing). |
| `signal_processor_config.py` | `load_signal_processors()` — reads `config/processors.yaml` into named `LevelSignalProcessor` instances. |
| `commit_sha.py` | `read_commit_sha()` — resolves the deployed commit SHA for `/health`. |
| `duration.py` | `format_duration()` — formats a seconds count as `"1d 2h 3m 4s"` for `/health`'s `uptime_human`. |
| `server.py` | Service entrypoint (`pondpi-server`). Starts the background polling thread and the Flask app. Owns all CLI configuration. |

## API

### `GET /level`

Returns the current instantaneous and smoothed distance readings.

```json
{
  "instantaneous_distance_cm": 11.3,
  "rolling_avg_distance_cm": 11.2,
  "polling_interval_ms": 150,
  "processors": {
    "median5": {
      "distance_cm": 11.2,
      "window_size": 5,
      "samples_in_window": 5
    },
    "rolling_avg": {
      "distance_cm": 11.2,
      "steps": [
        {"processor": "median5", "window_size": 5, "samples_in_window": 5},
        {"processor": "rolling_average", "window_size": 40, "samples_in_window": 40}
      ]
    },
    "instantaneous_raw": {
      "distance_cm": 11.3
    }
  }
}
```

| Field | Meaning |
|---|---|
| `instantaneous_distance_cm` | The most recent single valid raw reading (not processed by any processor). |
| `rolling_avg_distance_cm` | The output of whichever processor is marked `primary: true` in `config/processors.yaml` — kept as a stable top-level field because the deployed Home Assistant sensor depends on it (see [Signal processing](#signal-processing)). |
| `polling_interval_ms` | How often the poller checks the serial buffer for a new frame (see `--polling-interval-ms`). This is the poll rate, not necessarily the sensor's own update rate. |
| `processors` | Every configured `LevelSignalProcessor` instance's current output, keyed by name. `distance_cm` is always present; the rest of each entry is that processor's own `extra_state()` (window sizes, sample counts, ...) and varies by processor type. |

Returns `503 {"error": "no readings yet"}` if no valid reading has come in
since the server started.

Distance is measured from the sensor down to the water surface — it's not
a depth/level in absolute terms unless you subtract it from the sensor's
fixed mounting height.

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
| `median` | `window_size` | Median-filters the raw reading — rejects spikes/outliers. |
| `rolling_average` | `window_size` | Averages the raw reading over a rolling window. |
| `chain` | `steps` | Runs a value through other processors in sequence, feeding each stage's output into the next. See below. |

`chain`'s `steps` is a list where each entry is either `ref: <name>` —
reuses another processor's `type`/`params` to build a fresh, **independent**
instance (a config alias, never the literal same object, so state is
never shared between the two) — or an inline `type:`/`params:`, built
directly (recursively, so a step can itself be a chain). This is how
today's production pipeline (median-then-rolling-average) is built,
without needing a dedicated hardcoded class for it.

Example `config/processors.yaml`:

```yaml
processors:
  - name: median5
    type: median
    params:
      window_size: 5
  - name: rolling_avg
    type: chain
    primary: true
    params:
      steps:
        - ref: median5
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
`from pondpi.median_filter import MedianFilter`), and don't need any
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
