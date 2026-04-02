#!/usr/bin/env python3
"""Gerrit stream-events → OpenClaw webhooks.

Connects to Gerrit via SSH, listens to stream-events,
and forwards patchset-created events to OpenClaw via /hooks/agent webhook.

Usage:
    gerrit-reviewer-stream
    gerrit-reviewer-stream --config ~/.gerrit-reviewer/config.yml

All settings are read from the unified config file (~/.gerrit-reviewer/config.yml).
Environment variables override config values for backward compatibility:

    GERRIT_SSH_HOST       Override SSH host (default: hostname from gerrit.url)
    GERRIT_SSH_PORT       Override gerrit.ssh_port
    GERRIT_SSH_USER       Override gerrit.username (SSH user)
    GERRIT_SSH_KEY        Override gerrit.ssh_key
    OPENCLAW_URL          Override openclaw.url
    OPENCLAW_HOOK_TOKEN   Override hooks.token from openclaw.json
    OPENCLAW_AGENT_ID     Override openclaw.agent_id
    DELIVER_CHANNEL       Override openclaw.channel
    DELIVER_TO            Override openclaw.to
    ALLOWED_EVENTS        Override stream.allowed_events (comma-separated)
    ALLOWED_PROJECTS      Override stream.allowed_projects (comma-separated)
    RECONNECT_DELAY       Override stream.reconnect_delay
    LOG_LEVEL             Override stream.log_level (DEBUG, INFO, WARNING, ERROR)
"""

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

import httpx
import paramiko
from gerrit import GerritClient

from gerrit_reviewer.config import (
    DEFAULT_CONFIG_PATH,
    get_gerrit_config,
    get_gerrit_host,
    get_openclaw_config,
    get_openclaw_hook_token,
    get_stream_config,
    load_config,
)
from gerrit_reviewer.log_utils import setup_logging

logger = setup_logging("gerrit-event.log")

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %s, shutting down...", signum)
    _shutdown = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def _load_stream_settings(config_path: str = None) -> dict:
    """Load settings from config file with env var overrides."""
    cfg = load_config(config_path)
    gerrit = get_gerrit_config(cfg)
    stream = get_stream_config(cfg)
    openclaw = get_openclaw_config(cfg)

    def env(key: str) -> str | None:
        """Get env var, return None if not set."""
        return os.environ.get(key)

    return {
        "ssh_host": env("GERRIT_SSH_HOST") or get_gerrit_host(cfg),
        "ssh_port": int(env("GERRIT_SSH_PORT") or gerrit.get("ssh_port", 29418)),
        "ssh_user": env("GERRIT_SSH_USER") or gerrit.get("username", ""),
        "ssh_key": env("GERRIT_SSH_KEY") or gerrit.get("ssh_key", str(Path.home() / ".ssh" / "id_rsa")),
        "gerrit_url": gerrit.get("url", ""),
        "gerrit_username": gerrit.get("username", ""),
        "gerrit_password": gerrit.get("credential", ""),
        "openclaw_url": env("OPENCLAW_URL") or openclaw.get("url", "http://127.0.0.1:18789"),
        "openclaw_hook_token": env("OPENCLAW_HOOK_TOKEN") or get_openclaw_hook_token(),
        "openclaw_agent_id": env("OPENCLAW_AGENT_ID") or openclaw.get("agent_id", "main"),
        "deliver_channel": env("DELIVER_CHANNEL") or openclaw.get("channel", ""),
        "deliver_to": env("DELIVER_TO") or openclaw.get("to", ""),
        "allowed_events": set(
            env("ALLOWED_EVENTS").split(",") if env("ALLOWED_EVENTS")
            else stream.get("allowed_events", ["patchset-created"])
        ),
        "allowed_projects": set(
            p.strip() for p in env("ALLOWED_PROJECTS").split(",") if p.strip()
        ) if env("ALLOWED_PROJECTS") else set(stream.get("allowed_projects", [])),
        "reconnect_delay": int(env("RECONNECT_DELAY") or stream.get("reconnect_delay", 5)),
        "log_level": env("LOG_LEVEL") or stream.get("log_level", "INFO"),
    }


