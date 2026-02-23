# WeChat Agent CLI (V3)

本项目是一个本地运行的微信公众号文章聚合工具（账号直连版）：
- 通过微信扫码登录（无需安装微信桌面客户端）
- `view` 时触发同步（无后台常驻）
- 默认极致本地模式（会话仅本地存储，登录失效会阻断抓取）
- 支持阅读状态、每日 ID、历史回看、推荐视图

## 1) 用户安装（全局命令）

```bash
# 推荐：pipx
pipx install "git+https://github.com/v6582374-netizen/Cerebro.git"

# 或：uv tool
uv tool install "git+https://github.com/v6582374-netizen/Cerebro.git"
```

如果出现 `zsh: command not found: wechat-agent`：

```bash
uv tool update-shell
exec zsh
which wechat-agent
```

## 2) 开发者模式（仅本地开发）

> `uv` 只用于开发调试，不是终端用户使用门槛。

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
uv run ruff check
uv run pytest -q
```

## 3) 配置文件

全局配置路径：`~/.config/wechat-agent/.env`

```bash
wechat-agent config show
```

关键配置项：

```bash
# V3 主链路
WECHAT_WEB_ENABLED=true
WECHAT_WEB_BASE_URL=https://wx.qq.com
STRICT_AUTH_REQUIRED=true
SESSION_PROVIDER=wechat_web
SESSION_BACKEND=auto

# 极致本地模式（默认开启）
EXTREME_LOCAL_MODE=true

# 兼容开关（仅调试旧链路）
DISCOVERY_V2_ENABLED=true

# 同步与时间规则
SYNC_OVERLAP_SECONDS=120
INCREMENTAL_SYNC_ENABLED=true
MIDNIGHT_SHIFT_DAYS=2
```

## 4) 扫码登录（V3 必做）

先登录，再 `view`。

```bash
wechat-agent login
```

执行后会输出二维码链接，手机微信扫码确认即可。

查看登录状态：

```bash
wechat-agent auth status
```

退出登录：

```bash
wechat-agent logout
```

## 5) 订阅管理

新增订阅（只填公众号名）：

```bash
wechat-agent sub add --name "量子位"
```

如果自动绑定不到官方号，可手动绑定：

```bash
wechat-agent sub bind --name "量子位" --account gh_xxxxx
```

查看和删除：

```bash
wechat-agent sub list
wechat-agent sub remove --wechat-id auto_xxx
```

## 6) 查看与历史

```bash
# 先同步再展示
wechat-agent view --mode source
wechat-agent view --mode time --date 2026-02-23
wechat-agent view --mode recommend

# 历史（只查库，不同步）
wechat-agent history --date 2026-02-22 --mode source
```

注意：
- 在 `WECHAT_WEB_ENABLED=true` 且 `STRICT_AUTH_REQUIRED=true` 下，登录失效会直接阻断 `view`。
- 不会再静默降级到公开检索。

## 7) 已读与打开原文（每日 ID）

```bash
wechat-agent read mark --id 1 --state read
wechat-agent read mark --id 1 --state unread
wechat-agent done --ids 1,2,3
wechat-agent todo --ids 2,5
wechat-agent open --id 1
```

## 8) 状态与覆盖率

```bash
wechat-agent status
wechat-agent coverage --date 2026-02-23
```

`status` 额外输出：
- `sync_batches`
- `official_msgs`
- `article_refs_extracted`
- `blocked_by_auth`

## 9) 安全检查

```bash
wechat-agent security check
```

默认安全策略：
- 会话优先存系统钥匙串（Keychain）
- `EXTREME_LOCAL_MODE=true` 时默认不走远程 AI
- 网络白名单仅微信登录/同步与文章原文域名

## 10) 交互模式

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

## 11) 兼容说明

- V3 默认主链路是账号直连（扫码登录）。
- 旧 discovery/source 逻辑仅保留兼容，不再作为默认主路径。
- `sub add --wechat-id` 仅兼容保留，建议只用 `sub add --name`。
