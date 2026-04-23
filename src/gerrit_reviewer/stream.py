#!/usr/bin/env python3
"""Gerrit stream-events → Hermes webhooks.

Connects to Gerrit via SSH, listens to stream-events,
and forwards patchset-created events to Hermes webhook server.

Usage:
    gerrit-reviewer-stream
    gerrit-reviewer-stream --config ~/.gerrit-reviewer/config.yml

All settings are read from the unified config file (~/.gerrit-reviewer/config.yml).
Environment variables override config values for backward compatibility:

    GERRIT_SSH_HOST       Override SSH host (default: hostname from gerrit.url)
    GERRIT_SSH_PORT       Override gerrit.ssh_port
    GERRIT_SSH_USER       Override gerrit.username (SSH user)
    GERRIT_SSH_KEY        Override gerrit.ssh_key
    HERMES_URL            Override hermes.url
    HERMES_WEBHOOK_SECRET Override hermes.webhook_secret
    RECONNECT_DELAY       Override stream.reconnect_delay
    LOG_LEVEL             Override stream.log_level (DEBUG, INFO, WARNING, ERROR)
"""

import argparse
import hashlib
import hmac
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
    get_hermes_config,
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
    hermes = get_hermes_config(cfg)

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
        "hermes_url": env("HERMES_URL") or hermes.get("url", "http://127.0.0.1:8644"),
        "hermes_webhook_secret": env("HERMES_WEBHOOK_SECRET") or hermes.get("webhook_secret", ""),
        "allowed_events": ["patchset-created", "reviewer-added"],
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


def is_self_reviewer(change, gerrit_client: GerritClient, settings: dict) -> bool:
    """Check if the user is a reviewer on the given change."""
    if not gerrit_client:
        return False
    try:
        change = gerrit_client.changes.get(change)
        reviewers = change.reviewers.list()
        username = settings["gerrit_username"]
        for reviewer in reviewers:
            if reviewer.get("username") == username or reviewer.get("name") == username:
                return True
    except Exception as e:
        logger.debug("Failed to check reviewer status for change %s: %s", change, e)
    return False


def forward_to_hermes(change: str, patchset: str, settings: dict) -> bool:
    """Send payload to Hermes webhook. Returns True on success."""
    url = settings["hermes_url"]
    secret = settings["hermes_webhook_secret"]

    payload = {
        "event_type": "review_request",
        "change": change,
        "patchset": patchset,
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    payload_bytes = payload_json.encode("utf-8")

    # Compute HMAC signature
    signature = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()

    headers = {
        "X-Request-ID": f"review_require:{change}:{patchset}",
        "Content-Type": "application/json",
        "X-Webhook-Signature": signature,
    }

    try:
        with httpx.Client(timeout=30) as http:
            resp = http.post(url, content=payload_bytes, headers=headers)
        logger.info(
            "Forwarded change %s → Hermes: %d %s",
            payload.get("change", "unknown"), resp.status_code, resp.text[:200],
        )
        return resp.status_code < 400
    except Exception as e:
        logger.error("Failed to forward change %s: %s", payload.get("change", "unknown"), e)
        return False


def main():
    parser = argparse.ArgumentParser(
        prog="gerrit-reviewer-stream",
        description="Gerrit stream-events → Hermes webhooks",
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
    if not settings["hermes_webhook_secret"]:
        logger.error("hermes.webhook_secret must be set (config or HERMES_WEBHOOK_SECRET env var).")
        sys.exit(1)

    # Create Gerrit REST client for reviewer checks
    gerrit_client = None
    if settings["gerrit_url"] and settings["gerrit_username"]:
        gerrit_client = GerritClient(
            base_url=settings["gerrit_url"].rstrip("/"),
            username=settings["gerrit_username"],
            password=settings["gerrit_password"],
        )
    else:
        logger.error("gerrit.url and gerrit.username must be set to enable reviewer checks.")
        sys.exit(1)

    while not _shutdown:
        ssh_client = None
        try:
            ssh_client = connect_ssh(settings)
            for event in stream_events(ssh_client, settings):
                logger.debug("Raw event: %s", json.dumps(event, ensure_ascii=False))
                event_type = event.get("type", "")
                if event_type not in settings["allowed_events"]:
                    continue

                change = event.get("change", {}).get("number", "?")
                patchset = event.get("patchSet", {}).get("number", "?")
                project = event.get("change", {}).get("project", "")
                logger.info("Received %s for change %s/%s (project: %s)",
                    event_type, change, patchset, project)

                if event_type == "patchset-created":
                    kind = event.get("patchSet", {}).get("kind", "")
                    if kind != "REWORK":
                        logger.info("Skipping change %s: patchset kind is %s, not REWORK", change, kind)
                        continue
                    if not is_self_reviewer(change, gerrit_client, settings):
                        logger.info("Skipping change %s: user is not a reviewer", change)
                        continue
                elif event_type == "reviewer-added":
                    reviewer_username = event.get("reviewer", {}).get("username", "")
                    if reviewer_username != settings["gerrit_username"]:
                        logger.info("Skipping change %s: user is not a reviewer", change)
                        continue
                else:
                    logger.info("Skipping unsupported event type: %s", event_type)
                    continue

                forward_to_hermes(change, patchset, settings)

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
