#!/usr/bin/env python3
"""Read-only web viewer for the agent mailbox.

Shows the registered sessions and the messages between them, grouped into
threads, and refreshes every few seconds. It opens the database read-only and
listens on 127.0.0.1 only, so it can't change the mailbox or be reached from
other machines.

    python viewer.py [--port 8765] [--no-browser]

Database location: $AGENT_MAILBOX_DB, or mailbox.db next to this script.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
PAGE = HERE / "viewer.html"
MESSAGE_LIMIT = 2000


def db_path() -> Path:
    return Path(os.environ.get("AGENT_MAILBOX_DB") or HERE / "mailbox.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"{db_path().as_uri()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def state() -> dict:
    if not db_path().exists():
        return {"agents": [], "messages": [], "total": 0, "error": f"no database at {db_path()}"}
    conn = connect()
    try:
        agents = [dict(row) for row in conn.execute("SELECT * FROM agents ORDER BY last_seen DESC")]
        wanted = ["id", "thread_id", "reply_to", "sender", "recipient", "body", "created_at", "read_at",
                  "delivered_at", "delivery_attempts", "delivery_error"]
        present = columns(conn, "messages")
        select = ", ".join(c if c in present else f"NULL AS {c}" for c in wanted)
        rows = conn.execute(f"SELECT {select} FROM messages ORDER BY id DESC LIMIT ?", (MESSAGE_LIMIT,))
        messages = [dict(row) for row in rows][::-1]
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        counts = {}
        for row in conn.execute(
            "SELECT address, "
            " (SELECT COUNT(*) FROM messages WHERE sender = address) AS sent,"
            " (SELECT COUNT(*) FROM messages WHERE recipient = address) AS received,"
            " (SELECT COUNT(*) FROM messages WHERE recipient = address AND read_at IS NULL) AS unread "
            "FROM agents"
        ):
            counts[row["address"]] = {"sent": row["sent"], "received": row["received"], "unread": row["unread"]}
        for agent in agents:
            agent.update(counts.get(agent["address"], {}))
        return {"agents": agents, "messages": messages, "total": total, "db": str(db_path())}
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    allowed_hosts: set[str] = set()

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        # Only answer requests addressed to this machine by name, so a web page
        # on another site can't read the mailbox through DNS rebinding.
        if self.headers.get("Host", "") not in self.allowed_hosts:
            self.send_error(403, "unexpected Host header")
            return
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self.reply(200, "text/html; charset=utf-8", PAGE.read_bytes())
        elif path == "/api/state":
            try:
                body, code = json.dumps(state(), ensure_ascii=False), 200
            except sqlite3.Error as exc:
                body, code = json.dumps({"error": f"database error: {exc}"}), 500
            self.reply(code, "application/json; charset=utf-8", body.encode("utf-8"))
        else:
            self.send_error(404)

    def reply(self, code: int, kind: str, data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only web viewer for the agent mailbox.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    args = parser.parse_args()

    Handler.allowed_hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Agent mailbox viewer on {url} (database {db_path()}). Ctrl+C stops it.", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
