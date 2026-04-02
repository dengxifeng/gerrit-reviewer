"""Shared configuration for gerrit-reviewer.

Unified YAML config at ~/.gerrit-reviewer/config.yml used by both
the reviewer CLI and the stream-events bridge.
"""

import copy
import json
from pathlib import Path
from urllib.parse import urlparse

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".gerrit-reviewer" / "config.yml"

DEFAULT_CONFIG = {
    "gerrit": {
        "url": "https://gerrit.example.com",
        "username": "",
        "credential": "",
        "ssh_port": 29418,
        "ssh_key": str(Path.home() / ".ssh" / "id_rsa"),
        "cache_dir": str(Path.home() / ".gerrit-reviewer" / "cache"),
        # "clone_url": "ssh://{username}@{host}:{port}/{project}",
    },
    "stream": {
        "allowed_events": ["patchset-created"],
        "allowed_projects": [],
        "reconnect_delay": 5,
    },
    "openclaw": {
        "url": "http://127.0.0.1:18789",
        "agent_id": "main",
        "channel": "",
        "to": "",
    },
}

SENSITIVE_KEYS = {"credential"}

# Fields to prompt during interactive init, in order.
# (dotted_key, description)
INIT_FIELDS = [
    ("gerrit.url", "Gerrit URL"),
    ("gerrit.username", "Gerrit username"),
    ("gerrit.credential", "Gerrit HTTP credential"),
    ("gerrit.ssh_port", "Gerrit SSH port"),
    ("gerrit.ssh_key", "SSH private key path"),
    ("gerrit.cache_dir", "Git repo cache directory"),
    ("openclaw.url", "OpenClaw URL"),
    ("openclaw.agent_id", "OpenClaw agent ID"),
    ("openclaw.channel", "Delivery channel (e.g. feishu, optional)"),
    ("openclaw.to", "Delivery target (e.g. chat ID, optional)"),
    ("stream.allowed_events", "Allowed events (comma-separated)"),
    ("stream.allowed_projects", "Allowed projects (comma-separated)"),
    ("stream.reconnect_delay", "Reconnect delay in seconds"),
]


def load_config(path: str | Path = None) -> dict:
    """Load config from YAML file, merged with defaults."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path.exists():
        with open(path) as f:
            user_cfg = yaml.safe_load(f) or {}
        _deep_merge(cfg, user_cfg)
    return cfg


def save_config(cfg: dict, path: str | Path = None):
    """Write config dict to YAML file."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def get_gerrit_host(cfg: dict) -> str:
    """Extract hostname from gerrit.url."""
    return urlparse(cfg.get("gerrit", {}).get("url", "").rstrip("/")).hostname or ""


def get_gerrit_config(cfg: dict) -> dict:
    return cfg.get("gerrit", {})


def get_stream_config(cfg: dict) -> dict:
    return cfg.get("stream", {})


def get_openclaw_config(cfg: dict) -> dict:
    return cfg.get("openclaw", {})


OPENCLAW_JSON_PATH = Path.home() / ".openclaw" / "openclaw.json"


def get_openclaw_hook_token(openclaw_json_path: Path = None) -> str:
    """Read hooks.token from openclaw.json."""
    path = openclaw_json_path or OPENCLAW_JSON_PATH
    if not path.exists():
        return ""
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("hooks", {}).get("token", "")
    except (json.JSONDecodeError, OSError):
        return ""


def config_get(cfg: dict, dotted_key: str):
    """Get a value by dotted key path, e.g. 'gerrit.url'."""
    keys = dotted_key.split(".")
    node = cfg
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            raise KeyError(f"Key not found: {dotted_key}")
        node = node[k]
    return node


def config_set(cfg: dict, dotted_key: str, value: str) -> dict:
    """Set a value by dotted key path. Coerces types based on defaults."""
    keys = dotted_key.split(".")
    if len(keys) < 2:
        raise KeyError(f"Key must have at least two parts: {dotted_key}")

    # Navigate to parent
    node = cfg
    for k in keys[:-1]:
        if k not in node or not isinstance(node[k], dict):
            node[k] = {}
        node = node[k]

    # Coerce type based on default config
    default_value = _get_default(keys)
    if isinstance(default_value, int):
        value = int(value)
    elif isinstance(default_value, list):
        value = [v.strip() for v in value.split(",") if v.strip()]
    elif isinstance(default_value, bool):
        value = value.lower() in ("true", "1", "yes")

    node[keys[-1]] = value
    return cfg


def mask_sensitive(cfg: dict) -> dict:
    """Return a copy with sensitive values masked."""
    masked = copy.deepcopy(cfg)
    _mask_recursive(masked)
    return masked


def _mask_recursive(node: dict):
    for key, val in node.items():
        if isinstance(val, dict):
            _mask_recursive(val)
        elif key in SENSITIVE_KEYS and val:
            node[key] = "***"


def _deep_merge(base: dict, override: dict):
    """Merge override into base in-place."""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _get_default(keys: list[str]):
    """Get default value for a key path."""
    node = DEFAULT_CONFIG
    for k in keys:
        if isinstance(node, dict) and k in node:
            node = node[k]
        else:
            return None
    return node


def interactive_config(config_path: Path = None) -> dict:
    """Interactive config initialization. Returns the final config dict."""
    config_path = config_path or DEFAULT_CONFIG_PATH

    if config_path.exists():
        answer = input(f"Config exists at {config_path}.\nKeep current values? [Y/n] ").strip().lower()
        if answer in ("n", "no"):
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            print("Reset to defaults. Configure below:\n")
        else:
            cfg = load_config(config_path)
            print("Keeping current config.")
            return cfg
    else:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        print(f"Creating new config at {config_path}\n")

    for dotted_key, description in INIT_FIELDS:
        current = config_get(cfg, dotted_key)
        # Format display value
        if isinstance(current, list):
            display = ",".join(current) if current else ""
        else:
            display = str(current) if current else ""

        prompt = f"  {description} [{display}]: "
        user_input = input(prompt).strip()

        if user_input:
            config_set(cfg, dotted_key, user_input)

    save_config(cfg, config_path)
    print(f"\nConfig saved to {config_path}")
    return cfg
