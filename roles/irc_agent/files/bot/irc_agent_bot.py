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
from dataclasses import dataclass
from typing import Any


import httpx


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


def _parse_privmsg(line: str) -> tuple[str, str, str] | None:
    s = line.rstrip("\r\n")
    m = _RE_PRIVMSG.match(s)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def _wants_reply(msg: str, my_nick: str) -> bool:
    t = (msg or "").strip()
    if t.lower().startswith("!a "):
        return True
    m = t.lower()
    n = (my_nick or "").lower()
    return bool(n) and n in m


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


async def chat_completions(user_text: str) -> str:
    base = _env("LLM_BASE_URL").rstrip("/")
    url = f"{base}/chat/completions"
    key = _env("LLM_API_KEY")
    model = _env("LLM_MODEL", "gpt-4o-mini")
    system = _opt("LLM_SYSTEM") or "You are a concise helpful assistant on IRC. Keep answers short."
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
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


async def irc_run_loop(cfg: Cfg) -> None:
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
            if not _wants_reply(text, cfg.nick):
                continue
            user_line = (text or "").strip()
            if user_line.lower().startswith("!a "):
                user_line = user_line[3:].lstrip() or "hello"
            try:
                out = await chat_completions(user_line)
            except Exception as e:  # noqa: BLE001
                out = f"llm error: {e!s}"[:200]
            for part in _split_irc(out):
                if part:
                    _send(w, f"PRIVMSG {target} :{part}\r\n")
            await w.drain()


async def _main_run(cfg: Cfg) -> None:
    delay = 5.0
    while True:
        try:
            await irc_run_loop(cfg)
        except (ConnectionError, OSError, ssl.SSLError, TimeoutError) as e:
            logging.error("irc session ended: %s, reconnecting in %ss", e, delay)
            await asyncio.sleep(delay)
        except Exception as e:  # noqa: BLE001
            logging.exception("fatal irc: %s", e)
            await asyncio.sleep(delay)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = _load_cfg()
    try:
        asyncio.run(_main_run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
