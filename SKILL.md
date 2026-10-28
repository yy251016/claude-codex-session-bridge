---
name: codex-sessions
description: 检索并读取本机 Claude Code 与 Codex 的完整历史会话记录，支持通过 @会话标题 / @codex:标题 / @claude:标题 双向无缝继续对话（兼容 Windows/macOS/Linux 的 CLI 与桌面客户端）。当用户提到"@会话标题"、"codex 会话"、"claude 会话"、"codex 里聊过的 XX"、"接着 codex/claude 的活"、"继续刚才的对话"、"handoff"等时使用。
---

# 双向会话桥接：Claude Code ⇄ Codex（CLI & 客户端通用）

本技能提供三层双向会话桥接机制，无需导出或切换终端即可在 **Claude Code（CLI & 桌面/IDE 客户端）** 与 **Codex（CLI & 桌面端）** 之间直接按会话标题引用并继续对话，默认全量加载从 `#1` 到最后一条的完整对话：

## 1. 自动注入层（UserPromptSubmit Hook）
当用户在输入框中发送包含以下任一格式的提问时：
- `@会话标题`（例如：`@订单结算重构 接着刚才的做`、`@登录鉴权排查 继续分析`）
- `@「含空格的会话标题」` 或 `@"会话标题"`
- `@codex:会话标题` / `@claude:会话标题`

`UserPromptSubmit` 钩子会在毫秒级自动检索对应会话的**完整对话历史（从 `#1` 到最后一条消息）**，并以 `<cross-session-context>...</cross-session-context>` 标签注入到本轮上下文中。
- **如果当前上下文中已存在 `<cross-session-context>`**：请直接基于其中的历史对话与工作目录（`cwd`）继续推进任务，**不要**再重复调用工具去搜索同一会话。

## 2. MCP 协议层（`session-bridge` MCP Server）
适用于 CLI 与桌面客户端的主动检索、按区间/关键词过滤以及 `@` 资源菜单引用：
- **MCP 工具**：
  - `read_session(query, agent="all", tail, start, end, grep)`：按会话标题、关键词或会话 ID 前缀直接读取 Codex 或 Claude 的完整真实对话记录（默认读取从 `#1` 到末尾的全量消息，也支持 `start`/`end` 序号区间或 `grep` 关键词过滤）。
  - `search_sessions(query="", agent="all", limit=100)`：全量检索本机所有 Codex / Claude 历史会话列表。
- **MCP Resources**：
  - 自动暴露本机所有有正文的 Codex 与 Claude 会话（`session://codex/<id>`、`session://claude/<id>`），支持在客户端 `@` 菜单中直接选择。

## 3. 命令行脚本层（`session_bridge.py`）
当没有看到 `<cross-session-context>` 且未加载 MCP 工具时，直接执行技能目录下的脚本：

```bash
SB="$HOME/.claude/skills/codex-sessions/session_bridge.py"

# 1. 按标题或关键词直接读取会话从 #1 到末尾的全量对话（支持 @codex:标题 / @claude:标题）
python "$SB" read "会话标题或ID前缀" --agent all --full

# 2. 按消息序号区间或关键词在会话内检索
python "$SB" read "会话标题或ID前缀" --start 1 --end 50
python "$SB" read "会话标题或ID前缀" --grep "关键词"

# 3. 搜索匹配的候选会话（全量扫描本机历史会话）
python "$SB" search "关键词" --agent all

# 4. 导出完整 Markdown 交接包（可选留档）
python "$SB" handoff "会话标题或ID前缀" --to codex
```

## 数据源说明（全自动跨平台发现）

| Agent | 标题与元数据索引 | 对话正文 |
|---|---|---|
| **Codex** | `$CODEX_HOME/state_*.sqlite` 或 `~/.codex/state_*.sqlite` (`threads.name` 优先，其次 `session_index.jsonl` 与清洗后的 `title`) | 优先走 `thread_history_*.sqlite` 主键与用户消息索引毫秒级提取全量对话，兜底读 `sessions/**/rollout-*.jsonl` 及 `archived_sessions/*.jsonl.zst` |
| **Claude** | 自动发现 `~/.claude`、`$CLAUDE_CONFIG_DIR` 及 `settings.json -> claudeConfigDir` 下的 `projects/<slug>/<sessionId>.jsonl` (`custom-title` / `ai-title`) | 同文件内的 `user` / `assistant` 真实对话消息 |

## 流程规范

1. **优先读取已注入上下文**：若存在 `<cross-session-context>`，直接衔接到最新进展继续回答或执行任务。
2. **模糊指代主动检索**：若用户说“把刚才在 Codex/Claude 聊的 XX 接着做”而未写 `@标题`，优先调用 `read_session` / `search_sessions` MCP 工具（或 `session_bridge.py`）获取完整历史记录。
3. **多候选确认**：若同名或相近标题命中多个完全不同的会话且无法由最近更新时间唯一确定，列出前 3–5 个候选（含更新时间与 `cwd`）请用户确认。
