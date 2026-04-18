#!/usr/bin/env python3
"""gerrit-reviewer-cli: AI-powered code review CLI for Gerrit.

Default config: ~/.gerrit-reviewer/config.yml

Subcommands:
    config         View and edit configuration
    init           Generate config, install skill, and install systemd services
    uninstall      Remove skill and systemd services
    list-changes   Query changes from Gerrit
    get-diff       Get change details and file diffs via Gerrit API
    checkout       Clone/fetch repo and checkout a patchset (cached per project)
    post-review    Post review comments and score
    add-reviewer   Add reviewers to a change
    remove-reviewer Remove reviewers from a change
    approve        Approve a change with labels
    submit         Submit (merge) a change
"""

import argparse
import json
import secrets
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
import httpx
import time

import yaml
from gerrit import GerritClient
from gerrit.utils.exceptions import ConflictError

from gerrit_reviewer.config import (
    DEFAULT_CONFIG_PATH,
    config_get,
    config_set,
    get_gerrit_config,
    interactive_config,
    load_config,
    mask_sensitive,
    save_config,
)


def make_client(gerrit_cfg: dict) -> GerritClient:
    base_url = gerrit_cfg["url"].rstrip("/")
    return GerritClient(
        base_url=base_url,
        username=gerrit_cfg["username"],
        password=gerrit_cfg["credential"],
    )


def get_cache_root(cfg: dict) -> Path:
    cache_dir = cfg.get("cache_dir", str(Path.home() / ".gerrit-reviewer" / "cache"))
    return Path(cache_dir)


def get_clone_url(cfg: dict, project: str) -> str:
    template = cfg.get("clone_url")
    if template:
        return template.replace("{project}", project)
    from urllib.parse import urlparse
    parsed = urlparse(cfg["url"].rstrip("/"))
    host = parsed.hostname
    username = cfg.get("username", "")
    ssh_port = cfg.get("ssh_port", 29418)
    return f"ssh://{username}@{host}:{ssh_port}/{project}"


def _run_git(args: list[str], cwd: str = None) -> str:
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _checkout_patchset(client: GerritClient, cfg: dict, change_id, patchset="current"):
    """Shared checkout logic. Returns dict with workdir, branch, change, patchset, ref."""
    change = client.changes.get(change_id)
    project = change.project
    branch = change.branch
    change_number = change.number

    if patchset == "current":
        detail = client.get(f"/changes/{change_id}/detail?o=CURRENT_REVISION")
        current_rev = detail.get("current_revision", "")
        patchset_num = detail.get("revisions", {}).get(current_rev, {}).get("_number", 1)
    else:
        patchset_num = int(patchset)

    short = str(change_number)[-2:].zfill(2)
    ref = f"refs/changes/{short}/{change_number}/{patchset_num}"

    cache_root = get_cache_root(cfg)
    clone_url = get_clone_url(cfg, project)
    project_dir = cache_root / project

    if project_dir.exists():
        if (project_dir / ".git").exists():
            _run_git(["fetch", "origin"], cwd=str(project_dir))
        else:
            shutil.rmtree(project_dir)
            _run_git(["clone", clone_url, str(project_dir)])
    else:
        cache_root.mkdir(parents=True, exist_ok=True)
        _run_git(["clone", clone_url, str(project_dir)])

    _run_git(["clean", "-fd"], cwd=str(project_dir))
    _run_git(["checkout", "-f", f"origin/{branch}"], cwd=str(project_dir))
    _run_git(["fetch", "origin", ref], cwd=str(project_dir))
    _run_git(["checkout", "-f", "FETCH_HEAD"], cwd=str(project_dir))

    # Diff stats
    diff_stat = _run_git(["diff", "--stat", "HEAD~1..HEAD"], cwd=str(project_dir))

    return {
        "workdir": str(project_dir),
        "project": project,
        "branch": branch,
        "change": change_number,
        "patchset": patchset_num,
        "subject": change.subject,
        "diff_stat": diff_stat,
    }


