#!/usr/bin/env python3
"""
Layered configuration shared by every depth-camera service.

Settings come from three layers; later layers win:

    config.yaml              Shipped defaults, documented. Replaced on every
                             upgrade, so don't edit the installed copy.
    /etc/depth-camera.yaml   Your changes, and only your changes. Upgrades
                             never touch it. The gallery Settings page writes
                             here too.
    Environment variables    Secrets, from /etc/depth-camera.env and
                             /etc/ntfy.env (see ENV_OVERRIDES).

Because the overrides file holds only what you changed, a new setting or a
better default reaches you on upgrade unless you've deliberately overridden it.

Usage:
    python3 config.py check [--config PATH]
        List your overrides next to the defaults they replace, and flag
        settings that don't exist (typos, or removed in this version).
    python3 config.py init PATH [--from OLD_CONFIG]
        Create an overrides file. With --from, keep only the values in an
        old full config.yaml that differ from the current defaults.
"""

import argparse
import copy
import logging
import os
import sys
import tempfile
from pathlib import Path

import yaml

DEFAULTS_PATH = Path(__file__).resolve().with_name("config.yaml")
OVERRIDES_PATH = Path("/etc/depth-camera.yaml")

# Environment variable -> (section, key). Applied last, only when set.
ENV_OVERRIDES = {
    "CAMERA_RTSP_URL": ("camera", "rtsp_url"),
    "HEALTHCHECK_RING_BUFFER_URL": ("notifications", "ring_buffer_heartbeat_url"),
    "HEALTHCHECK_WEBHOOK_URL": ("notifications", "webhook_heartbeat_url"),
    "NTFY_TOPIC_ALERTS": ("notifications", "ntfy_topic_url"),
}

OVERRIDES_HEADER = """\
# depth-camera local settings: only the values you've changed.
#
# Every setting and its default is documented in the installed config.yaml
# (/opt/depth-camera/config.yaml). Copy a key here, keeping its section, to
# override it. Delete a key to go back to the default, including any improved
# default a future upgrade brings. Upgrades never modify this file.
#
# The gallery Settings page also writes this file, and rewrites it without
# comments. Secrets belong in /etc/depth-camera.env, not here.
#
# Check it with:  python3 /opt/depth-camera/config.py check
"""

log = logging.getLogger("config")


# ---------------------------------------------------------------------------
# Dict helpers
# ---------------------------------------------------------------------------

def merge(base, override):
    """Return a deep copy of base with override's values layered on top."""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(result.get(k), dict):
            if isinstance(v, dict):
                result[k] = merge(result[k], v)
            # Otherwise keep the defaults. None is a section whose every key was
            # deleted (`detection:` alone); replacing the section with it would
            # crash every service. Other non-mappings are reported by
            # section_mismatches().
        else:
            result[k] = copy.deepcopy(v)
    return result


def diff(defaults, values):
    """Return the subset of values that differs from defaults (unknown keys included)."""
    out = {}
    for k, v in values.items():
        if isinstance(v, dict) and isinstance(defaults.get(k), dict):
            sub = diff(defaults[k], v)
            if sub:
                out[k] = sub
        elif k not in defaults or defaults[k] != v:
            out[k] = v
    return out


def unknown_keys(defaults, values, prefix=""):
    """Dotted paths in values that don't exist in defaults."""
    found = []
    for k, v in values.items():
        path = f"{prefix}{k}"
        if k not in defaults:
            found.append(path)
        elif isinstance(v, dict) and isinstance(defaults[k], dict):
            found += unknown_keys(defaults[k], v, f"{path}.")
    return found


def section_mismatches(defaults, values, prefix=""):
    """Dotted paths where defaults have a section but values have something
    other than a mapping. An empty section (None) is fine and not included."""
    found = []
    for k, v in values.items():
        if not isinstance(defaults.get(k), dict):
            continue
        if isinstance(v, dict):
            found += section_mismatches(defaults[k], v, f"{prefix}{k}.")
        elif v is not None:
            found.append(f"{prefix}{k}")
    return found


def _flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


# ---------------------------------------------------------------------------
# Loading and saving
# ---------------------------------------------------------------------------

def _read_yaml(path):
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping of sections")
    return data


def load_defaults():
    return _read_yaml(DEFAULTS_PATH)


def resolve_overrides_path(path=None):
    """The overrides file to use, given a --config argument (or None)."""
    if path is None:
        return OVERRIDES_PATH
    resolved = Path(path).resolve()
    if resolved == DEFAULTS_PATH:
        # Unit files from before the overrides layer pass --config config.yaml,
        # which is now the defaults file. Treat it as "no --config given".
        log.warning(f"--config {path} is the shipped defaults file; "
                    f"reading local settings from {OVERRIDES_PATH} instead. "
                    "Re-run setup.sh to update the unit files.")
        return OVERRIDES_PATH
    return resolved


