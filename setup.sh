#!/usr/bin/env bash
# =============================================================================
# Depth Camera — Raspberry Pi Setup
# =============================================================================
# Sets up four services:
#   1. depth-ring     — Ring buffer (continuous RTSP recording, ~0% CPU)
#   2. depth-relay    — IFTTT webhook receiver + depth processing
#   3. depth-gallery  — Web gallery server (port 8080)
#   4. depth-monitor  — Local motion detection fallback (optional)
#
# Everything runs on the Pi. No VPS required.
#
# Usage (install, and every upgrade after a git pull):
#   chmod +x setup.sh
#   sudo ./setup.sh
#
# Re-running is safe: it replaces code, templates, the shipped defaults
# (config.yaml), and the unit files, and restarts services that are running.
# It never touches your settings in /etc/depth-camera.yaml or the secrets in
# /etc/depth-camera.env. For local changes to a unit, use
# `sudo systemctl edit depth-<name>`: a drop-in survives re-runs.
# =============================================================================

set -euo pipefail

# Detect the user who invoked sudo — services will run as this user.
SERVICE_USER="${SUDO_USER:-}"
if [[ -z "$SERVICE_USER" ]]; then
    echo "Run with sudo: sudo ./setup.sh"
    exit 1
fi

# An existing ring-buffer unit means this run is an upgrade.
FRESH_INSTALL=true
[ -f /etc/systemd/system/depth-ring.service ] && FRESH_INSTALL=false

echo "=========================================="
echo "  Depth Camera — Pi Setup"
echo "=========================================="

ARCH=$(uname -m)
echo ""
echo "  Architecture: $ARCH"
echo "  Memory: $(free -h | awk '/^Mem:/{print $2}')"

if [[ "$ARCH" == "armv6l" ]]; then
    echo "  ⚠  ARMv6 detected — too slow. Use Pi Zero 2 W or newer."
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
echo ""
echo "[1/5] Installing system packages..."
apt-get update -qq
apt-get install -y -qq ffmpeg python3 python3-pip python3-venv curl > /dev/null 2>&1
echo "  ✓ System packages"

# ---------------------------------------------------------------------------
# 2. Python dependencies
# ---------------------------------------------------------------------------
echo ""
echo "[2/5] Installing Python packages..."
pip3 install --break-system-packages -q \
    flask pyyaml requests onnxruntime numpy Pillow matplotlib 2>/dev/null \
    || pip3 install -q flask pyyaml requests onnxruntime numpy Pillow matplotlib
echo "  ✓ Python packages (including onnxruntime for ARM64)"

# ---------------------------------------------------------------------------
# 3. Application files
# ---------------------------------------------------------------------------
echo ""
echo "[3/5] Installing application..."
INSTALL_DIR="/opt/depth-camera"
OVERRIDES="/etc/depth-camera.yaml"
mkdir -p "$INSTALL_DIR/templates"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Your settings live in $OVERRIDES, which this script creates once and never
# overwrites. Versions before it kept every setting in $INSTALL_DIR/config.yaml.
# On the first run after such an install, carry over only the values that
# differ from the new defaults, before the defaults below replace that file.
# (A config.yaml with the "shipped defaults" banner is already new-style.)
if [ ! -f "$OVERRIDES" ]; then
    OLD="$INSTALL_DIR/config.yaml"
    if [ -f "$OLD" ] && ! grep -q "Configuration: shipped defaults" "$OLD"; then
        cp -p "$OLD" "$OLD.pre-overrides"
        python3 "$SCRIPT_DIR/config.py" init "$OVERRIDES" --from "$OLD.pre-overrides" | sed 's/^/  /'
        echo "  ✓ Old settings file kept as $OLD.pre-overrides"
    else
        python3 "$SCRIPT_DIR/config.py" init "$OVERRIDES" | sed 's/^/  /'
    fi
    # 0600: it can hold the gallery PIN. Owned by the service user so the
    # gallery Settings page can write it.
    chown "$SERVICE_USER:$SERVICE_USER" "$OVERRIDES"
    chmod 600 "$OVERRIDES"
else
    echo "  ✓ $OVERRIDES already exists (not overwritten)"
fi

