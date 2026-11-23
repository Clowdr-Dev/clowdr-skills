"""Stand-in for the codex CLI: records its arguments; behavior set by STUB_MODE."""
import json, os, sys, time
from pathlib import Path

mode = os.environ.get("STUB_MODE", "ok")
with Path(os.environ["STUB_LOG"]).open("a", encoding="utf-8") as f:
    f.write(json.dumps({"mode": mode, "argv": sys.argv[1:]}, ensure_ascii=False) + "\n")
if mode == "fail":
    print("error: thread not found", file=sys.stderr)
    sys.exit(1)
if mode == "hang":
    time.sleep(30)
print("Warning: message was queued, but could not resume thread", file=sys.stderr)
print("Queued message 0000 for thread x.")