def load_overrides(path=None):
    """The overrides file's contents, or {} if it doesn't exist."""
    resolved = resolve_overrides_path(path)
    return _read_yaml(resolved) if resolved.exists() else {}


def load_config(path=None):
    """Defaults, then the overrides file, then environment variables."""
    defaults = load_defaults()
    overrides = load_overrides(path)
    for key in unknown_keys(defaults, overrides):
        log.warning(f"Unknown setting '{key}' in {resolve_overrides_path(path)}: "
                    "not a setting in this version (typo, or removed?)")
    for key in section_mismatches(defaults, overrides):
        log.warning(f"'{key}' in {resolve_overrides_path(path)} should be a section "
                    "of settings, not a single value; ignoring it and using the defaults")
    config = merge(defaults, overrides)
    for var, (section, key) in ENV_OVERRIDES.items():
        if os.environ.get(var):
            config.setdefault(section, {})[key] = os.environ[var]
    return config


def save_overrides(overrides, path=None):
    """Write the overrides file, header included.

    Writes a temp file and renames it over the target, so a crash can't leave
    a truncated file (which would silently reset every setting to default).
    The service user can't create files in /etc, so the Settings page falls
    back to rewriting /etc/depth-camera.yaml in place, flushed to disk.
    """
    target = resolve_overrides_path(path)
    text = OVERRIDES_HEADER
    if overrides:
        text += "\n" + yaml.safe_dump(overrides, default_flow_style=False,
                                      allow_unicode=True, sort_keys=False)
    try:
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")  # mode 0600
    except PermissionError:
        with open(target, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        return
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if target.exists():
            st = target.stat()
            os.chmod(tmp, st.st_mode & 0o777)
            if os.geteuid() == 0:
                # Root editing on the user's behalf: keep the service user as owner.
                os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _display(key, value):
    """repr(value), except that a set PIN is masked so it never lands in a terminal log."""
    return "(set)" if key == "gallery.pin" and value else repr(value)


def _cmd_check(args):
    path = resolve_overrides_path(args.config)
    defaults = load_defaults()
    overrides = load_overrides(args.config)
    print(f"Defaults:  {DEFAULTS_PATH}")
    print(f"Overrides: {path}{'' if path.exists() else ' (not found; using defaults only)'}")
    flat_defaults = _flatten(defaults)
    unknown = set(unknown_keys(defaults, overrides))
    mismatched = set(section_mismatches(defaults, overrides))
    rows = _flatten(overrides)
    if not rows:
        print("\nNo overrides: every setting is at its default.")
    else:
        print()
        for key, value in rows.items():
            is_section = any(k.startswith(f"{key}.") for k in flat_defaults)
            if any(key == u or key.startswith(f"{u}.") for u in unknown):
                print(f"  ! {key} = {value!r}    (unknown setting, ignored by the code)")
            elif key in mismatched:
                print(f"  ! {key} = {value!r}    (should be a section of settings; ignored)")
            elif is_section and value is None:
                print(f"  = {key}:    (empty section; safe to delete)")
            elif key in flat_defaults and flat_defaults[key] == value:
                print(f"  = {key} = {value!r}    (same as default; safe to delete)")
            else:
                print(f"    {key} = {_display(key, value)}    (default {flat_defaults.get(key)!r})")
    return 1 if unknown or mismatched else 0


def _cmd_init(args):
    out = Path(args.path)
    if out.exists():
        print(f"{out} already exists; not overwriting.", file=sys.stderr)
        return 1
    overrides = {}
    if args.from_path:
        overrides = diff(load_defaults(), _read_yaml(args.from_path))
    save_overrides(overrides, out)
    if not overrides:
        print(f"Created {out} (no overrides).")
        return 0
    print(f"Created {out}, keeping {len(_flatten(overrides))} value(s) that differ "
          f"from the current defaults:")
    for key, value in _flatten(overrides).items():
        print(f"    {key} = {_display(key, value)}")
    print("Some of these may be old defaults rather than choices you made. "
          f"Delete any you don't want from {out}.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="depth-camera configuration tool")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check", help="show your overrides and flag unknown settings")
    p.add_argument("--config", "-c", help=f"overrides file (default {OVERRIDES_PATH})")
    p = sub.add_parser("init", help="create an overrides file")
    p.add_argument("path")
    p.add_argument("--from", dest="from_path", metavar="OLD_CONFIG",
                   help="keep only the values in this old full config that differ from the defaults")
    args = parser.parse_args()
    return {"check": _cmd_check, "init": _cmd_init}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
