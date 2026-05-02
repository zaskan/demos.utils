# SPDX-License-Identifier: GPL-2.0-or-later
"""
Minimal asyncio IRC client + OpenAI-compatible /v1/chat/completions.
No pydle: modern Ergo 005 (ISUPPORT) can expose edge cases that crash pydle 1.x
(int(None) in on_isupport_modes).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
import sys
from collections import deque
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import httpx

from mcp_client import McpHub, create_hub_from_env, open_http_sessions


@dataclass
class Cfg:
    irc_host: str
    irc_port: int
    irc_channel: str
    nick: str
    irc_password: str | None
    irc_account: str | None
    tls: bool


def _env(name: str, default: str | None = None) -> str:
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    if default is not None:
        return default
    print(f"Missing required environment variable: {name}", file=sys.stderr)
    sys.exit(1)


def _opt(name: str) -> str | None:
    v = (os.environ.get(name) or "").strip()
    return v or None


def _truthy(name: str) -> bool:
    return (os.environ.get(name) or "").lower() in ("1", "true", "yes", "on")


def irc_connect_pass(cfg: Cfg) -> str | None:
    """Ergo: PASS account:password when accounts.login-via-pass-command is true."""
    p = (cfg.irc_password or "").strip() if cfg.irc_password else None
    if not p:
        return None
    a = (cfg.irc_account or "").strip() if cfg.irc_account else None
    if a:
        return f"{a}:{p}"
    return p


def _split_irc(m: str, n: int = 420) -> list[str]:
    m = m.replace("\r", " ").replace("\n", " ")
    if len(m) <= n:
        return [m] if m else [""]
    return [m[i : i + n] for i in range(0, len(m), n)]


# :nick!user@host PRIVMSG #chan :trailing
_RE_PRIVMSG = re.compile(
    r"^:([^!]+)![^ ]+ PRIVMSG (#[^ :]+) :(.*)$",
    re.DOTALL,
)
# Assistant text that claims work but never calls tools (common failure mode)
_RE_HOLLOW_ACK = re.compile(
    r"(?i)\b(checking|looking up|hang on|one moment|please wait|querying|pulling up|"
    r"i['']ll check|i['']ll look|i['']ll query|let me (check|look|query)|"
    r"give me a moment)\b"
)


def _parse_privmsg(line: str) -> tuple[str, str, str] | None:
    s = line.rstrip("\r\n")
    m = _RE_PRIVMSG.match(s)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def _wants_reply(msg: str, my_nick: str) -> bool:
    """Legacy gate: only !a or nick mention (when IRC_REPLY_ONLY_WHEN_MENTIONED is set)."""
    t = (msg or "").strip()
    if t.lower().startswith("!a "):
        return True
    m = t.lower()
    n = (my_nick or "").lower()
    return bool(n) and n in m


def _reply_only_when_mentioned() -> bool:
    return _truthy("IRC_REPLY_ONLY_WHEN_MENTIONED")


def _context_limits() -> tuple[bool, int, int]:
    """(enabled, max_messages, max_chars_for_formatted_block)."""
    if _truthy("IRC_CONTEXT_DISABLED"):
        return False, 0, 0
    n = int(os.environ.get("IRC_CONTEXT_MAX_MESSAGES", "50") or 50)
    c = int(os.environ.get("IRC_CONTEXT_MAX_CHARS", "12000") or 12000)
    if n <= 0:
        return False, 0, 0
    return True, max(5, min(200, n)), max(800, min(100_000, c))


class ChannelTranscript:
    """Rolling buffer of channel PRIVMSG lines for LLM context (in-memory; lost on reconnect)."""

    def __init__(self) -> None:
        ok, maxlen, self._max_chars = _context_limits()
        self._enabled = ok
        self._d: deque[tuple[str, str]] = deque(maxlen=maxlen) if ok else deque()

    def add(self, nick: str, text: str) -> None:
        if not self._enabled:
            return
        one = (text or "").replace("\r", " ").replace("\n", " ").strip()
        if not one:
            return
        n = (nick or "?").strip() or "?"
        self._d.append((n, one[:4000]))

    def format(self) -> str:
        if not self._enabled or not self._d:
            return ""
        lines = [f"{nick}: {msg}" for nick, msg in self._d]
        while len(lines) > 1 and sum(len(x) + 1 for x in lines) > self._max_chars:
            lines.pop(0)
        return "\n".join(lines)


def _user_with_channel_history(history: str, core_user_message: str) -> str:
    h = (history or "").strip()
    if not h:
        return core_user_message
    return (
        "Recent channel context (same channel; oldest first):\n"
        + h
        + "\n---\n"
        + core_user_message
    )


def _channel_decision_system(cfg: Cfg, hub: McpHub | None) -> str:
    base = _opt("LLM_SYSTEM")
    if base:
        persona = base
    else:
        persona = (
            f"You are “{cfg.nick}”, an IRC participant in {cfg.irc_channel}. "
            "You read channel traffic and may reply in public when it genuinely helps: "
            "answer questions, clarify, add concise technical insight, or respond naturally "
            "when someone is clearly talking to the room and you have something useful to say. "
            "Do not reply to every line: skip filler, pure greetings unless someone greets you, "
            "join/part noise, obvious bot output, spam, or threads where a reply would be noise. "
            "When recent context is provided, use it for continuity (follow-ups, pronouns, unresolved threads)."
        )
    if hub and hub.server_meta:
        block = hub.catalog_text() if hub.active else hub.server_catalog_text()
        persona += (
            "\n\nYou have MCP-backed integrations (see below). In this step you only choose "
            "whether a reply is warranted; you do not call tools here. If the topic matches "
            "an integration (jobs, inventory, Kubernetes, etc.), set respond:true so a second "
            "step can run tools and answer with facts.\n\n"
            + block
        )
    suffix = (
        "\n\nFor the next message, respond with ONLY one JSON object (no markdown fences), "
        'exactly this shape: {"respond":true,"message":"<single-line IRC-safe text>"} or '
        '{"respond":false}. If respond is false, use "message":"". '
        "Keep message under 400 characters, no newlines in message. "
        "If respond is true, you may use \"message\":\"\" (empty) when a tool follow-up will "
        "carry the full answer—avoid filler like 'checking…' unless you are not using tools."
    )
    return persona + suffix


def _strip_json_fence(raw: str) -> str:
    t = (raw or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


def _parse_channel_decision(raw: str, allow_empty_message: bool) -> tuple[bool, str]:
    """Returns (should_send, message). Empty message allowed only when tools will answer."""
    t = _strip_json_fence(raw)
    if not t:
        return False, ""
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        return False, ""
    if not isinstance(data, dict):
        return False, ""
    if not data.get("respond"):
        return False, ""
    msg = (data.get("message") or "").strip()
    msg = msg.replace("\r", " ").replace("\n", " ").strip()
    if not msg:
        if allow_empty_message:
            return True, ""
        return False, ""
    return True, msg[:420]


def _is_hollow_pre_tool_reply(text: str) -> bool:
    """True if the model answered with filler instead of invoking tools."""
    t = (text or "").strip()
    if not t:
        return False
    if len(t) > 220:
        return False
    return bool(_RE_HOLLOW_ACK.search(t))


async def chat_completions_channel_decision(
    cfg: Cfg,
    from_nick: str,
    text: str,
    direct: bool,
    hub: McpHub | None,
    history: str,
) -> tuple[bool, str]:
    """Ask the LLM whether to reply; returns (send, irc_line)."""
    base = _env("LLM_BASE_URL").rstrip("/")
    url = f"{base}/chat/completions"
    key = _env("LLM_API_KEY")
    model = _env("LLM_MODEL", "gpt-4o-mini")
    system = _channel_decision_system(cfg, hub)
    hints: list[str] = []
    if direct:
        hints.append("The sender explicitly invoked you (!a) or mentioned your nick — lean toward responding helpfully if the content warrants it.")
    core = (
        f'Channel: {cfg.irc_channel}\nFrom: {from_nick}\nText: {text or ""}\n'
        + ("\n".join(hints) if hints else "")
    )
    user_body = _user_with_channel_history(history, core)
    body: dict[str, Any] = {
        "model": model,
        "messages": _fit_llm_prompt_to_budget(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_body},
            ],
            None,
        )[0],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    t = float(os.environ.get("LLM_TIMEOUT", "120") or 120.0)
    async with httpx.AsyncClient(timeout=t) as client:
        r = await client.post(url, content=json.dumps(body), headers=headers)
        r.raise_for_status()
        data = r.json()
    choices = data.get("choices") or []
    if not choices:
        return False, ""
    m = (choices[0] or {}).get("message") or {}
    c = (m.get("content") or "").strip()
    return _parse_channel_decision(c, allow_empty_message=bool(hub and hub.active))


def _load_cfg() -> Cfg:
    return Cfg(
        irc_host=_env("IRC_HOST"),
        irc_port=int(_env("IRC_PORT", "6667")),
        irc_channel=_env("IRC_CHANNEL"),
        nick=_env("IRC_NICK", "irc-agent"),
        irc_password=_opt("IRC_PASSWORD"),
        irc_account=_opt("IRC_ACCOUNT"),
        tls=_truthy("IRC_TLS"),
    )


async def chat_completions(user_text: str, history: str = "") -> str:
    base = _env("LLM_BASE_URL").rstrip("/")
    url = f"{base}/chat/completions"
    key = _env("LLM_API_KEY")
    model = _env("LLM_MODEL", "gpt-4o-mini")
    system = _opt("LLM_SYSTEM") or "You are a concise helpful assistant on IRC. Keep answers short."
    if (history or "").strip():
        system += " Use recent channel context when provided for continuity."
    user_content = _user_with_channel_history(history, user_text)
    body: dict[str, Any] = {
        "model": model,
        "messages": _fit_llm_prompt_to_budget(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            None,
        )[0],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    t = float(os.environ.get("LLM_TIMEOUT", "120") or 120.0)
    async with httpx.AsyncClient(timeout=t) as client:
        r = await client.post(url, content=json.dumps(body), headers=headers)
        r.raise_for_status()
        data = r.json()
    choices = data.get("choices") or []
    if not choices:
        return "no model reply"
    m = (choices[0] or {}).get("message") or {}
    c = (m.get("content") or "").strip()
    return c or "empty model reply"


def _tools_reply_system(cfg: Cfg, hub: McpHub) -> str:
    base = _opt("LLM_SYSTEM")
    persona = (
        base
        if base
        else (
            f"You are “{cfg.nick}” on IRC in {cfg.irc_channel}. "
            "Use MCP tools only when they clearly improve the answer. "
            "After tools, reply in short, plain lines suitable for IRC (no JSON)."
        )
    )
    return (
        persona
        + "\n\nAvailable tools are listed in the API tools[] schema. "
        "For factual questions (counts, lists, status), you must call the relevant tool(s) "
        "— do not answer with only phrases like 'checking…' or 'looking that up' without tool calls. "
        "After tool results return, reply in short plain IRC lines with the actual numbers or facts. "
        "Call at most a few tools per request. "
        "When the user message includes recent channel context, use it for continuity."
    )


def _assistant_api_dict(msg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "assistant"}
    if msg.get("tool_calls"):
        # LiteLLM / some proxies reject null assistant content when tool_calls are present.
        out["content"] = ""
        out["tool_calls"] = msg["tool_calls"]
    else:
        c = msg.get("content")
        out["content"] = c if c is not None else None
    return out


def _sanitize_messages_for_llm(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure JSON bodies never send content: null (strict OpenAI-compatible gateways)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        m2 = dict(m)
        if m2.get("content") is None:
            m2["content"] = ""
        out.append(m2)
    return out


def _llm_max_input_tokens() -> int:
    """Estimated input token budget (messages + tools JSON). 0 = disable trimming."""
    raw = (
        os.environ.get("IRC_LLM_MAX_INPUT_TOKENS")
        or os.environ.get("IRC_AGENT_LLM_MAX_INPUT_TOKENS")
        or "32000"
    ).strip()
    try:
        n = int(raw)
    except ValueError:
        n = 32000
    return max(0, min(1_000_000, n))


def _message_payload_chars(messages: list[dict[str, Any]]) -> int:
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        elif c is not None:
            n += len(json.dumps(c, default=str))
        tc = m.get("tool_calls")
        if tc:
            n += len(json.dumps(tc, default=str))
    return n


def _tools_json_chars(tools: list[dict[str, Any]] | None) -> int:
    if not tools:
        return 0
    return len(json.dumps(tools, default=str))


def _estimated_input_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> int:
    # ~4 chars/token is a common heuristic for English + JSON; stay under real tokenizer counts.
    return (_message_payload_chars(messages) + _tools_json_chars(tools)) // 4


def _tools_cap_descriptions(tools: list[dict[str, Any]], max_len: int) -> None:
    for t in tools:
        fn = t.get("function")
        if not isinstance(fn, dict):
            continue
        d = fn.get("description")
        if not isinstance(d, str):
            fn["description"] = ""
            continue
        if max_len <= 0:
            fn["description"] = ""
        elif len(d) > max_len:
            fn["description"] = d[:max_len] + "…"


def _tools_strip_params(tools: list[dict[str, Any]]) -> None:
    for t in tools:
        fn = t.get("function")
        if isinstance(fn, dict):
            fn["parameters"] = {"type": "object", "properties": {}}


def _shrink_tools_budget_step(tools: list[dict[str, Any]], phase: list[int]) -> bool:
    """Progressively shrink tools[] JSON for context limits. phase[0] advances monotonically."""
    if not tools:
        return False
    p = phase[0]
    if p == 0:
        _tools_cap_descriptions(tools, 500)
        phase[0] = 1
        return True
    if p == 1:
        _tools_cap_descriptions(tools, 160)
        phase[0] = 2
        return True
    if p == 2:
        _tools_cap_descriptions(tools, 0)
        phase[0] = 3
        return True
    if p == 3:
        _tools_strip_params(tools)
        phase[0] = 4
        return True
    if len(tools) <= 1:
        return False
    tools.pop()
    return True


def _trim_user_preserving_tail(content: str, max_chars: int) -> str:
    """Keep the trailing '---' user question; shrink or drop leading channel history."""
    max_chars = max(max_chars, 200)
    if len(content) <= max_chars:
        return content
    sep = "\n---\n"
    pos = content.find(sep)
    if pos != -1:
        head, tail = content[:pos], content[pos + len(sep) :]
        room = max_chars - len(tail) - len(sep) - 80
        if room < 120:
            return "…[channel context omitted]" + sep + tail
        if len(head) > room:
            head = "…[earlier channel lines omitted]\n" + head[-(room - 40) :]
        return head + sep + tail
    return "…[truncated]\n" + content[-(max_chars - 16) :]


def _trim_one_message_round(msgs: list[dict[str, Any]]) -> bool:
    """One shrink step for prompt budgeting. Returns True if something changed."""
    best_i = -1
    best_len = 0
    for i, m in enumerate(msgs):
        if m.get("role") != "tool":
            continue
        c = m.get("content")
        if not isinstance(c, str):
            continue
        if len(c) > best_len:
            best_len = len(c)
            best_i = i
    if best_i >= 0 and best_len > 450:
        c = str(msgs[best_i]["content"])
        new_len = max(320, (best_len * 2) // 3)
        msgs[best_i] = dict(msgs[best_i])
        msgs[best_i]["content"] = c[:new_len] + "\n…[truncated for IRC agent context limit]"
        return True
    for i, m in enumerate(msgs):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if not isinstance(c, str) or len(c) < 900:
            continue
        msgs[i] = dict(msgs[i])
        msgs[i]["content"] = _trim_user_preserving_tail(c, int(len(c) * 0.65))
        return True
    for i, m in enumerate(msgs):
        if m.get("role") != "system":
            continue
        c = m.get("content")
        if not isinstance(c, str) or len(c) < 900:
            continue
        msgs[i] = dict(msgs[i])
        msgs[i]["content"] = c[: int(len(c) * 0.65)] + "\n…[truncated]"
        return True
    return False


def _fit_llm_prompt_to_budget(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Shrink messages and optionally tools[] until estimated input tokens <= budget."""
    cap = _llm_max_input_tokens()
    msgs: list[dict[str, Any]] = [dict(m) for m in messages]
    if cap <= 0:
        return _sanitize_messages_for_llm(msgs), tools
    tls: list[dict[str, Any]] | None = json.loads(json.dumps(tools)) if tools else None
    tool_phase = [0]
    steps = 0
    while _estimated_input_tokens(msgs, tls) > cap and steps < 2000:
        if _trim_one_message_round(msgs):
            steps += 1
            continue
        if tls is not None and _shrink_tools_budget_step(tls, tool_phase):
            steps += 1
            continue
        est = _estimated_input_tokens(msgs, tls)
        logging.warning(
            "LLM prompt ~%d est. tokens > budget %d (messages ~%d chars, tools ~%d chars); giving up",
            est,
            cap,
            _message_payload_chars(msgs),
            _tools_json_chars(tls),
        )
        break
    if steps:
        logging.info(
            "LLM prompt fitted to ~%d est. tokens (budget %d, %d steps; tools phase %s)",
            _estimated_input_tokens(msgs, tls),
            cap,
            steps,
            tool_phase[0] if tls else "-",
        )
    return _sanitize_messages_for_llm(msgs), tls


