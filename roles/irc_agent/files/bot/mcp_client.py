# SPDX-License-Identifier: GPL-2.0-or-later
"""MCP client: load mcp.json, discover tools, map OpenAI tool_calls to MCP call_tool."""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from urllib.parse import urlparse
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import mcp.types as mtypes
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client

logger = logging.getLogger(__name__)

_AAP_TOKEN_ENV_KEYS = ("AAP_MCP_SERVER_TOKEN", "IRC_AGENT_AAP_MCP_SERVER_TOKEN")


def _aap_bearer_from_env() -> str:
    for k in _AAP_TOKEN_ENV_KEYS:
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    return ""


def _normalize_mcp_http_url(url: str) -> str:
    """Many gateways serve MCP under a subpath; bare host often 404s on POST /."""
    u = (url or "").strip()
    if not u:
        return u
    pr = urlparse(u)
    path = pr.path or ""
    if path not in ("", "/"):
        return u
    suffix = (os.environ.get("IRC_MCP_HTTP_PATH") or "/mcp").strip()
    if suffix.lower() in ("", "-", "0", "false", "none"):
        return u
    if not suffix.startswith("/"):
        suffix = "/" + suffix
    scheme = pr.scheme or "https"
    netloc = pr.netloc
    if not netloc:
        return u
    return f"{scheme}://{netloc}{suffix}"


def _apply_aap_runtime_url_overrides(raw: dict[str, Any]) -> None:
    """If AAP_MCP_SERVER_URL (or IRC_AGENT_AAP_MCP_SERVER_URL) is set in the pod env, use it for all HTTP servers that use the AAP bearer token."""
    override = (
        (os.environ.get("AAP_MCP_SERVER_URL") or "").strip()
        or (os.environ.get("IRC_AGENT_AAP_MCP_SERVER_URL") or "").strip()
    )
    if not override:
        return
    override = _normalize_mcp_http_url(override)
    servers = raw.get("mcpServers") or raw.get("mcp_servers")
    if not isinstance(servers, dict):
        return
    n = 0
    for sid, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        if (cfg.get("type") or "").strip().lower() == "stdio":
            continue
        if not (cfg.get("url") or (cfg.get("type") or "").strip().lower() == "http"):
            continue
        auth = cfg.get("auth")
        if not isinstance(auth, dict):
            continue
        if str(auth.get("bearerTokenEnv") or "") != "AAP_MCP_SERVER_TOKEN":
            continue
        cfg["url"] = override
        n += 1
    if n:
        logger.info("MCP: applied AAP HTTP base URL from environment to %d server(s)", n)


def _opt(name: str) -> str | None:
    v = (os.environ.get(name) or "").strip()
    return v or None


def _truthy(name: str) -> bool:
    return (os.environ.get(name) or "").lower() in ("1", "true", "yes", "on")


def _mcp_http_transport() -> str:
    v = (os.environ.get("IRC_MCP_HTTP_TRANSPORT") or "streamable").strip().lower()
    return v if v in ("streamable", "sse") else "streamable"


def _openai_tool_name(s: str) -> str:
    x = re.sub(r"[^a-zA-Z0-9_-]", "_", (s or "").strip())
    x = re.sub(r"_+", "_", x).strip("_") or "tool"
    return x[:64]


def _call_tool_result_to_text(res: mtypes.CallToolResult, max_chars: int) -> str:
    parts: list[str] = []
    if res.structuredContent:
        try:
            parts.append(json.dumps(res.structuredContent, default=str))
        except TypeError:
            parts.append(str(res.structuredContent))
    for block in res.content:
        if isinstance(block, mtypes.TextContent):
            parts.append(block.text)
        else:
            parts.append(block.model_dump_json(exclude_none=True))
    out = "\n".join(parts).strip()
    if res.isError:
        out = "TOOL_ERROR: " + out
    if len(out) > max_chars:
        out = out[: max_chars - 20] + "\n…(truncated)"
    return out


