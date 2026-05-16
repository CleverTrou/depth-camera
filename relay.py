#!/usr/bin/env python3
"""
IFTTT Webhook Relay — receives cloud detection events, captures a
time-correct frame from the ring buffer, and processes it locally.

Flow:
    Aqara G5 Pro AI detection
        → Aqara Cloud
        → IFTTT webhook → this relay (port 9090)
        → Extract frame from ring buffer at T-5s
        → Run depth estimation (~3-6s)
        → Generate outputs (colormap, parallax depth map, point cloud)
        → Gallery server shows the results

Usage:
    python3 relay.py
    python3 relay.py --config config.yaml
"""

import argparse
import logging
import os
import threading
import time
from pathlib import Path

import yaml

try:
    from flask import Flask, request, jsonify
except ImportError:
    print("Install dependencies: pip install flask pyyaml")
    exit(1)

from capture import extract_frame, capture_direct
from notifications import ping_healthcheck, push_ntfy
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
    "relay": {
        "host": "0.0.0.0",
        "port": 9090,
        "lookback_s": 5,
        "snapshot_quality": 2,
    },
    "pipeline": {
        "data_dir": "/data/depth-camera",
        "max_events": 200,
        "ply_downsample": 2,
        "depth_input_size": 518,
        "colormap": "inferno",
        "camera_hfov_deg": 113.0,
        "ply_depth_scale": 1.5,
        "ply_ground_correction": True,
    },
    "notifications": {
        "webhook_heartbeat_url": "",
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
    if os.environ.get("NTFY_TOPIC_ALERTS"):
        config["notifications"]["ntfy_topic_url"] = os.environ["NTFY_TOPIC_ALERTS"]
    if os.environ.get("HEALTHCHECK_WEBHOOK_URL"):
        config["notifications"]["webhook_heartbeat_url"] = os.environ["HEALTHCHECK_WEBHOOK_URL"]
    return config


def _deep_merge(base, override):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("relay")

app = Flask(__name__)
_config = {}


def _capture_and_process(source, event_type, lookback_override=None):
    """Capture a frame from the ring buffer and run the depth pipeline."""
    relay = _config["relay"]
    ring = _config["ring_buffer"]
    pipe = _config["pipeline"]
    notif = _config.get("notifications", {})
    ntfy_url = notif.get("ntfy_topic_url") or None

    lookback = lookback_override if lookback_override is not None else relay["lookback_s"]

    # Try ring buffer first, fall back to live RTSP.
    # Retry once after a short delay: the camera can briefly send
    # undecodable frames on wake-up, and the ring buffer may be mid-restart
    # when an event arrives. A 3s pause usually lets both stabilise.
    # Adjust lookback on each attempt so the absolute target timestamp stays
    # anchored to the original event time despite the sleep between attempts.
    image_data = None
    start_time = time.time()
    for attempt in range(2):
        if attempt > 0:
            log.warning(f"Capture attempt {attempt} failed — retrying in 3s...")
            time.sleep(3)
        current_lookback = lookback + (time.time() - start_time)
        image_data = extract_frame(
            ring["dir"], current_lookback,
            ring["segment_seconds"], relay["snapshot_quality"],
        )
        if image_data is not None:
            break
        log.warning("Ring buffer unavailable — falling back to direct RTSP")
        image_data = capture_direct(
            _config["camera"]["rtsp_url"],
            _config["camera"]["rtsp_transport"],
            relay["snapshot_quality"],
        )
        if image_data is not None:
            break

    if image_data is None:
        log.error(
            f"Failed to capture frame from any source "
            f"(source={source}, type={event_type})"
        )
        push_ntfy(
            ntfy_url,
            "Depth Camera: capture failed",
            f"{event_type} event from {source}, but ring buffer AND live RTSP "
            f"both failed. Check camera reachability and /etc/depth-camera.env.",
            priority="high",
            tags=["warning", "camera"],
        )
        return None

    return process_event(
        image_data,
        data_dir=pipe["data_dir"],
        source=source,
        event_type=event_type,
        max_events=pipe["max_events"],
        ply_downsample=pipe["ply_downsample"],
        depth_input_size=pipe["depth_input_size"],
        colormap=pipe["colormap"],
        camera_hfov_deg=pipe.get("camera_hfov_deg", 113.0),
        ply_depth_scale=pipe.get("ply_depth_scale", 2.5),
        ply_ground_correction=pipe.get("ply_ground_correction", True),
        ntfy_topic_url=ntfy_url,
    )


@app.route("/ifttt", methods=["POST"])
@app.route("/ifttt/<path_type>", methods=["POST"])
def ifttt_webhook(path_type=None):
    """Receive a webhook and process the event.

    Event type can come from:
      1. URL path: /ifttt/person, /ifttt/animal, /ifttt/package (most reliable for IFTTT)
      2. JSON body: {"event_type": "person_detected"} (for Home Assistant, etc.)
      3. Fallback: "detection"
    """
    data = request.get_json(silent=True) or {}
    event_type = path_type or data.get("event_type", "detection")
    source = data.get("source", "ifttt")

    # Dead-man's-switch: if these stop arriving, healthchecks.io alerts us.
    ping_healthcheck(_config.get("notifications", {}).get("webhook_heartbeat_url") or None)

    # If the caller provides a Unix timestamp of the actual detection,
    # compute an exact lookback instead of using the configured guess.
    # Works with Home Assistant, Aqara API, or any source that knows
    # when the event actually happened.
    lookback_override = None
    event_ts = data.get("timestamp")
    if event_ts is not None:
        try:
            lookback_override = max(0.5, time.time() - float(event_ts))
            log.info(f"IFTTT webhook received: type={event_type}, "
                     f"exact lookback={lookback_override:.1f}s")
        except (ValueError, TypeError):
            log.warning(f"Invalid timestamp in payload: {event_ts}")
            log.info(f"IFTTT webhook received: type={event_type}")
    else:
        log.info(f"IFTTT webhook received: type={event_type}, "
                 f"using default lookback={_config['relay']['lookback_s']}s")

    # Process in a background thread so the caller doesn't time out
    thread = threading.Thread(
        target=_capture_and_process,
        args=(source, event_type, lookback_override),
        daemon=True,
    )
    thread.start()

    lookback_used = lookback_override or _config["relay"]["lookback_s"]
    return jsonify({
        "status": "processing",
        "lookback_s": round(lookback_used, 1),
        "exact_timestamp": lookback_override is not None,
    })


@app.route("/health")
def health():
    ring_dir = Path(_config["ring_buffer"]["dir"])
    segments = list(ring_dir.glob("seg_*.ts")) if ring_dir.exists() else []

    newest_age = None
    if segments:
        newest_mtime = max(s.stat().st_mtime for s in segments)
        newest_age = round(time.time() - newest_mtime, 1)

    return jsonify({
        "status": "ok",
        "role": "ifttt_relay",
        "ring_buffer": {
            "segments": len(segments),
            "newest_age_s": newest_age,
            "healthy": newest_age is not None and newest_age < 10,
        },
    })


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    global _config
    parser = argparse.ArgumentParser(description="IFTTT webhook relay")
    parser.add_argument("--config", "-c", help="Path to YAML config file")
    args = parser.parse_args()

    _config = load_config(args.config)

    relay = _config["relay"]
    ring = _config["ring_buffer"]

    log.info("=" * 55)
    log.info("Depth Camera — IFTTT Webhook Relay")
    log.info(f"  Listening:  :{relay['port']}/ifttt")
    log.info(f"  Ring buf:   {ring['dir']}")
    log.info(f"  Lookback:   {relay['lookback_s']}s")
    log.info(f"  Data dir:   {_config['pipeline']['data_dir']}")
    log.info("=" * 55)

    # Pre-load depth model at startup so first event isn't slow
    from depth import load_model
    load_model()

    app.run(host=relay["host"], port=relay["port"], debug=False)


if __name__ == "__main__":
    main()
