# PondPi

Measures pond water level with an A02YYUW ultrasonic sensor (UART) on a
Raspberry Pi, and exposes the current reading over a small HTTP API.

## How it works

The sensor sits above the pond and streams distance-to-water-surface
readings continuously over UART. A background thread polls the serial
connection, validates each frame, and feeds it into a rolling average;
a Flask server exposes the result on `GET /level`.

```
┌───────────────┐   UART frames    ┌────────────────┐    add()    ┌──────────────────┐
│ A02YYUW sensor│ ───────────────> │ poll_sensor()   │ ──────────> │ RollingAverage    │
└───────────────┘                  │ (background     │             └──────────────────┘
                                    │  thread)        │                     │
                                    └────────┬────────┘                     │
                                             │ writes shared state          │
                                             v                              v
                                    ┌─────────────────────────────────────────┐
                                    │ Flask app: GET /level, GET /health       │
                                    └─────────────────────────────────────────┘
```

### Files

| File | Responsibility |
|---|---|
| `read_sensor.py` | Protocol/hardware layer only: checksum validation, frame parsing, a single instantaneous `read_frame(ser)` call, and `SimulatedSerial` (a fake serial source for local dev). No smoothing, no I/O loop. |
| `rolling_average.py` | `RollingAverage` — tracks the average of the last N values added. |
| `server.py` | Service entrypoint. Starts the background polling thread and the Flask app. Owns all CLI configuration. |

## API

### `GET /level`

Returns the current instantaneous and smoothed distance readings.

```json
{
  "instantaneous_distance_cm": 11.3,
  "rolling_avg_distance_cm": 11.2,
  "rolling_window_size": 100,
  "samples_in_rolling_window": 100,
  "polling_interval_ms": 10
}
```

| Field | Meaning |
|---|---|
| `instantaneous_distance_cm` | The most recent single valid reading. |
| `rolling_avg_distance_cm` | Average over the last `rolling_window_size` valid readings. |
| `rolling_window_size` | Configured window size (see `--window-size` below). |
| `samples_in_rolling_window` | How many samples are currently in the window (ramps up to `rolling_window_size` after startup). |
| `polling_interval_ms` | How often the poller checks the serial buffer for a new frame (see `--polling-interval-ms`). This is the poll rate, not necessarily the sensor's own update rate. |

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
  "uptime_seconds": 11.8,
  "rolling_window_size": 100
}
```

`status` is `"degraded"` (HTTP 503) if the background polling thread has
died — e.g. an unhandled exception in `poll_sensor()` — which otherwise
would silently leave `/level` serving stale data forever with no signal
anything was wrong.

## Configuration

All configuration is via CLI flags to `server.py`, not environment
variables — a single boolean/numeric mode switch is more visible this way
(shows up in `ps aux` and the systemd unit's `ExecStart` line), so there's
no risk of a stray inherited env var silently changing behavior.

| Flag | Default | Meaning |
|---|---|---|
| `--window-size` | `100` | Number of readings averaged for `rolling_avg_distance_cm`. |
| `--polling-interval-ms` | `10` | How often (ms) to check the serial buffer for a new frame. |
| `--host` | `0.0.0.0` | Address the HTTP server binds to. |
| `--port` | `8080` | Port the HTTP server binds to. |
| `--simulate` | off | Use `SimulatedSerial` (synthetic sine-wave + noise data) instead of opening `/dev/serial0`. For local development with no sensor hardware attached. |

Change the deployed configuration by editing `ExecStart` in
`deploy/pondpi.service`, e.g.:

```
ExecStart=/opt/pondpi/.venv/bin/python /opt/pondpi/server.py --window-size 50 --polling-interval-ms 20
```

## Developing locally

No Raspberry Pi or sensor hardware required — `--simulate` swaps in a fake
serial source that generates valid, correctly-checksummed A02YYUW frames
with a distance that wanders on a sine wave plus noise, running through
the exact same parsing/averaging code path as production.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

python server.py --simulate
# in another terminal:
curl http://localhost:8080/level
curl http://localhost:8080/health
```

Useful flags while developing: `--window-size 5` (see the average react
faster) and `--polling-interval-ms 200` (slow the stream down to read it
by eye).

## Testing

```bash
python -m pytest -q      # unit tests
ruff check .              # lint
```

Tests live in `tests/` and don't need any hardware or network access —
`tests/test_read_sensor.py` and `tests/test_rolling_average.py` exercise
pure functions directly, and `tests/test_server.py` uses Flask's test
client against `server.app` with `server._state` set directly.

Note: run pytest as `python -m pytest`, not bare `pytest` — the module
import (`import read_sensor`) relies on the current directory being on
`sys.path`, which `python -m` adds automatically.

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
  3. **Install dependencies** into `/opt/pondpi/.venv`.
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
