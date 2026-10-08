# CLAUDE.md

Project-specific guidance for Claude Code when working in this repo.

## What This Is

Security camera detection events turned into depth-augmented images, entirely on
a Raspberry Pi 5. No VPS, no cloud compute, no GPU.

When the Aqara G5 Pro detects a person/animal/motion, the Pi extracts a frame
from a ring buffer (compensating for IFTTT notification delay), runs Depth
Anything V2 via ONNX Runtime, and generates interactive parallax views,
colorized depth maps, and colored point clouds.

## Architecture

Four systemd services, all on the Pi:

- **depth-ring** — ffmpeg ring buffer (`-c copy -f segment`), ~0% CPU, tmpfs-backed
- **depth-relay** — IFTTT webhook receiver (port 9090), triggers depth pipeline
- **depth-gallery** — Flask web gallery (port 8080), serves events + WebGL viewer
- **depth-monitor** — optional local motion detection fallback

## Key Design Decisions

- **Ring buffer for timing**: IFTTT notifications arrive 5-7s late. The ring
  buffer keeps 16s of history so we extract the frame from when the event
  actually happened.
- **Depth convention**: Normalized float32 [0,1] where 1.0 = nearest, 0.0 = farthest.
  Established in `depth.py`, respected everywhere.
- **ONNX Runtime on ARM64**: Depth Anything V2 Small runs in ~3-6s on Pi 5 CPU.
  No GPU or accelerator needed.
- **WebGL1 depth proxy**: The parallax viewer uses the inferno colormap red
  channel as a depth approximation (WebGL1 can't read 16-bit textures).

## Running Locally (Development)

This is designed to run on a Raspberry Pi, not macOS. Python files can be
syntax-checked locally but actual execution requires ffmpeg + RTSP camera.

```bash
# Syntax check all Python files
for f in *.py; do python3 -c "import py_compile; py_compile.compile('$f', doraise=True)"; done
```

## Config

Three layers, loaded by `config.py` (`load_config()`); later wins:

1. `config.yaml`: the **only** source of defaults, with every key documented.
   Installed to `/opt/depth-camera/config.yaml` and replaced on every
   `setup.sh` run. There are no `DEFAULT_CONFIG` dicts in the modules. A new
   setting goes in `config.yaml`, and code reads `config[section][key]` directly.
2. `/etc/depth-camera.yaml`: the user's overrides only (0600, owned by the
   service user). `setup.sh` creates it once and never overwrites it. The
   gallery Settings page writes it through `config.diff()`, so values equal to
   the default are dropped instead of frozen.
3. Env vars, mapped in `config.ENV_OVERRIDES`.

`--config PATH` names the overrides file, not the defaults. Old unit files pass
`--config config.yaml`, and the loader treats that as "use /etc/depth-camera.yaml"
with a warning. `python3 config.py check` lists overrides vs defaults and flags
unknown keys, which the services also warn about at startup.

The live Pi's overrides were migrated on 2026-10-08 from a full-copy
`/opt/depth-camera/config.yaml`, which is kept as `config.yaml.pre-overrides`.
Before that, every Settings-page save froze all ten form fields. That's how
`detection.confirm_frames` stayed at 2 for months after the repo moved to 3.

**Secrets**: Four env-driven values, all loaded via systemd `EnvironmentFile=`.
The `config.yaml` in the repo has only placeholder values (empty strings for
the ntfy topic and both healthcheck URLs). Never commit real credentials.

- `CAMERA_RTSP_URL` (camera credentials) — set in `/etc/depth-camera.env` on the
  Pi (mode 0600 root:root).
- `HEALTHCHECK_RING_BUFFER_URL` (healthchecks.io dead-man's-switch ping URL,
  fired every ~60s while ring-buffer segments are flowing) — set in
  `/etc/depth-camera.env`.
- `HEALTHCHECK_WEBHOOK_URL` (healthchecks.io URL, pinged on each `/ifttt` POST so
  long quiet periods trigger an alert) — set in `/etc/depth-camera.env`.
- `NTFY_TOPIC_ALERTS` (ntfy push topic for active error alerts) — set in
  `/etc/ntfy.env` on the Pi (mode 0640 root:trevor).

**Deployment**: `git pull && sudo ./setup.sh` is the upgrade path, not `sudo cp`.
It replaces code, `config.yaml`, templates and unit files, then runs
`systemctl try-restart`, leaving enabled/disabled state alone. Ring, relay and
monitor load `/etc/depth-camera.env` (the gallery needs no secrets). Relay and
monitor also load `EnvironmentFile=-/etc/ntfy.env` (optional). Local unit tweaks belong in
`systemctl edit` drop-ins, because edits to the unit files are overwritten.
IFTTT reaches the Pi via Tailscale Funnel (HTTPS, no port forwarding).

## Notifications

Two channels, both optional and fail-silently (see `notifications.py`):

- **Healthchecks.io** — dead-man's-switch pings. `ring_buffer.py` pings `notifications.ring_buffer_heartbeat_url` every ~60s while segments are flowing. `relay.py` pings `notifications.webhook_heartbeat_url` two ways: immediately on each `/ifttt` POST, and on a fixed `notifications.heartbeat_interval_s` schedule (default 12h) independent of webhook traffic — this second path is a relay-process liveness check, so a genuinely quiet night (no detections) doesn't get misread as the relay being down. Both heartbeat URLs come from env vars (`HEALTHCHECK_RING_BUFFER_URL`, `HEALTHCHECK_WEBHOOK_URL`) set in `/etc/depth-camera.env`. Committed `config.yaml` has them empty so the URLs stay out of git.
- **ntfy** — active error pushes. Topic URL comes from the `NTFY_TOPIC_ALERTS` env var (set in `/etc/ntfy.env` on the Pi). Falls back to `notifications.ntfy_topic_url` in `config.yaml`, which is intentionally empty in the committed copy. The topic value is never in any tracked file — keeps it out of GitHub.

## Coexistence

Runs alongside other services on the Pi 5 with no conflicts.
Different ports, different runtimes, plenty of RAM headroom.
