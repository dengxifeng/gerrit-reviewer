# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

gerrit-reviewer is an AI-powered code review system for Gerrit. It provides two CLI entry points:
- `gerrit-reviewer-cli` — CLI for querying changes, checking out patchsets, posting reviews, and managing reviewers
- `gerrit-reviewer-stream` — Long-running daemon that listens to Gerrit SSH stream-events and forwards them to OpenClaw webhooks for automated review

The system integrates with OpenClaw as a skill: stream events trigger an OpenClaw agent that checks out code, spawns a Claude ACP session for analysis, and posts structured review comments back to Gerrit.

## Build & Install

```bash
pip install .               # install package
gerrit-reviewer-cli init    # interactive config + install skill + systemd service
```

Build system is Hatchling (pyproject.toml). Python 3.11+ required.

## Project Structure

All source code is under `src/gerrit_reviewer/`:

- **cli.py** — Main CLI entry point. Subcommands dispatch to `cmd_*` functions. Uses `python-gerrit-api` for REST calls and subprocess git for checkout operations. Repos are cached per-project under `~/.gerrit-reviewer/cache/`.
- **stream.py** — Event bridge daemon. Connects via paramiko SSH, parses JSON events line-by-line, filters by event type and project/reviewer logic, forwards to OpenClaw `/hooks/gerrit-review` endpoint, and auto-reconnects on SSH failures.
- **config.py** — Unified YAML config at `~/.gerrit-reviewer/config.yml` with two sections: `gerrit`, `stream`. The `openclaw` section only stores non-sensitive settings (`url`, `agent_id`, `channel`, `to`). The webhook hook token is stored in `~/.openclaw/openclaw.json` under `hooks.token` and read via `get_openclaw_hook_token()`. Supports dotted-key access, type coercion from defaults, sensitive value masking, and env var overrides (in stream.py).
- **log_utils.py** — Rotating file logger setup for the stream daemon (`~/.gerrit-reviewer/logs/`). Accepts a `level` parameter (default `INFO`); stderr handler is always `INFO`.
- **skill/SKILL.md** — OpenClaw skill definition. Defines the review workflow (checkout → ACP Claude session → post review) and notification output format.
- **hooks/transforms/gerrit-review.js** — OpenClaw webhook transform. Converts the incoming webhook payload into a wake action with session key, agent routing, and delivery settings. Installed to `~/.openclaw/hooks/transforms/` during init.
- **systemd/** — User systemd service file for the stream daemon.

## Key Dependencies

- `python-gerrit-api` — Gerrit REST API client (GerritClient, changes, revisions)
- `paramiko` — SSH client for stream-events
- `httpx` — HTTP client for OpenClaw webhooks
- `pyyaml` — Config file parsing

## Architecture Notes

- CLI commands print JSON to stdout on success; failures print a plain-text error to stderr and exit with status 1
- The checkout flow uses git ref format `refs/changes/{short}/{number}/{patchset}` where `short` is the last 2 digits zero-padded
- `checkout` and `post-review` default to the current patchset unless `--patchset` is provided explicitly
- The stream daemon forwards prompt text as `/gerrit-reviewer-cli <change_number>` and uses session key `hook:gerrit-review:<change_number>`
- The stream daemon first checks whether the project is in `stream.allowed_projects`; if not, it falls back to checking whether the configured Gerrit user is already a reviewer on the change
- With the current implementation, an empty `stream.allowed_projects` does not mean "all projects"; it means only changes where the configured user is already a reviewer will pass the filter
- Config supports both interactive setup (`gerrit-reviewer-cli init`) and non-interactive (`--set key=value`)
- The skill is installed by copying the packaged `skill/` directory into `~/.openclaw/skills/gerrit-reviewer`
- The webhook transform is installed by copying `hooks/transforms/gerrit-review.js` into `~/.openclaw/hooks/transforms/`; the `init` command also configures `openclaw.json` hooks section (`enabled`, `token`, `path`, `allowRequestSessionKey`, `mappings`) via a single `openclaw config set --batch-json` call, generating a random token with `secrets.token_hex(24)`
- The stream daemon reads the hook token from `~/.openclaw/openclaw.json` (`hooks.token`) via `get_openclaw_hook_token()`; the `OPENCLAW_HOOK_TOKEN` env var can still override it
- Uninstall flow for systemd services follows the proper order: `stop` → `disable` → delete file → `daemon-reload`
- Importing `stream.py` initializes file logging at `INFO` via `setup_logging("gerrit-event.log")`; the log level is reconfigured in `main()` from `stream.log_level` config (or `LOG_LEVEL` env var). When set to `DEBUG`, raw Gerrit events are logged to the file; stderr stays at `INFO`.
- Review scores are constrained to -1/0/+1 in the automated workflow; +2/-2 and submit require explicit user instruction

## No Tests or Linting

There is currently no test suite, CI/CD, or linting configuration.
