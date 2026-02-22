# WeChat Agent V1

本项目是一个本地运行的 CLI 系统，用于聚合微信公众号文章、生成 AI 摘要、并按不同视图推荐阅读。

## 用户安装（不需要虚拟环境）

面向最终用户，推荐直接通过包管理器全局安装，安装后可在任意目录执行 `wechat-agent`。

```bash
# 方案1：pipx（推荐）
pipx install wechat-agent

# 方案2：uv tool
uv tool install wechat-agent

# 方案3：pip --user
python -m pip install --user wechat-agent
```

## 开发者模式（本地调试）

`uv` 虚拟环境仅用于本地开发调试，不是最终用户安装路径。

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

## 核心命令

```bash
wechat-agent sub add --name 公众号名称 --wechat-id gh_xxxxx
wechat-agent sub list
wechat-agent view --mode source --interactive
wechat-agent view --mode time --date 2026-02-22
wechat-agent view --mode recommend --no-interactive
wechat-agent read mark --article-id 1 --state read
wechat-agent status
```

## 说明

- `view` 命令会先执行抓取同步，再展示结果。
- `view` 输出包含 `原文链接` 列，方便复制到浏览器打开。
- `view --interactive` 支持直接在终端里标记已读：
  - `r 1,2` 标记已读
  - `u 3` 标记未读
  - `t 4` 切换状态
  - `p` 重绘列表
  - `q` 退出交互
- 初版源匹配失败会跳过并记录，可用 `sub set-source` 手动修复。
- AI 摘要目标长度为 30-50 字，调用失败时会自动降级。
- 每次命令输出末尾会显示当前 AI 模型信息。