# Code, templates, and shipped defaults are replaced on every run.
cp "$SCRIPT_DIR"/*.py "$INSTALL_DIR/"
cp "$SCRIPT_DIR/config.yaml" "$INSTALL_DIR/"
cp "$SCRIPT_DIR"/templates/*.html "$INSTALL_DIR/templates/"

# Create environment file for secrets (not in the repo)
ENV_FILE="/etc/depth-camera.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'ENVEOF'
# Camera RTSP URL — the only secret this project needs.
# Find it in: Aqara App → Camera → Settings → RTSP
CAMERA_RTSP_URL=rtsp://USER:PASS@CAMERA_IP:8554/stream_path
ENVEOF
    chmod 600 "$ENV_FILE"
    echo "  ⚠  Edit /etc/depth-camera.env with your camera's RTSP URL!"
else
    echo "  ✓ /etc/depth-camera.env already exists (not overwritten)"
fi

# Create data directory and give the service user ownership so services can write to it
mkdir -p /data/depth-camera/events
chown -R "$SERVICE_USER:$SERVICE_USER" /data/depth-camera
echo "  ✓ Installed at $INSTALL_DIR"

# ---------------------------------------------------------------------------
# 4. Verify /tmp is tmpfs
# ---------------------------------------------------------------------------
echo ""
echo "[4/5] Checking /tmp..."
if mount | grep -q "tmpfs on /tmp"; then
    echo "  ✓ /tmp is tmpfs (RAM-backed — ring buffer uses no SD card wear)"
else
    echo "  ⚠  /tmp is on disk. Ring buffer works but wears SD card."
    echo "     Add to /etc/fstab: tmpfs /tmp tmpfs defaults,noatime,nosuid,size=100m 0 0"
fi

# ---------------------------------------------------------------------------
# 5. Systemd services
# ---------------------------------------------------------------------------
echo ""
echo "[5/5] Installing systemd services..."

# --- Ring buffer ---
cat > /etc/systemd/system/depth-ring.service << UNIT
[Unit]
Description=Depth Camera — RTSP Ring Buffer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=/opt/depth-camera
EnvironmentFile=/etc/depth-camera.env
ExecStart=/usr/bin/python3 ring_buffer.py
Restart=always
RestartSec=5
MemoryMax=100M

[Install]
WantedBy=multi-user.target
UNIT

# --- IFTTT relay ---
cat > /etc/systemd/system/depth-relay.service << UNIT
[Unit]
Description=Depth Camera — IFTTT Relay + Depth Processing
After=network-online.target depth-ring.service
Wants=depth-ring.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=/opt/depth-camera
EnvironmentFile=/etc/depth-camera.env
EnvironmentFile=-/etc/ntfy.env
ExecStart=/usr/bin/python3 relay.py
Restart=on-failure
RestartSec=15
MemoryMax=2G

[Install]
WantedBy=multi-user.target
UNIT

# --- Gallery server ---
cat > /etc/systemd/system/depth-gallery.service << UNIT
[Unit]
Description=Depth Camera — Web Gallery
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=/opt/depth-camera
ExecStart=/usr/bin/python3 server.py
Restart=on-failure
RestartSec=10
MemoryMax=200M

[Install]
WantedBy=multi-user.target
UNIT

# --- Motion monitor (optional) ---
cat > /etc/systemd/system/depth-monitor.service << UNIT
[Unit]
Description=Depth Camera — Local Motion Monitor (Fallback)
After=network-online.target depth-ring.service
Wants=depth-ring.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=/opt/depth-camera
EnvironmentFile=/etc/depth-camera.env
EnvironmentFile=-/etc/ntfy.env
ExecStart=/usr/bin/python3 monitor.py
Restart=on-failure
RestartSec=15
MemoryMax=2G

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload

if $FRESH_INSTALL; then
    systemctl enable depth-ring depth-relay depth-gallery
    echo "  ✓ depth-ring.service     — enabled (ring buffer)"
    echo "  ✓ depth-relay.service    — enabled (IFTTT + processing)"
    echo "  ✓ depth-gallery.service  — enabled (web gallery)"
    echo "  ✓ depth-monitor.service  — installed (optional, not enabled)"
else
    # Upgrade: leave enabled/disabled choices alone; restart only what's running.
    systemctl try-restart depth-ring depth-relay depth-gallery depth-monitor
    echo "  ✓ Unit files updated; running services restarted"
    echo ""
    echo "=========================================="
    echo "  Upgrade Complete!"
    echo "=========================================="
    echo ""
    sudo -u "$SERVICE_USER" python3 "$INSTALL_DIR/config.py" check | sed 's/^/  /' || true
    echo ""
    exit 0
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=========================================="
echo "  Setup Complete!"
echo "=========================================="
echo ""
echo "  Your settings go in $OVERRIDES (defaults: $INSTALL_DIR/config.yaml)."
echo ""
echo "  STEP 1 — Set your camera RTSP URL:"
echo "    sudo nano /etc/depth-camera.env"
echo "    Set: CAMERA_RTSP_URL=rtsp://USER:PASS@CAMERA_IP:8554/stream_path"
echo ""
echo "  STEP 2 — Start services:"
echo "    sudo systemctl start depth-ring depth-relay depth-gallery"
echo ""
echo "  STEP 3 — Verify ring buffer:"
echo "    ls -la /tmp/depth-ring/"
echo ""
echo "  STEP 4 — Browse the gallery:"
echo "    http://$(hostname -I | awk '{print $1}'):8080/"
echo ""
echo "  STEP 5 — Expose webhook for IFTTT (via Tailscale Funnel):"
echo "    curl -fsSL https://tailscale.com/install.sh | sh"
echo "    sudo tailscale up"
echo "    sudo tailscale funnel 9090"
echo "    # Then use your Funnel URL in IFTTT (e.g. https://your-hostname.your-tailnet.ts.net/ifttt)"
echo ""
echo "  NOTE: The depth model (~50 MB) downloads automatically on first run."
echo "  First event will take ~15s (download + inference). Subsequent: ~3-6s."
echo ""
echo "  OPTIONAL — Enable local motion monitor:"
echo "    sudo systemctl enable --now depth-monitor"
echo ""