async def _list_tools_all(session: ClientSession) -> list[mtypes.Tool]:
    out: list[mtypes.Tool] = []
    cursor: str | None = None
    while True:
        if cursor:
            r = await session.list_tools(params=mtypes.PaginatedRequestParams(cursor=cursor))
        else:
            r = await session.list_tools()
        out.extend(r.tools)
        cursor = r.nextCursor
        if not cursor:
            break
    return out


def _headers_for_server(cfg: dict[str, Any]) -> dict[str, str]:
    h: dict[str, str] = {}
    raw = cfg.get("headers")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and isinstance(v, str):
                h[k] = v
    auth = cfg.get("auth")
    if isinstance(auth, dict) and auth.get("bearerTokenEnv"):
        envk = str(auth["bearerTokenEnv"])
        tok = (os.environ.get(envk) or "").strip()
        if not tok and envk == "AAP_MCP_SERVER_TOKEN":
            tok = _aap_bearer_from_env()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
    return h


def _is_stdio_server(cfg: dict[str, Any]) -> bool:
    typ = (cfg.get("type") or "").strip().lower()
    if typ == "stdio":
        return True
    return bool(cfg.get("command")) and typ != "http"


@dataclass
class RegisteredTool:
    openai_name: str
    server_id: str
    mcp_name: str
    description: str
    parameters: dict[str, Any]
    transport: str  # "http" | "stdio"
    url: str | None = None
    stdio: StdioServerParameters | None = None


@dataclass
class IrcAgentMcpOptions:
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    max_tool_roundtrips: int = 6
    max_tool_result_chars: int = 8000
    max_catalog_chars: int = 6000


def _parse_irc_agent_opts(raw: dict[str, Any]) -> IrcAgentMcpOptions:
    block = raw.get("ircAgent") or raw.get("irc_agent") or {}
    if not isinstance(block, dict):
        block = {}
    allowed = block.get("allowedTools") or block.get("allowed_tools") or []
    if not isinstance(allowed, list):
        allowed = []
    allowed_set = frozenset(str(x).strip() for x in allowed if str(x).strip())
    return IrcAgentMcpOptions(
        allowed_tools=allowed_set,
        max_tool_roundtrips=int(block.get("maxToolRoundtrips") or block.get("max_tool_roundtrips") or 6),
        max_tool_result_chars=int(block.get("maxToolResultChars") or block.get("max_tool_result_chars") or 8000),
        max_catalog_chars=int(block.get("maxCatalogChars") or block.get("max_catalog_chars") or 6000),
    )


