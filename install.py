#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
install.py — Cross-platform installer for claude-codex-session-bridge (codex-sessions skill).
Automatically installs the skill, UserPromptSubmit hooks, and MCP servers for both:
  - Claude Code (CLI & Desktop/IDE Client)
  - OpenAI Codex (CLI & Desktop App)
Supports Windows, macOS, and Linux with zero hardcoded paths.
"""
import argparse
import json
import os
import re
import shutil
import sys

SKILL_NAME = "codex-sessions"
CORE_FILES = ("SKILL.md", "session_bridge.py", "session_bridge_mcp.mjs", "install.py")


def _norm_slash(p):
    return os.path.abspath(p).replace("\\", "/")


def _discover_claude_dirs():
    dirs = []
    seen = set()

    def _add(d):
        if not d:
            return
        expanded = os.path.abspath(os.path.expanduser(d))
        norm = expanded.lower() if os.name == "nt" else expanded
        if norm not in seen:
            seen.add(norm)
            dirs.append(expanded)

    _add("~/.claude")
    _add(os.environ.get("CLAUDE_CONFIG_DIR"))

    for base in list(dirs):
        settings_path = os.path.join(base, "settings.json")
        if os.path.isfile(settings_path):
            try:
                with open(settings_path, encoding="utf-8") as f:
                    cfg = json.load(f)
                custom = cfg.get("claudeConfigDir")
                if isinstance(custom, str) and custom.strip():
                    _add(custom.strip())
            except Exception:
                pass
    return dirs


def _load_json(path, default):
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def install_files(src_dir, target_skill_dir):
    os.makedirs(target_skill_dir, exist_ok=True)
    if os.path.abspath(src_dir) == os.path.abspath(target_skill_dir):
        return
    for fname in CORE_FILES:
        s = os.path.join(src_dir, fname)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(target_skill_dir, fname))


def configure_claude(claude_dir, bridge_py_slash, mcp_mjs_slash, py_cmd):
    # 1. settings.json -> hooks.UserPromptSubmit
    settings_path = os.path.join(claude_dir, "settings.json")
    settings = _load_json(settings_path, {})
    hooks = settings.setdefault("hooks", {})
    ups = hooks.setdefault("UserPromptSubmit", [])

    hook_cmd = f'{py_cmd} "{bridge_py_slash}" hook --current-agent claude || echo {{}}'
    already = False
    for group in ups:
        for h in group.get("hooks", []):
            if "session_bridge.py" in h.get("command", ""):
                h["command"] = hook_cmd
                already = True
    if not already:
        if ups and isinstance(ups[0].get("hooks"), list):
            ups[0]["hooks"].append({"type": "command", "command": hook_cmd, "timeout": 10})
        else:
            ups.append({"hooks": [{"type": "command", "command": hook_cmd, "timeout": 10}]})
    _save_json(settings_path, settings)

    # 2. .mcp.json (if present in custom claudeConfigDir)
    mcp_json_path = os.path.join(claude_dir, ".mcp.json")
    if os.path.isfile(mcp_json_path):
        mcp_cfg = _load_json(mcp_json_path, {})
        mcp_cfg.setdefault("mcpServers", {})["session-bridge"] = {
            "command": "node",
            "args": [mcp_mjs_slash],
        }
        _save_json(mcp_json_path, mcp_cfg)


def configure_claude_root_mcp(mcp_mjs_slash):
    claude_json_path = os.path.expanduser("~/.claude.json")
    data = _load_json(claude_json_path, {})
    data.setdefault("mcpServers", {})["session-bridge"] = {
        "type": "stdio",
        "command": "node",
        "args": [mcp_mjs_slash],
        "env": {},
    }
    _save_json(claude_json_path, data)


def configure_codex(codex_dir, bridge_py_slash, bridge_py_win, mcp_mjs_slash, py_cmd):
    os.makedirs(codex_dir, exist_ok=True)

    # 1. hooks.json -> hooks.UserPromptSubmit
    hooks_path = os.path.join(codex_dir, "hooks.json")
    hooks_data = _load_json(hooks_path, {"hooks": {}})
    ups = hooks_data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    cmd_posix = f'{py_cmd} "{bridge_py_slash}" hook --current-agent codex'
    cmd_win = f'python "{bridge_py_win}" hook --current-agent codex'

    already = False
    for group in ups:
        for h in group.get("hooks", []):
            if "session_bridge.py" in h.get("command", ""):
                h["command"] = cmd_posix
                h["commandWindows"] = cmd_win
                already = True
    if not already:
        ups.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd_posix,
                        "commandWindows": cmd_win,
                        "timeout": 10,
                        "statusMessage": "Loading @session context",
                    }
                ]
            }
        )
    _save_json(hooks_path, hooks_data)

    # 2. config.toml -> [mcp_servers.session-bridge]
    config_toml_path = os.path.join(codex_dir, "config.toml")
    toml_text = ""
    if os.path.isfile(config_toml_path):
        with open(config_toml_path, encoding="utf-8") as f:
            toml_text = f.read()

    block = (
        "\n[mcp_servers.session-bridge]\n"
        'command = "node"\n'
        f'args = ["{mcp_mjs_slash}"]\n'
        "startup_timeout_sec = 30\n"
        "tool_timeout_sec = 60\n\n"
        "[mcp_servers.session-bridge.env]\n"
        'PYTHONUTF8 = "1"\n'
        'PYTHONIOENCODING = "utf-8"\n'
    )

    if "[mcp_servers.session-bridge]" not in toml_text:
        toml_text = toml_text.rstrip() + "\n" + block
        with open(config_toml_path, "w", encoding="utf-8") as f:
            f.write(toml_text)


def main():
    ap = argparse.ArgumentParser(description="Install claude-codex-session-bridge for Claude Code & Codex")
    ap.parse_args()

    src_dir = os.path.dirname(os.path.abspath(__file__))
    claude_dirs = _discover_claude_dirs()
    codex_dir = os.path.abspath(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"))

    primary_skill_dir = os.path.join(claude_dirs[0], "skills", SKILL_NAME)
    for cdir in claude_dirs:
        install_files(src_dir, os.path.join(cdir, "skills", SKILL_NAME))
    install_files(src_dir, os.path.join(codex_dir, "skills", SKILL_NAME))

    bridge_py_slash = _norm_slash(os.path.join(primary_skill_dir, "session_bridge.py"))
    bridge_py_win = os.path.abspath(os.path.join(primary_skill_dir, "session_bridge.py")).replace("\\", "\\\\")
    mcp_mjs_slash = _norm_slash(os.path.join(primary_skill_dir, "session_bridge_mcp.mjs"))
    py_cmd = "python" if os.name == "nt" else "python3"

    for cdir in claude_dirs:
        configure_claude(cdir, bridge_py_slash, mcp_mjs_slash, py_cmd)
    configure_claude_root_mcp(mcp_mjs_slash)
    configure_codex(codex_dir, bridge_py_slash, bridge_py_win, mcp_mjs_slash, py_cmd)

    print("[OK] Skill files installed to:")
    for cdir in claude_dirs:
        print("  -", os.path.join(cdir, "skills", SKILL_NAME))
    print("  -", os.path.join(codex_dir, "skills", SKILL_NAME))
    print("[OK] UserPromptSubmit hooks & session-bridge MCP server configured for both Claude Code and Codex.")


if __name__ == "__main__":
    main()