def _mcp_first_tool_choice() -> str | None:
    """OpenAI-style tool_choice for first turn: 'required' forces a tool call (omit on gateways that 400)."""
    v = (
        os.environ.get("IRC_MCP_FIRST_TOOL_CHOICE")
        or os.environ.get("IRC_AGENT_MCP_FIRST_TOOL_CHOICE")
        or "auto"
    ).strip().lower()
    if v in ("required", "auto", "none"):
        return v
    return "auto"


async def _chat_completion_post(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> dict[str, Any]:
    r = await client.post(url, content=json.dumps(body), headers=headers)
    if r.status_code >= 400:
        snippet = (r.text or "")[:2000]
        logging.warning("LLM HTTP %s: %s", r.status_code, snippet or "(empty body)")
    r.raise_for_status()
    return r.json()


async def _synthesize_tool_answer_to_irc(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    model: str,
    messages: list[dict[str, Any]],
) -> str:
    """One non-tool completion after tool results so the operator always gets a plain IRC answer."""
    tail = list(messages)
    tail.append(
        {
            "role": "user",
            "content": (
                "Summarize the conversation above for IRC: one to four short lines, plain text, "
                "include concrete numbers or facts from tool results. No JSON, no markdown fences."
            ),
        }
    )
    body: dict[str, Any] = {"model": model, "messages": _fit_llm_prompt_to_budget(tail, None)[0]}
    data = await _chat_completion_post(client, url, headers, body)
    choices = data.get("choices") or []
    if not choices:
        return ""
    m = (choices[0] or {}).get("message") or {}
    return (m.get("content") or "").strip()


async def chat_completions_with_tools(
    cfg: Cfg,
    hub: McpHub,
    user_block: str,
) -> str:
    """Multi-turn OpenAI-style chat with MCP tool execution (HTTP sessions pooled per URL)."""
    tools = hub.openai_tool_schemas()
    if not tools:
        return ""
    base = _env("LLM_BASE_URL").rstrip("/")
    url = f"{base}/chat/completions"
    key = _env("LLM_API_KEY")
    model = _env("LLM_MODEL", "gpt-4o-mini")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    t = float(os.environ.get("LLM_TIMEOUT", "120") or 120.0)
    max_rounds = max(1, hub.opts.max_tool_roundtrips)
    first_choice = _mcp_first_tool_choice()
    hollow_nudge_used = False
    async with AsyncExitStack() as stack:
        sessions = await open_http_sessions(hub, stack)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _tools_reply_system(cfg, hub)},
            {"role": "user", "content": user_block},
        ]
        async with httpx.AsyncClient(timeout=t) as client:
            for idx in range(max_rounds):
                send_msgs, send_tools = _fit_llm_prompt_to_budget(messages, tools)
                body: dict[str, Any] = {
                    "model": model,
                    "messages": send_msgs,
                    "tools": send_tools,
                }
                if idx == 0 and first_choice == "required":
                    body["tool_choice"] = "required"
                elif idx == 0 and first_choice == "none":
                    body["tool_choice"] = "none"
                try:
                    data = await _chat_completion_post(client, url, headers, body)
                except httpx.HTTPStatusError as e:
                    # Some OpenAI-compatible servers reject tool_choice "required"
                    if (
                        idx == 0
                        and first_choice == "required"
                        and e.response is not None
                        and e.response.status_code in (400, 422)
                    ):
                        body["tool_choice"] = "auto"
                        data = await _chat_completion_post(client, url, headers, body)
                    else:
                        raise
                choices = data.get("choices") or []
                if not choices:
                    break
                msg = (choices[0] or {}).get("message") or {}
                tcs = msg.get("tool_calls")
                content = (msg.get("content") or "").strip()
                if tcs:
                    messages.append(_assistant_api_dict(msg))
                    for tc in tcs:
                        fn = tc.get("function") or {}
                        name = (fn.get("name") or "").strip()
                        raw_args = fn.get("arguments") or "{}"
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        except json.JSONDecodeError:
                            args = {}
                        if not isinstance(args, dict):
                            args = {}
                        out = await hub.call_tool(name, args, sessions)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.get("id") or "",
                                "content": out,
                            }
                        )
                    continue
                if content and not hollow_nudge_used and _is_hollow_pre_tool_reply(content):
                    hollow_nudge_used = True
                    messages.append(_assistant_api_dict(msg))
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "That reply does not include data. Call the appropriate MCP tool(s) "
                                "now to fetch facts, then answer with counts or a clear summary—no filler."
                            ),
                        }
                    )
                    continue
                if content:
                    if hollow_nudge_used and _is_hollow_pre_tool_reply(content):
                        break
                    return content
                break
            if any(m.get("role") == "tool" for m in messages):
                syn = await _synthesize_tool_answer_to_irc(client, url, headers, model, messages)
                if syn:
                    return syn
    return ""


