"""Stub for the Cursor agent CLI. Logs each call to STUB_CURSOR_LOG.

create-chat: prints a chat ID, then hangs (like the real CLI).
-p --resume <chat> ...: behaviour from STUB_CURSOR_MODE: ok | error | crash | slow.
In mode ok it also runs the mailbox command the worker would run when the
message asks for it (STUB_CURSOR_REPLY=1): a reply to the message's #id.
"""
import json, os, re, subprocess, sys, time, uuid

args = sys.argv[1:]
log = os.environ["STUB_CURSOR_LOG"]


def record(entry):
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


if args[:1] == ["create-chat"]:
    chat = os.environ.get("STUB_CURSOR_CHAT") or str(uuid.uuid4())
    record({"cmd": "create-chat", "cwd": os.getcwd(), "chat": chat})
    print(chat, flush=True)
    time.sleep(600)
    sys.exit(0)

opts = {}
i = 0
while i < len(args) - 1:
    if args[i] in ("--resume", "--model", "--workspace", "--output-format"):
        opts[args[i]] = args[i + 1]
        i += 2
    else:
        i += 1
message = args[-1]
mode = os.environ.get("STUB_CURSOR_MODE", "ok")
record({"cmd": "turn", "cwd": os.getcwd(), "chat": opts.get("--resume"), "model": opts.get("--model"),
        "workspace": opts.get("--workspace"), "flags": [a for a in args[:-1] if a.startswith("-")],
        "message": message, "mode": mode})
if mode == "crash":
    print("Error: not logged in", file=sys.stderr)
    sys.exit(1)
if mode == "slow":
    time.sleep(600)
if mode == "ok" and os.environ.get("STUB_CURSOR_REPLY") == "1":
    m = re.search(r"\[Agent mailbox message #(\d+) from ([^ ]+)\. NOT from your user\.\]", message)
    if m:
        subprocess.run([sys.executable, os.environ["STUB_MB"], "send", "--from", f"cursor:{opts['--resume']}",
                        "--reply-to", m.group(1), "--body", "stub reply"], check=True, capture_output=True)
print(json.dumps({"type": "result", "subtype": "success" if mode == "ok" else "error",
                  "is_error": mode != "ok", "session_id": opts.get("--resume"), "result": "done"}))