class McpHub:
    """Single-process MCP: HTTP sessions pooled by URL; stdio per call_tool."""

    def __init__(self, raw: dict[str, Any], opts: IrcAgentMcpOptions):
        self._raw = raw
        self.opts = opts
        self.server_meta: list[tuple[str, str]] = []  # (server_id, description)
        self.tools: list[RegisteredTool] = []
        self._by_openai: dict[str, RegisteredTool] = {}
        self._http_urls: dict[str, dict[str, str]] = {}
        self._stdio_servers: dict[str, StdioServerParameters] = {}
        self._scan_servers()

    @classmethod
    def load_file(cls, path: str) -> McpHub | None:
        p = Path(path)
        if not p.is_file():
            logger.warning("MCP config path missing: %s", path)
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Could not load MCP config %s: %s", path, e)
            return None
        if not isinstance(raw, dict):
            return None
        _apply_aap_runtime_url_overrides(raw)
        opts = _parse_irc_agent_opts(raw)
        return cls(raw, opts)

    def _scan_servers(self) -> None:
        servers = self._raw.get("mcpServers") or self._raw.get("mcp_servers") or {}
        if not isinstance(servers, dict):
            return
        for sid, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            desc = str(cfg.get("description") or "").strip()
            self.server_meta.append((str(sid), desc))
            typ = (cfg.get("type") or "").strip().lower()
            if typ == "http" or cfg.get("url"):
                url = _normalize_mcp_http_url(str(cfg.get("url") or "").strip())
                if url:
                    self._http_urls[url] = _headers_for_server(cfg)
            elif _is_stdio_server(cfg):
                cmd = cfg.get("command")
                if not cmd:
                    continue
                args = cfg.get("args") if isinstance(cfg.get("args"), list) else []
                env = cfg.get("env") if isinstance(cfg.get("env"), dict) else None
                self._stdio_servers[str(sid)] = StdioServerParameters(
                    command=str(cmd),
                    args=[str(a) for a in args],
                    env={str(k): str(v) for k, v in env.items()} if env else None,
                )

    @property
    def active(self) -> bool:
        return bool(self.tools)

    def server_catalog_text(self) -> str:
        lines = ["Registered MCP servers (use tools only when relevant):"]
        for sid, desc in self.server_meta:
            if desc:
                lines.append(f"- **{sid}**: {desc}")
            else:
                lines.append(f"- **{sid}**")
        return "\n".join(lines)

    def catalog_text(self) -> str:
        lines = [self.server_catalog_text(), "", "Exposed tools (name — summary):"]
        for t in self.tools:
            d = (t.description or "").replace("\n", " ").strip()
            if len(d) > 160:
                d = d[:157] + "…"
            lines.append(f"- `{t.openai_name}` — {d}")
        s = "\n".join(lines)
        cap = self.opts.max_catalog_chars
        if len(s) > cap:
            s = s[: cap - 40] + "\n…(catalog truncated; full tool list in tool phase)"
        return s

    def openai_tool_schemas(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in self.tools:
            params = t.parameters if isinstance(t.parameters, dict) else {}
            if not params:
                params = {"type": "object", "properties": {}}
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.openai_name,
                        "description": (t.description or "")[:4096],
                        "parameters": params,
                    },
                }
            )
        return out

    def _allow_tool(self, openai_name: str, mcp_name: str) -> bool:
        if not self.opts.allowed_tools:
            return True
        return openai_name in self.opts.allowed_tools or mcp_name in self.opts.allowed_tools

    async def refresh_tools(self) -> None:
        self.tools = []
        self._by_openai = {}
        servers = self._raw.get("mcpServers") or self._raw.get("mcp_servers") or {}
        if not isinstance(servers, dict):
            return

        used_openai: set[str] = set()

        def alloc_name(base: str) -> str:
            name = _openai_tool_name(base)
            n = 2
            while name in used_openai:
                suffix = f"_{n}"
                root = _openai_tool_name(base)
                name = (root[: max(1, 64 - len(suffix))] + suffix)[:64]
                n += 1
            used_openai.add(name)
            return name

        def append_tool(
            server_id: str,
            transport: str,
            url: str | None,
            stdio: StdioServerParameters | None,
            tool: mtypes.Tool,
        ) -> None:
            oname = alloc_name(f"{server_id}__{tool.name}")
            if not self._allow_tool(oname, tool.name):
                return
            desc = (tool.description or "").strip() or tool.name
            schema = tool.inputSchema
            params: dict[str, Any] = schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
            self.tools.append(
                RegisteredTool(
                    openai_name=oname,
                    server_id=server_id,
                    mcp_name=tool.name,
                    description=desc,
                    parameters=params,
                    transport=transport,
                    url=url,
                    stdio=stdio,
                )
            )

        # HTTP: group by (url, headers fingerprint)
        groups: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for sid, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            url = _normalize_mcp_http_url(str(cfg.get("url") or "").strip())
            if not url:
                continue
            typ = (cfg.get("type") or "").strip().lower()
            if typ == "stdio":
                continue
            hdr = _headers_for_server(cfg)
            key = url + "\0" + json.dumps(hdr, sort_keys=True)
            groups[key].append((str(sid), cfg))

        for _key, members in groups.items():
            members = sorted(members, key=lambda x: x[0])
            url = _normalize_mcp_http_url(str(members[0][1].get("url") or "").strip())
            hdr = _headers_for_server(members[0][1])
            primary_sid = members[0][0]
            try:
                timeout = httpx.Timeout(60.0, read=120.0)
                transport = _mcp_http_transport()
                if transport == "sse":
                    async with sse_client(url, headers=hdr, timeout=60.0, sse_read_timeout=120.0) as (
                        read,
                        write,
                    ):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            mcp_tools = await _list_tools_all(session)
                            for tool in mcp_tools:
                                append_tool(primary_sid, "http", url, None, tool)
                else:
                    async with httpx.AsyncClient(headers=hdr, timeout=timeout) as client:
                        async with streamable_http_client(url, http_client=client) as streams:
                            read, write, _ = streams
                            async with ClientSession(read, write) as session:
                                await session.initialize()
                                mcp_tools = await _list_tools_all(session)
                                for tool in mcp_tools:
                                    append_tool(primary_sid, "http", url, None, tool)
            except Exception as e:  # noqa: BLE001
                tok_ok = bool(_aap_bearer_from_env())
                hint = "" if tok_ok else " (no bearer token in AAP_MCP_SERVER_TOKEN / IRC_AGENT_AAP_MCP_SERVER_TOKEN)"
                logger.warning("MCP HTTP list_tools failed for %s%s: %s", url, hint, e)

        for sid, stdio in sorted(self._stdio_servers.items()):
            try:
                async with stdio_client(stdio) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        for tool in await _list_tools_all(session):
                            append_tool(sid, "stdio", None, stdio, tool)
            except Exception as e:  # noqa: BLE001
                logger.warning("MCP stdio list_tools failed for %s: %s", sid, e)

        self._by_openai = {t.openai_name: t for t in self.tools}

    async def call_tool(
        self,
        openai_name: str,
        arguments: dict[str, Any] | None,
        sessions_by_url: dict[str, ClientSession],
    ) -> str:
        spec = self._by_openai.get(openai_name)
        if not spec:
            return f"TOOL_ERROR: unknown tool {openai_name!r}"
        args = arguments or {}
        maxc = self.opts.max_tool_result_chars
        try:
            if spec.transport == "http" and spec.url:
                session = sessions_by_url.get(spec.url)
                if not session:
                    return "TOOL_ERROR: no active MCP session for URL"
                res = await session.call_tool(spec.mcp_name, args)
                return _call_tool_result_to_text(res, maxc)
            if spec.transport == "stdio" and spec.stdio:
                async with stdio_client(spec.stdio) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        res = await session.call_tool(spec.mcp_name, args)
                        return _call_tool_result_to_text(res, maxc)
        except Exception as e:  # noqa: BLE001
            logger.exception("MCP call_tool %s", openai_name)
            return f"TOOL_ERROR: {e!s}"[:maxc]
        return "TOOL_ERROR: misconfigured tool transport"


