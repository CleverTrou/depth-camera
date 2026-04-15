#!/usr/bin/env python3
"""
Gallery Server — browse depth camera events in a web browser.

Serves the captured snapshots, depth maps, colorized visualizations,
point cloud downloads, and interactive WebGL parallax viewers.

Runs on the Pi alongside the ring buffer and relay services.

Usage:
    python3 server.py
    python3 server.py --config config.yaml
"""

import argparse
import json
import logging
import os
from pathlib import Path

import yaml
from flask import Flask, jsonify, send_from_directory, render_template

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "gallery": {
        "host": "0.0.0.0",
        "port": 8080,
    },
    "pipeline": {
        "data_dir": "/data/depth-camera",
    },
}


def load_config(path):
    config = DEFAULT_CONFIG.copy()
    if path and Path(path).exists():
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        for k, v in user.items():
            if k in config and isinstance(config[k], dict) and isinstance(v, dict):
                config[k].update(v)
            else:
                config[k] = v
    return config


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gallery")

app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
_config = {}


def _events_dir() -> Path:
    return Path(_config["pipeline"]["data_dir"]) / "events"


def _get_events() -> list[dict]:
    """Scan the events directory and return metadata for all events."""
    events_dir = _events_dir()
    if not events_dir.exists():
        return []

    events = []
    for event_dir in sorted(events_dir.iterdir(), key=lambda p: p.name, reverse=True):
        if not event_dir.is_dir():
            continue

        snapshot = event_dir / "snapshot.jpg"
        colormap = event_dir / "depth_colormap.jpg"
        depth_map = event_dir / "depth_map.png"
        ply = event_dir / "pointcloud.ply"
        meta_file = event_dir / "metadata.json"

        # Read saved metadata if available
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        events.append({
            "event_id": event_dir.name,
            "has_snapshot": snapshot.exists(),
            "has_colormap": colormap.exists(),
            "has_depth_map": depth_map.exists(),
            "has_ply": ply.exists(),
            "ply_size": ply.stat().st_size if ply.exists() else 0,
            "event_type": meta.get("event_type", ""),
            "source": meta.get("source", ""),
            "timestamp": meta.get("timestamp", ""),
            "elapsed_s": meta.get("elapsed_s", 0),
        })

    return events


# --- Gallery page ---

@app.route("/")
def gallery():
    events = _get_events()
    return render_template("gallery.html", events=events)


# --- Parallax viewer ---

@app.route("/events/<event_id>/viewer")
def viewer(event_id):
    event_dir = _events_dir() / event_id
    if not event_dir.exists():
        return "Event not found", 404
    return render_template("viewer.html", event_id=event_id)


# --- Static file serving ---

@app.route("/events/<event_id>/snapshot.jpg")
def serve_snapshot(event_id):
    return send_from_directory(str(_events_dir() / event_id), "snapshot.jpg")


@app.route("/events/<event_id>/depth_colormap.jpg")
def serve_colormap(event_id):
    return send_from_directory(str(_events_dir() / event_id), "depth_colormap.jpg")


@app.route("/events/<event_id>/depth_map.png")
def serve_depth_map(event_id):
    return send_from_directory(str(_events_dir() / event_id), "depth_map.png")


@app.route("/events/<event_id>/pointcloud.ply")
def serve_ply(event_id):
    directory = str(_events_dir() / event_id)
    return send_from_directory(
        directory, "pointcloud.ply",
        mimetype="application/x-ply",
        as_attachment=True,
        download_name=f"{event_id}.ply",
    )


# --- API ---

@app.route("/api/events")
def api_events():
    return jsonify(_get_events())


@app.route("/api/status")
def api_status():
    events = _get_events()
    return jsonify({
        "status": "ok",
        "event_count": len(events),
    })


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    global _config
    parser = argparse.ArgumentParser(description="Depth camera gallery server")
    parser.add_argument("--config", "-c", help="Path to YAML config file")
    args = parser.parse_args()

    _config = load_config(args.config)

    gallery_cfg = _config["gallery"]
    data_dir = _config["pipeline"]["data_dir"]

    # Ensure data directory exists
    (_events_dir()).mkdir(parents=True, exist_ok=True)

    log.info("=" * 50)
    log.info("Depth Camera — Gallery Server")
    log.info(f"  Listening: :{gallery_cfg['port']}")
    log.info(f"  Data dir:  {data_dir}")
    log.info("=" * 50)

    app.run(
        host=gallery_cfg["host"],
        port=gallery_cfg["port"],
        debug=False,
    )


if __name__ == "__main__":
    main()
