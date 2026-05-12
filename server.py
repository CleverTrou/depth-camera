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
import hashlib
import json
import logging
import os
import re
from functools import wraps
from pathlib import Path

import yaml
from flask import (
    Flask, jsonify, redirect, render_template,
    request, send_from_directory, session, url_for,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "gallery": {
        "host": "0.0.0.0",
        "port": 8080,
        "pin": "",
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

# Event IDs are always generated as YYYYMMDD_HHMMSS_xxxxxxxx (6 hex chars).
# Validate before using as a filesystem path component.
_EVENT_ID_RE = re.compile(r'^\d{8}_\d{6}_[0-9a-f]{6}$')

# The gallery defaults to showing only Aqara → IFTTT events; pi_monitor
# (coarse pixel-diff fallback) events are loaded but hidden behind a UI
# toggle so they don't pollute the timeline. The filter happens client-side
# in gallery.html so the existing PIN-protected event URLs keep working.
_DEFAULT_GALLERY_SOURCES = ["ifttt"]


def _valid_event_id(event_id: str) -> bool:
    return bool(_EVENT_ID_RE.match(event_id))


def _events_dir() -> Path:
    return Path(_config["pipeline"]["data_dir"]) / "events"


def _get_pin() -> str:
    return str(_config.get("gallery", {}).get("pin", "") or "")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _require_pin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _get_pin() and not session.get("authenticated"):
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login_page():
    pin = _get_pin()
    if not pin:
        return redirect(url_for("gallery"))

    error = False
    if request.method == "POST":
        if request.form.get("pin", "") == pin:
            session["authenticated"] = True
            next_url = request.args.get("next", "/")
            # Only allow same-origin relative paths to prevent open redirect.
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = "/"
            return redirect(next_url)
        error = True

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("authenticated", None)
    return redirect(url_for("login_page"))


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------

def _get_events() -> list[dict]:
    """Scan the events directory and return metadata for gallery-visible events."""
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


# ---------------------------------------------------------------------------
# Routes — Gallery page
# ---------------------------------------------------------------------------

@app.route("/")
@_require_pin
def gallery():
    events = _get_events()
    pin_enabled = bool(_get_pin())
    return render_template(
        "gallery.html",
        events=events,
        pin_enabled=pin_enabled,
        default_sources=_DEFAULT_GALLERY_SOURCES,
    )


# ---------------------------------------------------------------------------
# Routes — Viewer
# ---------------------------------------------------------------------------

def _get_event_meta(event_id: str) -> dict:
    """Load metadata for a single event without scanning the full directory."""
    event_dir = _events_dir() / event_id
    meta: dict = {}
    meta_file = event_dir / "metadata.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    ply = event_dir / "pointcloud.ply"
    return {
        "event_id": event_id,
        "has_snapshot": (event_dir / "snapshot.jpg").exists(),
        "has_colormap": (event_dir / "depth_colormap.jpg").exists(),
        "has_ply": ply.exists(),
        "ply_size": ply.stat().st_size if ply.exists() else 0,
        "event_type": meta.get("event_type", ""),
        "source": meta.get("source", ""),
        "timestamp": meta.get("timestamp", ""),
        "elapsed_s": meta.get("elapsed_s", 0),
    }


@app.route("/events/<event_id>/viewer")
@_require_pin
def viewer(event_id):
    if not _valid_event_id(event_id):
        return "Not found", 404
    event_dir = _events_dir() / event_id
    if not event_dir.exists():
        return "Event not found", 404
    event = _get_event_meta(event_id)
    return render_template("viewer.html", event_id=event_id, event=event)


# ---------------------------------------------------------------------------
# Routes — Static file serving
# ---------------------------------------------------------------------------

@app.route("/events/<event_id>/snapshot.jpg")
@_require_pin
def serve_snapshot(event_id):
    if not _valid_event_id(event_id):
        return "Not found", 404
    return send_from_directory(str(_events_dir() / event_id), "snapshot.jpg")


@app.route("/events/<event_id>/depth_colormap.jpg")
@_require_pin
def serve_colormap(event_id):
    if not _valid_event_id(event_id):
        return "Not found", 404
    return send_from_directory(str(_events_dir() / event_id), "depth_colormap.jpg")


@app.route("/events/<event_id>/depth_map.png")
@_require_pin
def serve_depth_map(event_id):
    if not _valid_event_id(event_id):
        return "Not found", 404
    return send_from_directory(str(_events_dir() / event_id), "depth_map.png")


@app.route("/events/<event_id>/pointcloud.ply")
@_require_pin
def serve_ply(event_id):
    if not _valid_event_id(event_id):
        return "Not found", 404
    return send_from_directory(
        str(_events_dir() / event_id), "pointcloud.ply",
        mimetype="application/x-ply",
        as_attachment=True,
        download_name=f"{event_id}.ply",
    )


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------

@app.route("/api/events")
@_require_pin
def api_events():
    return jsonify(_get_events())


@app.route("/api/status")
@_require_pin
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

    pin = _get_pin()
    if pin:
        # Derive a stable secret key from the PIN so sessions survive restarts
        # and are automatically invalidated if the PIN changes.
        app.secret_key = hashlib.sha256(f"depth-camera:{pin}".encode()).digest()
    else:
        # Sessions aren't used without a PIN, but Flask requires a key.
        app.secret_key = os.urandom(24)

    (_events_dir()).mkdir(parents=True, exist_ok=True)

    log.info("=" * 50)
    log.info("Depth Camera — Gallery Server")
    log.info(f"  Listening: :{gallery_cfg['port']}")
    log.info(f"  Data dir:  {data_dir}")
    log.info(f"  PIN auth:  {'enabled' if pin else 'disabled (set gallery.pin in config.yaml)'}")
    log.info("=" * 50)

    app.run(
        host=gallery_cfg["host"],
        port=gallery_cfg["port"],
        debug=False,
    )


if __name__ == "__main__":
    main()
