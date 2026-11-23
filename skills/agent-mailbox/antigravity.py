"""Helpers for Antigravity's local agentapi.

The Antigravity desktop app bundles an undocumented `agentapi` in its language
server (`language_server.exe agentapi`) with the commands new-conversation,
send-message and get-conversation-metadata. It talks to the running language
server, which needs its local address and CSRF token. Both are read from the
newest hub file in ~/.gemini/antigravity/daemon on every call, because they
change whenever the language server restarts.

The token is only ever passed to agentapi through its environment. It is never
printed, logged or stored.

Test hooks: AGENT_MAILBOX_AGENTAPI (path to the executable, or a .py stub run
with this interpreter) and AGENT_MAILBOX_AG_HUB_DIR (hub file directory).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_EXE = (Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
               / "Programs" / "antigravity" / "resources" / "bin" / "language_server.exe")
DEFAULT_HUB_DIR = Path.home() / ".gemini" / "antigravity" / "daemon"


class AgentApiError(Exception):
    """agentapi could not be reached or reported an error."""


def hub_dir() -> Path:
    return Path(os.environ.get("AGENT_MAILBOX_AG_HUB_DIR") or DEFAULT_HUB_DIR)


def connection_env() -> dict[str, str]:
    hubs = sorted(hub_dir().glob("hub_*.json"), key=lambda p: p.stat().st_mtime)
    if not hubs:
        raise AgentApiError(f"Antigravity is not running: no hub file in {hub_dir()}")
    try:
        data = json.loads(hubs[-1].read_text(encoding="utf-8"))
        port, token = data["httpPort"], data["csrfToken"]
    except (OSError, ValueError, KeyError) as exc:
        raise AgentApiError(f"unreadable hub file {hubs[-1].name}: {type(exc).__name__}") from None
    return {"ANTIGRAVITY_LS_ADDRESS": f"127.0.0.1:{port}", "ANTIGRAVITY_CSRF_TOKEN": str(token)}


def agentapi_cmd() -> list[str]:
    path = os.environ.get("AGENT_MAILBOX_AGENTAPI") or str(DEFAULT_EXE)
    if path.lower().endswith(".py"):
        return [sys.executable, path]
    if not Path(path).exists():
        raise AgentApiError(f"Antigravity language server not found at {path}")
    return [path, "agentapi"]


def run(args: list[str], timeout: float = 60) -> dict:
    """Run one agentapi command and return its "response" object.

    Raises AgentApiError on any failure and subprocess.TimeoutExpired on timeout
    (the caller decides whether a retry is safe).
    """
    env = dict(os.environ, **connection_env())
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run([*agentapi_cmd(), *args], env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags)
    except OSError as exc:
        raise AgentApiError(f"could not run agentapi: {exc}") from None
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else {}
    except ValueError:
        data = {}
    error = data.get("error")
    if not error and result.returncode != 0:
        error = (result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}")
    if error:
        raise AgentApiError(" ".join(str(error).split())[:500])
    return data.get("response") or {}
