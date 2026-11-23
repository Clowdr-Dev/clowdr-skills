"""Checks for antigravity-send delivery and start-antigravity-worker.py, using a stub agentapi."""
import importlib.util, json, os, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
T = HERE / ".work"
T.mkdir(exist_ok=True)
DB, AG_LOG, HUB = T / "ag.db", T / "stub_ag.log", T / "hub"
for p in [*T.glob("ag.db*"), AG_LOG, T / "ag_dispatch.log"]:
    p.unlink(missing_ok=True)
HUB.mkdir(exist_ok=True)
for p in HUB.glob("*"):
    p.unlink()
TOKEN = "TESTTOKEN-7f3a9c"
(HUB / "hub_59445.json").write_text(json.dumps({"pid": 1, "httpPort": 59445, "httpsPort": 59444, "csrfToken": TOKEN}))
os.environ.update(AGENT_MAILBOX_DB=str(DB), STUB_AG_LOG=str(AG_LOG), STUB_EXPECT_TOKEN=TOKEN,
                  AGENT_MAILBOX_AGENTAPI=str(HERE / "stub_agentapi.py"), AGENT_MAILBOX_AG_HUB_DIR=str(HUB),
                  STUB_AG_MODE="ok")
REPO = HERE.parent
MB = str(REPO / "mailbox.py")

spec = importlib.util.spec_from_file_location("dispatcher", REPO / "dispatcher.py")
d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
d.BACKOFF_SECONDS = [0, 0]
failed = False


def mb(*args, env=None):
    out = subprocess.run([sys.executable, MB, *args], capture_output=True, text=True, encoding="utf-8",
                         env=env or os.environ)
    assert out.returncode == 0, out.stderr
    return [json.loads(l) for l in out.stdout.splitlines()]


def run_once():
    d.Dispatcher(None, T / "ag_dispatch.log", 0).run(once=True)


def calls():
    return [json.loads(l) for l in AG_LOG.read_text(encoding="utf-8").splitlines()] if AG_LOG.exists() else []


def check(name, cond):
    global failed
    print(("PASS " if cond else "FAIL ") + name)
    failed |= not cond


# registration defaults
r1 = mb("register", "--tool", "antigravity", "--session", "conv-a")[0]
check("antigravity with session -> antigravity-send", r1["delivery"] == "antigravity-send")
env = dict(os.environ, ANTIGRAVITY_CONVERSATION_ID="conv-from-env")
r2 = mb("register", "--tool", "antigravity", env=env)[0]
check("session ID taken from ANTIGRAVITY_CONVERSATION_ID",
      r2["address"] == "antigravity:conv-from-env" and r2["session_id_source"] == "env:ANTIGRAVITY_CONVERSATION_ID"
      and r2["delivery"] == "antigravity-send")
r3 = mb("register", "--tool", "antigravity")[0]
check("antigravity without known ID -> pull", r3["delivery"] == "pull")
mb("register", "--tool", "claude", "--session", "orch")

# delivery
mb("send", "--from", "claude:orch", "--to", "antigravity:conv-a", "--body", 'Task "quoted" äöü\nline 2')
run_once()
c = calls()
check("one agentapi call", len(c) == 1)
check("send-message to conversation ID", c[0]["argv"][:2] == ["send-message", "conv-a"])
env_text = c[0]["argv"][2]
check("envelope labels sender, not user or system",
      "from claude:orch. NOT from your user" in env_text and "not from your user or the system" in env_text)
check("body intact", 'Task "quoted" äöü\nline 2' in env_text)
check("address and token passed via environment", c[0]["has_addr"] and c[0]["token_ok"])
check("token not on the command line", all(TOKEN not in a for a in c[0]["argv"]))
log = (T / "ag_dispatch.log").read_text(encoding="utf-8")
check("token not in dispatcher log", TOKEN not in log)
check("message body not echoed into log", "line 2" not in log)
m = mb("show", "1")[0]
check("delivered_at recorded", m["delivered_at"] is not None and m["delivery_error"] is None)

# agentapi error -> retry then give up
os.environ["STUB_AG_MODE"] = "error"
mb("send", "--from", "claude:orch", "--to", "antigravity:conv-a", "--body", "will fail")
for _ in range(4):
    run_once()
check("failing agentapi tried 3 times", len([x for x in calls() if x["mode"] == "error"]) == 3)
check("agentapi error recorded", "missing CSRF token" in (mb("show", "2")[0]["delivery_error"] or ""))
check("token not in log after errors", TOKEN not in (T / "ag_dispatch.log").read_text(encoding="utf-8"))

# Antigravity not running (no hub file) -> error, no call
os.environ["STUB_AG_MODE"] = "ok"
(HUB / "hub_59445.json").rename(HUB / "gone.json")
n = len(calls())
mb("send", "--from", "claude:orch", "--to", "antigravity:conv-a", "--body", "no hub")
run_once()
check("no hub file: no call made", len(calls()) == n)
check("no hub file: error recorded", "not running" in (mb("show", "3")[0]["delivery_error"] or ""))
(HUB / "gone.json").rename(HUB / "hub_59445.json")

# codex message with no codex CLI -> error, antigravity unaffected
mb("register", "--tool", "codex", "--session", "cx")
mb("send", "--from", "claude:orch", "--to", "codex:cx", "--body", "codex without cli")
run_once()
check("codex delivery without CLI records error", "codex CLI not found" in (mb("show", "4")[0]["delivery_error"] or ""))

# start script
start = [sys.executable, str(REPO / "start-antigravity-worker.py"), "--orchestrator", "claude:orch"]
dry = subprocess.run([*start, "--dry-run"], capture_output=True, text=True, encoding="utf-8")
dj = json.loads(dry.stdout)
check("dry run: no agentapi call", len(calls()) == len(calls()) and dj["agentapi"][0] == "new-conversation"
      and dj["agentapi"][1] == "--model=gemini-4-neon-low")
n = len(calls())
real = subprocess.run([*start, "--label", "reviewer"], capture_output=True, text=True, encoding="utf-8")
rj = json.loads(real.stdout)
check("start: conversation ID and expected address returned",
      rj["conversation_id"] == "conv-stub-1" and rj["expected_address"] == "antigravity:conv-stub-1")
sc = calls()[-1]
check("start: one new-conversation call with token-bearing prompt",
      len(calls()) == n + 1 and sc["argv"][0] == "new-conversation" and rj["token"] in sc["argv"][-1]
      and sc["token_ok"])
check("start: prompt has no double quotes", '"' not in sc["argv"][-1])
bad = subprocess.run([*start[:2], "--orchestrator", "nope", "--dry-run"], capture_output=True, text=True)
check("start: bad orchestrator rejected", bad.returncode != 0)
bad = subprocess.run([*start, "--label", "a'b", "--dry-run"], capture_output=True, text=True)
check("start: quote in label rejected", bad.returncode != 0)

print("ALL PASSED" if not failed else "SOME FAILED")
