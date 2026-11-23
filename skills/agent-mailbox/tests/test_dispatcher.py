"""End-to-end checks for dispatcher.py against a scratch database and a stub codex CLI."""
import importlib.util, json, os, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
T = HERE / ".work"
T.mkdir(exist_ok=True)
DB = T / "test.db"
STUB_LOG = T / "stub.log"
for p in [*T.glob("test.db*"), STUB_LOG, T / "dispatch.log"]:
    p.unlink(missing_ok=True)
os.environ.update(AGENT_MAILBOX_DB=str(DB), STUB_LOG=str(STUB_LOG), STUB_MODE="ok")
MB = str(HERE.parent / "mailbox.py")

spec = importlib.util.spec_from_file_location("dispatcher", str(HERE.parent / "dispatcher.py"))
d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
d.CLI_TIMEOUT_SECONDS = 2
d.BACKOFF_SECONDS = [0, 0]


def mb(*args, body=None):
    out = subprocess.run([sys.executable, MB, *args], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return [json.loads(l) for l in out.stdout.splitlines()]


def run_once():
    disp = d.Dispatcher(d.resolve_codex(str(HERE / "stub_codex.py")), T / "dispatch.log", 0)
    disp.run(once=True)


def calls():
    return [json.loads(l) for l in STUB_LOG.read_text(encoding="utf-8").splitlines()] if STUB_LOG.exists() else []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        global failed; failed = True

failed = False
reg = {r["address"]: r["delivery"] for r in [
    *mb("register", "--tool", "claude", "--session", "c1"),
    *mb("register", "--tool", "codex", "--session", "cx-uuid"),
    *mb("register", "--tool", "codex"),
    *mb("register", "--tool", "gemini", "--session", "g1"),
]}
check("default delivery: claude=self", reg["claude:c1"] == "self")
check("default delivery: codex with session=codex-queue", reg["codex:cx-uuid"] == "codex-queue")
check("default delivery: codex generated=pull", [v for k, v in reg.items() if k.startswith("codex:") and k != "codex:cx-uuid"] == ["pull"])
check("default delivery: gemini=pull", reg["gemini:g1"] == "pull")

# 1. normal delivery, only to codex-queue recipients
m1 = mb("send", "--from", "claude:c1", "--to", "codex:cx-uuid", "--to", "gemini:g1", "--to", "claude:c1",
        "--body", 'Task with "quotes", Umlaute äöü and\nnew lines')
run_once()
c = calls()
check("exactly one CLI call for three recipients", len(c) == 1)
argv = c[0]["argv"]
check("calls queue with thread and --resume", argv[:4] == ["queue", "--thread", "cx-uuid", "--resume"])
env = argv[argv.index("--message") + 1]
check("envelope labels sender and not-from-user", "from claude:c1. NOT from your user" in env)
check("body passed intact (quotes, umlauts, newline)", 'Task with "quotes", Umlaute äöü and\nnew lines' in env)
check("envelope has reply and ack commands", "--reply-to 1" in env and "ack --as codex:cx-uuid 1" in env)
shown = mb("show", "1")[0]
check("delivered_at recorded", shown["delivered_at"] is not None and shown["delivery_error"] is None)
run_once()
check("no redelivery on next pass", len(calls()) == 1)

# 2. already-read messages are not delivered
mb("send", "--from", "claude:c1", "--to", "codex:cx-uuid", "--body", "read before delivery")
mid = 4
mb("ack", "--as", "codex:cx-uuid", str(mid))
run_once()
check("read message skipped", len(calls()) == 1)

# 3. failure -> retry -> give up after MAX_ATTEMPTS
os.environ["STUB_MODE"] = "fail"
mb("send", "--from", "claude:c1", "--to", "codex:cx-uuid", "--body", "will fail")
for _ in range(4):
    run_once()
fails = [x for x in calls() if x["mode"] == "fail"]
check("failed delivery tried exactly 3 times", len(fails) == 3)
m = mb("show", "5")[0]
check("error recorded, not delivered", m["delivered_at"] is None and "exited 1" in (m["delivery_error"] or ""))

# 4. timeout -> no retry
os.environ["STUB_MODE"] = "hang"
mb("send", "--from", "claude:c1", "--to", "codex:cx-uuid", "--body", "will hang")
run_once(); run_once()
check("timed-out delivery not retried", len([x for x in calls() if x["mode"] == "hang"]) == 1)
check("timeout error recorded", "timed out" in (mb("show", "6")[0]["delivery_error"] or ""))

# 5. long body -> pointer instead of inline text
os.environ["STUB_MODE"] = "ok"
mb("send", "--from", "claude:c1", "--to", "codex:cx-uuid", "--body", "x" * 20000)
run_once()
env = calls()[-1]["argv"][-1]
check("long body replaced by show pointer", "too long to deliver inline" in env and "show 7" in env and len(env) < 2000)

# 6. switching a session to pull stops delivery
mb("set-delivery", "codex:cx-uuid", "pull")
mb("send", "--from", "claude:c1", "--to", "codex:cx-uuid", "--body", "pull mode")
n = len(calls()); run_once()
check("pull-mode session not pushed", len(calls()) == n)

# 7. single instance lock
lock = d.acquire_lock(DB.with_name("dispatcher.lock"))
second = subprocess.run([sys.executable, str(HERE.parent / "dispatcher.py"), "--once",
                         "--codex", str(HERE / "stub_codex.py"), "--log", str(T / "dispatch.log")],
                        capture_output=True, text=True)
check("second dispatcher refused while lock held", second.returncode != 0 and "already running" in second.stderr)
lock.close()

print("ALL PASSED" if not failed else "SOME FAILED")
