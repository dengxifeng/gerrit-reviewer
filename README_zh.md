# gerrit-reviewer

基于 AI 的 Gerrit 代码审查系统。它监听 Gerrit 事件，利用 AI 自动审查代码变更，并将结构化的审查评论回写到 Gerrit。通过 [Hermes](https://hermes-agent.nousresearch.com) 实现基于 Agent 的自动审查工作流。

## 功能特性

- **自动化代码审查** — 监听 Gerrit 事件流，在新 patchset 提交时自动触发 AI 代码审查，并将带评分的结构化审查评论发布到 Gerrit。
- **CLI 命令行工具** — 查询变更、检出 patchset、发布审查评论、管理审查人、批准和提交变更，所有操作均可通过命令行完成。
- **事件流守护进程** — 长期运行的后台服务，通过 SSH 连接 Gerrit，监听事件（`patchset-created`、`reviewer-added`），并将事件转发到 [Hermes](https://hermes-agent.nousresearch.com) webhook 以触发自动审查。
- **Hermes 集成** — 作为 Hermes 技能运行：事件流触发 Hermes Agent 检出代码、启动 Claude 进行分析，并将审查结果回写到 Gerrit。
- **灵活的配置** — 统一的 YAML 配置文件，支持环境变量覆盖和基于审查人的过滤。
- **Systemd 服务** — 内置用户级 systemd 服务文件，方便在后台运行事件流守护进程。

## 环境要求

- Python 3.11+
- Git
- 可访问的 Gerrit 实例（需支持 SSH 和 REST API）
- [Hermes](https://hermes-agent.nousresearch.com)（用于自动审查工作流）
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — 自动审查工作流依赖 Claude Code，使用前请确保已安装并初始化（`claude` 命令可用）。

> **注意：** 如果使用非 Anthropic 官方 API 的第三方平台，请在 `~/.hermes/.env` 中添加以下配置：
>
> ```bash
> ANTHROPIC_BASE_URL=https://your-api-provider.example.com
> ANTHROPIC_AUTH_TOKEN=your-token
> ```

## 安装

### 从源码安装

```bash
pip install .
```

### 初始化

运行交互式配置向导，设置 Gerrit 凭据，安装 Hermes 技能、webhook 订阅和 systemd 服务：

```bash
gerrit-reviewer-cli init
```

该命令将：
1. 提示输入 Gerrit URL、用户名、凭据和 SSH 密钥路径
2. 在 `~/.gerrit-reviewer/config.yml` 生成配置文件
3. 将 Hermes 技能安装到 `~/.agents/skills/gerrit-reviewer`
4. 通过 Hermes webhook 订阅 Gerrit 事件
5. 设置事件流守护进程的用户级 systemd 服务

也可以通过命令行非交互式设置配置项：

```bash
gerrit-reviewer-cli config --set gerrit.url=https://gerrit.example.com
gerrit-reviewer-cli config --set gerrit.username=your-username
```

### 卸载

```bash
gerrit-reviewer-cli uninstall
```

## 使用方法

### CLI 命令行工具 (`gerrit-reviewer-cli`)

```bash
# 查看当前配置
gerrit-reviewer-cli config

# 列出待审查的变更
gerrit-reviewer-cli list-changes --query "status:open"

# 获取变更的 diff
gerrit-reviewer-cli get-diff <change_number>

# 在本地检出 patchset（按项目缓存）
gerrit-reviewer-cli checkout <change_number> [--patchset N]

# 发布审查评论和评分
gerrit-reviewer-cli post-review <change_number> --message "LGTM" --score 1

# 添加/移除审查人
gerrit-reviewer-cli add-reviewer <change_number> --reviewer user@example.com
gerrit-reviewer-cli remove-reviewer <change_number> --reviewer user@example.com

# 批准和提交
gerrit-reviewer-cli approve <change_number>
gerrit-reviewer-cli submit <change_number>

# 清理指定 patchset 的工作目录
gerrit-reviewer-cli cleanup <change_number> --patchset N
```

### 事件流守护进程 (`gerrit-reviewer-stream`)

```bash
# 启动事件流守护进程
gerrit-reviewer-stream

# 使用自定义配置文件
gerrit-reviewer-stream --config /path/to/config.yml

# 通过 systemd 运行
systemctl --user start gerrit-reviewer-stream
systemctl --user enable gerrit-reviewer-stream
```

环境变量可覆盖配置文件中的值：

| 变量 | 说明 |
|---|---|
| `GERRIT_SSH_HOST` | SSH 主机（默认：从 `gerrit.url` 提取） |
| `GERRIT_SSH_PORT` | SSH 端口（默认：29418） |
| `GERRIT_SSH_USER` | SSH 用户名 |
| `GERRIT_SSH_KEY` | SSH 私钥路径 |
| `HERMES_URL` | Hermes webhook 服务器 URL |
| `HERMES_WEBHOOK_SECRET` | Webhook HMAC 密钥 |
| `RECONNECT_DELAY` | 重连延迟（秒） |
| `LOG_LEVEL` | 日志级别（`DEBUG`、`INFO`、`WARNING`、`ERROR`） |

## 构建

项目使用 [Hatchling](https://hatch.pypa.io/) 作为构建后端。

```bash
# 构建 wheel 和 sdist 包
pip install build
python -m build

# 以开发模式安装（可编辑安装）
pip install -e .
```

## 参与开发

### 项目结构

```
src/gerrit_reviewer/
├── cli.py          # CLI 入口和子命令
├── stream.py       # 事件流守护进程
├── config.py       # 统一的 YAML 配置管理
├── log_utils.py    # 滚动日志文件设置
├── skill/          # Hermes 技能定义
│   └── SKILL.md
└── systemd/        # 用户级 systemd 服务文件
```

### 核心依赖

- [python-gerrit-api](https://github.com/shijl0925/python-gerrit-api) — Gerrit REST API 客户端
- [paramiko](https://www.paramiko.org/) — SSH 客户端，用于事件流连接
- [httpx](https://www.python-httpx.org/) — HTTP 客户端，用于 webhook 请求
- [PyYAML](https://pyyaml.org/) — YAML 配置文件解析

### 开发环境搭建

```bash
# 克隆仓库
git clone <repo-url>
cd gerrit-reviewer

# 以开发模式安装
pip install -e .
```

### 注意事项

- CLI 命令成功时将 JSON 输出到 stdout；失败时将错误信息输出到 stderr 并以退出码 1 退出。
- 自动审查工作流中的评分限制为 -1/0/+1；+2/-2 和提交操作需要用户明确指示。
- 事件流守护进程在 SSH 连接断开时会自动重连。
- 事件流守护进程仅处理 `REWORK` 类型且已配置用户为审查人的 `patchset-created` 事件，以及已配置用户被添加为审查人的 `reviewer-added` 事件。

## 许可证

详见 [LICENSE](LICENSE)。