def cmd_list_changes(client: GerritClient, cfg: dict, args):
    query_str = args.query if args.query else "status:open"
    options = ["LABELS", "CURRENT_REVISION", "DETAILED_ACCOUNTS"]
    changes = client.changes.search(query=query_str, options=options)

    result = []
    for c in changes:
        owner = c.get("owner", {})
        labels = {}
        for label, detail in c.get("labels", {}).items():
            if "approved" in detail:
                labels[label] = detail["approved"].get("name", "approved")
            elif "rejected" in detail:
                labels[label] = detail["rejected"].get("name", "rejected")
            elif "value" in detail:
                labels[label] = detail["value"]
            else:
                labels[label] = None
        result.append({
            "number": c.get("_number"),
            "change_id": c.get("change_id"),
            "subject": c.get("subject"),
            "project": c.get("project"),
            "branch": c.get("branch"),
            "status": c.get("status"),
            "owner": owner.get("name") or owner.get("username", str(owner)),
            "updated": c.get("updated", ""),
            "insertions": c.get("insertions", 0),
            "deletions": c.get("deletions", 0),
            "labels": labels,
            "unresolved_comments": c.get("unresolved_comment_count", 0),
        })
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_get_diff(client: GerritClient, cfg: dict, args):
    change = client.changes.get(args.change)
    revision = change.get_revision("current")

    # Get patchset number and owner via detail API
    detail = client.get(f"/changes/{args.change}/detail?o=CURRENT_REVISION&o=DETAILED_ACCOUNTS")
    current_rev = detail.get("current_revision", "")
    patchset_num = detail.get("revisions", {}).get(current_rev, {}).get("_number", 0)
    detail_owner = detail.get("owner", {})

    files_data = {}
    for file_path in revision.files.keys():
        if file_path == "/COMMIT_MSG":
            continue
        file_obj = revision.files.get(file_path)
        info = file_obj.to_dict()
        try:
            diff = file_obj.get_diff()
        except Exception as e:
            diff = f"<error fetching diff: {e}>"

        files_data[file_path] = {
            "status": info.get("status", "M"),
            "lines_inserted": info.get("lines_inserted", 0),
            "lines_deleted": info.get("lines_deleted", 0),
            "diff": _format_diff(diff),
        }

    commit = revision.get_commit()

    result = {
        "change_number": change.number,
        "change_id": change.change_id,
        "subject": change.subject,
        "project": change.project,
        "branch": change.branch,
        "status": change.status,
        "patchset": patchset_num,
        "owner": detail_owner.get("name") or detail_owner.get("username", "unknown"),
        "url": f"{cfg['url'].rstrip('/')}/c/{change.project}/+/{change.number}",
        "commit_message": commit.get("message", ""),
        "files": files_data,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def _format_diff(diff_obj, context_lines=3) -> str:
    if isinstance(diff_obj, str):
        return diff_obj
    if isinstance(diff_obj, dict):
        lines = []
        for chunk in diff_obj.get("content", []):
            has_change = "a" in chunk or "b" in chunk
            if "ab" in chunk and not has_change:
                # Pure context chunk — only keep tail as leading context for next change
                # and head as trailing context for previous change
                ab = chunk["ab"]
                if lines:
                    # Trailing context for previous change
                    for line in ab[:context_lines]:
                        lines.append(f" {line}")
                if len(ab) > context_lines * 2:
                    lines.append("...")
                # Leading context for next change (kept in output, may be trimmed later)
                for line in ab[-context_lines:]:
                    lines.append(f" {line}")
            else:
                for ab_line in chunk.get("ab", []):
                    lines.append(f" {ab_line}")
                for a_line in chunk.get("a", []):
                    lines.append(f"-{a_line}")
                for b_line in chunk.get("b", []):
                    lines.append(f"+{b_line}")
        return "\n".join(lines)
    return str(diff_obj)


def cmd_checkout(client: GerritClient, cfg: dict, args):
    info = _checkout_patchset(client, cfg, args.change, args.patchset)
    print(json.dumps({"status": "ok", **info}, indent=2, ensure_ascii=False))


def _parse_labels(label_args: list[str]) -> dict:
    """Parse label args like ['Code-Review=+2', 'Verified=+1'] into dict."""
    labels = {}
    for item in label_args:
        for part in item.split(","):
            label, value = part.split("=")
            labels[label.strip()] = int(value.strip())
    return labels


def cmd_post_review(client: GerritClient, cfg: dict, args):
    change = client.changes.get(args.change)

    if args.patchset:
        revision = change.get_revision(args.patchset)
    else:
        revision = change.get_revision("current")

    review_input = {
        "message": args.message or "",
        "drafts": "PUBLISH",
    }

    if args.comments_file:
        with open(args.comments_file) as f:
            comments = json.load(f)
        review_input["comments"] = comments

    if args.score:
        review_input["labels"] = _parse_labels(args.score)

    result = revision.set_review(review_input)
    print(json.dumps({"status": "ok", "result": str(result)}, ensure_ascii=False))


def cmd_add_reviewer(client: GerritClient, cfg: dict, args):
    change = client.changes.get(args.change)
    added = []
    for reviewer in args.reviewer.split(","):
        reviewer = reviewer.strip()
        if not reviewer:
            continue
        change.reviewers.add({"reviewer": reviewer})
        added.append(reviewer)
    print(json.dumps({"status": "ok", "added": added}, ensure_ascii=False))


def cmd_remove_reviewer(client: GerritClient, cfg: dict, args):
    change = client.changes.get(args.change)
    removed = []
    for reviewer in args.reviewer.split(","):
        reviewer = reviewer.strip()
        if not reviewer:
            continue
        change.reviewers.get(reviewer).delete()
        removed.append(reviewer)
    print(json.dumps({"status": "ok", "removed": removed}, ensure_ascii=False))


def cmd_approve(client: GerritClient, cfg: dict, args):
    change = client.changes.get(args.change)
    revision = change.get_revision("current")
    labels = _parse_labels(args.label) if args.label else {"Code-Review": 2, "Verified": 1}
    review_input = {
        "message": args.message or "Approved",
        "labels": labels,
    }
    result = revision.set_review(review_input)
    print(json.dumps({"status": "ok", "labels": labels, "result": str(result)}, ensure_ascii=False))


def cmd_submit(client: GerritClient, cfg: dict, args):
    change = client.changes.get(args.change)
    change_id = change.id
    base_url = cfg["url"].rstrip("/")
    url = f"{base_url}/a/changes/{change_id}/submit"
    resp = client.requester.post(url, json={}, raise_for_status=False)
    if resp.status_code in (200, 204):
        print(json.dumps({"status": "ok", "result": resp.text.strip()}, ensure_ascii=False))
    else:
        body = resp.text.strip()
        # Gerrit prefixes JSON responses with )]}'
        if body.startswith(")]}'"):
            body = body[4:].strip()
        raise RuntimeError(f"Submit failed ({resp.status_code}): {body}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gerrit-reviewer-cli",
        description="AI-powered code review CLI for Gerrit",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help=f"Path to config YAML file (default: {DEFAULT_CONFIG_PATH})")

    # config
    p_config = sub.add_parser("config", help="View and edit configuration")
    p_config.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                          help=f"Path to config YAML file (default: {DEFAULT_CONFIG_PATH})")
    config_sub = p_config.add_subparsers(dest="config_action", required=True)
    config_sub.add_parser("show", help="Show current config (sensitive values masked)")
    config_sub.add_parser("path", help="Print config file path")
    p_get = config_sub.add_parser("get", help="Get a config value by dotted key")
    p_get.add_argument("key", help="Dotted key path, e.g. gerrit.url")
    p_set = config_sub.add_parser("set", help="Set a config value by dotted key")
    p_set.add_argument("key", help="Dotted key path, e.g. gerrit.url")
    p_set.add_argument("value", help="Value to set")

    # list-changes
    p = sub.add_parser("list-changes", parents=[shared], help="Query changes")
    p.add_argument("--query", "-q", default="status:open", help="Gerrit query string")

    # get-diff
    p = sub.add_parser("get-diff", parents=[shared], help="Get change detail with diffs via Gerrit API")
    p.add_argument("change", help="Change number or ID")

    # checkout
    p = sub.add_parser("checkout", parents=[shared], help="Clone/fetch and checkout a patchset")
    p.add_argument("change", help="Change number or ID")
    p.add_argument("--patchset", default="current", help="Patchset number (default: current)")

    # post-review
    p = sub.add_parser("post-review", parents=[shared], help="Post review comments and score")
    p.add_argument("change", help="Change number or ID")
    p.add_argument("--patchset", help="Patchset number to review (default: current)")
    p.add_argument("--message", "-m", help="Review message")
    p.add_argument("--comments-file", help="JSON file with inline comments")
    p.add_argument("--score", action="append", help="Label score, e.g. Code-Review=+1")

    # add-reviewer
    p = sub.add_parser("add-reviewer", parents=[shared], help="Add reviewers to a change")
    p.add_argument("change", help="Change number or ID")
    p.add_argument("--reviewer", required=True, help="Comma-separated reviewer usernames")

    # remove-reviewer
    p = sub.add_parser("remove-reviewer", parents=[shared], help="Remove reviewers from a change")
    p.add_argument("change", help="Change number or ID")
    p.add_argument("--reviewer", required=True, help="Comma-separated reviewer usernames")

    # approve
    p = sub.add_parser("approve", parents=[shared], help="Approve a change (default: Code-Review=+2, Verified=+1)")
    p.add_argument("change", help="Change number or ID")
    p.add_argument("--label", action="append",
                    help="Label=score, e.g. Code-Review=+2 (default: Code-Review=+2,Verified=+1)")
    p.add_argument("--message", "-m", help="Approval message")

    # submit
    p = sub.add_parser("submit", parents=[shared], help="Submit (merge) a change")
    p.add_argument("change", help="Change number or ID")

    # init / uninstall
    p = sub.add_parser("init", help="Generate config, install skill, and install systemd services")
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="Set config value non-interactively, e.g. --set gerrit.url=https://...")
    sub.add_parser("uninstall", help="Remove skill and systemd services")

    return parser


