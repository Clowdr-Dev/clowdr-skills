#!/usr/bin/env python3
"""Start a Cursor agent CLI worker (for example with Grok) that joins the agent mailbox.

Writes a start request to cursor-chats/requests/. The mailbox dispatcher picks
it up within seconds: it creates a Cursor chat, records the chat's model and
folder, and runs the first turn. In that turn the worker registers itself
(cursor:<chat-id>, delivery mode cursor-resume) and sends the orchestrator
"started <token> cursor:<chat-id> cwd=<folder>". After that, every message to
the worker runs one headless turn in its chat.

The dispatcher does the Cursor calls because it runs outside any agent
sandbox, where the logged-in Cursor CLI is. If the start fails, the dispatcher
sends the orchestrator a message from dispatcher:local.

The worker folder must contain .cursor/cli.json with an allow rule for the
mailbox CLI, or the worker cannot reply. The default folder cursor-worker/
is created on first use and allows only the mailbox CLI. See ORCHESTRATOR.md.

    python start-cursor-worker.py --orchestrator claude:<session-id> [--model grok-4.7-low]
        [--workspace <folder>] [--label reviewer] [--dry-run]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("agent_mailbox_cursor", HERE / "cursor.py")
cur = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cur)

ADDRESS = re.compile(r"^[a-z0-9_-]+:[A-Za-z0-9_.-]+$")
MODEL = re.compile(r"^[a-z0-9_.-]+$")
LABEL = re.compile(r"^[A-Za-z0-9 _.,:/+-]{1,60}$")
REQUESTS = HERE / "cursor-chats" / "requests"
RULE = cur.mailbox_rule()
DEFAULT_WORKSPACE = HERE / "cursor-worker"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--orchestrator", required=True, help="the orchestrator's mailbox address, e.g. claude:<id>")
    p.add_argument("--model", default="grok-4.7-low",
                   help="Cursor model ID, see `agent models` (default grok-4.7-low)")
    p.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                   help="folder the worker runs in (default: cursor-worker, which allows only the mailbox CLI)")
    p.add_argument("--label", default="orchestrated worker", help="role label for the mailbox registration")
    p.add_argument("--dry-run", action="store_true", help="print the request without writing it")
    args = p.parse_args()

    if not ADDRESS.match(args.orchestrator):
        sys.exit("start-cursor-worker: --orchestrator must be a mailbox address like claude:<session-id>")
    if not MODEL.match(args.model):
        sys.exit("start-cursor-worker: --model may only contain a-z, 0-9, '.', '_' and '-'")
    if not LABEL.match(args.label):
        sys.exit("start-cursor-worker: --label may use letters, digits, spaces and _ . , : / + - (max 60)")
    if args.workspace == DEFAULT_WORKSPACE:
        cur.ensure_worker_folder(DEFAULT_WORKSPACE)
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        sys.exit(f"start-cursor-worker: --workspace {workspace} is not a folder")
    if not cur.workspace_allows_mailbox(workspace):
        sys.exit(f"start-cursor-worker: {workspace / '.cursor' / 'cli.json'} has no allow rule for the mailbox "
                 f"CLI, so the worker could not reply. Add {RULE} to permissions.allow there (with the user's OK), "
                 f"or use the default folder {HERE / 'cursor-worker'}.")

    token = "orch-" + uuid.uuid4().hex[:10]
    request = {"token": token, "orchestrator": args.orchestrator, "model": args.model,
               "workspace": str(workspace), "label": args.label}
    if args.dry_run:
        print(json.dumps({**request, "dry_run": True}, ensure_ascii=False))
        return
    REQUESTS.mkdir(parents=True, exist_ok=True)
    tmp = REQUESTS / f"{token}.tmp"
    tmp.write_text(json.dumps(request, indent=2), encoding="utf-8")
    tmp.replace(REQUESTS / f"{token}.json")
    print(json.dumps({**request, "request": str(REQUESTS / f"{token}.json"),
                      "next": f"wait for 'started {token} cursor:<chat-id>' in your inbox"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
