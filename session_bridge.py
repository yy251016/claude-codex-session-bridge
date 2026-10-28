#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""session_bridge.py — Claude Code ⇄ Codex 会话互读/接力工具（支持 @会话标题 毫秒级自动注入与 MCP/CLI/客户端通用）。

子命令:
  search [关键词] [--agent codex|claude|all] [--limit N]
      按标题/首条消息/日期搜会话（关键词为空时列出最近会话）
  read <id前缀|标题关键词> [--agent ...] [--tail N] [--max-chars C] [--full]
      读取某个会话的对话内容
  handoff <id前缀|标题关键词> [--agent ...] [--to codex|claude] [--out FILE]
      把一个会话导出成 Markdown 交接包，供另一个 agent 直接读取
  list-claude [--limit N]
      列出最近改动的 Claude 会话（带标题）
  list-json [--agent codex|claude|all] [--limit N]
      输出 JSON 格式的最近会话列表（供 MCP Resources 与脚本调用）
  hook [--current-agent claude|codex]
      UserPromptSubmit 钩子：从 stdin 读取 prompt，自动识别 @会话标题 并注入该会话上下文
"""
import argparse
import glob
import io
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import zstandard as zstd
except ImportError:
    zstd = None

CODEX_DIR = os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")
CLAUDE_DIR = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def _find_latest_sqlite(base_dir, prefix, default_name):
    if os.path.isdir(base_dir):
        candidates = glob.glob(os.path.join(base_dir, "%s*.sqlite" % prefix))
        if candidates:
            candidates.sort(key=lambda p: (os.path.getmtime(p), p), reverse=True)
            return candidates[0]
    return os.path.join(base_dir, default_name)


def _discover_claude_dirs():
    """自动发现所有 Claude 配置目录（支持 ~/.claude、环境变量 CLAUDE_CONFIG_DIR 及 settings.json 中的 claudeConfigDir）。"""
    dirs = []
    seen = set()

    def _add(d):
        if not d:
            return
        expanded = os.path.abspath(os.path.expanduser(d))
        norm = expanded.lower() if os.name == "nt" else expanded
        if norm not in seen and os.path.isdir(expanded):
            seen.add(norm)
            dirs.append(expanded)

    _add(os.path.expanduser("~/.claude"))
    _add(os.environ.get("CLAUDE_CONFIG_DIR"))

    for base in list(dirs):
        settings_path = os.path.join(base, "settings.json")
        if os.path.isfile(settings_path):
            try:
                with open(settings_path, encoding="utf-8", errors="replace") as f:
                    cfg = json.load(f)
                custom_dir = cfg.get("claudeConfigDir")
                if isinstance(custom_dir, str) and custom_dir.strip():
                    _add(custom_dir.strip())
            except Exception:
                pass

    extra_env = os.environ.get("SESSION_BRIDGE_CLAUDE_DIRS", "")
    if extra_env:
        for part in extra_env.split(os.pathsep):
            _add(part.strip())

    return dirs or [CLAUDE_DIR]


STATE_DB = _find_latest_sqlite(CODEX_DIR, "state_", "state_5.sqlite")
HIST_DB = _find_latest_sqlite(CODEX_DIR, "thread_history_", "thread_history_1.sqlite")
CODEX_INDEX = os.path.join(CODEX_DIR, "session_index.jsonl")

INJECT_PREFIXES = (
    "# AGENTS.md",
    "<user_instructions>",
    "<INSTRUCTIONS>",
    "<environment_context>",
    "<turn_context>",
    "<permissions",
    "<ENVIRONMENT_CONTEXT>",
    "<system-reminder>",
    "<local-command",
    "<command-name>",
)


# ==================== 通用工具 ====================

def _fmt_ts(ts):
    try:
        val = float(ts)
        if val > 1e12:
            val = val / 1000.0
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(val)))
    except (TypeError, ValueError, OSError):
        return ""


def _norm_win_path(p):
    if not p:
        return ""
    if p.startswith("\\\\?\\"):
        p = p[4:]
    return p


def _clean_codex_prompt_text(text):
    if not text:
        return ""
    s = text.strip()
    if "## My request:" in s:
        s = s.split("## My request:", 1)[1].strip()
    s = re.sub(r"\[@[^\]]+\]\(thread://[^)]+\)\s*", "", s).strip()
    s = re.sub(r"<oai-mem-citation>[\s\S]*?</oai-mem-citation>", "", s).strip()
    return s


def open_text(path):
    if path.endswith(".zst"):
        if zstd is None:
            raise RuntimeError("需要 pip install zstandard 才能读 .zst 会话")
        reader = zstd.ZstdDecompressor().stream_reader(open(path, "rb"))
        return io.TextIOWrapper(reader, encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def looks_like_id(s):
    s = s.replace("-", "")
    return len(s) >= 6 and all(c in "0123456789abcdefABCDEF" for c in s)


# ==================== Codex 侧 ====================

def _load_codex_index():
    names = {}
    extra = []
    if os.path.exists(CODEX_INDEX):
        try:
            with open(CODEX_INDEX, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        o = json.loads(line)
                        tid = o.get("id", "")
                        tname = (o.get("thread_name") or "").strip()
                        if tid:
                            if tname:
                                names[tid] = tname
                            extra.append((tid, tname, (o.get("updated_at", "") or "")[:16].replace("T", " ")))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
    return names, extra


def codex_threads():
    rows = []
    index_names, index_extra = _load_codex_index()
    seen_ids = set()

    if os.path.exists(STATE_DB):
        try:
            con = sqlite3.connect("file:%s?mode=ro" % STATE_DB, uri=True)
            cur = con.execute(
                """
                SELECT id, name, title, first_user_message, cwd, created_at, updated_at, rollout_path, thread_source
                FROM threads
                WHERE COALESCE(thread_source, '') != 'subagent'
                """
            )
            for r in cur:
                tid = r[0]
                raw_name = (r[1] or "").strip()
                raw_title = _clean_codex_prompt_text(r[2] or "")
                first_msg = _clean_codex_prompt_text(r[3] or "")
                cwd = _norm_win_path(r[4] or "")
                created_raw = r[5] or 0
                updated_raw = r[6] or 0
                rollout_path = _norm_win_path(r[7] or "")

                display_title = (
                    raw_name
                    or index_names.get(tid, "")
                    or (raw_title.split("\n", 1)[0][:80] if raw_title else "")
                    or (first_msg.split("\n", 1)[0][:80] if first_msg else "")
                )
                if display_title == "Imported session" and first_msg:
                    display_title = first_msg.split("\n", 1)[0][:80]

                seen_ids.add(tid)
                rows.append(
                    {
                        "agent": "codex",
                        "id": tid,
                        "title": display_title,
                        "raw_title": raw_title[:200],
                        "first_user_message": first_msg[:300],
                        "cwd": cwd,
                        "created_at": _fmt_ts(created_raw),
                        "updated_at": _fmt_ts(updated_raw),
                        "updated_ts": float(updated_raw) if updated_raw else 0.0,
                        "path": rollout_path or None,
                    }
                )
            con.close()
        except sqlite3.Error:
            pass

    for tid, tname, upd_str in index_extra:
        if tid in seen_ids:
            continue
        seen_ids.add(tid)
        rows.append(
            {
                "agent": "codex",
                "id": tid,
                "title": tname,
                "raw_title": "",
                "first_user_message": "",
                "cwd": "",
                "created_at": "",
                "updated_at": upd_str,
                "updated_ts": 0.0,
                "path": None,
            }
        )
    return rows


def codex_rollouts(sess_id):
    hits = []
    for base in ("sessions", "archived_sessions"):
        base_dir = os.path.join(CODEX_DIR, base)
        if not os.path.exists(base_dir):
            continue
        hits += glob.glob(
            os.path.join(base_dir, "**", "*%s*.jsonl*" % sess_id), recursive=True
        )
    return sorted(hits)


def _codex_messages_from_sqlite(thread_id, tail=None):
    """优先从 thread_history_1.sqlite 主键与用户消息索引毫秒级提取全量对话（从第 1 条到最后 1 条）。"""
    if not thread_id or not os.path.exists(HIST_DB):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % HIST_DB, uri=True)
        turns_row = con.execute(
            "SELECT COUNT(*) FROM thread_turns WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        turns_count = turns_row[0] if turns_row else 0
        if turns_count == 0:
            con.close()
            return None

        # 1. 利用主键 sqlite_autoindex_thread_items_1 与 idx_thread_items_user_messages 毫秒级提取全量轮次的用户与回复消息
        rows = con.execute(
            """
            SELECT item_type, item_json, rollout_ordinal FROM (
              SELECT i.item_type, i.item_json, i.rollout_ordinal
              FROM thread_items i INDEXED BY idx_thread_items_user_messages
              WHERE i.thread_id = ? AND i.item_type = 'userMessage'
              UNION ALL
              SELECT i.item_type, i.item_json, i.rollout_ordinal
              FROM thread_turns t
              JOIN thread_items i INDEXED BY sqlite_autoindex_thread_items_1
                ON i.thread_id = t.thread_id AND i.turn_id = t.turn_id AND i.item_id = t.first_user_item_id
              WHERE t.thread_id = ?
              UNION ALL
              SELECT i.item_type, i.item_json, i.rollout_ordinal
              FROM thread_turns t
              JOIN thread_items i INDEXED BY sqlite_autoindex_thread_items_1
                ON i.thread_id = t.thread_id AND i.turn_id = t.turn_id AND i.item_id = t.final_agent_item_id
              WHERE t.thread_id = ?
            )
            ORDER BY rollout_ordinal ASC
            """,
            (thread_id, thread_id, thread_id),
        ).fetchall()

        # 2. 补充末尾最近 80 条记录（覆盖正在进行中 turn 的中间 agentMessage）
        tail_rows = con.execute(
            """
            SELECT item_type, item_json, rollout_ordinal
            FROM thread_items INDEXED BY idx_thread_items_page
            WHERE thread_id = ?
            ORDER BY rollout_ordinal DESC
            LIMIT 80
            """,
            (thread_id,),
        ).fetchall()
        con.close()

        by_ord = {}
        for itype, ijson, r_ord in rows:
            if itype in ("userMessage", "agentMessage"):
                by_ord[r_ord] = (itype, ijson)
        for itype, ijson, r_ord in tail_rows:
            if itype in ("userMessage", "agentMessage"):
                by_ord[r_ord] = (itype, ijson)

        msgs = []
        for r_ord in sorted(by_ord.keys()):
            itype, ijson = by_ord[r_ord]
            try:
                obj = json.loads(ijson)
            except json.JSONDecodeError:
                continue
            role = "user" if itype == "userMessage" else "assistant"
            if role == "user":
                text = "\n".join(
                    c.get("text", "")
                    for c in obj.get("content", [])
                    if isinstance(c, dict) and c.get("text")
                ).strip()
            else:
                text = (obj.get("text") or "").strip()

            if not text or text.startswith(INJECT_PREFIXES):
                continue
            if text.startswith("[external_agent_tool_") or text.startswith("<send_user_message_question_reply>"):
                continue
            cleaned = _clean_codex_prompt_text(text)
            if not cleaned:
                continue
            if msgs and msgs[-1] == (role, cleaned):
                continue
            msgs.append((role, cleaned))

        if not msgs:
            return None
        total = len(msgs)
        if tail and tail > 0:
            return total, msgs[-int(tail):]
        return total, msgs
    except sqlite3.Error:
        return None


def codex_messages(path, thread_id=None, tail=None):
    fast = _codex_messages_from_sqlite(thread_id, tail=tail)
    if fast is not None:
        total, msgs = fast
        return {"total_count": total}, msgs

    meta, msgs = {}, []
    with open_text(path) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            otype = obj.get("type")
            if otype == "session_meta":
                p = obj.get("payload", {})
                meta = {
                    "id": p.get("id", ""),
                    "cwd": _norm_win_path(p.get("cwd", "")),
                    "forked_from": p.get("forked_from_id", ""),
                }
                continue
            if otype == "response_item":
                p = obj.get("payload", {})
                if p.get("type") != "message" or p.get("role") not in ("user", "assistant"):
                    continue
                text = "\n".join(
                    c.get("text", "")
                    for c in p.get("content", [])
                    if isinstance(c, dict) and c.get("text")
                ).strip()
                if not text or text.startswith(INJECT_PREFIXES):
                    continue
                if text.startswith("[external_agent_tool_") or text.startswith("<send_user_message_question_reply>"):
                    continue
                cleaned = _clean_codex_prompt_text(text)
                if not cleaned:
                    continue
                if msgs and msgs[-1] == (p["role"], cleaned):
                    continue
                msgs.append((p["role"], cleaned))
    meta["total_count"] = len(msgs)
    return meta, msgs


# ==================== Claude 侧 ====================

def _extract_claude_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for x in content:
            if isinstance(x, dict) and x.get("type") == "text" and x.get("text"):
                parts.append(x["text"].strip())
        return "\n".join(parts).strip()
    return ""


def _scan_claude_file_fast(path, size):
    """快速读取 Claude JSONL 的首尾块以提取标题、首条消息与工作目录。"""
    title = ""
    custom_title = ""
    cwd = ""
    first_msg = ""
    has_real_turn = False

    try:
        with open(path, "rb") as fb:
            head_bytes = fb.read(min(size, 32768))
            tail_bytes = b""
            if size > 32768:
                fb.seek(max(32768, size - 32768))
                tail_bytes = fb.read()
    except OSError:
        return None

    head_lines = head_bytes.decode("utf-8", errors="replace").splitlines()
    tail_lines = tail_bytes.decode("utf-8", errors="replace").splitlines()[1:] if tail_bytes else []

    for line in head_lines:
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = o.get("type")
        if t == "custom-title":
            custom_title = o.get("customTitle") or o.get("title") or custom_title
        elif t == "ai-title":
            title = o.get("aiTitle") or title
        if not cwd and o.get("cwd"):
            cwd = o.get("cwd")
        if not first_msg and t == "user" and not o.get("isSidechain") and not o.get("isMeta"):
            txt = _extract_claude_text(o.get("message", {}).get("content"))
            if txt and not txt.startswith(INJECT_PREFIXES):
                first_msg = txt
                has_real_turn = True

    for line in tail_lines:
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = o.get("type")
        if t == "custom-title":
            custom_title = o.get("customTitle") or o.get("title") or custom_title
        elif t == "ai-title":
            title = o.get("aiTitle") or title
        if o.get("cwd"):
            cwd = o.get("cwd")
        if t in ("user", "assistant") and not o.get("isSidechain") and not o.get("isMeta"):
            has_real_turn = True

    display_title = (
        custom_title.strip()
        or title.strip()
        or (first_msg.split("\n", 1)[0][:72].strip() if first_msg else "")
    )
    if not display_title and not has_real_turn:
        return None

    return {
        "title": display_title,
        "first_user_message": first_msg[:300],
        "cwd": cwd,
    }


def claude_sessions(max_files=None):
    """利用 os.scandir 零额外系统调用极速全量扫描所有 Claude 配置目录下的 projects/*/*.jsonl。"""
    out = []
    seen_ids = set()
    roots = _discover_claude_dirs()

    candidates = []
    for root in roots:
        proj_root = os.path.join(root, "projects")
        if not os.path.isdir(proj_root):
            continue
        try:
            for sub in os.scandir(proj_root):
                if not sub.is_dir():
                    continue
                try:
                    for entry in os.scandir(sub.path):
                        if entry.is_file() and entry.name.endswith(".jsonl"):
                            st = entry.stat()
                            candidates.append((st.st_mtime, st.st_size, entry.path, entry.name[:-6]))
                except OSError:
                    continue
        except OSError:
            continue

    candidates.sort(key=lambda x: x[0], reverse=True)
    scan_list = candidates[:max_files] if max_files else candidates
    for mtime, size, path, sess_id in scan_list:
        if sess_id in seen_ids:
            continue
        info = _scan_claude_file_fast(path, size)
        if not info:
            continue
        seen_ids.add(sess_id)
        out.append(
            {
                "agent": "claude",
                "id": sess_id,
                "title": info["title"],
                "raw_title": info["title"],
                "first_user_message": info["first_user_message"],
                "cwd": info["cwd"],
                "created_at": _fmt_ts(mtime),
                "updated_at": _fmt_ts(mtime),
                "updated_ts": float(mtime),
                "path": path,
            }
        )
    return out


def claude_messages(path):
    """提取 Claude 会话的真实对话（跳过工具结果/命令输出/注入块）。"""
    msgs = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            if t not in ("user", "assistant"):
                continue
            if o.get("isSidechain") or o.get("isMeta"):
                continue
            m = o.get("message", {})
            text = _extract_claude_text(m.get("content"))
            if not text or text.startswith(INJECT_PREFIXES):
                continue
            msgs.append((t, text))
    return msgs


# ==================== 统一索引与智能匹配 ====================

def all_sessions(agent="all"):
    rows = []
    if agent in ("codex", "all"):
        rows += codex_threads()
    if agent in ("claude", "all"):
        rows += claude_sessions()
    return rows


def find_rollout_for(row):
    """按 agent 类型找正文文件路径（优先使用已缓存的直接路径）。"""
    p = row.get("path")
    if p and os.path.exists(p):
        return p
    if row["agent"] == "codex":
        hits = codex_rollouts(row["id"])
        return hits[-1] if hits else None
    return None


def _parse_agent_prefix(key, default_agent="all"):
    """支持 @codex标题 / @codex 标题 / @codex：标题 / @codex:标题 / @claude标题 等所有写法。"""
    s = (key or "").strip()
    if s.startswith("@"):
        s = s[1:].strip()
    m = re.match(r"^(codex|claude)(?:[\s:：/\-_]+|(?![a-zA-Z0-9]))(.+)$", s, flags=re.IGNORECASE)
    if m:
        return m.group(2).strip().strip('"\'「」【】《》'), m.group(1).lower()
    return s.strip('"\'「」【】《》'), default_agent


def _is_subsequence(small, large):
    if not small or not large or len(small) > len(large):
        return False
    it = iter(large)
    return all(ch in it for ch in small)


def score_candidate(row, kw_lower):
    title = (row.get("title") or "").strip().lower()
    first_msg = (row.get("first_user_message") or "").strip().lower()
    raw_title = (row.get("raw_title") or "").strip().lower()

    if not kw_lower:
        return 50 if title else 0

    score = 0
    if title == kw_lower:
        score = 100
    elif title.startswith(kw_lower):
        score = 90
    elif kw_lower in title:
        score = 80
    elif kw_lower and title and title in kw_lower and len(title) >= 4:
        score = 70
    else:
        tokens = [t for t in re.split(r"\s+", kw_lower) if t]
        if len(tokens) > 1 and all(t in title for t in tokens):
            score = 72
        elif len(kw_lower) >= 2 and _is_subsequence(kw_lower.replace(" ", ""), title):
            score = 62
        elif kw_lower in first_msg:
            score = 35
        elif kw_lower in raw_title:
            score = 25

    if score > 0 and row.get("path"):
        score += 5
    return score


def resolve(key, agent="all"):
    """返回 (row, path)。path 为 None 表示正文已丢。"""
    clean_key, target_agent = _parse_agent_prefix(key, agent)
    if not clean_key:
        return None, None

    sessions = all_sessions(target_agent)
    if looks_like_id(clean_key):
        norm_k = clean_key.replace("-", "").lower()
        for row in sessions:
            if row["id"].replace("-", "").lower().startswith(norm_k):
                return row, find_rollout_for(row)
        return None, None

    kw = clean_key.lower()
    scored = []
    for r in sessions:
        sc = score_candidate(r, kw)
        if sc > 0:
            scored.append((sc, r.get("updated_ts", 0.0), r.get("updated_at", ""), r))

    if not scored:
        return None, None

    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    best = scored[0][3]
    return best, find_rollout_for(best)


def load_messages(row, path, tail=None):
    if row["agent"] == "codex":
        meta, msgs = codex_messages(path, thread_id=row["id"], tail=tail)
        return meta, msgs
    msgs = claude_messages(path)
    return {"total_count": len(msgs)}, msgs


def clip(text, limit):
    if limit and len(text) > limit:
        return text[:limit] + "\n…[截断，共 %d 字符，用 --full 或 --max-chars 看全]" % len(text)
    return text


def format_session_transcript(row, path, tail=None, max_chars=None, full=True, start=None, end=None, grep=None):
    if not path:
        return (
            "# %s 会话: %s\n- id: %s\n- 时间: %s | cwd: %s\n- ⚠ 正文文件已不存在，只剩标题元信息。\n- 首条消息: %s\n"
            % (
                row["agent"].capitalize(),
                row.get("title") or "(无标题)",
                row["id"],
                row.get("updated_at", "?"),
                row.get("cwd", "-"),
                row.get("first_user_message", "-"),
            )
        )
    meta, msgs = load_messages(row, path, tail=None)
    total_count = len(msgs)
    cwd = meta.get("cwd") or row.get("cwd")
    lines = [
        "# %s 会话: %s" % (row["agent"].capitalize(), row.get("title") or "(无标题)"),
        "- 会话 ID: %s" % row["id"],
        "- 更新时间: %s" % row.get("updated_at", "?"),
        "- 正文路径: %s" % path,
    ]
    if cwd:
        lines.append("- 工作目录: %s" % cwd)

    indexed_msgs = [(idx + 1, role, text) for idx, (role, text) in enumerate(msgs)]

    if grep:
        g_low = grep.lower()
        indexed_msgs = [(idx, role, text) for idx, role, text in indexed_msgs if g_low in text.lower()]
        lines.append("- 有效消息总数: %d (关键词 `%s` 命中 %d 条)" % (total_count, grep, len(indexed_msgs)))
    elif start is not None or end is not None:
        s_idx = max(1, int(start)) if start is not None else 1
        e_idx = min(total_count, int(end)) if end is not None else total_count
        indexed_msgs = [(idx, role, text) for idx, role, text in indexed_msgs if s_idx <= idx <= e_idx]
        lines.append("- 有效消息总数: %d (显示区间 #%d ~ #%d，共 %d 条)" % (total_count, s_idx, e_idx, len(indexed_msgs)))
    elif tail and int(tail) > 0 and not full:
        indexed_msgs = indexed_msgs[-int(tail):]
        lines.append("- 有效消息总数: %d (显示最后 %d 条)" % (total_count, len(indexed_msgs)))
    else:
        lines.append("- 有效消息总数: %d (已完整加载从 #1 到 #%d 的全量对话)" % (total_count, len(indexed_msgs)))

    lines.append("")

    for idx, role, text in indexed_msgs:
        who = {"user": "🧑 用户", "assistant": "🤖 Agent"}.get(role, role)
        lines.append("## [%d] %s" % (idx, who))
        lines.append(text if (full or not max_chars) else clip(text, max_chars))
        lines.append("")
    return "\n".join(lines)


# ==================== 子命令 ====================

def cmd_search(args):
    clean_kw, target_agent = _parse_agent_prefix(args.keyword or "", args.agent)
    kw = clean_kw.lower()
    sessions = all_sessions(target_agent)

    if not kw:
        sessions.sort(key=lambda r: (r.get("updated_ts", 0.0), r.get("updated_at", "")), reverse=True)
        matched = sessions
    else:
        scored = []
        for r in sessions:
            sc = score_candidate(r, kw)
            if sc == 0 and (kw in (r.get("cwd") or "").lower() or kw in (r.get("updated_at") or "").lower()):
                sc = 10
            if sc > 0:
                scored.append((sc, r.get("updated_ts", 0.0), r))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        matched = [x[2] for x in scored]

    if not matched:
        print("没有匹配的会话（关键词: %s）" % args.keyword)
        return
    shown_limit = args.limit if args.limit and args.limit > 0 else len(matched)
    print("共 %d 个匹配（显示前 %d 个）:" % (len(matched), min(shown_limit, len(matched))))
    for r in matched[:shown_limit]:
        path = find_rollout_for(r)
        status = "内容 OK" if path else "⚠ 正文已丢(仅剩标题)"
        tag = "[Claude]" if r["agent"] == "claude" else "[Codex] "
        print()
        print("%s %s %s  %s" % (r.get("updated_at", "?"), tag, r["id"][:13], r.get("title") or "(无标题)"))
        print("    %s | cwd=%s" % (status, r.get("cwd", "-")))
        if path:
            print("    -> %s" % path)


def cmd_read(args):
    row, path = resolve(args.key, args.agent)
    if not row:
        print("没找到会话: %s" % args.key)
        sys.exit(1)
    use_full = True if (args.full or not args.tail) else False
    out = format_session_transcript(
        row,
        path,
        tail=args.tail,
        max_chars=args.max_chars,
        full=use_full,
        start=args.start,
        end=args.end,
        grep=args.grep,
    )
    print(out)
    if not path:
        sys.exit(1)


def cmd_handoff(args):
    row, path = resolve(args.key, args.agent)
    if not row:
        print("没找到会话: %s" % args.key)
        sys.exit(1)
    out = args.out
    if not out:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", row.get("title") or row["id"])[:40]
        out = os.path.join(CLAUDE_DIR, "handoffs", "%s-%s.md" % (stamp, slug))

    use_full = True if (args.full or not args.tail) else False
    body = format_session_transcript(row, path, tail=args.tail, max_chars=args.max_chars, full=use_full)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(body)
    win_out = os.path.abspath(out).replace("/", "\\")
    print("交接包已写入: %s (%d 字符)" % (win_out, len(body)))


def cmd_list_claude(args):
    rows = claude_sessions()
    rows.sort(key=lambda r: (r.get("updated_ts", 0.0), r.get("updated_at", "")), reverse=True)
    shown_limit = args.limit if args.limit and args.limit > 0 else len(rows)
    print("Claude 会话共 %d 个（按修改时间倒序，显示 %d 个）:" % (len(rows), min(shown_limit, len(rows))))
    for r in rows[:shown_limit]:
        print("  %s  %s  %s" % (r.get("updated_at", "?"), r["id"][:13], r.get("title") or "(无标题)"))
        print("      cwd=%s" % r.get("cwd", "-"))


def cmd_list_json(args):
    rows = all_sessions(args.agent)
    rows = [r for r in rows if r.get("title")]
    rows.sort(key=lambda r: (r.get("updated_ts", 0.0), r.get("updated_at", "")), reverse=True)
    shown_limit = args.limit if args.limit and args.limit > 0 else len(rows)
    result = []
    for r in rows[:shown_limit]:
        result.append(
            {
                "agent": r["agent"],
                "id": r["id"],
                "title": r["title"],
                "cwd": r.get("cwd", ""),
                "updated_at": r.get("updated_at", ""),
                "has_transcript": bool(find_rollout_for(r)),
            }
        )
    print(json.dumps(result, ensure_ascii=False))


def extract_session_mentions_from_prompt(prompt_text, current_agent="claude", cwd=None):
    """从用户输入的 prompt 中提取显式 @codex / @claude 会话标题引用（避免与客户端自带裸 @ 冲突）。
    支持语法:
      - @codex 会话标题 [后续提问]
      - @claude 会话标题 [后续提问]
      - @codex:会话标题 / @claude:会话标题
      - @codex「含空格的会话标题」 / @claude "含空格的会话标题"
    """
    if not prompt_text or "@" not in prompt_text:
        return []
    if not re.search(r'@[「【《"\']?(?:codex|claude)', prompt_text, flags=re.IGNORECASE):
        return []

    cached_by_agent = {}

    def _get_agent_sessions(ag):
        if ag not in cached_by_agent:
            rows = all_sessions(ag)
            rows.sort(key=lambda r: (len((r.get("title") or "").strip()), r.get("updated_ts", 0.0)), reverse=True)
            cached_by_agent[ag] = rows
        return cached_by_agent[ag]

    resolved = []
    seen_ids = set()

    # 兼容全拼无分隔符 (@codex标题)、空格 (@codex 标题)、中文冒号 (@codex：标题)、英文冒号/斜杠 (@codex:标题) 及引号包裹
    pattern = re.compile(
        r"@(?:[「【《\"'](codex|claude)[\s:：/\-_]*([^」】》\"'\n]+)[」】》\"']|(codex|claude)(?:[\s:：/\-_]+|(?![a-zA-Z]))(?:[「【《\"']([^」】》\"'\n]+)[」】》\"']|([^\n]+)))",
        flags=re.IGNORECASE,
    )

    for m in pattern.finditer(prompt_text):
        ag = (m.group(1) or m.group(3) or "").lower()
        bracketed = (m.group(2) or m.group(4) or "").strip()
        rest_line = (m.group(5) or "").strip()

        row, path = None, None
        if bracketed:
            # 剥离可能带有的 #shortId 后缀
            clean_b = re.sub(r"#[0-9a-fA-F]{6,12}$", "", bracketed).strip()
            row, path = resolve(clean_b, ag)
        elif rest_line:
            rest_clean = re.sub(r"#[0-9a-fA-F]{6,12}(?=\s|$)", "", rest_line)
            # 1. 先尝试最长前缀匹配已知会话标题（兼容标题本身带空格的情况）
            rest_lower = rest_clean.lower()
            for cand in _get_agent_sessions(ag):
                t = (cand.get("title") or "").strip()
                if len(t) >= 3 and rest_lower.startswith(t.lower()):
                    row, path = cand, find_rollout_for(cand)
                    break
            # 2. 若未完整前缀命中，取第一个词（空格或标点前）做关键词智能匹配
            if not row:
                first_token = re.split(r"[\s,，。！？!?；;：:]+", rest_clean, maxsplit=1)[0].strip().strip('"\'「」【】《》')
                if first_token:
                    row, path = resolve(first_token, ag)

        if not row or row["id"] in seen_ids:
            continue
        seen_ids.add(row["id"])
        resolved.append((row, path))

    return resolved


def cmd_hook(args):
    try:
        raw_bytes = sys.stdin.buffer.read()
        if not raw_bytes:
            print("{}")
            return
        try:
            raw = raw_bytes.decode("utf-8-sig").strip()
        except UnicodeDecodeError:
            raw = raw_bytes.decode("gbk", errors="replace").strip()
        if not raw:
            print("{}")
            return
        data = json.loads(raw)
    except Exception:
        print("{}")
        return

    prompt_text = data.get("prompt") or data.get("user_prompt") or ""
    cwd = data.get("cwd") or os.getcwd()
    if not prompt_text or "@" not in prompt_text:
        print("{}")
        return

    matches = extract_session_mentions_from_prompt(prompt_text, current_agent=args.current_agent, cwd=cwd)
    if not matches:
        print("{}")
        return

    blocks = [
        "<cross-session-context>",
        "系统已自动为您识别并加载了用户通过 `@会话标题` 引用的完整历史会话上下文，请直接基于以下全量历史对话继续响应用户的请求（无需再次调用工具重复搜索该会话）：",
        "",
    ]
    for row, path in matches[:3]:
        blocks.append(format_session_transcript(row, path, tail=None, max_chars=None, full=True))
        blocks.append("---")
    blocks.append("</cross-session-context>")

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(blocks),
        }
    }
    print(json.dumps(output, ensure_ascii=False))


def _suggest_sessions_for_agent(agent, kw_raw, limit=15, include_prefix=True, sep=""):
    kw = (kw_raw or "").strip().lower()
    rows = all_sessions(agent)
    scored = []
    for r in rows:
        t = (r.get("title") or "").strip()
        if not t:
            continue
        sc = score_candidate(r, kw) if kw else 50
        if sc >= 50:
            scored.append((sc, r.get("updated_ts", 0.0), r.get("updated_at", ""), r))

    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    out = []
    seen_titles = {}
    for _, _, _, r in scored[:limit]:
        t = re.sub(r"[\r\n]+", " ", (r.get("title") or "").strip())
        if not t:
            continue
        count = seen_titles.get(t, 0)
        seen_titles[t] = count + 1
        label = t if count == 0 else "%s#%s" % (t, r["id"][:8])
        out.append(("%s%s%s" % (r["agent"], sep, label)) if include_prefix else label)
    return out


def _suggest_local_files(cwd, query, limit=15):
    if not cwd or not os.path.isdir(cwd):
        return []
    q = (query or "").strip().replace("\\", "/")
    if q.startswith("./"):
        q = q[2:]
    q_low = q.lower()

    results = []
    ignore_dirs = {".git", "node_modules", "__pycache__", ".idea", ".vscode", "target", "dist", "build", ".next", "venv", ".venv"}

    if not q_low:
        try:
            for entry in os.scandir(cwd):
                if entry.name.startswith(".") and entry.name in ignore_dirs:
                    continue
                results.append(entry.name + ("/" if entry.is_dir() else ""))
                if len(results) >= limit:
                    break
        except OSError:
            pass
        return results

    try:
        scanned = 0
        for root, dirs, files in os.walk(cwd):
            dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".git")]
            rel_root = os.path.relpath(root, cwd).replace("\\", "/")
            if rel_root == ".":
                rel_root = ""
            for d in dirs:
                rel_p = ("%s/%s/" % (rel_root, d)) if rel_root else ("%s/" % d)
                if q_low in rel_p.lower():
                    results.append(rel_p)
            for f in files:
                rel_p = ("%s/%s" % (rel_root, f)) if rel_root else f
                if q_low in rel_p.lower() or _is_subsequence(q_low, f.lower()):
                    results.append(rel_p)
            scanned += len(files) + len(dirs)
            if len(results) >= limit * 2 or scanned > 5000:
                break
    except OSError:
        pass

    results.sort(key=lambda p: (0 if os.path.basename(p.rstrip("/")).lower().startswith(q_low) else 1, len(p), p))
    return results[:limit]


def cmd_suggest(args):
    """供 Claude Code fileSuggestion 与 MCP completion/complete 调用的毫秒级 @ 模糊补全。"""
    query = args.query
    cwd = os.getcwd()

    if query is None:
        try:
            raw_bytes = sys.stdin.buffer.read()
            if raw_bytes:
                try:
                    raw = raw_bytes.decode("utf-8-sig").strip()
                except UnicodeDecodeError:
                    raw = raw_bytes.decode("gbk", errors="replace").strip()
                if raw:
                    data = json.loads(raw)
                    query = data.get("query", "")
                    if data.get("cwd"):
                        cwd = data["cwd"]
        except Exception:
            query = ""

    q = (query or "").strip()
    if q.startswith("@"):
        q = q[1:].strip()

    other_agent = "codex" if args.current_agent == "claude" else "claude"

    if args.values_only:
        items = _suggest_sessions_for_agent(other_agent, q, limit=args.limit, include_prefix=False)
        print(json.dumps(items, ensure_ascii=False))
        return

    # 1. 输入了 @codex标题 / @codex：标题 / @codex 标题 / @codex:标题（无需冒号直接连打中文或任意分隔符均可）
    m = re.match(r"^(codex|claude)([\s:：/\-_]*)(.*)$", q, flags=re.IGNORECASE)
    if m:
        ag = m.group(1).lower()
        raw_sep = m.group(2) or ""
        sep = ":" if ":" in raw_sep else ("/" if "/" in raw_sep else ("-" if "-" in raw_sep else ""))
        kw = (m.group(3) or "").strip()
        for item in _suggest_sessions_for_agent(ag, kw, limit=args.limit, include_prefix=True, sep=sep):
            print(item)
        return

    # 2. 正在输入前缀（如 @c / @co / @cod / @code）
    q_low = q.lower()
    out = []
    if q_low and other_agent.startswith(q_low):
        out.append(other_agent)
        out.extend(_suggest_sessions_for_agent(other_agent, "", limit=5, include_prefix=True, sep=""))

    # 3. 常规文件补全 + 跨端会话标题模糊命中补充
    files = _suggest_local_files(cwd, q, limit=max(5, args.limit - len(out)))
    out.extend(files)

    if not q:
        out.append(other_agent)
        out.extend(_suggest_sessions_for_agent(other_agent, "", limit=5, include_prefix=True, sep=""))
    elif len(q) >= 2:
        sess_hits = _suggest_sessions_for_agent(other_agent, q, limit=5, include_prefix=True, sep="")
        for sh in sess_hits:
            if sh not in out:
                out.append(sh)

    for item in out[: args.limit]:
        print(item)


def main():
    ap = argparse.ArgumentParser(description="Claude ⇄ Codex 会话互读/接力（全量无损版）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="搜会话（默认全量检索所有历史会话）")
    s.add_argument("keyword", nargs="?", default="")
    s.add_argument("--agent", default="all", choices=["codex", "claude", "all"])
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_search)

    r = sub.add_parser("read", help="读会话内容（默认加载从 #1 到最后一条的全量对话）")
    r.add_argument("key")
    r.add_argument("--agent", default="all", choices=["codex", "claude", "all"])
    r.add_argument("--tail", type=int, default=None, help="仅看最后 N 条（默认不限，读取全量对话）")
    r.add_argument("--start", type=int, default=None, help="起始消息序号（从 1 开始）")
    r.add_argument("--end", type=int, default=None, help="结束消息序号")
    r.add_argument("--grep", default=None, help="在会话消息内按关键词过滤")
    r.add_argument("--max-chars", type=int, default=None, help="单条消息截断字符数（默认不截断）")
    r.add_argument("--full", action="store_true", default=True, help="加载完整对话（默认开启）")
    r.set_defaults(func=cmd_read)

    h = sub.add_parser("handoff", help="导出完整交接包给另一个 agent")
    h.add_argument("key")
    h.add_argument("--agent", default="all", choices=["codex", "claude", "all"])
    h.add_argument("--to", default="codex", choices=["codex", "claude"])
    h.add_argument("--tail", type=int, default=None)
    h.add_argument("--max-chars", type=int, default=None)
    h.add_argument("--full", action="store_true", default=True)
    h.add_argument("--out", default=None)
    h.set_defaults(func=cmd_handoff)

    lc = sub.add_parser("list-claude", help="列出 Claude 会话")
    lc.add_argument("--limit", type=int, default=100)
    lc.set_defaults(func=cmd_list_claude)

    lj = sub.add_parser("list-json", help="输出 JSON 格式全量会话列表")
    lj.add_argument("--agent", default="all", choices=["codex", "claude", "all"])
    lj.add_argument("--limit", type=int, default=500)
    lj.set_defaults(func=cmd_list_json)

    hk = sub.add_parser("hook", help="UserPromptSubmit 钩子自动注入 @会话标题 全量对话")
    hk.add_argument("--current-agent", default="claude", choices=["claude", "codex"])
    hk.set_defaults(func=cmd_hook)

    sg = sub.add_parser("suggest", help="@ 菜单实时模糊补全（支持 @codex:关键词 与文件补全）")
    sg.add_argument("--current-agent", default="claude", choices=["claude", "codex"])
    sg.add_argument("--query", default=None, help="直接传入查询词（不传则从 stdin JSON 读取）")
    sg.add_argument("--limit", type=int, default=15)
    sg.add_argument("--values-only", action="store_true", help="仅输出 JSON 标题数组供 MCP completion 使用")
    sg.set_defaults(func=cmd_suggest)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