def _install_skill():
    """Copy SKILL.md to ~/.agents/skills/gerrit-reviewer/."""
    skill_src = Path(files("gerrit_reviewer.skill").joinpath("SKILL.md")).parent
    target = Path.home() / ".agents" / "skills" / "gerrit-reviewer"

    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        answer = input(f"Skill already installed at {target}\nOverwrite? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Kept current skill.")
            return
        shutil.rmtree(target)
        print(f"Overwritten: {target}")
    else:
        print(f"Installed: {target}")

    shutil.copytree(skill_src, target, ignore=shutil.ignore_patterns("__init__.py", "__pycache__"))


def _uninstall_skill():
    """Remove skill directory from ~/.agents/skills/."""
    target = Path.home() / ".agents" / "skills" / "gerrit-reviewer"

    if target.exists():
        shutil.rmtree(target)
        print(f"Removed: {target}")
    else:
        print(f"Not installed: {target}")


SYSTEMD_SERVICES = [
    "gerrit-reviewer-stream.service",
]


def _install_services():
    """Copy systemd user service files into ~/.config/systemd/user/."""
    systemd_src = Path(files("gerrit_reviewer.systemd").joinpath(SYSTEMD_SERVICES[0])).parent
    target_dir = Path.home() / ".config" / "systemd" / "user"
    target_dir.mkdir(parents=True, exist_ok=True)

    for name in SYSTEMD_SERVICES:
        src = systemd_src / name
        target = target_dir / name
        if target.exists():
            bak = target.with_suffix(target.suffix + ".bak")
            target.rename(bak)
            print(f"Backed up: {target} -> {bak}")
        # Copy and rewrite ExecStart with absolute path
        content = src.read_text()
        for line in content.splitlines():
            if line.startswith("ExecStart="):
                cmd = line.split("=", 1)[1].split()[0]
                abs_cmd = shutil.which(cmd)
                if abs_cmd:
                    content = content.replace(line, line.replace(cmd, abs_cmd, 1))
                else:
                    print(f"Warning: {cmd} not found in PATH", file=sys.stderr)
                break
        target.write_text(content)
        print(f"Installed: {target}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    for name in SYSTEMD_SERVICES:
        subprocess.run(["systemctl", "--user", "enable", name], check=True)
        subprocess.run(["systemctl", "--user", "restart", name], check=True)
        print(f"Enabled and (re)started: {name}")


def _uninstall_services():
    """Stop, disable, and remove systemd service files."""
    target_dir = Path.home() / ".config" / "systemd" / "user"

    for name in SYSTEMD_SERVICES:
        subprocess.run(["systemctl", "--user", "stop", name], check=False)
        subprocess.run(["systemctl", "--user", "disable", name], check=False)

        target = target_dir / name
        if target.exists():
            target.unlink()
            print(f"Removed: {target}")
        else:
            print(f"Not installed: {target}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)


def _setup_hermes_webhook():
    """Setup Hermes webhook"""
    # Configure webhook platform in ~/.hermes/config.yaml
    hermes_cfg_path = Path.home() / ".hermes" / "config.yaml"
    hermes_cfg_data = {}
    if hermes_cfg_path.exists():
        shutil.copy2(hermes_cfg_path, hermes_cfg_path.with_suffix(hermes_cfg_path.suffix + ".bak"))
        with open(hermes_cfg_path) as f:
            hermes_cfg_data = yaml.safe_load(f) or {}

    platforms = hermes_cfg_data.setdefault("platforms", {})
    webhook = platforms.setdefault("webhook", {})
    webhook["enabled"] = True
    extra = webhook.setdefault("extra", {})

    # Use existing values if present, otherwise set defaults
    extra.setdefault("host", "0.0.0.0")
    host = extra.setdefault("port", 8644)
    extra.setdefault("secret", secrets.token_hex(32))

    # Save hermes config
    hermes_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(hermes_cfg_path, "w") as f:
        yaml.dump(hermes_cfg_data, f, default_flow_style=False, sort_keys=False)

    print(f"Setup Hermes webhook platform in {hermes_cfg_path}")


def _verify_webhook_server():
    """Verify the webhook server is running and accepting connections."""
    print("Waiting for webhook server to be ready...")
    max_retries = 30
    retry_interval = 1

    for attempt in range(max_retries):
        try:
            with httpx.Client() as client:
                response = client.get("http://localhost:8644/health", timeout=2.0)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("status") == "ok":
                        print(f"Webhook server is ready: {data}")
                        return
        except (httpx.ConnectError, httpx.TimeoutException, ValueError):
            pass

        if attempt < max_retries - 1:
            print(f"Attempt {attempt + 1}/{max_retries}: Server not ready, retrying in {retry_interval}s...")
            time.sleep(retry_interval)

    print("Error: Webhook server failed to start", file=sys.stderr)
    sys.exit(1)


def _subscribe_hermes_webhook(cfg: dict):
    """Subscribe to Gerrit events with Hermes CLI."""
    prompt = "/gerrit-reviewer {change_number} --patchset {patchset}"
    events = "gerrit_patchset_created"
    description = "Review Gerrit change"
    skills = "gerrit-reviewer"
    deliver = cfg.get("hermes", {}).get("deliver", "log")
    name = "gerrit-reviewer"

    cmd = [
        "hermes", "webhook", "add",
        "--prompt", prompt,
        "--events", events,
        "--description", description,
        "--skills", skills,
        "--deliver", deliver,
        name,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        output = result.stdout.strip()

        # Parse output to extract URL and secret
        url = None
        secret = None
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("URL:"):
                url = line.split(":", 1)[1].strip()
            elif line.startswith("Secret:"):
                secret = line.split(":", 1)[1].strip()

        if not url or not secret:
            print("Error: Could not parse webhook URL or secret from hermes output", file=sys.stderr)
            print(f"Output:\n{output}", file=sys.stderr)
            sys.exit(1)

        # Update config with webhook details
        hermes = cfg.setdefault("hermes", {})
        hermes["url"] = url
        hermes["webhook_secret"] = secret

        save_config(cfg)
        print(f"Subscription created: {url}")
        print(f"Secret saved to config")

    except Exception as e:
        print(f"Error: Failed to subscribe Hermes webhook: {e}", file=sys.stderr)
        sys.exit(1)


def _unsubscribe_hermes_webhook():
    """Unsubscribe Hermes webhook."""
    name = "gerrit-reviewer"

    try:
        subprocess.run(
            ["hermes", "webhook", "rm", name],
            capture_output=True,
            text=True,
            check=True
        )
        print(f"Unsubscribed Hermes webhook: {name}")
    except Exception as e:
        print(f"Error: Failed to unsubscribe Hermes webhook: {e}", file=sys.stderr)


def cmd_init(args):
    """Generate config, install skill, and install systemd services."""
    print("==> Checking dependencies...")
    if not shutil.which("hermes"):
        print("Error: hermes is not installed or not in PATH.", file=sys.stderr)
        print("Install hermes first: https://hermes-agent.nousresearch.com", file=sys.stderr)
        sys.exit(1)

    print("==> Configuring...")
    if args.set:
        # Non-interactive: apply --set values
        cfg = load_config()
        for item in args.set:
            if "=" not in item:
                print(f"Invalid format: {item} (expected KEY=VALUE)", file=sys.stderr)
                sys.exit(1)
            key, value = item.split("=", 1)
            config_set(cfg, key, value)
        save_config(cfg)
        print(f"Config saved to {DEFAULT_CONFIG_PATH}")
    else:
        cfg = interactive_config()

    print("==> Setting up Hermes webhook...")
    _setup_hermes_webhook()
    _verify_webhook_server()
    print("==> Subscribing to Gerrit events with Hermes...")
    _subscribe_hermes_webhook(cfg)
    print("==> Installing skill...")
    _install_skill()
    print("==> Installing systemd services...")
    _install_services()


def cmd_uninstall(args):
    """Remove skill, systemd services, and webhook."""
    print("==> Removing webhook...")
    cfg = load_config()
    _unsubscribe_hermes_webhook()
    print("==> Removing skill...")
    _uninstall_skill()
    print("==> Removing systemd services...")
    _uninstall_services()


def cmd_config(args):
    """View and edit configuration."""
    config_path = Path(args.config) if hasattr(args, "config") and args.config else DEFAULT_CONFIG_PATH

    if args.config_action == "path":
        print(str(config_path))
        return

    if args.config_action == "show":
        cfg = load_config(config_path)
        masked = mask_sensitive(cfg)
        print(yaml.dump(masked, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip())
        return

    if args.config_action == "get":
        cfg = load_config(config_path)
        try:
            val = config_get(cfg, args.key)
        except KeyError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if isinstance(val, (dict, list)):
            print(yaml.dump(val, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip())
        else:
            print(val)
        return

    if args.config_action == "set":
        cfg = load_config(config_path)
        try:
            config_set(cfg, args.key, args.value)
        except KeyError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        save_config(cfg, config_path)
        print(f"{args.key} = {config_get(cfg, args.key)}")
        return


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Commands that don't need Gerrit config
    if args.command == "init":
        cmd_init(args)
        return
    if args.command == "uninstall":
        cmd_uninstall(args)
        return
    if args.command == "config":
        cmd_config(args)
        return

    cfg = load_config(args.config)
    gerrit_cfg = get_gerrit_config(cfg)
    client = make_client(gerrit_cfg)

    commands = {
        "list-changes": cmd_list_changes,
        "get-diff": cmd_get_diff,
        "checkout": cmd_checkout,
        "post-review": cmd_post_review,
        "add-reviewer": cmd_add_reviewer,
        "remove-reviewer": cmd_remove_reviewer,
        "approve": cmd_approve,
        "submit": cmd_submit,
    }

    try:
        commands[args.command](client, gerrit_cfg, args)
    except Exception as e:
        error_msg = str(e)
        resp_text = getattr(getattr(e, "response", None), "text", "")
        if resp_text:
            error_msg += f"\nDetails: {resp_text.strip()}"
        print(error_msg, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
