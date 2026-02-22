# WeChat Agent CLI

本项目用于聚合微信公众号文章、生成 AI 摘要、并在终端进行推荐阅读和已读管理。

## 1) 用户安装（不需要虚拟环境）

面向最终用户，直接用包管理器全局安装即可，在任意目录都能执行 `wechat-agent`。

```bash
# 推荐
pipx install wechat-agent

# 或
uv tool install wechat-agent

# 或
python -m pip install --user wechat-agent
```

升级 / 卸载：

```bash
pipx upgrade wechat-agent
pipx uninstall wechat-agent
```

## 2) 开发者模式（仅本地调试）

`uv` 只用于开发调试，不是面向用户的安装方式。

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
pytest -q
```

## 3) AI 配置（支持 OpenAI / DeepSeek）

推荐直接使用交互命令配置（会写入全局配置文件 `~/.config/wechat-agent/.env`）：

```bash
wechat-agent config api
wechat-agent config show
```

如需手动编辑，也可直接修改 `.env`：

```bash
# 自动选择：优先 OPENAI_API_KEY，其次 DEEPSEEK_API_KEY
AI_PROVIDER=auto

# OpenAI（默认值已内置）
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_CHAT_MODEL=gpt-4o-mini
OPENAI_EMBED_MODEL=text-embedding-3-small

# DeepSeek
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_CHAT_MODEL=deepseek-chat
DEEPSEEK_EMBED_MODEL=

# 业务规则：00:00 发布时间按后两天归类（例如 23号00:00 -> 25号）
MIDNIGHT_SHIFT_DAYS=2
```

说明：
- `OPENAI_BASE_URL` 默认使用 OpenAI 官方接口地址 `https://api.openai.com/v1`。
- 摘要会使用当前 provider 的 chat model。
- 如果 embedding model 未配置或接口不可用，系统自动回退到本地向量（`local-hash`）。

## 4) 命令总览（完整）

订阅管理：

```bash
wechat-agent sub add --name "量子位" --wechat-id QbitAI
wechat-agent sub list
wechat-agent sub remove --wechat-id QbitAI
wechat-agent sub set-source --wechat-id QbitAI --url "https://..."
```

文章查看：

```bash
# 默认按订阅号分组
wechat-agent view --mode source

# 按时间
wechat-agent view --mode time --date 2026-02-22

# 按推荐
wechat-agent view --mode recommend
```

已读管理：

```bash
# 单条
wechat-agent read mark --article-id 12 --state read
wechat-agent read mark --article-id 12 --state unread

# 在系统浏览器打开原文
wechat-agent open --article-id 12
```

状态查看：

```bash
wechat-agent status
```

配置管理：

```bash
wechat-agent config api
wechat-agent config show
```

## 5) 命令总览（简化版，便于记忆）

建议先设 alias：

```bash
alias wa='wechat-agent'
```

然后用短命令：

```bash
wa add -n "量子位" -i QbitAI
wa list
wa show -m source
wa show -m recommend
wa done -i 1,2,3
wa todo -i 2
wa open -i 12
wa config api
wa config show
wa remove -i QbitAI
wa status
```

## 6) 终端交互已读（最方便）

```bash
wechat-agent view --mode source --interactive
```

进入后可直接操作：
- `r 1,2` 标记已读
- `u 3` 标记未读
- `t 4` 切换已读状态
- `o 4` 打开原文
- `p` 重绘列表
- `q` 退出

## 7) 输出说明

- 标题列是可点击链接（支持 OSC 8 的终端可直接点击打开原文）。
- 标题点击使用原始完整链接（保留全部 query 参数，避免参数丢失）。
- 若终端对外链有安全拦截，使用 `wechat-agent open --article-id <id>` 强制调用系统浏览器打开。
- AI 摘要优先基于正文全文提取后总结；正文抓取失败时自动回退。
- 已移除 `--test-prev-day` 测试参数，日期归类统一由发布时刻规则自动处理。
- 每次命令输出末尾都会显示当前 AI 引擎信息，例如：
  - `AI: provider=openai | summary=gpt-4o-mini | embedding=text-embedding-3-small`
