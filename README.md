# WeChat Agent CLI (V2)

本项目是一个本地运行的微信公众号文章聚合工具：
- 用户只需添加公众号名
- `view` 时触发抓取（无后台常驻）
- 默认走免费发现链路（本地登录态 + 公开检索）
- 支持阅读状态、每日 ID、历史回看、推荐视图

## 用户安装（全局可用）

```bash
# 推荐：pipx
pipx install "git+https://github.com/v6582374-netizen/Cerebro.git"

# 或：uv tool
uv tool install "git+https://github.com/v6582374-netizen/Cerebro.git"
```

本地源码调试后也可全局挂载：

```bash
uv tool install -e /Users/shiwen/Desktop/Wechat_agent
```

如果出现 `zsh: command not found: wechat-agent`：

```bash
uv tool update-shell
exec zsh
which wechat-agent
```

## 开发者模式（仅本地开发）

> `uv` 只用于开发调试，不是终端用户使用门槛。

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
uv run ruff check
uv run pytest -q
```

## 配置（直接使用 `.env`）

全局配置文件路径：`~/.config/wechat-agent/.env`

推荐交互式配置：

```bash
wechat-agent config api
wechat-agent config show
```

常用配置项：

```bash
# AI（可选，未配置会走本地免费 fallback 摘要/向量）
AI_PROVIDER=auto
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_CHAT_MODEL=gpt-4o-mini
OPENAI_EMBED_MODEL=text-embedding-3-small
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_CHAT_MODEL=deepseek-chat
DEEPSEEK_EMBED_MODEL=

# V2 发现链路开关
DISCOVERY_V2_ENABLED=true
SESSION_PROVIDER=weread
SESSION_BACKEND=auto
COVERAGE_SLA_TARGET=0.95

# 同步与时间规则
SYNC_OVERLAP_SECONDS=120
INCREMENTAL_SYNC_ENABLED=true
MIDNIGHT_SHIFT_DAYS=2
```

## 核心命令

### 1) 登录态管理（本地保存）

```bash
wechat-agent login --provider weread
wechat-agent logout --provider weread
```

说明：
- 登录态只存本机（优先 Keychain，或本地文件后备）
- `status` 会显示 `session_state=valid/expired/missing`

### 2) 订阅管理

```bash
# 只需公众号名（推荐）
wechat-agent sub add --name "量子位"

# 兼容参数（将弃用）
wechat-agent sub add --name "量子位" --wechat-id QbitAI

wechat-agent sub list
wechat-agent sub remove --wechat-id QbitAI
```

### 3) 查看与历史

```bash
# 查看（先同步再展示）
wechat-agent view --mode source
wechat-agent view --mode time --date 2026-02-23
wechat-agent view --mode recommend

# 严格实时（不使用缓存兜底）
wechat-agent view --mode source --strict-live

# 历史（只查库，不触发抓取）
wechat-agent history --date 2026-02-22 --mode source
```

### 4) 阅读状态与打开原文（每日 ID）

```bash
wechat-agent read mark --id 1 --state read
wechat-agent read mark --id 1 --state unread
wechat-agent done --ids 1,2,3
wechat-agent todo --ids 2,5
wechat-agent open --id 1

# 指定日期（日内 ID）
wechat-agent open --id 3 --date 2026-02-22
```

### 5) 状态与覆盖率

```bash
wechat-agent status
wechat-agent coverage --date 2026-02-23
```

## 交互模式（终端内快速已读操作）

```bash
wechat-agent view --mode source --interactive
```

可用操作：
- `r 1,2` 标记已读
- `u 3` 标记未读
- `t 4` 切换状态
- `o 4` 打开原文
- `p` 重绘
- `q` 退出

## 输出与行为说明

- 标题为可点击链接（OSC 8 终端）
- 每次命令末尾都会输出当前 AI 引擎信息
- `view` 会输出发现指标：`discover_ok / discover_delayed / discover_failed / coverage_ratio`
- 失败时默认允许缓存兜底，并标记“使用缓存(延迟xx小时)”
- `--strict-live` 会过滤掉缓存结果
- `history` 不会触发抓取
- 用户可见 ID 为“每日 ID”（每个日期从 1 开始）

## 精简说明

- 已移除手工 RSS 源命令（`source` / `set-source`），统一为自动发现。
- `sub add --wechat-id` 仅做短期兼容保留，推荐只用 `sub add --name`。