def connect_ssh(settings: dict) -> paramiko.SSHClient:
    """Establish SSH connection to Gerrit."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    key_path = Path(settings["ssh_key"])
    pkey = None
    if key_path.exists():
        try:
            pkey = paramiko.RSAKey.from_private_key_file(str(key_path))
        except paramiko.ssh_exception.SSHException:
            try:
                pkey = paramiko.Ed25519Key.from_private_key_file(str(key_path))
            except paramiko.ssh_exception.SSHException:
                pkey = paramiko.ECDSAKey.from_private_key_file(str(key_path))

    logger.info(
        "Connecting to %s@%s:%d ...",
        settings["ssh_user"], settings["ssh_host"], settings["ssh_port"],
    )
    client.connect(
        hostname=settings["ssh_host"],
        port=settings["ssh_port"],
        username=settings["ssh_user"],
        pkey=pkey,
        timeout=30,
    )

    # Enable SSH keepalive packets every 30 seconds to prevent silent disconnects
    transport = client.get_transport()
    if transport:
        transport.set_keepalive(30)

    logger.info("SSH connection established.")
    return client


def stream_events(ssh_client: paramiko.SSHClient, settings: dict):
    """Execute gerrit stream-events and yield parsed JSON events."""
    event_filter = " ".join(f"-s {e}" for e in settings["allowed_events"])
    cmd = f"gerrit stream-events {event_filter}"
    logger.info("Running: %s", cmd)

    transport = ssh_client.get_transport()
    channel = transport.open_session()
    channel.exec_command(cmd)

    buf = ""
    while not _shutdown:
        if channel.closed or channel.exit_status_ready():
            exit_code = channel.recv_exit_status() if channel.exit_status_ready() else "unknown"
            logger.warning("stream-events channel closed (exit=%s)", exit_code)
            break

        if not transport.is_active():
            logger.warning("SSH transport dropped silently")
            break

        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Failed to parse JSON: %s", line[:200])
        else:
            time.sleep(0.1)

    channel.close()


def build_prompt(event: dict) -> str:
    """Build OpenClaw prompt from a Gerrit event."""
    change = event.get("change", {})
    change_number = change.get("number", "unknown")
    return f"/gerrit-reviewer {change_number}"


def is_self_reviewer(change_number, gerrit_client: GerritClient, settings: dict) -> bool:
    """Check if the user is a reviewer on the given change."""
    if not gerrit_client:
        return False
    try:
        change = gerrit_client.changes.get(change_number)
        reviewers = change.reviewers.list()
        username = settings["gerrit_username"]
        for reviewer in reviewers:
            if reviewer.get("username") == username or reviewer.get("name") == username:
                return True
    except Exception as e:
        logger.debug("Failed to check reviewer status for change %s: %s", change_number, e)
    return False


def forward_to_openclaw(prompt: str, change_number, settings: dict) -> bool:
    """Send prompt to OpenClaw via /hooks/agent webhook. Returns True on success."""
    session_key = f"hook:gerrit-review:{change_number}"

    payload = {
        "message": prompt,
        "sessionKey": session_key,
        "agentId": settings["openclaw_agent_id"],
        "wakeMode": "now",
    }
    if settings.get("deliver_channel") and settings.get("deliver_to"):
        payload["deliver"] = True
        payload["channel"] = settings["deliver_channel"]
        payload["to"] = settings["deliver_to"]

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings['openclaw_hook_token']}",
    }

    try:
        with httpx.Client(timeout=30) as http:
            resp = http.post(
                f"{settings['openclaw_url']}/hooks/gerrit-review",
                json=payload,
                headers=headers,
            )
        logger.info(
            "Forwarded change %s → OpenClaw: %d %s",
            change_number, resp.status_code, resp.text[:200],
        )
        return resp.status_code < 400
    except Exception as e:
        logger.error("Failed to forward change %s: %s", change_number, e)
        return False


def main():
    parser = argparse.ArgumentParser(
        prog="gerrit-reviewer-stream",
        description="Gerrit stream-events → OpenClaw webhooks",
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to config YAML file (default: {DEFAULT_CONFIG_PATH})",
    )
    args = parser.parse_args()

    settings = _load_stream_settings(args.config)

    # Reconfigure log level from config
    import logging as _logging
    log_level = getattr(_logging, settings["log_level"].upper(), _logging.INFO)
    logger.setLevel(log_level)
    for handler in logger.handlers:
        if isinstance(handler, _logging.handlers.RotatingFileHandler):
            handler.setLevel(log_level)

    if not settings["ssh_host"] or not settings["ssh_user"]:
        logger.error("gerrit.url and gerrit.username must be set.")
        sys.exit(1)
    if not settings["openclaw_hook_token"]:
        logger.error("openclaw.hook_token must be set (config or OPENCLAW_HOOK_TOKEN env var).")
        sys.exit(1)

    logger.info("Config: %s", args.config)
    if settings["allowed_projects"]:
        logger.info("Filtering projects: %s", settings["allowed_projects"])

    # Create Gerrit REST client for reviewer checks
    gerrit_client = None
    if settings["gerrit_url"] and settings["gerrit_username"]:
        gerrit_client = GerritClient(
            base_url=settings["gerrit_url"].rstrip("/"),
            username=settings["gerrit_username"],
            password=settings["gerrit_password"],
        )

    while not _shutdown:
        ssh_client = None
        try:
            ssh_client = connect_ssh(settings)
            for event in stream_events(ssh_client, settings):
                logger.debug("Raw event: %s", json.dumps(event, ensure_ascii=False))
                event_type = event.get("type", "")
                if event_type not in settings["allowed_events"]:
                    continue

                change_number = event.get("change", {}).get("number", "?")
                project = event.get("change", {}).get("project", "")
                logger.info("Received %s for change %s (project: %s)", event_type, change_number, project)

                in_allowed = project in settings["allowed_projects"]
                if not in_allowed and not is_self_reviewer(change_number, gerrit_client, settings):
                    logger.info(
                        "Skipping change %s: project %s not in allowed_projects and user is not a reviewer",
                        change_number, project,
                    )
                    continue

                prompt = build_prompt(event)
                forward_to_openclaw(prompt, change_number, settings)

        except paramiko.ssh_exception.SSHException as e:
            logger.error("SSH error: %s", e)
        except Exception as e:
            logger.error("Unexpected error: %s", e)
        finally:
            if ssh_client:
                try:
                    ssh_client.close()
                except Exception:
                    pass

        if not _shutdown:
            logger.info("Reconnecting in %ds...", settings["reconnect_delay"])
            time.sleep(settings["reconnect_delay"])

    logger.info("Bridge stopped.")


if __name__ == "__main__":
    main()
