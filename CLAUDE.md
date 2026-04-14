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

All configuration lives in `config.yaml`. The Python files have inline
`DEFAULT_CONFIG` dicts that mirror the YAML structure — keep them in sync.

**Secrets**: The RTSP URL (contains camera credentials) is loaded from the
environment variable `CAMERA_RTSP_URL`, set in `/etc/depth-camera.env` on the
Pi. The `config.yaml` in the repo has only placeholder values. Never commit
real credentials. The env var override is in `ring_buffer.py`, `relay.py`,
and `monitor.py`.

**Deployment**: `setup.sh` installs to `/opt/depth-camera/` and creates systemd
services that read from there. The env file is at `/etc/depth-camera.env`
(mode 600). IFTTT reaches the Pi via Tailscale Funnel (HTTPS, no port forwarding).

## Coexistence

Runs alongside `family-calendar` on the same Pi 5 (16GB) with no conflicts.
Different ports, different runtimes, plenty of RAM headroom.