async def open_http_sessions(hub: McpHub, stack: AsyncExitStack) -> dict[str, ClientSession]:
    sessions: dict[str, ClientSession] = {}
    timeout = httpx.Timeout(60.0, read=300.0)
    mode = _mcp_http_transport()
    for url, hdr in hub._http_urls.items():
        try:
            if mode == "sse":
                streams = await stack.enter_async_context(
                    sse_client(url, headers=hdr, timeout=60.0, sse_read_timeout=300.0)
                )
                read, write = streams
            else:
                client = httpx.AsyncClient(headers=hdr, timeout=timeout)
                await stack.enter_async_context(client)
                transport = await stack.enter_async_context(streamable_http_client(url, http_client=client))
                read, write, _ = transport
            sess = await stack.enter_async_context(ClientSession(read, write))
            await sess.initialize()
            sessions[url] = sess
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP session open failed for %s: %s", url, e)
    return sessions


def default_config_path() -> str | None:
    return _opt("MCP_CONFIG_PATH")


async def create_hub_from_env() -> McpHub | None:
    if not _truthy("IRC_AGENT_MCP_ENABLED"):
        return None
    path = default_config_path()
    if not path:
        logger.info("IRC_AGENT_MCP_ENABLED but MCP_CONFIG_PATH unset; skipping MCP.")
        return None
    hub = McpHub.load_file(path)
    if not hub:
        return None
    await hub.refresh_tools()
    if not hub.tools:
        logger.info("MCP enabled but no tools discovered (check servers, URL, and AAP_MCP_SERVER_TOKEN).")
    return hub
