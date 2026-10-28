---
name: codex-sessions
description: 检索并读取本机 Claude Code 与 Codex 的完整历史会话记录，支持在 Claude 中通过 @codex 会话标题、在 Codex 中通过 @claude 会话标题 双向无缝继续对话（避免与客户端自带 @ 冲突，兼容 Windows/macOS/Linux 的 CLI 与桌面客户端）。当用户提到"@codex"、"@claude"、"codex 会话"、"claude 会话"、"接着 codex/claude 的活"、"继续刚才的对话"、"handoff"等时使用。
---

# 双向会话桥接：Claude Code ⇄ Codex（CLI & 客户端通用）

本技能提供三层双向会话桥接机制，无需导出或切换终端即可在 **Claude Code（CLI & 桌面/IDE 客户端）** 与 **Codex（CLI & 桌面端）** 之间直接按会话标题引用并继续对话，默认全量加载从 `#1` 到最后一条的完整对话：

## 1. 自动注入层（UserPromptSubmit Hook，智能 @ 语法与防冲突）
为了不与 Codex 客户端自带的 `@`（引用 Codex 自身会话或文件）以及 Claude 自带的 `@文件` 冲突，只要用户输入带 `@codex` 或 `@claude` 前缀时自动触发跨端会话桥接注入：
- **人性化输入支持（免冒号、智能分词、直接连写中英文）**：
  - `@claude店发测试 帮我分析` / `@codex店发测试 接着做`（直接连写，最推荐）
  - `@claude：店发测试` / `@codex：店发测试`（中文冒号兼容）
  - `@claude:店发测试` / `@codex:店发测试`（英文冒号兼容）
  - `@claude 店发测试` / `@codex 店发测试`（空格分隔兼容）
  - `@claude"店发测试"` / `@codex“店发测试”`（引号精准包裹）
  - `[@codex 店发测试](thread://...)`（兼容客户端下拉或拖拽生成的 Markdown 链接）
  - 自然语言 `引用codex:店发测试` / `同步claude店发测试`
- **视觉透明与智能反馈横幅**：
  - **唯一/精准匹配**：以 `<cross-session-context>` 注入**从 `#1` 到最后一条消息的完整真实对话**，并标注目标 Agent、标题、ID、更新时间和消息总数。
  - **多项匹配提示**：自动注入最新的一条会话，同时在提示横幅中列出其余候选会话及对应的精确引用语法（如 `@claude完整标题`）。
  - **0 命中友好提示**：若未搜到关键词，列出目标 Agent 最近的 3 个活跃会话及对应语法，方便向用户确认。
- **如果当前上下文中已存在 `<cross-session-context>`**：请直接基于其中的历史对话与工作目录（`cwd`）继续推进任务，**不要**再重复调用工具去搜索同一会话。

## 2. MCP 协议层（`session-bridge` MCP Server）
适用于 CLI 与桌面客户端的主动检索、按区间/关键词过滤以及 `@` 资源菜单引用：
- **MCP 工具**：
  - `read_session(query, agent="all", tail, start, end, grep)`：按会话标题、关键词或会话 ID 前缀直接读取 Codex 或 Claude 的完整真实对话记录（默认读取从 `#1` 到末尾的全量消息，也支持 `start`/`end` 序号区间或 `grep` 关键词过滤）。
  - `search_sessions(query="", agent="all", limit=100)`：全量检索本机所有 Codex / Claude 历史会话列表。
- **MCP Resources**：
  - 自动暴露本机所有有正文的会话（命名为 `codex <标题>` 与 `claude <标题>`），支持在客户端 `@` 菜单中输入 `@codex` 或 `@claude` 过滤点选。

## 3. 命令行脚本层（`session_bridge.py`）
当没有看到 `<cross-session-context>` 且未加载 MCP 工具时，直接执行技能目录下的脚本：

```bash
SB="$HOME/.claude/skills/codex-sessions/session_bridge.py"

# 1. 毫秒级模糊查询历史会话表格与快捷引用语法（支持关键词/子序列模糊匹配）
python "$SB" list "关键词"
python "$SB" ls

# 2. 按标题或关键词直接读取会话从 #1 到末尾的全量对话（支持 @codex 标题 / @claude 标题）
python "$SB" read "会话标题或ID前缀" --agent all --full

# 3. 按消息序号区间或关键词在会话内检索
python "$SB" read "会话标题或ID前缀" --start 1 --end 50
python "$SB" read "会话标题或ID前缀" --grep "关键词"

# 4. 搜索匹配的候选会话（全量扫描本机历史会话）
python "$SB" search "关键词" --agent all

# 5. 导出完整 Markdown 交接包（可选留档）
python "$SB" handoff "会话标题或ID前缀" --to codex
```

## 数据源说明（全自动跨平台发现）

| Agent | 标题与元数据索引 | 对话正文 |
|---|---|---|
| **Codex** | `$CODEX_HOME/state_*.sqlite` 或 `~/.codex/state_*.sqlite` (`threads.name` 优先，其次 `session_index.jsonl` 与清洗后的 `title`) | 优先走 `thread_history_*.sqlite` 主键与用户消息索引毫秒级提取全量对话，兜底读 `sessions/**/rollout-*.jsonl` 及 `archived_sessions/*.jsonl.zst` |
| **Claude** | 自动发现 `~/.claude`、`$CLAUDE_CONFIG_DIR` 及 `settings.json -> claudeConfigDir` 下的 `projects/<slug>/<sessionId>.jsonl` (`custom-title` / `ai-title`) | 同文件内的 `user` / `assistant` 真实对话消息 |

## 流程规范

1. **优先读取已注入上下文**：若存在 `<cross-session-context>`，直接衔接到最新进展继续回答或执行任务。
2. **模糊指代主动检索**：若用户说“把刚才在 Codex/Claude 聊的 XX 接着做”而未写 `@codex` / `@claude`，优先调用 `read_session` / `search_sessions` MCP 工具（或 `session_bridge.py`）获取完整历史记录。
3. **多候选确认**：若同名或相近标题命中多个完全不同的会话且无法由最近更新时间唯一确定，列出前 3–5 个候选（含更新时间与 `cwd`）请用户确认。
