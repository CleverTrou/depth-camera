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
import math
import os
import re
import subprocess
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
_config_path: Path | None = None   # set in main(); used by settings page

# Known cameras with diagonal FOV — used to pre-fill hfov_deg from spec
CAMERA_DB = {
    "Aqara G5 Pro":      {"diag_fov": 120, "note": "4MP wide-angle"},
    "Aqara G2H Pro":     {"diag_fov": 140, "note": "2MP ultra-wide"},
    "Aqara G2H":         {"diag_fov": 140, "note": "2MP ultra-wide"},
    "Aqara E1":          {"diag_fov": 115, "note": "outdoor 2MP"},
    "Reolink 410W":      {"diag_fov": 100, "note": "4K outdoor"},
    "Reolink 520A":      {"diag_fov": 100, "note": "5MP PoE"},
    "Reolink Duo 2 WiFi": {"diag_fov": 105, "note": "dual-lens 4K"},
    "Wyze Cam v3":       {"diag_fov": 130, "note": "indoor/outdoor"},
    "Wyze Cam Outdoor":  {"diag_fov": 110, "note": "outdoor"},
    "Amcrest IP8M-2493EW": {"diag_fov": 98, "note": "4K outdoor"},
    "Hikvision DS-2CD2143G2": {"diag_fov": 100, "note": "4MP dome"},
    "Dahua IPC-HDW2849H": {"diag_fov": 98, "note": "8MP eyeball"},
}


