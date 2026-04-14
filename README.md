# Depth Camera

Security camera detection events → depth-augmented images, entirely on a
Raspberry Pi 5. No VPS, no cloud compute, no GPU.

When your Aqara G5 Pro detects a person, animal, or motion, the Pi captures
the frame from a ring buffer (from *when the event actually happened*, not
after the notification delay), runs monocular depth estimation, and generates
interactive parallax views, colorized depth maps, and colored point clouds.

Browse the results in a web gallery on your phone or laptop.

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
│  │ depth-relay    │                            │  │
│  │                ▼                            │  │
│  │  IFTTT webhook arrives                      │  │
│  │  Extract frame from ring buffer at T-5s     │  │
│  │  Run Depth Anything V2 (~3-6s on CPU)       │  │
│  │  Generate: colormap, parallax depth, PLY    │  │
│  └─────────────────────────────────────────────┘  │
│                                                   │
│  ┌─────────────────────────────────────────────┐  │
│  │ depth-gallery.service (:8080)               │  │
│  │  /                    → event gallery       │  │
│  │  /events/:id/viewer   → WebGL parallax      │  │
│  │  /events/:id/*.ply    → point cloud         │  │
│  └─────────────────────────────────────────────┘  │
│                                                   │
└───────────────────────────────────────────────────┘
            │
            │  LAN / Tailscale (optional)
            ▼
    ┌───────────────┐
    │  Your Devices  │
    │  • iPhone      │  ← browse gallery, tilt for parallax
    │  • Laptop      │  ← mouse-driven parallax, download PLY
    └───────────────┘
```

### Ring Buffer: Why Timing Matters

The camera detects a squirrel at T=0. The IFTTT notification reaches the Pi
at T=5-7s. Without a ring buffer, you'd capture an empty patio. The ring
buffer continuously records the last 16 seconds, so when the webhook arrives,
we extract the frame from 5 seconds ago — when the squirrel was actually there.

## Output Formats

| Format | File | Description |
|--------|------|-------------|
| **Interactive parallax** | Web viewer | WebGL "living photo" — tilt phone or move mouse to explore depth |
| **Depth colormap** | `depth_colormap.jpg` | Colorized visualization (warm = near, cool = far) |
| **Point cloud** | `pointcloud.ply` | Colored 3D point cloud, opens in MeshLab/CloudCompare |
| **Raw depth** | `depth.npy` | Float32 numpy array for custom use |

## Requirements

- **Raspberry Pi 5** (8 GB or 16 GB). Pi 4 also works but slower.
- **Aqara G5 Pro** security camera (or any RTSP camera)
- **Raspberry Pi OS** (64-bit, Bookworm)
- **ffmpeg** (installed by setup script)
- **Python 3.11+** (ships with Bookworm)

No GPU, no AI accelerator, no VPS, no cloud compute account needed.

## Quick Start

### 1. Run setup

```bash
git clone <this-repo> ~/depth-camera
cd ~/depth-camera
sudo ./setup.sh
```

### 2. Configure camera

Set your RTSP URL in the environment file (keeps credentials out of the repo):

```bash
sudo nano /etc/depth-camera.env
```

```
CAMERA_RTSP_URL=rtsp://USER:PASS@CAMERA_IP:8554/stream_path
```

Find your Aqara G5 Pro's URL in: Aqara App → Camera → Settings → RTSP.

### 3. Start

```bash
sudo systemctl start depth-ring depth-relay depth-gallery
```

### 4. Verify

```bash
# Ring buffer should show updating segment files:
ls -la /tmp/depth-ring/

# Gallery should respond:
curl http://localhost:8080/api/status
```

### 5. Expose webhook for IFTTT (via Tailscale Funnel)

IFTTT needs to reach the Pi from the internet. Tailscale Funnel provides
encrypted HTTPS access without port forwarding:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale funnel 9090
```

Note the Funnel URL (e.g., `https://your-hostname.your-tailnet.ts.net`).

### 6. Set up IFTTT

1. In **Aqara Home** app, enable AI detection (person, animal, etc.)
2. In **IFTTT**, create an applet:
   - **If This:** Aqara Home → detection trigger
   - **Then That:** Webhooks → Make a web request
     - URL: `https://YOUR_TAILSCALE_FUNNEL_URL/ifttt`
     - Method: POST
     - Content-Type: application/json
     - Body: `{"event_type": "person", "source": "ifttt"}`

### 7. Browse

Open `http://<PI_IP>:8080/` on your phone or laptop (LAN or Tailscale IP).

## Project Structure

```
depth-camera/
├── ring_buffer.py       # RTSP ring buffer daemon
├── capture.py           # Extract frame from ring buffer
├── depth.py             # Depth Anything V2 ONNX inference
├── outputs.py           # Generate colormap, depth image, point cloud
├── pipeline.py          # Orchestrate capture → depth → outputs
├── relay.py             # IFTTT webhook receiver + triggers pipeline
├── monitor.py           # Local motion detection fallback
├── server.py            # Web gallery server
├── config.yaml          # Configuration
├── setup.sh             # One-command Pi setup
├── requirements.txt     # Python dependencies
└── templates/
    ├── gallery.html     # Event list page
    └── viewer.html      # Interactive WebGL parallax viewer
```

## Services

| Service | Port | Description | Auto-enabled |
|---------|------|-------------|-------------|
| `depth-ring` | — | Ring buffer (always-on, ~0% CPU) | Yes |
| `depth-relay` | 9090 | IFTTT webhook + depth processing | Yes |
| `depth-gallery` | 8080 | Web gallery | Yes |
| `depth-monitor` | — | Local motion fallback | No |

```bash
# Manage services
sudo systemctl start|stop|restart depth-ring
sudo systemctl start|stop|restart depth-relay
sudo systemctl start|stop|restart depth-gallery

# View logs
journalctl -u depth-relay -f

# Enable local motion detection (optional)
sudo systemctl enable --now depth-monitor
```

## Resource Usage

| Component | CPU | RAM | Disk |
|-----------|-----|-----|------|
| Ring buffer | ~0% | ~8-16 MB (tmpfs) | 0 (RAM-backed) |
| IFTTT relay (idle) | 0% | ~50 MB | 0 |
| Depth inference | ~100% for 3-6s | ~1-2 GB peak | 0 |
| Gallery server | ~0% | ~30 MB | 0 |
| **Total idle** | **~0%** | **~100 MB** | **0** |
| **During event** | **~100% for 3-6s** | **~2 GB peak** | **~10 MB/event** |

Leaves plenty of headroom on a 16 GB Pi 5 running alongside family-calendar.

## Configuration Reference

### Ring Buffer

| Parameter | Default | Description |
|-----------|---------|-------------|
| `segment_seconds` | 2 | Length of each segment file |
| `segment_count` | 8 | Segments to keep (8 × 2s = 16s history) |

### Lookback Timing

| Parameter | Default | Description |
|-----------|---------|-------------|
| `relay.lookback_s` | 5 | Seconds back for IFTTT triggers (cloud delay ~3-7s) |
| `detection.lookback_s` | 2 | Seconds back for local motion triggers |

Increase `lookback_s` if captured frames miss the detected subject.

### Depth Processing

| Parameter | Default | Description |
|-----------|---------|-------------|
| `depth_input_size` | 518 | Model input resolution (384 for ~40% faster) |
| `ply_downsample` | 2 | Point cloud sampling (1=full 31MB, 2=8MB, 4=2MB) |
| `colormap` | inferno | Depth visualization colormap |
| `max_events` | 200 | Max events on disk before cleanup |

## Viewing Point Clouds

The `.ply` files are standard colored point clouds:

- **Desktop:** MeshLab, CloudCompare, Blender
- **iPhone:** MeshLab for iOS (free), 3D Point Cloud Viewer
- **Web:** Download from the gallery and open in any PLY viewer

## Coexistence with family-calendar

Both run on the same Pi 5 with no conflicts:

| Resource | family-calendar | depth-camera | Conflict? |
|----------|----------------|--------------|-----------|
| Ports | 3000 | 8080, 9090 | No |
| Display | X11 kiosk | Headless | No |
| RAM (idle) | ~400 MB | ~100 MB | No |
| RAM (peak) | ~400 MB | ~2 GB | No (16 GB Pi) |
