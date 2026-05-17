# Depth Camera

Security camera detection events → depth-augmented images, entirely on a
Raspberry Pi 5. No VPS, no cloud compute, no GPU.

When your Aqara G5 Pro detects a person, animal, or motion, the Pi captures
the frame from a ring buffer (from *when the event actually happened*, not
after the notification delay), runs monocular depth estimation, and generates
interactive parallax views, colorized depth maps, and colored point clouds.

Browse the results in a web gallery on your phone, laptop, or VR/AR headset.

## How It Works

```
┌──────────────────── Your LAN ────────────────────┐
│                                                   │
│  Aqara G5 Pro ◄──RTSP──┐                         │
│  (LAN camera)           │ (one persistent conn)   │
│                         │                         │
│  Raspberry Pi 5 ────────┘                         │
│  ┌─────────────────────────────────────────────┐  │
│  │ depth-ring.service                          │  │
│  │ Continuous ring buffer (16s, ~0% CPU)       │  │
│  │ /tmp/depth-ring/seg_000.ts ... seg_007.ts   │  │
│  └────────────────┬────────────────────────────┘  │
│                   │                               │
│  ┌────────────────┼────────────────────────────┐  │
│  │ depth-relay    │  (Aqara cloud → IFTTT)     │  │
│  │                ▼                            │  │
│  │  Webhook arrives → extract frame at T-5s   │  │
│  │  Run Depth Anything V2 (~3-6s on CPU)      │  │
│  │  Generate: colormap, parallax depth, PLY   │  │
│  └─────────────────────────────────────────────┘  │
│                                                   │
│  ┌─────────────────────────────────────────────┐  │
│  │ depth-monitor.service (optional fallback)   │  │
│  │  EWMA background model pixel-diff detection │  │
│  │  → same depth pipeline as relay            │  │
│  │  + motion diff visualization (orange mask)  │  │
│  └─────────────────────────────────────────────┘  │
│                                                   │
│  ┌─────────────────────────────────────────────┐  │
│  │ depth-gallery.service (:8080)               │  │
│  │  /                    → event gallery       │  │
│  │  /settings            → configuration UI   │  │
│  │  /events/:id/viewer   → WebGL parallax      │  │
│  │  /events/:id/*.ply    → point cloud         │  │
│  └─────────────────────────────────────────────┘  │
│                                                   │
└───────────────────────────────────────────────────┘
            │
            │  LAN / Tailscale (optional)
            ▼
    ┌───────────────────┐
    │  Your Devices     │
    │  • iPhone/Android │  ← browse gallery, tilt for parallax
    │  • Laptop         │  ← mouse-driven parallax, download PLY
    │  • Quest/Vision   │  ← Glass UI passthrough mode
    └───────────────────┘
```

### Ring Buffer: Why Timing Matters

The camera detects an opossum at T=0. The IFTTT notification reaches the Pi
at T=5–7s. Without a ring buffer, you'd capture an empty patio. The ring
buffer continuously records the last 16 seconds — when the webhook arrives,
we extract the frame from 5 seconds ago, when the opossum was actually there.

## Features

### Gallery

- **5 UI modes** — switchable from a dropdown in the gallery header, persisted across pages:
  - **Glass** — Minority Report-inspired translucent panels with cyan glows. On AR/VR headsets (Quest, Vision Pro, Pico) the page background becomes fully transparent so the real room shows through.
  - **Instrument** — scientific sensor readout aesthetic (default); JetBrains Mono display type
  - **Cinematic** — Fraunces serif, generous spacing, soft focus
  - **Brutalist** — thick borders, 800-weight uppercase
  - **Soft** — rounded corners, warm light-mode tones
- **Paginated events** — shows 20 events initially with "Show 20 more / Show all N" buttons to keep memory usage low
- **Sticky filters** — source, type, date range, and layout persist across reloads and viewer round-trips
- **Stable sequence numbers** — `#003/221` stays `#003/221` when you load more events
- **Timestamps to the second** — HH:MM:SS in both the gallery and viewer header
- **Auto-refresh on tab return** — navigating back from the viewer fetches fresh events immediately

### Viewer

- **Parallax** — WebGL mouse/touch/gyro depth displacement
- **Depth slider** — drag to compare RGB ↔ depth colormap
- **Point cloud** — Three.js PLY viewer with orbit controls
- **XR** — WebXR immersive-VR/AR entry with headset auto-detection and QR fallback
- **Motion Diff mode** — pi\_monitor events include a "Diff" view showing which pixels triggered detection (orange mask against the EWMA background)

