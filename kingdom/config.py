"""Loads the repo's config.toml and merges the user's on top."""
import re
import tomllib
from pathlib import Path

from . import legacy, paths


def _merge(base: dict, over: dict) -> dict:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load(extra: Path | None = None) -> dict:
    paths.migrate_legacy()
    with open(paths.DEFAULT_CONFIG, "rb") as f:
        cfg = tomllib.load(f)
    for path in (paths.user_config(), extra):
        if path and Path(path).exists():
            with open(path, "rb") as f:
                _merge(cfg, tomllib.load(f))
    return legacy.config(cfg)            # a user config from before 0.4 may use Spanish power names


def set_top_level(key: str, value: str):
    """Writes `key = "value"` into the user config (above the tables),
    keeping the rest of the file."""
    path = paths.user_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    line = f'{key} = "{value}"'
    for k, text in enumerate(lines):
        if re.match(rf"\s*{re.escape(key)}\s*=", text):
            lines[k] = line
            break
        if text.lstrip().startswith("["):
            lines.insert(k, line)
            break
    else:
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
