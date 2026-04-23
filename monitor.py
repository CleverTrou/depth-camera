#!/usr/bin/env python3
"""
Local Motion Monitor — RTSP frame-differencing trigger.

Fallback for when IFTTT/Aqara cloud is unavailable. Detects pixel-level
changes in the camera stream and triggers the depth pipeline locally.

This is less smart than the Aqara AI (can't tell a person from a swaying
tree), but works fully offline.

Usage:
    python3 monitor.py
    python3 monitor.py --config config.yaml
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

from capture import extract_frame, capture_direct
from pipeline import process_event

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "camera": {
        "rtsp_url": "rtsp://USER:PASS@CAMERA_IP:8554/stream_path",
        "rtsp_transport": "tcp",
    },
    "ring_buffer": {
        "dir": "/tmp/depth-ring",
        "segment_seconds": 2,
    },
    "detection": {
        "poll_interval": 3,
        "compare_width": 320,
        "compare_height": 240,
        "threshold": 20,
        "min_changed_pct": 5.0,
        "cooldown": 30,
        "confirm_frames": 2,
        "lookback_s": 2,
        "snapshot_quality": 2,
    },
    "pipeline": {
        "data_dir": "/data/depth-camera",
        "max_events": 200,
        "ply_downsample": 2,
        "depth_input_size": 518,
        "colormap": "inferno",
    },
    "notifications": {
        "ntfy_topic_url": "",
    },
}


def load_config(path):
    config = DEFAULT_CONFIG.copy()
    if path and Path(path).exists():
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        _deep_merge(config, user)
    if os.environ.get("CAMERA_RTSP_URL"):
        config["camera"]["rtsp_url"] = os.environ["CAMERA_RTSP_URL"]
    return config


def _deep_merge(base, override):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")

# ---------------------------------------------------------------------------
# Frame capture and comparison
# ---------------------------------------------------------------------------


def capture_raw_frame(rtsp_url, transport, width, height):
    """Capture a low-res raw RGB frame for motion comparison."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-rtsp_transport", transport,
        "-i", rtsp_url,
        "-vframes", "1",
        "-vf", f"scale={width}:{height}",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "pipe:1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        if result.returncode != 0:
            return None
        expected = width * height * 3
        if len(result.stdout) < expected:
            return None
        return result.stdout[:expected]
    except (subprocess.TimeoutExpired, Exception):
        return None


def compute_frame_diff(frame_a, frame_b, threshold):
    """Compare two raw RGB frames. Returns (mean_diff, pct_changed)."""
    if len(frame_a) != len(frame_b):
        return 0.0, 0.0

    total_pixels = len(frame_a) // 3
    total_diff = 0
    changed = 0

    for i in range(0, len(frame_a), 3):
        avg_a = (frame_a[i] + frame_a[i + 1] + frame_a[i + 2]) // 3
        avg_b = (frame_b[i] + frame_b[i + 1] + frame_b[i + 2]) // 3
        diff = abs(avg_a - avg_b)
        total_diff += diff
        if diff > threshold:
            changed += 1

    return total_diff / total_pixels, (changed / total_pixels) * 100


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

running = True


def handle_signal(signum, _):
    global running
    running = False


def main():
    parser = argparse.ArgumentParser(description="Motion monitor")
    parser.add_argument("--config", "-c", help="Path to YAML config file")
    args = parser.parse_args()

    config = load_config(args.config)
    cam = config["camera"]
    det = config["detection"]
    ring = config["ring_buffer"]
    pipe = config["pipeline"]
    ntfy_url = config.get("notifications", {}).get("ntfy_topic_url") or None

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log.info("=" * 55)
    log.info("Depth Camera — Motion Monitor")
    log.info(f"  Camera:   {cam['rtsp_url'][:40]}...")
    log.info(f"  Poll:     every {det['poll_interval']}s")
    log.info(f"  Confirm:  {det['confirm_frames']} consecutive frames")
    log.info(f"  Lookback: {det['lookback_s']}s into ring buffer")
    log.info("=" * 55)

    # Pre-load depth model
    from depth import load_model
    load_model()

    # Capture reference frame
    log.info("Capturing reference frame...")
    reference = None
    while reference is None and running:
        reference = capture_raw_frame(
            cam["rtsp_url"], cam["rtsp_transport"],
            det["compare_width"], det["compare_height"],
        )
        if reference is None:
            log.warning("Failed — retrying in 5s...")
            time.sleep(5)

    if not running:
        return

    log.info("Reference captured. Monitoring...")

    last_trigger = 0
    confirm_count = 0

    while running:
        time.sleep(det["poll_interval"])

        current = capture_raw_frame(
            cam["rtsp_url"], cam["rtsp_transport"],
            det["compare_width"], det["compare_height"],
        )
        if current is None:
            confirm_count = 0
            continue

        _, pct = compute_frame_diff(reference, current, det["threshold"])
        in_cooldown = (time.time() - last_trigger) < det["cooldown"]

        if pct >= det["min_changed_pct"]:
            confirm_count += 1
            if confirm_count >= det["confirm_frames"] and not in_cooldown:
                log.info("MOTION CONFIRMED — running depth pipeline")

                image_data = extract_frame(
                    ring["dir"], det["lookback_s"],
                    ring["segment_seconds"], det["snapshot_quality"],
                )
                if image_data is None:
                    image_data = capture_direct(
                        cam["rtsp_url"], cam["rtsp_transport"],
                        det["snapshot_quality"],
                    )

                if image_data:
                    result = process_event(
                        image_data,
                        data_dir=pipe["data_dir"],
                        source="pi_monitor",
                        event_type="motion",
                        max_events=pipe["max_events"],
                        ply_downsample=pipe["ply_downsample"],
                        depth_input_size=pipe["depth_input_size"],
                        colormap=pipe["colormap"],
                        ntfy_topic_url=ntfy_url,
                    )
                    if result:
                        log.info(f"Event {result['event_id']} processed in {result['elapsed_s']}s")

                last_trigger = time.time()
                confirm_count = 0
        else:
            confirm_count = 0

        if pct < det["min_changed_pct"]:
            reference = current

    log.info("Monitor stopped.")


if __name__ == "__main__":
    main()
