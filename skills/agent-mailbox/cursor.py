"""Helpers for the Cursor agent CLI (`agent` / `cursor-agent`).

Cursor has no command that pushes a message into a running chat. Instead each
message runs one headless turn in an existing chat:

    agent -p --resume <chat_id> --model <model> --trust --workspace <folder>
          --output-format json "<message>"

The turn runs, the CLI prints one JSON result ({"type": "result", "is_error":
..., "session_id": <chat_id>, ...}) and exits. The chat keeps its history.

Shell commands in print mode run only if a permission rule allows them. The
rules are read from <cwd>/.cursor/cli.json, so a turn must be started with the
worker folder as its working directory (--workspace alone is not enough). A
worker folder needs at least:

    {"permissions": {"allow": ["Shell(python:<mailbox>/mailbox.py **)"], "deny": []}}

where <mailbox> is this folder's absolute path with forward slashes
(mailbox_rule() returns the exact rule).

`agent create-chat` prints the new chat ID and then hangs (known Cursor bug),
so create_chat() reads the first line and stops the process.

The CLI is called as <version>/node.exe <version>/index.js, which passes the
message as a plain argument (agent.cmd would route it through cmd.exe and
PowerShell, which mangle quotes).

Test hook: AGENT_MAILBOX_CURSOR_AGENT (path to an executable, or a .py stub run
with this interpreter).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

AGENT_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "cursor-agent"
MAILBOX_PATH = (Path(__file__).resolve().parent / "mailbox.py").as_posix()
CHAT_ID = re.compile(r"^[0-9a-fA-F-]{36}$")


class CursorError(Exception):
    """The Cursor agent CLI could not be run or did not behave as expected."""


def _version_key(name: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", name.split("-")[0]))


def agent_cmd() -> list[str]:
    override = os.environ.get("AGENT_MAILBOX_CURSOR_AGENT")
    if override:
        return [sys.executable, override] if override.lower().endswith(".py") else [override]
    versions = AGENT_ROOT / "versions"
    candidates = [d for d in versions.iterdir() if (d / "node.exe").exists() and (d / "index.js").exists()] \
        if versions.is_dir() else []
    if not candidates:
        raise CursorError(f"Cursor agent CLI not found in {versions}; install it and run `agent login`")
    latest = max(candidates, key=lambda d: _version_key(d.name))
    return [str(latest / "node.exe"), str(latest / "index.js")]


def no_window() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def mailbox_rule() -> str:
    """The Cursor allow rule that lets a worker run this mailbox's CLI."""
    return f"Shell(python:{MAILBOX_PATH} **)"


def ensure_worker_folder(folder: Path) -> None:
    """Create a worker folder whose .cursor/cli.json allows only the mailbox CLI (no-op if present)."""
    cfg = folder / ".cursor" / "cli.json"
    if cfg.exists():
        return
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"permissions": {"allow": [mailbox_rule()], "deny": []}}, indent=2) + "\n",
                   encoding="utf-8")


def workspace_allows_mailbox(workspace: Path) -> bool:
    """True if <workspace>/.cursor/cli.json has an allow rule for the mailbox CLI."""
    try:
        data = json.loads((workspace / ".cursor" / "cli.json").read_text(encoding="utf-8-sig"))
        rules = data["permissions"]["allow"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return any(isinstance(r, str) and r.startswith("Shell(") and MAILBOX_PATH.lower() in r.replace("\\", "/").lower()
               for r in rules)


def create_chat(workspace: Path, timeout: float = 60) -> str:
    """Create an empty chat and return its ID."""
    try:
        proc = subprocess.Popen([*agent_cmd(), "create-chat"], cwd=str(workspace), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", creationflags=no_window())
    except OSError as exc:
        raise CursorError(f"could not run the Cursor agent CLI: {exc}") from None
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    stop(proc)
    first = lines[0].strip() if lines else ""
    if not CHAT_ID.match(first):
        err = ""
        try:
            err = proc.stderr.read()[:300] if proc.stderr else ""
        except (OSError, ValueError):
            pass
        raise CursorError(f"create-chat returned no chat ID (got {first[:100]!r}; stderr {' '.join(err.split())!r})")
    return first


def turn_cmd(chat_id: str, model: str, workspace: Path, message: str) -> list[str]:
    return [*agent_cmd(), "-p", "--resume", chat_id, "--model", model, "--trust",
            "--workspace", str(workspace), "--output-format", "json", message]


def start_turn(chat_id: str, model: str, workspace: Path, message: str, out_path: Path) -> subprocess.Popen:
    """Start one headless turn in the background; its JSON output goes to out_path."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as out:
        try:
            return subprocess.Popen(turn_cmd(chat_id, model, workspace, message), cwd=str(workspace),
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    creationflags=no_window())
        except OSError as exc:
            raise CursorError(f"could not run the Cursor agent CLI: {exc}") from None


def read_result(out_path: Path) -> dict | None:
    """The turn's JSON result object, or None if the output has none."""
    try:
        text = out_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if data.get("type") == "result":
                return data
    return None


def output_tail(out_path: Path, limit: int = 300) -> str:
    try:
        text = out_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return " ".join(text.split())[-limit:]


def stop(proc: subprocess.Popen) -> None:
    """Stop a CLI process and its children."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=no_window())
    else:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
