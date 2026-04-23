# gerrit-reviewer

AI-powered code review system for Gerrit. It listens to Gerrit events and uses AI to automatically review code changes, posting structured comments back to Gerrit. Integrates with [Hermes](https://hermes-agent.nousresearch.com) for agent-based automated review workflows.

## Features

- **Automated Code Review** — Monitors Gerrit stream events, automatically triggers AI-powered code review on new patchsets, and posts structured review comments with scores back to Gerrit.
- **CLI Tools** — Query changes, checkout patchsets, post reviews, manage reviewers, approve and submit changes — all from the command line.
- **Stream Daemon** — Long-running service that connects to Gerrit via SSH, listens for events (`patchset-created`, `reviewer-added`), and forwards them to [Hermes](https://hermes-agent.nousresearch.com) webhooks for automated review.
- **Hermes Integration** — Works as a Hermes skill: stream events trigger a Hermes agent that checks out code, invoke Claude for analysis, and posts review results.
- **Flexible Configuration** — Unified YAML config with environment variable overrides and reviewer-based filtering.
- **Systemd Service** — Ships with a user systemd service file for running the stream daemon in the background.

## Requirements

- Python 3.11+
- Git
- Access to a Gerrit instance (with SSH and REST API)
- [Hermes](https://hermes-agent.nousresearch.com) (for automated review workflow)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — The automated review workflow depends on Claude Code. Please ensure Claude Code is installed and initialized (`claude` command available) before use.

> **Note:** If you are using a third-party API provider instead of the official Anthropic API, add the following to `~/.hermes/.env`:
>
> ```bash
> ANTHROPIC_BASE_URL=https://your-api-provider.example.com
> ANTHROPIC_AUTH_TOKEN=your-token
> ```

## Installation

### From source

```bash
pip install .
```

### Initialize

Run the interactive setup wizard to configure Gerrit credentials, install the Hermes skill, webhook subscription, and systemd service:

```bash
gerrit-reviewer-cli init
```

This will:
1. Prompt for Gerrit URL, username, credential, and SSH key path
2. Generate the config file at `~/.gerrit-reviewer/config.yml`
3. Install the Hermes skill to `~/.agents/skills/gerrit-reviewer`
4. Subscribe to Gerrit events via Hermes webhook
5. Set up a user systemd service for the stream daemon

You can also set config values non-interactively:

```bash
gerrit-reviewer-cli config --set gerrit.url=https://gerrit.example.com
gerrit-reviewer-cli config --set gerrit.username=your-username
```

### Uninstall

```bash
gerrit-reviewer-cli uninstall
```

## Usage

### CLI (`gerrit-reviewer-cli`)

```bash
# View current configuration
gerrit-reviewer-cli config

# List open changes
gerrit-reviewer-cli list-changes --query "status:open"

# Get diff for a change
gerrit-reviewer-cli get-diff <change_number>

# Checkout a patchset locally (cached per project)
gerrit-reviewer-cli checkout <change_number> [--patchset N]

# Post a review comment with score
gerrit-reviewer-cli post-review <change_number> --message "LGTM" --score 1

# Add/remove reviewers
gerrit-reviewer-cli add-reviewer <change_number> --reviewer user@example.com
gerrit-reviewer-cli remove-reviewer <change_number> --reviewer user@example.com

# Approve and submit
gerrit-reviewer-cli approve <change_number>
gerrit-reviewer-cli submit <change_number>

# Clean up work directory for a patchset
gerrit-reviewer-cli cleanup <change_number> --patchset N
```

### Stream Daemon (`gerrit-reviewer-stream`)

```bash
# Start the stream daemon
gerrit-reviewer-stream

# With a custom config file
gerrit-reviewer-stream --config /path/to/config.yml

# Or run via systemd
systemctl --user start gerrit-reviewer-stream
systemctl --user enable gerrit-reviewer-stream
```

Environment variables can override config values:

| Variable | Description |
|---|---|
| `GERRIT_SSH_HOST` | SSH host (default: hostname from `gerrit.url`) |
| `GERRIT_SSH_PORT` | SSH port (default: 29418) |
| `GERRIT_SSH_USER` | SSH username |
| `GERRIT_SSH_KEY` | Path to SSH private key |
| `HERMES_URL` | Hermes webhook server URL |
| `HERMES_WEBHOOK_SECRET` | Webhook HMAC secret |
| `RECONNECT_DELAY` | Reconnect delay in seconds |
| `LOG_LEVEL` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## Build

The project uses [Hatchling](https://hatch.pypa.io/) as the build backend.

```bash
# Build a wheel and sdist
pip install build
python -m build

# Install in editable mode for development
pip install -e .
```

## Contributing

### Project Structure

```
src/gerrit_reviewer/
├── cli.py          # CLI entry point and subcommands
├── stream.py       # Stream-events daemon
├── config.py       # Unified YAML config management
├── log_utils.py    # Rotating file logger setup
├── skill/          # Hermes skill definition
│   └── SKILL.md
└── systemd/        # User systemd service file
```

### Key Dependencies

- [python-gerrit-api](https://github.com/shijl0925/python-gerrit-api) — Gerrit REST API client
- [paramiko](https://www.paramiko.org/) — SSH client for stream-events
- [httpx](https://www.python-httpx.org/) — HTTP client for webhooks
- [PyYAML](https://pyyaml.org/) — Config file parsing

### Development Setup

```bash
# Clone the repo
git clone <repo-url>
cd gerrit-reviewer

# Install in editable mode
pip install -e .
```

### Notes

- CLI commands output JSON to stdout on success; errors go to stderr with exit code 1.
- Review scores in the automated workflow are constrained to -1/0/+1; +2/-2 and submit require explicit user instruction.
- The stream daemon auto-reconnects on SSH failures.
- The stream daemon processes `patchset-created` events only for `REWORK` kind patchsets where the configured user is already a reviewer, and `reviewer-added` events when the configured user is the added reviewer.

## License

See [LICENSE](LICENSE) for details.
