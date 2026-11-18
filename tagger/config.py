"""Configuration: a JSON file, overlaid by TAGGER_* environment variables.

Env wins over file so a container can inject secrets without a config file at
all. Nothing is required at load: the tool starts with no keys and asks for them
in the page.
"""

import json
import math
import os

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config.json")

#: StashDB's rate limit; asking faster than this is never honoured.
MIN_REQUEST_INTERVAL = 0.34


class ConfigError(Exception):
    """Configuration is unreadable or cannot be written."""


DEFAULTS = {
    "stashdb_api_key": "",
    "stashdb_endpoint": "https://stashdb.org/graphql",
    "bind_port": 8426,
    "request_interval_seconds": MIN_REQUEST_INTERVAL,
    "state_db_path": "tagger-state.db",
    "pronoisseur_url": "http://127.0.0.1:8869",
    "pronoisseur_token": "",
    "pronoisseur_page_size": 500,
    # Review stills need ffmpeg; both default to a PATH lookup.
    "ffmpeg_path": "ffmpeg",
    "ffprobe_path": "ffprobe",
}


def _read_object(path):
    """Return the JSON object stored at path, or {} when the file is absent."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfigError("could not read %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise ConfigError("could not read %s: expected a JSON object" % path)
    return data


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _validate(cfg, where):
    """Refuse a value that would only fail later, far from its cause.

    A bool is not a number here: ``true`` in a JSON file would otherwise pass as
    a port of 1. ``where`` names the file or the variable the values came from.
    """
    for key in ("stashdb_api_key", "pronoisseur_token"):
        if key in cfg and not isinstance(cfg[key], str):
            raise ConfigError("%s: %s must be a string" % (where, key))
    if "request_interval_seconds" in cfg and not _is_number(
            cfg["request_interval_seconds"]):
        raise ConfigError("%s: request_interval_seconds must be a number of seconds"
                          % where)
    if "bind_port" in cfg:
        port = cfg["bind_port"]
        if (not isinstance(port, int) or isinstance(port, bool)
                or not 1 <= port <= 65535):
            raise ConfigError("%s: bind_port must be a whole number from 1 to 65535"
                              % where)


def load(path=None, env=None):
    """Return a complete config dict, or raise ConfigError for an unreadable file."""
    env = os.environ if env is None else env
    cfg = dict(DEFAULTS)

    if path:
        stored = _read_object(path)
        _validate(stored, path)
        cfg.update(stored)

    for key, default in DEFAULTS.items():
        raw = env.get("TAGGER_" + key.upper())
        if raw is None:
            continue
        try:
            cfg[key] = _coerce(raw, default)
        except ValueError as exc:
            raise ConfigError(
                "TAGGER_%s is not a valid %s: %r"
                % (key.upper(), type(default).__name__, raw)) from exc

    _validate(cfg, "the environment")
    # A key or token with a stray newline or space cannot go in a header, and the
    # HTTP layer would otherwise quote it back in its error.
    for key in ("stashdb_api_key", "pronoisseur_token"):
        cfg[key] = cfg[key].strip()

    if cfg["request_interval_seconds"] < MIN_REQUEST_INTERVAL:
        cfg["request_interval_seconds"] = MIN_REQUEST_INTERVAL
    return cfg


def save(path, updates):
    """Merge updates into the JSON file at path, replacing it atomically."""
    data = _read_object(path)
    data.update(updates)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ConfigError("could not write %s: %s" % (path, exc)) from exc


def _coerce(raw, default):
    """Coerce an environment string to the type of the default it overrides."""
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw
