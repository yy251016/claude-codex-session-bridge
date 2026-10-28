#!/usr/bin/env node
/**
 * session_bridge_mcp.mjs
 * Zero-dependency MCP Server (stdio) for Claude Code (CLI & Desktop/IDE) and Codex (CLI & Desktop App).
 * Supports:
 *   - Real-time @codex:<fuzzy-query> autocomplete via MCP Resources + Resource Template Completion (completion/complete)
 *   - Direct @codex:<title> resource reading (resources/read)
 *   - Tools: search_sessions, read_session
 */
import { execFile } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const BRIDGE_PY = path.join(__dirname, "session_bridge.py");
const PYTHON_CANDIDATES = process.platform === "win32" ? ["python", "python3"] : ["python3", "python"];

// Parse optional --agent codex|claude|all flag
let defaultTargetAgent = "all";
for (let i = 2; i < process.argv.length; i++) {
  if (process.argv[i] === "--agent" && process.argv[i + 1]) {
    defaultTargetAgent = process.argv[i + 1].toLowerCase();
    i++;
  }
}

function runBridgeWithCmd(pyCmd, args, timeoutMs) {
  return new Promise((resolve, reject) => {
    execFile(
      pyCmd,
      [BRIDGE_PY, ...args],
      {
        encoding: "utf8",
        maxBuffer: 50 * 1024 * 1024,
        timeout: timeoutMs,
        windowsHide: true,
        env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
      },
      (err, stdout) => {
        if (err && !stdout) {
          reject(err);
          return;
        }
        resolve((stdout || "").trim());
      }
    );
  });
}

async function runBridge(args, timeoutMs = 30000) {
  let lastErr;
  for (const pyCmd of PYTHON_CANDIDATES) {
    try {
      return await runBridgeWithCmd(pyCmd, args, timeoutMs);
    } catch (err) {
      lastErr = err;
      if (err.code !== "ENOENT") {
        break;
      }
    }
  }
  throw new Error(lastErr?.stderr || lastErr?.message || "Failed to execute session_bridge.py");
}

// In-memory session cache for <1ms fuzzy autocomplete
let cachedSessions = [];
let lastCacheMs = 0;
let refreshPromise = null;

async function getSessionsCached(force = false) {
  const now = Date.now();
  if (!force && cachedSessions.length > 0 && now - lastCacheMs < 8000) {
    return cachedSessions;
  }
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    try {
      const raw = await runBridge(["list-json", "--agent", defaultTargetAgent, "--limit", "1000"]);
      const items = JSON.parse(raw || "[]").filter((it) => it.has_transcript && it.title);
      const seenTitles = new Map();
      for (const it of items) {
        const cleanTitle = it.title.replace(/[\r\n]+/g, " ").trim();
        const cnt = seenTitles.get(cleanTitle) || 0;
        seenTitles.set(cleanTitle, cnt + 1);
        it.uniqueTitle = cnt === 0 ? cleanTitle : `${cleanTitle}#${it.id.slice(0, 8)}`;
        it.cleanTitle = cleanTitle;
      }
      cachedSessions = items;
      lastCacheMs = Date.now();
    } catch {
      // Keep existing cache on transient error
    } finally {
      refreshPromise = null;
    }
    return cachedSessions;
  })();
  return refreshPromise;
}

// Pre-warm cache immediately on server startup
getSessionsCached();

function isSubsequence(small, large) {
  if (!small || !large || small.length > large.length) return false;
  let j = 0;
  for (let i = 0; i < large.length && j < small.length; i++) {
    if (large[i] === small[j]) j++;
  }
  return j === small.length;
}

function scoreSession(item, query) {
  if (!query) return 50;
  const q = query.toLowerCase().trim();
  const title = (item.cleanTitle || "").toLowerCase();
  const cwd = (item.cwd || "").toLowerCase();
  if (title === q) return 100;
  if (title.startsWith(q)) return 90;
  if (title.includes(q)) return 80;
  const tokens = q.split(/\s+/).filter(Boolean);
  if (tokens.length > 1 && tokens.every((t) => title.includes(t))) return 75;
  if (q.length >= 2 && isSubsequence(q.replace(/\s+/g, ""), title)) return 65;
  if (cwd.includes(q)) return 40;
  return 0;
}

const TOOLS = [
  {
    name: "search_sessions",
    description:
      "Search or list all local Codex and Claude Code conversation sessions by title, keyword, or date. Searches the entire historical database with no session count limit.",
    inputSchema: {
      type: "object",
      properties: {
        query: {
          type: "string",
          description:
            "Session title or keyword to search for (leave empty to list sessions). Supports prefixes like codex:标题 or claude:标题.",
        },
        agent: {
          type: "string",
          enum: ["all", "codex", "claude"],
          description: "Filter by source agent ('codex', 'claude', or 'all'). Defaults to 'all'.",
        },
        limit: {
          type: "number",
          description: "Maximum number of sessions to return (default: 100, use 0 for all).",
        },
      },
    },
  },
  {
    name: "read_session",
    description:
      "Read the full conversation transcript (from message #1 to the last message by default) of a Codex or Claude Code session by its title (e.g., '@codex 订单结算重构' or '登录鉴权优化') or session ID prefix.",
    inputSchema: {
      type: "object",
      properties: {
        query: {
          type: "string",
          description:
            "Session title, keyword, or session ID prefix (e.g. '订单结算重构', '@codex:订单结算重构', '01a13d21').",
        },
        agent: {
          type: "string",
          enum: ["all", "codex", "claude"],
          description: "Target agent ('codex', 'claude', or 'all'). Defaults to 'all'.",
        },
        tail: {
          type: "number",
          description: "Optional: only read the last N messages. Leave omitted to read the full conversation from #1 to the end.",
        },
        start: {
          type: "number",
          description: "Optional: start message index (1-based).",
        },
        end: {
          type: "number",
          description: "Optional: end message index (inclusive).",
        },
        grep: {
          type: "string",
          description: "Optional: filter messages inside the session by keyword.",
        },
      },
      required: ["query"],
    },
  },
];

async function handleRequest(msg) {
  const { id, method, params } = msg;

  if (method === "initialize") {
    const clientVersion = params?.protocolVersion || "2024-11-05";
    return {
      protocolVersion: clientVersion,
      capabilities: {
        resources: { listChanged: false },
        completions: {},
        tools: { listChanged: false },
      },
      serverInfo: {
        name: defaultTargetAgent === "all" ? "session-bridge" : defaultTargetAgent,
        version: "2.2.0",
      },
    };
  }

  if (method === "ping") {
    return {};
  }

  if (method === "resources/list") {
    const items = await getSessionsCached();
    const resources = items.map((it) => {
      const tag = it.agent === "codex" ? "Codex" : "Claude";
      return {
        uri: it.uniqueTitle,
        name: it.cleanTitle,
        description: `${tag} 会话 (${it.updated_at}) | cwd: ${it.cwd || "-"}`,
        mimeType: "text/markdown",
      };
    });
    return { resources };
  }

  if (method === "resources/templates/list") {
    const label = defaultTargetAgent === "claude" ? "Claude" : "Codex";
    return {
      resourceTemplates: [
        {
          uriTemplate: "{title}",
          name: `${label} 历史会话模糊检索`,
          description: `输入关键词实时模糊匹配 ${label} 历史会话标题`,
          mimeType: "text/markdown",
        },
      ],
    };
  }

  if (method === "completion/complete") {
    const rawVal = String(params?.argument?.value || "").trim();
    const items = await getSessionsCached();
    const scored = [];
    for (const it of items) {
      const sc = scoreSession(it, rawVal);
      if (sc > 0) {
        scored.push({ sc, item: it });
      }
    }
    scored.sort((a, b) => b.sc - a.sc);
    const values = scored.slice(0, 50).map((x) => x.item.uniqueTitle);
    return {
      completion: {
        values,
        total: scored.length,
        hasMore: scored.length > 50,
      },
    };
  }

  if (method === "resources/read") {
    const rawUri = params?.uri || "";
    const match = rawUri.match(/^session:\/\/(codex|claude|all)\/(.+)$/i);
    const agent = match ? match[1].toLowerCase() : defaultTargetAgent;
    let key = match ? decodeURIComponent(match[2]) : decodeURIComponent(rawUri);
    // If key ends with #shortId (from duplicate title disambiguation), use the shortId directly for exact lookup
    const hashMatch = key.match(/#([0-9a-fA-F]{6,12})$/);
    if (hashMatch) {
      key = hashMatch[1];
    }
    const text = await runBridge(["read", key, "--agent", agent, "--full"]);
    return {
      contents: [
        {
          uri: rawUri,
          mimeType: "text/markdown",
          text: text || `未找到会话: ${key}`,
        },
      ],
    };
  }

  if (method === "tools/list") {
    return { tools: TOOLS };
  }

  if (method === "tools/call") {
    const name = params?.name;
    const args = params?.arguments || {};

    if (name === "search_sessions") {
      const q = String(args.query || "").trim();
      const agent = String(args.agent || defaultTargetAgent).toLowerCase();
      const limit = args.limit !== undefined ? String(args.limit) : "100";
      const cliArgs = ["search"];
      if (q) cliArgs.push(q);
      cliArgs.push("--agent", ["codex", "claude", "all"].includes(agent) ? agent : "all");
      cliArgs.push("--limit", limit);
      const out = await runBridge(cliArgs);
      return {
        content: [{ type: "text", text: out || "没有找到匹配的会话。" }],
      };
    }

    if (name === "read_session") {
      const q = String(args.query || "").trim();
      if (!q) {
        return {
          isError: true,
          content: [{ type: "text", text: "参数错误: 必须提供 query (会话标题或 ID 前缀)" }],
        };
      }
      const agent = String(args.agent || defaultTargetAgent).toLowerCase();
      const cliArgs = [
        "read",
        q,
        "--agent",
        ["codex", "claude", "all"].includes(agent) ? agent : "all",
      ];
      if (args.tail) {
        cliArgs.push("--tail", String(args.tail));
      } else {
        cliArgs.push("--full");
      }
      if (args.start !== undefined) cliArgs.push("--start", String(args.start));
      if (args.end !== undefined) cliArgs.push("--end", String(args.end));
      if (args.grep) cliArgs.push("--grep", String(args.grep));

      const out = await runBridge(cliArgs);
      return {
        content: [{ type: "text", text: out || `未找到会话: ${q}` }],
      };
    }

    throw new Error(`Unknown tool: ${name}`);
  }

  if (id !== undefined) {
    throw new Error(`Method not supported: ${method}`);
  }
  return null;
}

let useContentLength = false;
let buffer = Buffer.alloc(0);

function sendResponse(payload) {
  const json = JSON.stringify(payload);
  if (useContentLength) {
    const byteLen = Buffer.byteLength(json, "utf8");
    process.stdout.write(`Content-Length: ${byteLen}\r\n\r\n${json}`);
  } else {
    process.stdout.write(`${json}\n`);
  }
}

async function dispatchMessage(rawStr) {
  let msg;
  try {
    msg = JSON.parse(rawStr);
  } catch {
    return;
  }
  if (msg.id === undefined) {
    return;
  }
  try {
    const result = await handleRequest(msg);
    sendResponse({ jsonrpc: "2.0", id: msg.id, result });
  } catch (err) {
    sendResponse({
      jsonrpc: "2.0",
      id: msg.id,
      error: {
        code: -32603,
        message: err?.message || String(err),
      },
    });
  }
}

process.stdin.on("data", (chunk) => {
  buffer = Buffer.concat([buffer, chunk]);

  while (buffer.length > 0) {
    const preview = buffer.subarray(0, Math.min(buffer.length, 32)).toString("utf8");
    if (preview.startsWith("Content-Length:")) {
      useContentLength = true;
      const headerEnd = buffer.indexOf("\r\n\r\n");
      if (headerEnd === -1) return;
      const headerStr = buffer.subarray(0, headerEnd).toString("utf8");
      const m = headerStr.match(/Content-Length:\s*(\d+)/i);
      if (!m) {
        buffer = buffer.subarray(headerEnd + 4);
        continue;
      }
      const len = parseInt(m[1], 10);
      const totalLen = headerEnd + 4 + len;
      if (buffer.length < totalLen) return;
      const bodyStr = buffer.subarray(headerEnd + 4, totalLen).toString("utf8");
      buffer = buffer.subarray(totalLen);
      dispatchMessage(bodyStr);
    } else {
      const nlIdx = buffer.indexOf("\n");
      if (nlIdx === -1) return;
      const lineStr = buffer.subarray(0, nlIdx).toString("utf8").trim();
      buffer = buffer.subarray(nlIdx + 1);
      if (lineStr) {
        dispatchMessage(lineStr);
      }
    }
  }
});
