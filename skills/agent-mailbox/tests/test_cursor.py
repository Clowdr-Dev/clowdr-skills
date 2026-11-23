"""Checks for cursor-resume delivery, Cursor worker starts and start-cursor-worker.py, using a stub CLI."""
import importlib.util, json, os, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
T = HERE / ".work"
T.mkdir(exist_ok=True)
DB, LOG, CHATS, WS = T / "cur.db", T / "stub_cur.log", T / "cur-chats", T / "cur-ws"
for p in [*T.glob("cur.db*"), LOG, T / "cur_dispatch.log"]:
    p.unlink(missing_ok=True)
shutil.rmtree(CHATS, ignore_errors=True)
shutil.rmtree(WS, ignore_errors=True)
BARE = T / "cur-ws-bare"
BARE.mkdir(exist_ok=True)
REPO = HERE.parent
MB = str(REPO / "mailbox.py")
os.environ.update(AGENT_MAILBOX_DB=str(DB), STUB_CURSOR_LOG=str(LOG), STUB_MB=MB,
                  AGENT_MAILBOX_CURSOR_AGENT=str(HERE / "stub_cursor.py"), STUB_CURSOR_MODE="ok")

spec = importlib.util.spec_from_file_location("dispatcher", REPO / "dispatcher.py")
d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
d.BACKOFF_SECONDS = [0, 0]
(WS / ".cursor").mkdir(parents=True)
(WS / ".cursor" / "cli.json").write_text(json.dumps(
    {"permissions": {"allow": [d.cur.mailbox_rule()], "deny": []}}))
failed = False