def _send(w: asyncio.StreamWriter, s: str) -> None:
    w.write(s.encode("utf-8", "replace"))


def _pong(s: str) -> str:
    u = s.rstrip()
    if "PRIVMSG" in u:  # don't treat "ping" in chat as server PING
        return ""
    m = re.search("(?i)PING", u)
    if not m:
        return ""
    rest = u[m.end() :].lstrip()
    if not rest:
        return "PONG :i\r\n"
    if rest.startswith(":"):
        return f"PONG {rest}\r\n"
    return f"PONG :{rest.split()[0] if rest else 'i'}\r\n"


def _one_line(s: str) -> str:
    t = s.rstrip("\n")
    if t.endswith("\r"):
        t = t[:-1]
    return t


async def irc_run_loop(cfg: Cfg, hub: McpHub | None) -> None:
    sslctx: ssl.SSLContext | None
    if cfg.tls:
        sslctx = ssl.create_default_context()
        if _truthy("IRC_TLS_INSECURE"):
            sslctx.check_hostname = False
            sslctx.verify_mode = ssl.CERT_NONE
    else:
        sslctx = None
    r, w = await asyncio.open_connection(
        cfg.irc_host, cfg.irc_port, ssl=sslctx, server_hostname=cfg.irc_host if cfg.tls else None
    )
    pw = irc_connect_pass(cfg)
    if pw:
        _send(w, f"PASS {pw}\r\n")
    _send(w, f"NICK {cfg.nick}\r\n")
    _send(w, f"USER {cfg.nick} 0 * :irc-llm-agent\r\n")
    await w.drain()
    ch = cfg.irc_channel if cfg.irc_channel.startswith("#") else f"#{cfg.irc_channel}"
    ch_norm = ch.rstrip()
    buf = b""
    seen_welcome = False
    transcript = ChannelTranscript()
    while True:
        data = await r.read(4096)
        if not data:
            raise ConnectionError("server closed (EOF)")
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            s = _one_line(line.decode("utf-8", "replace"))
            pong = _pong(s)
            if pong:
                w.write(pong.encode("utf-8", "replace"))
                await w.drain()
            if not seen_welcome:
                if " 001 " in s:
                    seen_welcome = True
                    _send(w, f"JOIN {ch_norm}\r\n")
                    await w.drain()
                    logging.info("server welcome, joined %s", ch_norm)
                continue
            if not s.strip():
                continue
            pm = _parse_privmsg(s)
            if not pm:
                continue
            by, target, text = pm
            if ch_norm.lower() != target.rstrip().lower():
                continue
            if (by or "").casefold() == (cfg.nick or "").casefold():
                continue
            raw_line = (text or "").strip()
            if not raw_line:
                continue
            direct = _wants_reply(raw_line, cfg.nick)
            if _reply_only_when_mentioned() and not direct:
                continue
            hist = transcript.format()
            parts: list[str] = []
            try:
                if _reply_only_when_mentioned():
                    user_line = raw_line
                    if user_line.lower().startswith("!a "):
                        user_line = user_line[3:].lstrip() or "hello"
                    ub_core = f"Channel: {cfg.irc_channel}\nFrom: {by}\nText: {user_line}\n"
                    ub = _user_with_channel_history(hist, ub_core)
                    if hub and hub.active:
                        out = await chat_completions_with_tools(cfg, hub, ub)
                        parts = _split_irc(out or "empty model reply")
                    else:
                        out = await chat_completions(user_line, hist)
                        parts = _split_irc(out)
                else:
                    send, reply = await chat_completions_channel_decision(
                        cfg, by, raw_line, direct=direct, hub=hub, history=hist
                    )
                    user_block_core = (
                        f"Channel: {cfg.irc_channel}\nFrom: {by}\nText: {raw_line}\n"
                    )
                    user_block = _user_with_channel_history(hist, user_block_core)
                    if send and hub and hub.active:
                        tool_reply = await chat_completions_with_tools(cfg, hub, user_block)
                        final = (tool_reply or "").strip() or (reply or "").strip()
                        parts = _split_irc(final)
                    elif send:
                        parts = _split_irc(reply)
                    else:
                        parts = []
            except Exception as e:  # noqa: BLE001
                err = f"llm error: {e!s}"[:200]
                parts = _split_irc(err) if direct else []
            finally:
                transcript.add(by, raw_line)
                joined = " ".join(p for p in parts if p).strip()
                if joined:
                    transcript.add(cfg.nick, joined[:4000])
            for part in parts:
                if part:
                    _send(w, f"PRIVMSG {target} :{part}\r\n")
            await w.drain()


async def _main_run(cfg: Cfg) -> None:
    delay = 5.0
    hub = await create_hub_from_env()
    while True:
        try:
            await irc_run_loop(cfg, hub)
        except (ConnectionError, OSError, ssl.SSLError, TimeoutError) as e:
            logging.error("irc session ended: %s, reconnecting in %ss", e, delay)
            await asyncio.sleep(delay)
        except Exception as e:  # noqa: BLE001
            logging.exception("fatal irc: %s", e)
            await asyncio.sleep(delay)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("mcp.client.streamable_http").setLevel(logging.WARNING)
    cfg = _load_cfg()
    try:
        asyncio.run(_main_run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
