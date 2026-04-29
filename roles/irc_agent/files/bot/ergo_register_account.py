# SPDX-License-Identifier: GPL-2.0-or-later
"""Idempotent: connect to Ergo, NickServ REGISTER, QUIT. Matches allow-before-connect + public registration in ircd."""
import os
import re
import socket
import sys


def main() -> int:
    host = os.environ.get("IRC_HOST", "127.0.0.1")
    port = int(os.environ.get("IRC_PORT", "6667"))
    nick = os.environ.get("IRC_NICK", "irc-agent")
    password = (os.environ.get("IRC_PASSWORD") or "").strip()
    if not password:
        print("IRC_PASSWORD is required", file=sys.stderr)
        return 1

    s = socket.create_connection((host, port), timeout=60)
    s.settimeout(30)
    buf = b""

    def read_some() -> None:
        nonlocal buf
        try:
            b = s.recv(4096)
            if b:
                buf += b
        except OSError as e:
            print(f"recv: {e}", file=sys.stderr)

    def get_lines() -> list[str]:
        nonlocal buf
        out = []
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line.endswith(b"\r"):
                line = line[:-1]
            try:
                out.append(line.decode("utf-8", "replace"))
            except Exception:
                out.append(str(line))
        return out

    def send_line(line: str) -> None:
        s.sendall((line + "\r\n").encode("utf-8", "replace"))

    send_line("NICK " + nick)
    send_line(f"USER {nick} 0 * :irc_agent register")
    saw_001 = False
    for _ in range(400):
        read_some()
        for line in get_lines():
            up = line.upper()
            if up.startswith("PING"):
                p = line.split(":", 1)
                arg = p[1] if len(p) > 1 else line[5:].strip()
                send_line("PONG :" + arg.lstrip(" "))
                continue
            if " 001 " in up or re.search(r"^:\S+ 001 ", line):
                saw_001 = True
                break
        if saw_001:
            break
    if not saw_001:
        read_some()
        for line in get_lines():
            if " 001 " in line or re.search(r"^:\S+ 001 ", line):
                saw_001 = True
                break

    send_line(f"PRIVMSG NickServ :REGISTER {password}")
    ok = False
    for _ in range(200):
        read_some()
        for line in get_lines():
            low = line.lower()
            if "successfully registered" in low or "now registered" in low:
                ok = True
            if "account is already" in low or "already registered" in low:
                ok = True
            if " 433 " in line:
                ok = True
        if ok:
            break
    send_line("QUIT :irc_agent seed done")
    for _ in range(30):
        read_some()
        get_lines()
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    s.close()
    if ok or saw_001:
        print("ergo_register_account finished (REGISTER idempotent or welcome seen)")
    else:
        print("ergo_register_account: NickServ may have failed; check ircd logs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as ex:
        print(f"error: {ex}", file=sys.stderr)
        raise SystemExit(1) from ex