def _diag_to_hfov(diag_fov_deg: float, width: int = 2688, height: int = 1520) -> float:
    """Convert diagonal FOV (degrees) to horizontal FOV for a given resolution."""
    d = math.sqrt(width ** 2 + height ** 2)
    fx = d / (2 * math.tan(math.radians(diag_fov_deg / 2)))
    return round(math.degrees(2 * math.atan(width / (2 * fx))), 1)

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
        motion_diff = event_dir / "motion_diff.jpg"
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
            "has_motion_diff": motion_diff.exists(),
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
    # defaults → meta (preserves all extra keys like trigger_pct) → calculated
    # (calculated values always win so filesystem checks can't be overridden)
    return {
        "event_type": "", "source": "", "timestamp": "", "elapsed_s": 0,
        **(meta if isinstance(meta, dict) else {}),
        "event_id": event_id,
        "has_snapshot": (event_dir / "snapshot.jpg").exists(),
        "has_colormap": (event_dir / "depth_colormap.jpg").exists(),
        "has_ply": ply.exists(),
        "ply_size": ply.stat().st_size if ply.exists() else 0,
        "has_motion_diff": (event_dir / "motion_diff.jpg").exists(),
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


@app.route("/events/<event_id>/motion_diff.jpg")
@_require_pin
def serve_motion_diff(event_id):
    if not _valid_event_id(event_id):
        return "Not found", 404
    return send_from_directory(str(_events_dir() / event_id), "motion_diff.jpg")


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
# Settings
# ---------------------------------------------------------------------------

def _read_deployed_config() -> dict:
    """Read the config file the server was started with."""
    if _config_path and _config_path.exists():
        with open(_config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _write_deployed_config(raw: dict) -> str | None:
    """Write config; returns an error string on failure, None on success."""
    if not _config_path:
        return "No config file path — start the server with --config."
    try:
        with open(_config_path, "w") as f:
            yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return None
    except OSError as e:
        return f"Could not write config: {e}"


def _restart_service(name: str) -> bool:
    try:
        subprocess.run(["sudo", "systemctl", "restart", name],
                       check=True, timeout=15, capture_output=True)
        return True
    except Exception:
        return False


def _service_active(name: str) -> bool:
    r = subprocess.run(["systemctl", "is-active", "--quiet", name],
                       timeout=5, capture_output=True)
    return r.returncode == 0


@app.route("/settings", methods=["GET", "POST"])
@_require_pin
def settings_page():
    saved = []
    errors = []

    if request.method == "POST":
        raw = _read_deployed_config()

        def _set(section, key, cast, form_key):
            try:
                raw.setdefault(section, {})[key] = cast(request.form[form_key])
            except (KeyError, ValueError) as e:
                errors.append(f"{form_key}: {e}")

        _set("relay",     "lookback_s",         float, "relay_lookback_s")
        _set("pipeline",  "camera_hfov_deg",     float, "camera_hfov_deg")
        _set("pipeline",  "ply_depth_scale",     float, "ply_depth_scale")
        _set("pipeline",  "ply_downsample",      int,   "ply_downsample")
        raw.setdefault("pipeline", {})["ply_ground_correction"] = \
            "ply_ground_correction" in request.form

        _set("detection", "lookback_s",          float, "monitor_lookback_s")
        _set("detection", "min_changed_pct",     float, "monitor_min_changed_pct")
        _set("detection", "confirm_frames",      int,   "monitor_confirm_frames")
        _set("detection", "cooldown",            int,   "monitor_cooldown")
        _set("detection", "diff_display_threshold", int, "monitor_diff_display_threshold")

        new_pin = request.form.get("gallery_pin", "").strip()
        raw.setdefault("gallery", {})["pin"] = new_pin

        if not errors:
            write_err = _write_deployed_config(raw)
            if write_err:
                errors.append(write_err)
            else:
                relay_ok   = _restart_service("depth-relay")
                monitor_ok = _restart_service("depth-monitor")
                saved = ["Settings saved."]
                if not relay_ok:   saved.append("Warning: could not restart depth-relay.")
                if not monitor_ok: saved.append("Warning: could not restart depth-monitor.")
                saved.append("Gallery PIN/config changes take effect after the next manual gallery restart.")

    raw = _read_deployed_config()
    current = {
        "relay_lookback_s":              raw.get("relay", {}).get("lookback_s", 5),
        "camera_hfov_deg":               raw.get("pipeline", {}).get("camera_hfov_deg", 113.0),
        "ply_depth_scale":               raw.get("pipeline", {}).get("ply_depth_scale", 1.5),
        "ply_downsample":                raw.get("pipeline", {}).get("ply_downsample", 2),
        "ply_ground_correction":         raw.get("pipeline", {}).get("ply_ground_correction", True),
        "monitor_lookback_s":            raw.get("detection", {}).get("lookback_s", 2),
        "monitor_min_changed_pct":       raw.get("detection", {}).get("min_changed_pct", 20.0),
        "monitor_confirm_frames":        raw.get("detection", {}).get("confirm_frames", 3),
        "monitor_cooldown":              raw.get("detection", {}).get("cooldown", 300),
        "monitor_diff_display_threshold": raw.get("detection", {}).get("diff_display_threshold", 40),
        "gallery_pin":                   raw.get("gallery", {}).get("pin", ""),
        "monitor_active":                _service_active("depth-monitor"),
    }
    # Most recent snapshot + image dimensions for the FOV two-point tool
    recent_snap = None
    snap_width, snap_height = 2688, 1520  # Aqara G5 Pro defaults
    events_dir = _events_dir()
    if events_dir.exists():
        try:
            dirs = sorted(events_dir.iterdir(), key=lambda p: p.name, reverse=True)
            for d in dirs:
                if (d / "snapshot.jpg").exists():
                    recent_snap = d.name
                    meta_f = d / "metadata.json"
                    if meta_f.exists():
                        try:
                            meta = json.loads(meta_f.read_text())
                            if isinstance(meta, dict) and meta.get("image_size"):
                                snap_width, snap_height = meta["image_size"]
                        except (json.JSONDecodeError, OSError, TypeError, ValueError):
                            pass
                    break
        except OSError:
            pass

    return render_template("settings.html",
                           current=current,
                           camera_db=CAMERA_DB,
                           recent_snap=recent_snap,
                           snap_width=snap_width,
                           snap_height=snap_height,
                           saved=saved,
                           errors=errors)


@app.route("/api/probe-camera")
@_require_pin
def api_probe_camera():
    """Try to extract camera model from the RTSP stream metadata via ffprobe."""
    rtsp_url = _config.get("camera", {}).get("rtsp_url", "")
    if not rtsp_url or "USER" in rtsp_url:
        return jsonify({"model": None, "hfov": None, "raw": {}})

    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-rtsp_transport", "tcp", "-i", rtsp_url],
            capture_output=True, text=True, timeout=12,
        )
        data = json.loads(r.stdout or "{}")
        tags = data.get("format", {}).get("tags", {})
        model = (tags.get("model") or tags.get("encoder") or
                 tags.get("manufacturer") or None)
        # Try fuzzy match against CAMERA_DB
        hfov = None
        if model:
            for cam, info in CAMERA_DB.items():
                if cam.lower() in model.lower() or model.lower() in cam.lower():
                    w = data.get("streams", [{}])[0].get("width", 2688) if data.get("streams") else 2688
                    h = data.get("streams", [{}])[0].get("height", 1520) if data.get("streams") else 1520
                    hfov = _diag_to_hfov(info["diag_fov"], w, h)
                    break
        return jsonify({"model": model, "hfov": hfov, "tags": tags})
    except Exception as e:
        return jsonify({"model": None, "hfov": None, "error": str(e)})


@app.route("/api/monitor-toggle", methods=["POST"])
@_require_pin
def api_monitor_toggle():
    """Enable or disable the depth-monitor service."""
    action = request.json.get("action") if request.is_json else None
    if action not in ("start", "stop"):
        return jsonify({"ok": False, "error": "action must be start or stop"}), 400
    cmd = ["sudo", "systemctl", action, "depth-monitor"]
    try:
        subprocess.run(cmd, check=True, timeout=10, capture_output=True)
        return jsonify({"ok": True, "active": _service_active("depth-monitor")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/compute-hfov")
def api_compute_hfov():
    """Compute horizontal FOV from diagonal FOV and image dimensions."""
    try:
        diag = float(request.args["diag_fov"])
        w    = int(request.args.get("width",  2688))
        h    = int(request.args.get("height", 1520))
        return jsonify({"hfov": _diag_to_hfov(diag, w, h)})
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    global _config
    parser = argparse.ArgumentParser(description="Depth camera gallery server")
    parser.add_argument("--config", "-c", help="Path to YAML config file")
    args = parser.parse_args()

    global _config_path
    _config = load_config(args.config)
    _config_path = Path(args.config) if args.config else None

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