### Point Cloud

- **Correct camera intrinsics** — horizontal FOV computed from the camera's diagonal spec (Aqara G5 Pro: ~113°). The wrong FOV was causing severe ground-plane bowing.
- **RANSAC ground-plane correction** — fits a plane to the lower 40% of the frame and rotates the cloud flat, compensating for monocular depth model bias
- **cos(θ) perspective correction** — converts along-ray depth to orthogonal z-depth, eliminating the U-shaped bowl distortion on wide-angle cameras
- **Configurable Z-scale** — `ply_depth_scale` stretches the depth axis independently (now that it no longer uniformly scales X/Y too)

### Local Motion Detection (`depth-monitor`)

An optional Aqara-free fallback that triggers the same depth pipeline using pixel-diff on the camera stream.

- **EWMA background model** — each poll updates `background = 0.9×background + 0.1×current`, so persistent motion (wind in trees) gets absorbed and only sudden changes (a person appearing) spike above the threshold. Baseline noise drops from p50 40–76% (single-frame diff) to p50 1–4%.
- **Ring-buffer comparison** — comparison frames are read from existing ring buffer segments rather than opening fresh RTSP connections, keeping the camera's session count at one and preventing ring-buffer instability.
- **Motion diff image** — every trigger saves `motion_diff.jpg`: the detection frame dimmed to 45% with motion pixels highlighted orange. Viewable in the gallery's "Diff" mode.
- **Separate display threshold** — `diff_display_threshold` (default 40) controls the orange mask sensitivity independently of the detection threshold (default 20), so the visualization stays clean without affecting detection.

### Settings Page

Browse to `/settings` (accessible via the ⚙ icon in the gallery header) to configure without SSH:

- **Camera** — IFTTT lookback, horizontal FOV with three auto-detection modes:
  - *Camera database* — pick from 12 known cameras; diagonal→horizontal FOV computed automatically
  - *Manual diagonal entry* — enter the spec-sheet diagonal FOV
  - *Two-point measurement* — click two points on the last snapshot, enter their real-world separation and depth, and the page calculates the focal length geometrically
  - *Probe RTSP stream* — ffprobe extracts camera model metadata and tries a database match
- **Point Cloud** — Z-scale, downsample factor, RANSAC ground correction toggle
- **Motion Detection** — start/stop toggle (instant, no save needed), lookback, trigger threshold, cooldown, diff display threshold
- **Gallery** — PIN lock
- **Regenerate all PLY files** — one button re-runs point cloud generation for all existing events with the current saved settings (progress bar, ~8 min for 200 events)

Settings are written to `/opt/depth-camera/config.yaml`; `depth-relay` and `depth-monitor` restart automatically on save.

## Output Formats

| Format | File | Description |
|--------|------|-------------|
| **Interactive parallax** | Web viewer | WebGL "living photo" — tilt phone or move mouse to explore depth |
| **Depth colormap** | `depth_colormap.jpg` | Colorized depth map (warm = near, cool = far) |
| **Point cloud** | `pointcloud.ply` | Colored 3D point cloud — corrected intrinsics, RANSAC-leveled ground |
| **Raw depth** | `depth.npy` | Float32 numpy array; reprocess with updated settings via Settings → Regenerate |
| **Motion diff** | `motion_diff.jpg` | *(pi\_monitor events only)* Orange-masked frame showing what triggered detection |

## Requirements