def mb(*args):
    out = subprocess.run([sys.executable, MB, *args], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return [json.loads(l) for l in out.stdout.splitlines()]


def dispatcher(timeout=None):
    disp = d.Dispatcher(None, T / "cur_dispatch.log", 0)
    disp.chat_dir = CHATS
    if timeout:
        disp.turn_timeout = timeout
    return disp


def calls(kind=None):
    rows = [json.loads(l) for l in LOG.read_text(encoding="utf-8").splitlines()] if LOG.exists() else []
    return [r for r in rows if kind is None or r["cmd"] == kind]


def show(i):
    return mb("show", str(i))[0]


def check(name, cond):
    global failed
    print(("PASS " if cond else "FAIL ") + name)
    failed |= not cond


# --- module helpers
cur = d.cur
check("workspace with mailbox rule allowed", cur.workspace_allows_mailbox(WS))
check("workspace without cli.json refused", not cur.workspace_allows_mailbox(BARE))
shutil.rmtree(T / "default-ws", ignore_errors=True)
cur.ensure_worker_folder(T / "default-ws")
check("generated worker folder allows mailbox", cur.workspace_allows_mailbox(T / "default-ws"))
check("mailbox CLI path uses forward slashes", d.cur.MAILBOX_PATH in d.MAILBOX_CLI)
t0 = time.time()
chat = cur.create_chat(WS, timeout=20)
check("create_chat returns the ID despite the hanging CLI", len(chat) == 36 and time.time() - t0 < 15)
check("create_chat ran in the workspace", calls("create-chat")[-1]["cwd"] == str(WS))

# --- worker start through a request file
mb("register", "--tool", "claude", "--session", "orch")
(CHATS / "requests").mkdir(parents=True)
req = {"token": "orch-test1", "orchestrator": "claude:orch", "model": "grok-4.7-low",
       "workspace": str(WS), "label": "tester"}
(CHATS / "requests" / "orch-test1.json").write_text(json.dumps(req))
dispatcher().run(once=True)
done = CHATS / "requests" / "done" / "orch-test1.json"
check("request moved to done with chat id", done.exists() and json.loads(done.read_text())["chat_id"])
start_chat = json.loads(done.read_text())["chat_id"]
cfg = json.loads((CHATS / f"{start_chat}.json").read_text())
check("chat config records model and workspace", cfg["model"] == "grok-4.7-low" and cfg["workspace"] == str(WS))
first = calls("turn")[-1]
check("first turn: right chat, model, cwd, flags",
      first["chat"] == start_chat and first["model"] == "grok-4.7-low" and first["cwd"] == str(WS)
      and {"-p", "--trust", "--resume"} <= set(first["flags"]))
check("start prompt names token, chat, register with cursor-resume",
      "orch-test1" in first["message"] and f"--session {start_chat}" in first["message"]
      and "--delivery cursor-resume" in first["message"] and d.cur.MAILBOX_PATH in first["message"])

# bad request: workspace without rule -> failed + notice to orchestrator
(CHATS / "requests" / "orch-test2.json").write_text(json.dumps({**req, "token": "orch-test2", "workspace": str(BARE)}))
dispatcher().run(once=True)
check("bad workspace request moved to failed", (CHATS / "requests" / "failed" / "orch-test2.json").exists())
notes = mb("inbox", "--as", "claude:orch")
check("orchestrator told about the failed start",
      any(n["from"] == "dispatcher:local" and "orch-test2" in n["body"] for n in notes))

# --- delivery
mb("register", "--tool", "cursor", "--session", start_chat, "--delivery", "cursor-resume")
os.environ["STUB_CURSOR_REPLY"] = "1"
m1 = mb("send", "--from", "claude:orch", "--to", f"cursor:{start_chat}", "--body", 'Task "quoted" äöü\nline 2')[0]["sent"]
m2 = mb("send", "--from", "claude:orch", "--to", f"cursor:{start_chat}", "--body", "second task")[0]["sent"]
disp = dispatcher()
disp.conn = d.mb.connect()
for row in disp.pending():
    if row["delivery"] == "cursor-resume" and row["session_id"] in disp.running:
        continue
    disp.deliver(row)
check("only one turn per chat at a time", len(disp.running) == 1 and disp.running[start_chat].msg_id == m1)
check("running message holds a lease", show(m1)["delivery_error"].startswith("cursor turn running"))
disp.wait_for_turns()
check("message 1 delivered after the turn", show(m1)["delivered_at"] and not show(m1)["delivery_error"])
check("message 2 still waiting", not show(m2)["delivered_at"])
dispatcher().run(once=True)
check("message 2 delivered on the next pass", show(m2)["delivered_at"])
turns = calls("turn")
check("envelope arrives intact with quotes, umlauts, newline",
      'Task "quoted" äöü\nline 2' in turns[-2]["message"] and "NOT from your user" in turns[-2]["message"])
replies = [n for n in mb("inbox", "--as", "claude:orch") if n["from"] == f"cursor:{start_chat}"]
check("worker replies reached the orchestrator", len(replies) == 2)
os.environ["STUB_CURSOR_REPLY"] = "0"

# crash without result -> retried, then gave up
os.environ["STUB_CURSOR_MODE"] = "crash"
m3 = mb("send", "--from", "claude:orch", "--to", f"cursor:{start_chat}", "--body", "crash")[0]["sent"]
dispatcher().run(once=True)
check("crash is retried", show(m3)["delivery_error"].startswith("cursor turn exited 1 without a result")
      and not show(m3)["delivered_at"])
dispatcher().run(once=True); dispatcher().run(once=True)
check("crash gives up after 3 attempts",
      len([t for t in calls("turn") if "crash" in t["message"].split("----- message -----")[-1]]) == 3)

# is_error result -> not retried
os.environ["STUB_CURSOR_MODE"] = "error"
m4 = mb("send", "--from", "claude:orch", "--to", f"cursor:{start_chat}", "--body", "err")[0]["sent"]
dispatcher().run(once=True); dispatcher().run(once=True)
check("error result not retried", "ended with an error" in show(m4)["delivery_error"]
      and len([t for t in calls("turn") if t["message"].rstrip().endswith("err\n----- end of message -----")
               or "\nerr\n" in t["message"]]) == 1)

# timeout -> stopped, not retried
os.environ["STUB_CURSOR_MODE"] = "slow"
m5 = mb("send", "--from", "claude:orch", "--to", f"cursor:{start_chat}", "--body", "slow")[0]["sent"]
t0 = time.time()
dispatcher(timeout=2).run(once=True)
check("slow turn stopped at the timeout", time.time() - t0 < 30 and "timed out" in show(m5)["delivery_error"])
dispatcher().run(once=True)
check("timed-out message not retried", len([t for t in calls("turn") if "\nslow\n" in t["message"]]) == 1)

# missing config -> not retried
os.environ["STUB_CURSOR_MODE"] = "ok"
mb("register", "--tool", "cursor", "--session", "no-config-chat", "--delivery", "cursor-resume")
m6 = mb("send", "--from", "claude:orch", "--to", "cursor:no-config-chat", "--body", "x")[0]["sent"]
dispatcher().run(once=True)
check("agent without config fails without retry", "no Cursor worker config" in show(m6)["delivery_error"])

# --- start-cursor-worker.py validation (dry run only)
def start(*a):
    return subprocess.run([sys.executable, str(REPO / "start-cursor-worker.py"), *a], capture_output=True,
                          text=True, encoding="utf-8")
ok = start("--orchestrator", "claude:orch", "--dry-run")
check("dry run uses the default folder and model", ok.returncode == 0 and
      json.loads(ok.stdout)["model"] == "grok-4.7-low" and json.loads(ok.stdout)["workspace"].endswith("cursor-worker"))
check("bad orchestrator refused", start("--orchestrator", "claude", "--dry-run").returncode != 0)
check("bad label refused", start("--orchestrator", "claude:orch", "--label", 'x"; rm', "--dry-run").returncode != 0)
check("workspace without rule refused", start("--orchestrator", "claude:orch", "--workspace", str(BARE),
                                              "--dry-run").returncode != 0)
check("dry run wrote no request", not (REPO / "cursor-chats" / "requests").exists()
      or not list((REPO / "cursor-chats" / "requests").glob("*.json")))

print("FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
