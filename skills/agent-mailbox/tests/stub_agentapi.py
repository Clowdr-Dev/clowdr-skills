"""Stand-in for Antigravity's agentapi: records args and whether connection env was passed."""
import json, os, sys
from pathlib import Path

mode = os.environ.get("STUB_AG_MODE", "ok")
rec = {"mode": mode, "argv": sys.argv[1:],
       "has_addr": bool(os.environ.get("ANTIGRAVITY_LS_ADDRESS")),
       "token_ok": os.environ.get("ANTIGRAVITY_CSRF_TOKEN") == os.environ.get("STUB_EXPECT_TOKEN")}
with Path(os.environ["STUB_AG_LOG"]).open("a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
if mode == "error":
    print(json.dumps({"response": {}, "error": "rpc error: code = Unauthenticated desc = missing CSRF token"}))
    sys.exit(1)
cmd = sys.argv[1]
if cmd == "send-message":
    print(json.dumps({"response": {"sendMessage": {"recipientId": sys.argv[2], "content": sys.argv[3]}}}))
elif cmd == "new-conversation":
    print(json.dumps({"response": {"newConversation": {"prompt": sys.argv[-1], "conversationId": "conv-stub-1"}}}))
