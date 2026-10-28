# Claude Code ⇄ Codex 双向会话桥接 (`codex-sessions`)

在 **Claude Code（CLI & 桌面/IDE 客户端）** 中通过 `@codex 会话标题`、在 **OpenAI Codex（CLI & 桌面端）** 中通过 `@claude 会话标题` 双向引用对方的历史会话并无缝接着对话。

采用显式 `@codex` / `@claude` 前缀，**完全不与 Codex 客户端自带的 `@`（引用自身会话/文件）或 Claude 自带的 `@文件` 冲突**。无需导出文件、无需切换终端，默认**从 `#1` 到最后一条消息全量无损加载**。支持 Windows / macOS / Linux，零外部第三方依赖。

---

## 核心特性

1. **`@codex 会话标题` / `@claude 会话标题` 毫秒级自动注入（`UserPromptSubmit` Hook）**
   - **在 Claude 中引用 Codex 会话**：`@codex 订单结算重构 接着刚才的做`（也支持 `@codex:订单结算重构` 或 `@codex「订单结算 AI 测试」`）
   - **在 Codex 中引用 Claude 会话**：`@claude 登录鉴权排查 继续刚才的分析`（也支持 `@claude:登录鉴权排查` 或 `@claude「登录鉴权 AI 排查」`）
   - **零冲突设计**：普通的裸 `@xxx` 完全交给客户端原生处理（如 Codex 自带的 `@` 会话/文件选择、Claude 自带的 `@` 文件选择），只有带 `@codex` 或 `@claude` 前缀时才触发跨端会话桥接。
   - 发送瞬间自动匹配对应历史会话，通过 SQLite 主键索引毫秒级提取**从第 `#1` 条到最后一条的完整对话**，并以 `<cross-session-context>` 标签自动注入。

2. **客户端 `@` 补全菜单与主动检索（`session-bridge` MCP Server）**
   - **MCP Resources**：自动暴露本机所有有正文的 Codex 与 Claude 历史会话（`codex <标题>` / `claude <标题>`），支持在客户端 `@` 菜单中输入 `@codex` 或 `@claude` 过滤点选。
   - **MCP Tools**：提供 `read_session`（支持全量读取、`start`/`end` 序号区间过滤、`grep` 会话内关键词检索）与 `search_sessions`（全量扫描本机所有历史会话）。

3. **跨平台零硬编码路径**
   - 自动发现 `~/.codex`（或 `$CODEX_HOME`）下的最新 `state_*.sqlite`、`thread_history_*.sqlite` 与 `sessions/`。
   - 自动发现 `~/.claude`、`$CLAUDE_CONFIG_DIR` 以及 `~/.claude/settings.json` 中配置的自定义 `claudeConfigDir`。

---

## 一键安装

### 前置要求
- Python 3.8+（标准库即可；若需读取早期 `.zst` 归档会话可按需 `pip install zstandard`）
- Node.js 18+（用于运行零依赖的 `session_bridge_mcp.mjs` MCP 服务）

### 方式一：克隆仓库并执行一键安装脚本（推荐，自动配置 Hook + MCP + Skill）

```bash
git clone https://github.com/yy251016/claude-codex-session-bridge.git
cd claude-codex-session-bridge
python install.py
```

`install.py` 会自动完成以下工作：
1. 将技能同步安装到 `~/.claude/skills/codex-sessions` 与 `~/.codex/skills/codex-sessions`；
2. 在 Claude Code (`~/.claude/settings.json`) 与 Codex (`~/.codex/hooks.json`) 中注册 `UserPromptSubmit` 自动注入钩子；
3. 在 Claude Code (`~/.claude.json`) 与 Codex (`~/.codex/config.toml`) 中注册 `session-bridge` MCP 服务。

### 方式二：通过 `npx skills` 安装

```bash
npx skills add yy251016/claude-codex-session-bridge
python ~/.claude/skills/codex-sessions/install.py
```

---

## 使用方式

### 1. 在对话中直接 `@codex 会话标题` / `@claude 会话标题`（CLI 与桌面客户端通用）

| 当前工具 | 输入示例 | 效果 |
|---|---|---|
| **Claude Code** | `@codex 订单结算重构 接着刚才的逻辑往下写` | 自动加载 Codex 中《订单结算重构》会话的全量历史对话并继续 |
| **Codex** | `@claude 登录鉴权排查 按刚才讨论的方案改代码` | 自动加载 Claude 中《登录鉴权排查》会话的全量历史对话并继续 |
| **任意一端** | `@codex:标题` / `@claude:标题` / `@codex「含空格标题」` | 冒号分隔或括号包裹含空格标题同样支持 |

### 2. 命令行直接使用 `session_bridge.py`

```bash
SB="$HOME/.claude/skills/codex-sessions/session_bridge.py"

# 搜索本机所有 Codex + Claude 会话
python "$SB" search "关键词" --agent all

# 读取指定会话从 #1 到最后一条的完整对话
python "$SB" read "会话标题或ID前缀" --full

# 按消息序号区间或会话内关键词读取
python "$SB" read "会话标题或ID前缀" --start 1 --end 50
python "$SB" read "会话标题或ID前缀" --grep "关键词"

# 导出完整 Markdown 交接包
python "$SB" handoff "会话标题或ID前缀" --to codex
```

---

## 文件结构

- `SKILL.md`：Claude Code / Codex 技能说明文件
- `session_bridge.py`：跨端会话索引、SQLite 主键极速全量提取、CLI 与 `UserPromptSubmit` Hook 引擎
- `session_bridge_mcp.mjs`：零依赖 Node.js stdio MCP Server（同时支持 `Content-Length` 与换行分隔 JSON-RPC）
- `install.py`：跨平台一键安装与配置脚本

## License

MIT