- **Raspberry Pi 5** (8 GB or 16 GB recommended). Pi 4 works but inference is slower.
- **Aqara G5 Pro** security camera — or any RTSP camera with a supported diagonal FOV in the settings database
- **Raspberry Pi OS** 64-bit, Bookworm/Trixie
- **ffmpeg 7.x** (installed by setup script; note: `ffmpeg <6` uses `nonkey` for `-skip_frame` — 7.x uses `nokey`)
- **Python 3.11+** (ships with Bookworm)
- **[IFTTT](https://ifttt.com) account** (free tier sufficient) with Aqara Home connected

No GPU, no AI accelerator, no VPS, no cloud compute account needed.

## Quick Start

### 1. Run setup

```bash
git clone <this-repo> ~/depth-camera
cd ~/depth-camera
sudo ./setup.sh
```

### 2. Set secrets

Credentials and notification URLs live in environment files that never touch git:

```bash
sudo nano /etc/depth-camera.env
```

```
CAMERA_RTSP_URL=rtsp://USER:PASS@CAMERA_IP:8554/stream_path
HEALTHCHECK_RING_BUFFER_URL=   # optional — healthchecks.io dead-man's switch
HEALTHCHECK_WEBHOOK_URL=       # optional — pinged on each IFTTT POST
```

Find your Aqara G5 Pro's RTSP URL: Aqara App → Camera → Settings → RTSP.

```bash
sudo nano /etc/ntfy.env
```

```
NTFY_TOPIC_ALERTS=   # optional — ntfy.sh topic for push notifications
```

### 3. Configure via the Settings page

After starting the services (step 5), open `http://<PI_IP>:8080/settings` and set:

- **Gallery PIN** — recommended; the gallery serves all camera images unauthenticated without it
- **Camera FOV** — use the camera database or two-point measurement tool for best point cloud geometry
- **IFTTT Lookback** — increase if captured frames miss the subject (cloud delay varies)

Or edit `/opt/depth-camera/config.yaml` directly. Key options:

```yaml
gallery:
  pin: "482916"          # 6-character PIN; blank = no lock

pipeline:
  camera_hfov_deg: 113.0     # horizontal FOV — critical for point cloud geometry
  ply_depth_scale: 1.5       # Z stretch; increase if scene looks flat front-to-back
  ply_ground_correction: true # RANSAC floor leveling

detection:
  min_changed_pct: 15.0      # EWMA baseline is p50 ~2%; 15% = sustained gusts only
  diff_display_threshold: 40  # orange mask threshold (0 = same as detection)
  cooldown: 300              # seconds between pi_monitor triggers
```

Restart after editing: `sudo systemctl restart depth-relay depth-monitor`

### 4. Start services

```bash
sudo systemctl start depth-ring depth-relay depth-gallery

# Optional: local motion detection fallback
sudo systemctl enable --now depth-monitor
```

### 5. Verify

```bash
# Ring buffer should show updating segment files
ls -la /tmp/depth-ring/

# Gallery should respond
curl http://localhost:8080/api/status
```

### 6. Expose webhook for IFTTT (via Tailscale Funnel)

[Tailscale Funnel](https://tailscale.com/kb/1223/funnel) gives IFTTT encrypted
HTTPS access without port forwarding:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale funnel 9090
```

Note the Funnel URL (e.g., `https://your-hostname.your-tailnet.ts.net`).

### 7. Set up IFTTT

1. Create a free account at [ifttt.com](https://ifttt.com)
2. Connect **Aqara Home** and authorize with your Aqara credentials
3. Enable AI detection in the Aqara Home app (person, animal, etc.)
4. Create an IFTTT applet:
   - **If:** Aqara Home → detection trigger
   - **Then:** Webhooks → Make a web request
     - URL: `https://YOUR_TAILSCALE_FUNNEL_URL/ifttt`
     - Method: POST / Content-Type: application/json
     - Body: `{"event_type": "person", "source": "ifttt"}`

### 8. Browse

Open `http://<PI_IP>:8080/` on your phone or laptop. Try the **Glass** UI mode on a VR headset for passthrough visualization.

## Project Structure

```
depth-camera/
├── ring_buffer.py       # RTSP ring buffer daemon
├── capture.py           # Frame extraction from ring buffer (ffmpeg)
├── depth.py             # Depth Anything V2 ONNX inference
├── outputs.py           # Colormap, depth image, point cloud (+ RANSAC correction)
├── pipeline.py          # Orchestrate capture → depth → outputs
├── relay.py             # IFTTT webhook receiver + depth pipeline trigger
├── monitor.py           # Local motion detection (EWMA background model)
├── server.py            # Web gallery + settings page + API
├── notifications.py     # ntfy push + healthchecks.io heartbeats
├── config.yaml          # Configuration reference (secrets via env files)
├── setup.sh             # One-command Pi setup
├── requirements.txt     # Python dependencies
└── templates/
    ├── gallery.html     # Event list (5 UI modes, pagination, filters)
    ├── viewer.html      # Parallax / Depth / Cloud / XR / Diff viewer
    └── settings.html    # Web configuration UI
```

## Services

| Service | Port | Description | Auto-enabled |
|---------|------|-------------|-------------|
| `depth-ring` | — | Ring buffer (always-on, ~0% CPU) | Yes |
| `depth-relay` | 9090 | IFTTT webhook + depth processing | Yes |
| `depth-gallery` | 8080 | Web gallery + settings | Yes |
| `depth-monitor` | — | Local motion detection fallback | **No** |

```bash
# Manage services
sudo systemctl start|stop|restart depth-ring depth-relay depth-gallery

# Enable local motion detection (optional)
sudo systemctl enable --now depth-monitor

# View logs
journalctl -u depth-relay -f
journalctl -u depth-monitor -f
```

## Configuration Reference

### Ring Buffer

| Parameter | Default | Description |
|-----------|---------|-------------|
| `segment_seconds` | 2 | Length of each segment file |
| `segment_count` | 8 | Segments to keep (8 × 2s = 16s history) |

### Lookback Timing

| Parameter | Default | Description |
|-----------|---------|-------------|
| `relay.lookback_s` | 5 | Seconds back for IFTTT triggers (cloud delay ~5–7s) |
| `detection.lookback_s` | 2 | Seconds back for pi\_monitor triggers |

Increase `lookback_s` if captured frames miss the detected subject.

### Depth Processing & Point Cloud

| Parameter | Default | Description |
|-----------|---------|-------------|
| `depth_input_size` | 518 | Model input resolution (384 = ~40% faster) |
| `colormap` | inferno | Depth visualization colormap |
| `max_events` | 200 | Max events per source before cleanup |
| `ply_downsample` | 2 | Point cloud sampling (1=31MB, 2=8MB, 4=2MB) |
| `camera_hfov_deg` | 113.0 | Horizontal FOV in degrees — **critical for correct geometry** |
| `ply_depth_scale` | 1.5 | Z-axis stretch factor (depth-only, not uniform scale) |
| `ply_ground_correction` | true | RANSAC ground-plane leveling |

For the Aqara G5 Pro (120° diagonal, 16:9 sensor), the horizontal FOV is ~113°. Use Settings → Detect FOV to measure from your actual scene.

### Local Motion Detection

| Parameter | Default | Description |
|-----------|---------|-------------|
| `compare_width` | 320 | Comparison frame width (pixels) |
| `compare_height` | 240 | Comparison frame height |
| `threshold` | 20 | Per-pixel change threshold (0–255) |
| `diff_display_threshold` | 40 | Threshold for orange diff visualization (0 = same as threshold) |
| `min_changed_pct` | 15.0 | % of pixels that must change to count as motion |
| `min_mean_diff` | 12.0 | Minimum mean pixel intensity change |
| `confirm_frames` | 3 | Consecutive above-threshold polls to confirm motion |
| `cooldown` | 300 | Seconds between triggers |
| `bg_alpha` | 0.9 | EWMA adaptation rate (higher = slower adaptation) |

With EWMA, the background absorbs persistent motion (wind, shadows) over ~30s. p50 noise floor drops to 1–4%, so `min_changed_pct: 15` only catches sustained significant changes.

## Resource Usage

| Component | CPU | RAM | Disk |
|-----------|-----|-----|------|
| Ring buffer | ~0% | ~8–16 MB (tmpfs) | 0 (RAM-backed) |
| IFTTT relay (idle) | 0% | ~50 MB | 0 |
| Depth inference | ~100% for 3–6s | ~1–2 GB peak | 0 |
| Gallery server | ~0% | ~30 MB | 0 |
| **Total idle** | **~0%** | **~100 MB** | **0** |
| **During event** | **~100% for 3–6s** | **~2 GB peak** | **~10 MB/event** |

## Security Notes

### Gallery PIN
The gallery binds to `0.0.0.0:8080` and serves all captured camera images. Without a PIN, anyone on your LAN can browse it. Set `gallery.pin` via the Settings page or directly in `/opt/depth-camera/config.yaml`.

### Webhook URL is a shared secret
The relay endpoint (`/ifttt`) has no per-request authentication — IFTTT's basic webhook doesn't support bearer tokens. Anyone who learns your Tailscale Funnel URL can trigger depth processing. Treat the URL as a secret and use a non-guessable Tailscale hostname.

### Secrets never touch git
Camera credentials, healthcheck URLs, and ntfy topics live exclusively in `/etc/depth-camera.env` and `/etc/ntfy.env` on the Pi. `config.yaml` in this repo contains only empty placeholders. The settings page writes to `/opt/depth-camera/config.yaml` (the deployed copy), never to the repo checkout.

### Services run as a non-root user
All systemd services run as the user who invoked `sudo ./setup.sh`. The data directory `/data/depth-camera` is `chown`'d to that user automatically.
