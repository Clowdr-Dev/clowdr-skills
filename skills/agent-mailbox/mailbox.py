#!/usr/bin/env python3
"""Agent mailbox: a shared SQLite inbox so agent sessions from different tools
(Claude Code, Codex, Gemini, Grok, ...) can message each other.

Every session has an address of the form <tool>:<session_id>, for example
claude:2c9e7f14-5b0a-4d8e-9f61-3a7d2e8b4c05. Messages go from one address to
another. Every command prints JSON, one object per line, so any agent can
parse the output.

Database location: $AGENT_MAILBOX_DB, or mailbox.db next to this script.
Default identity for --as / --from: $AGENT_MAILBOX_ADDRESS.

Run `python mailbox.py <command> --help` for details on each command.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "mailbox.db"
HEARTBEAT_SECONDS = 30
MAX_CONSECUTIVE_ERRORS = 10

# Environment variables that may hold the current session's ID, per tool.
SESSION_ENV_VARS = {
    "claude": ["CLAUDE_CODE_SESSION_ID"],
    "codex": ["CODEX_SESSION_ID", "CODEX_THREAD_ID"],
    "gemini": ["GEMINI_SESSION_ID"],
    "grok": ["GROK_SESSION_ID"],
    "antigravity": ["ANTIGRAVITY_CONVERSATION_ID"],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    address       TEXT PRIMARY KEY,
    tool          TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    label         TEXT,
    registered_at TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  TEXT NOT NULL,
    reply_to   INTEGER REFERENCES messages(id),
    sender     TEXT NOT NULL,
    recipient  TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_inbox  ON messages(recipient, read_at, id);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, id);
"""

# Columns added after the first release; connect() adds any that are missing.
MIGRATIONS = {
    "agents": {
        # How messages reach this session: it watches itself ("self"), the
        # dispatcher pushes them with `codex queue` ("codex-queue") or with
        # Antigravity's `agentapi send-message` ("antigravity-send"), runs
        # one headless Cursor agent turn per message ("cursor-resume"), or the
        # session checks its inbox when asked ("pull").
        "delivery": "TEXT NOT NULL DEFAULT 'pull'",
    },
    "messages": {
        "delivered_at": "TEXT",
        "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at": "TEXT",
        "delivery_error": "TEXT",
    },
}

DELIVERY_MODES = ("self", "codex-queue", "antigravity-send", "cursor-resume", "pull")
PUSH_MODES = ("codex-queue", "antigravity-send", "cursor-resume")  # modes the dispatcher delivers


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def warn(text: str) -> None:
    sys.stderr.write(f"mailbox: {text}\n")
    sys.stderr.flush()


def fail(text: str, code: int = 1) -> None:
    warn(text)
    sys.exit(code)


def db_path() -> Path:
    return Path(os.environ.get("AGENT_MAILBOX_DB") or DEFAULT_DB)


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name in present:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            except sqlite3.OperationalError as exc:  # another process added it first
                if "duplicate column" not in str(exc):
                    raise


def message_json(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "reply_to": row["reply_to"],
        "from": row["sender"],
        "to": row["recipient"],
        "created_at": row["created_at"],
        "read_at": row["read_at"],
        "delivered_at": row["delivered_at"],
        "delivery_error": row["delivery_error"],
        "body": row["body"],
    }


def identity(value: str | None, flag: str) -> str:
    address = value or os.environ.get("AGENT_MAILBOX_ADDRESS")
    if not address:
        fail(f"no address given: pass {flag} or set AGENT_MAILBOX_ADDRESS")
    return address


def is_registered(conn: sqlite3.Connection, address: str) -> bool:
    return conn.execute("SELECT 1 FROM agents WHERE address = ?", (address,)).fetchone() is not None


def touch(conn: sqlite3.Connection, address: str) -> None:
    conn.execute("UPDATE agents SET last_seen = ? WHERE address = ?", (now(), address))


# ---------------------------------------------------------------- commands


def cmd_register(args: argparse.Namespace) -> None:
    tool = args.tool.strip().lower()
    if not tool or ":" in tool:
        fail("--tool must be a non-empty name without ':'")
    session = args.session
    source = "argument"
    if not session:
        for var in SESSION_ENV_VARS.get(tool, []):
            if os.environ.get(var):
                session, source = os.environ[var], f"env:{var}"
                break
    if not session:
        session, source = uuid.uuid4().hex, "generated"
    address = f"{tool}:{session}"
    # Default for a new registration: Claude sessions watch themselves; a Codex
    # session with a real session ID can be woken by the dispatcher; everything
    # else checks its inbox. An existing registration keeps its mode unless
    # --delivery is given.
    if tool == "claude":
        default_delivery = "self"
    elif tool == "codex" and source != "generated":
        default_delivery = "codex-queue"
    elif tool == "antigravity" and source != "generated":
        default_delivery = "antigravity-send"
    else:
        default_delivery = "pull"
    conn = connect()
    stamp = now()
    conn.execute(
        """INSERT INTO agents (address, tool, session_id, label, registered_at, last_seen, delivery)
           VALUES (:address, :tool, :session, :label, :stamp, :stamp, COALESCE(:explicit, :default))
           ON CONFLICT(address) DO UPDATE SET
               label = COALESCE(excluded.label, agents.label),
               last_seen = excluded.last_seen,
               delivery = COALESCE(:explicit, agents.delivery)""",
        {"address": address, "tool": tool, "session": session, "label": args.label,
         "stamp": stamp, "explicit": args.delivery, "default": default_delivery},
    )
    row = conn.execute("SELECT * FROM agents WHERE address = ?", (address,)).fetchone()
    emit({"address": address, "tool": tool, "session_id": session,
          "session_id_source": source, "label": row["label"], "delivery": row["delivery"]})


