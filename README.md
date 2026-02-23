# WeChat Agent CLI (V2)

本项目是一个本地运行的微信公众号文章聚合工具：
- 用户只需添加公众号名
- `view` 时触发抓取（无后台常驻）
- 默认走免费发现链路（本地登录态 + wechat2rss目录 + 公开检索）
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

### 1) 登录态使用教程（新手版）

登录态可以理解为“你已经在浏览器里登录过的凭证”。  
系统用它来提高文章发现成功率，尤其是一些公开检索不稳定的号。

先记住两点：
- 登录态只保存在你本机（优先系统钥匙串，失败才回退本地文件）
- 你随时可以 `logout` 删除

#### 第一步：获取登录态（WeRead）

1. 在浏览器打开 [https://weread.qq.com](https://weread.qq.com) 并完成登录。  
2. 按 `F12` 打开开发者工具（DevTools）。  
3. 打开 `Network`（网络）面板，随便点一个请求。  
4. 在请求头里找到 `Cookie`。  
5. 复制完整 Cookie 字符串（通常是 `a=b; c=d; ...` 这种格式）。

如果你已经有 JSON 形式（例如 `{"cookie":"..."}`），也可以直接用，系统会自动识别。

#### 第二步：写入系统

方式 A（推荐，最简单）：

```bash
wechat-agent login --provider weread
```

然后按提示粘贴 Cookie（输入是隐藏的，看不到正常）。

方式 B（命令里直接传）：

```bash
wechat-agent login --provider weread --token '你的完整Cookie'
```

可选：设置登录态有效期（默认 30 天）：

```bash
wechat-agent login --provider weread --expires-days 30
```

#### 第三步：确认是否生效

```bash
wechat-agent status
```

看到 `session_state=valid` 就表示已生效。  
如果是 `missing` 或 `expired`，重新执行一次 `login` 即可。

#### 退出与删除登录态

```bash
wechat-agent logout --provider weread
```

#### 常见问题

- `session_state=missing`：本地没有保存成功，重新 `login`。  
- `session_state=expired`：超过你设置的有效期，重新 `login`。  
- `AUTH_EXPIRED`：Cookie 本身失效，去 WeRead 重新登录后再复制一次。  
- 担心泄露：不要把 Cookie 发给他人，不要粘贴到公开 issue。

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
