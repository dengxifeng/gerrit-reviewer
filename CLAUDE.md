# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

gerrit-reviewer is an AI-powered code review system for Gerrit. It provides two CLI entry points:
- `gerrit-reviewer-cli` — CLI for querying changes, checking out patchsets, posting reviews, and managing reviewers
- `gerrit-reviewer-stream` — Long-running daemon that listens to Gerrit SSH stream-events and forwards them to Hermes webhook server for automated review

The system integrates with Hermes Agents: stream events trigger a Hermes webhook that can spawn an AI agent session for code analysis and review.

## Build & Install

```bash
pip install .               # install package
gerrit-reviewer-cli init    # interactive config + install skill + systemd service
```

Build system is Hatchling (pyproject.toml). Python 3.11+ required.

## Project Structure

All source code is under `src/gerrit_reviewer/`:

- **cli.py** — Main CLI entry point. Subcommands dispatch to `cmd_*` functions. Uses `python-gerrit-api` for REST calls and subprocess git for checkout operations. Repos are cached per-project under `~/.gerrit-reviewer/cache/`. The `init` command performs full automated setup: configures `~/.hermes/config.yaml` webhook platform, subscribes via `hermes webhook add`, installs skill, and sets up systemd services.
- **stream.py** — Event bridge daemon. Connects via paramiko SSH, parses JSON events line-by-line, filters by event type and project/reviewer logic, forwards to Hermes `/webhooks/{route_name}` endpoint with HMAC signature authentication, and auto-reconnects on SSH failures.
- **config.py** — Unified YAML config at `~/.gerrit-reviewer/config.yml` with three sections: `gerrit`, `stream`, `hermes`. The `hermes` section stores webhook delivery target (log/feishu/weixin). The webhook URL and HMAC secret are automatically populated by `init` command via `hermes webhook add`. Supports dotted-key access, type coercion from defaults, sensitive value masking, and env var overrides (in stream.py).
- **log_utils.py** — Rotating file logger setup for the stream daemon (`~/.gerrit-reviewer/logs/`). Accepts a `level` parameter (default `INFO`); stderr handler is always `INFO`.
- **skill/SKILL.md** — Hermes skill definition. Defines the review workflow (checkout → Claude Code review → post review) and notification output format. Installed to `~/.agents/skills/gerrit-reviewer/` during init.
- **systemd/** — User systemd service file for the stream daemon.

## Key Dependencies

- `python-gerrit-api` — Gerrit REST API client (GerritClient, changes, revisions)
- `paramiko` — SSH client for stream-events
- `httpx` — HTTP client for Hermes webhooks
- `pyyaml` — Config file parsing

## Architecture Notes

- CLI commands print JSON to stdout on success; failures print a plain-text error to stderr and exit with status 1
- The checkout flow uses git ref format `refs/changes/{short}/{number}/{patchset}` where `short` is the last 2 digits zero-padded
- `checkout` and `post-review` default to the current patchset unless `--patchset` is provided explicitly
- The stream daemon forwards structured JSON payload with event metadata (change number, patchset, project, owner, etc.) to Hermes webhook endpoint (URL obtained from `hermes webhook add` during init)
- The stream daemon uses HMAC-SHA256 signature authentication via `X-Webhook-Signature` header
- The stream daemon first checks whether the project is in `stream.allowed_projects`; if not, it falls back to checking whether the configured Gerrit user is already a reviewer on the change
- With the current implementation, an empty `stream.allowed_projects` does not mean "all projects"; it means only changes where the configured user is already a reviewer will pass the filter
- Config supports both interactive setup (`gerrit-reviewer-cli init`) and non-interactive (`--set key=value`)
- The `init` command performs full automated setup: (1) generates `~/.gerrit-reviewer/config.yml`, (2) configures Hermes webhook platform in `~/.hermes/config.yaml` (enables webhook platform, sets host/port/secret), (3) subscribes to Gerrit events via `hermes webhook add` and saves the returned URL and secret to config, (4) installs SKILL.md to `~/.agents/skills/gerrit-reviewer/`, (5) installs systemd services
- The skill is installed by copying the packaged `skill/` directory into `~/.agents/skills/gerrit-reviewer`
- The `uninstall` command removes the webhook subscription via `hermes webhook rm`, removes the skill directory, and uninstalls systemd services
- Uninstall flow for systemd services follows the proper order: `stop` → `disable` → delete file → `daemon-reload`
- Importing `stream.py` initializes file logging at `INFO` via `setup_logging("gerrit-event.log")`; the log level is reconfigured in `main()` from `stream.log_level` config (or `LOG_LEVEL` env var). When set to `DEBUG`, raw Gerrit events are logged to the file; stderr stays at `INFO`.
- Review scores are constrained to -1/0/+1 in the automated workflow; +2/-2 and submit require explicit user instruction

## No Tests or Linting

There is currently no test suite, CI/CD, or linting configuration.