def cmd_set_delivery(args: argparse.Namespace) -> None:
    conn = connect()
    cur = conn.execute("UPDATE agents SET delivery = ? WHERE address = ?", (args.mode, args.address))
    if not cur.rowcount:
        fail(f"{args.address} is not registered")
    emit({"address": args.address, "delivery": args.mode})


def cmd_show(args: argparse.Namespace) -> None:
    conn = connect()
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        fail(f"no message {args.id}")
    emit(message_json(row))


def cmd_agents(args: argparse.Namespace) -> None:
    conn = connect()
    rows = conn.execute("SELECT * FROM agents ORDER BY last_seen DESC").fetchall()
    cutoff = time.time() - args.active * 60 if args.active else None
    for row in rows:
        seen = datetime.fromisoformat(row["last_seen"]).timestamp()
        if cutoff is not None and seen < cutoff:
            continue
        unread = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE recipient = ? AND read_at IS NULL",
            (row["address"],),
        ).fetchone()[0]
        emit({"address": row["address"], "tool": row["tool"], "label": row["label"],
              "delivery": row["delivery"], "last_seen": row["last_seen"], "unread": unread})


def read_body(args: argparse.Namespace) -> str:
    if args.body is not None:
        body = args.body
    elif args.body_file:
        # utf-8-sig drops the byte-order mark Windows PowerShell 5.1 writes.
        body = Path(args.body_file).read_text(encoding="utf-8-sig")
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        fail("no message body: pass --body, --body-file, or pipe text on stdin")
    body = body.lstrip("﻿")
    if not body.strip():
        fail("message body is empty")
    return body


def cmd_send(args: argparse.Namespace) -> None:
    sender = identity(args.sender, "--from")
    conn = connect()
    recipients = list(dict.fromkeys(args.to or []))
    thread = args.thread
    if args.reply_to is not None:
        parent = conn.execute("SELECT * FROM messages WHERE id = ?", (args.reply_to,)).fetchone()
        if parent is None:
            fail(f"--reply-to {args.reply_to}: no such message")
        thread = thread or parent["thread_id"]
        if not recipients:
            recipients = [parent["sender"]]
    if not recipients:
        fail("no recipient: pass --to (repeatable) or --reply-to")
    body = read_body(args)
    thread = thread or uuid.uuid4().hex[:12]
    if not is_registered(conn, sender):
        warn(f"sender {sender} is not registered (run `register` first)")
    for r in recipients:
        if not is_registered(conn, r):
            warn(f"recipient {r} is not registered; the message waits until it checks")
    stamp = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        ids = []
        for r in recipients:
            cur = conn.execute(
                """INSERT INTO messages (thread_id, reply_to, sender, recipient, body, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (thread, args.reply_to, sender, r, body, stamp),
            )
            ids.append((cur.lastrowid, r))
        touch(conn, sender)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    for msg_id, r in ids:
        emit({"sent": msg_id, "thread_id": thread, "to": r})


def cmd_inbox(args: argparse.Namespace) -> None:
    me = identity(args.me, "--as")
    conn = connect()
    sql = "SELECT * FROM messages WHERE recipient = ?"
    if not args.all:
        sql += " AND read_at IS NULL"
    sql += " ORDER BY id"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    for row in conn.execute(sql, (me,)):
        emit(message_json(row))
    touch(conn, me)


def cmd_ack(args: argparse.Namespace) -> None:
    me = identity(args.me, "--as")
    conn = connect()
    stamp = now()
    if args.all:
        cur = conn.execute(
            "UPDATE messages SET read_at = ? WHERE recipient = ? AND read_at IS NULL", (stamp, me))
        emit({"acked": cur.rowcount})
    else:
        if not args.ids:
            fail("pass message ids or --all")
        acked, skipped = [], []
        for msg_id in args.ids:
            cur = conn.execute(
                "UPDATE messages SET read_at = COALESCE(read_at, ?) WHERE id = ? AND recipient = ?",
                (stamp, msg_id, me))
            (acked if cur.rowcount else skipped).append(msg_id)
        emit({"acked": acked, "not_yours_or_missing": skipped})
    touch(conn, me)


def cmd_thread(args: argparse.Namespace) -> None:
    conn = connect()
    for row in conn.execute("SELECT * FROM messages WHERE thread_id = ? ORDER BY id", (args.thread_id,)):
        emit(message_json(row))


def cmd_watch(args: argparse.Namespace) -> None:
    """Print each unread message addressed to --as as one JSON line, as it arrives.

    Unread messages that already exist are printed first, so nothing that arrived
    while no watcher was running is lost. Messages are not marked read here; the
    agent acks them after handling. Database errors are printed as
    {"event": "error"} lines, so a broken watcher is never silent.
    """
    me = identity(args.me, "--as")
    deadline = time.monotonic() + args.timeout if args.timeout else None
    last_id = 0
    last_beat = 0.0
    errors = 0
    conn = None
    while True:
        try:
            if conn is None:
                conn = connect()
                if not is_registered(conn, me):
                    emit({"event": "warning", "warning": f"{me} is not registered"})
            rows = conn.execute(
                "SELECT * FROM messages WHERE recipient = ? AND read_at IS NULL AND id > ? ORDER BY id",
                (me, last_id),
            ).fetchall()
            for row in rows:
                emit(message_json(row))
                last_id = row["id"]
            if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                touch(conn, me)
                last_beat = time.monotonic()
            errors = 0
            if rows and args.once:
                return
        except sqlite3.Error as exc:
            errors += 1
            emit({"event": "error", "error": str(exc), "consecutive": errors})
            conn = None
            if errors >= MAX_CONSECUTIVE_ERRORS:
                emit({"event": "stopped", "reason": "too many consecutive database errors"})
                sys.exit(2)
        if deadline is not None and time.monotonic() >= deadline:
            emit({"event": "timeout", "watched": me})
            return
        time.sleep(args.interval)


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mailbox", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("register", help="register this session and print its address")
    s.add_argument("--tool", required=True, help="claude, codex, gemini, grok, orchestrator, ...")
    s.add_argument("--session", help="session ID; detected from the environment or generated if omitted")
    s.add_argument("--label", help="short human-readable role, e.g. 'reviewer'")
    s.add_argument("--delivery", choices=DELIVERY_MODES,
                   help="how messages reach you: self (you run watch), codex-queue / antigravity-send "
                        "(the dispatcher wakes your Codex session / Antigravity conversation), cursor-resume "
                        "(the dispatcher runs a Cursor agent turn in your chat; set up by "
                        "start-cursor-worker.py), pull (you run inbox). Default: self for claude, codex-queue "
                        "for codex and antigravity-send for antigravity with a known session ID, otherwise pull")
    s.set_defaults(func=cmd_register)

    s = sub.add_parser("set-delivery", help="change how messages reach a registered session")
    s.add_argument("address")
    s.add_argument("mode", choices=DELIVERY_MODES)
    s.set_defaults(func=cmd_set_delivery)

    s = sub.add_parser("show", help="print one message by ID")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("agents", help="list registered sessions with unread counts")
    s.add_argument("--active", type=float, metavar="MINUTES", help="only sessions seen in the last N minutes")
    s.set_defaults(func=cmd_agents)

    s = sub.add_parser("send", help="send a message to one or more addresses")
    s.add_argument("--from", dest="sender", help="your address (default: $AGENT_MAILBOX_ADDRESS)")
    s.add_argument("--to", action="append", help="recipient address; repeat for fan-out")
    s.add_argument("--thread", help="thread ID to continue (default: new thread, or the replied-to thread)")
    s.add_argument("--reply-to", type=int, help="message ID being answered; defaults --to to its sender")
    s.add_argument("--body", help="message text")
    s.add_argument("--body-file", help="read message text from a UTF-8 file")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("inbox", help="print unread messages once (does not mark them read)")
    s.add_argument("--as", dest="me", help="your address (default: $AGENT_MAILBOX_ADDRESS)")
    s.add_argument("--all", action="store_true", help="include already-read messages")
    s.add_argument("--limit", type=int)
    s.set_defaults(func=cmd_inbox)

    s = sub.add_parser("ack", help="mark messages addressed to you as read")
    s.add_argument("--as", dest="me", help="your address (default: $AGENT_MAILBOX_ADDRESS)")
    s.add_argument("ids", nargs="*", type=int)
    s.add_argument("--all", action="store_true", help="mark every unread message read")
    s.set_defaults(func=cmd_ack)

    s = sub.add_parser("thread", help="print every message in a thread")
    s.add_argument("thread_id")
    s.set_defaults(func=cmd_thread)

    s = sub.add_parser("watch", help="stream incoming messages as JSON lines")
    s.add_argument("--as", dest="me", help="your address (default: $AGENT_MAILBOX_ADDRESS)")
    s.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds (default 2)")
    s.add_argument("--once", action="store_true", help="exit after the first batch of messages")
    s.add_argument("--timeout", type=float, metavar="SECONDS", help="exit after this long")
    s.set_defaults(func=cmd_watch)
    return p


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    args = build_parser().parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
