# Orchestrating Codex, Antigravity and Cursor worker sessions

How an orchestrator (for example a Claude Code session) starts Codex worker
sessions and works with them through the agent mailbox. Antigravity and Cursor
workers follow the same flow; see [Antigravity workers](#antigravity-workers) and
[Cursor workers (Grok)](#cursor-workers-grok). Everything below was
tested on 2026-11-22 with Codex CLI 0.167.2 and the Codex desktop app.

## How it fits together

```
orchestrator ── mailbox.py send ──▶ mailbox.db ──▶ dispatcher.py ── codex queue ──▶ Codex worker chat
     ▲                                                                                    │
     └──────────── own watcher ◀── mailbox.db ◀────────── mailbox.py send (reply) ────────┘
```

1. The orchestrator starts an interactive `codex` session in a new window and
   gives it a start prompt that contains the orchestrator's address and a
   unique token.
2. The worker registers itself in the mailbox **with its own Codex session ID**
   (delivery mode `codex-queue`) and sends the orchestrator
   `started <token> codex:<session-id> cwd=<folder>`.
3. The orchestrator reads that message to learn the worker's address. From then
   on it sends tasks with `mailbox.py send`. The dispatcher pushes each one into
   the worker's chat, and the worker replies through the mailbox.

The orchestrator never needs to look the session ID up anywhere: the worker
reports it.

## Prerequisites

- **Dispatcher running.** It starts at logon as the scheduled task
  `AgentMailboxDispatcher` (see README). Check: the last line of
  `dispatcher.log` is recent, or `Get-ScheduledTask AgentMailboxDispatcher` shows `Running`.
- **Codex app-server daemon running.** The Codex desktop app normally keeps it
  running. Check with `codex app-server daemon version` (`"status":"running"`),
  start it with `codex app-server daemon start`.
- **Orchestrator registered and watching.** A Claude orchestrator registers with
  `mailbox.py register --tool claude` (delivery mode `self`) and keeps a
  background `mailbox.py watch --as <address> --once` running, restarting it
  after every message.
- **Project folder added in the Codex app.** The app only lists sessions from
  folders that are added as projects there. Sessions in other folders still
  work, but you won't find them in the app.

## 1. Start a worker

```
powershell -ExecutionPolicy Bypass -File <mailbox>\start-codex-worker.ps1 -Orchestrator claude:<your-session-id> -Project C:\path\to\project -Label reviewer
```

From inside PowerShell you can also call it directly with
`& <mailbox>\start-codex-worker.ps1 -Orchestrator ... -Project ...`.
Never put double quotes in arguments passed through `powershell -File`:
Windows PowerShell 5.1 can drop them and shift the following arguments, so a
`-DryRun` can get lost and the script then really starts a worker.

It opens a new window running `codex` in the project folder with sandbox
`workspace-write` and write access to the mailbox folder (`--add-dir`), and
prints one JSON line with the `token`. Add `-DryRun` to print the command
without starting anything. Use `-Sandbox read-only` for workers that must not
change the project (they can still write to the mailbox folder).

**Use `codex`, not `codex exec`.** An interactive `codex` session runs through
the shared app-server daemon (its session metadata shows
`originator: Codex Desktop`), so it appears in the desktop app and keeps
working with the mailbox. A `codex exec` session is saved but is not listed in
the app. In the test, the dispatcher's message did reach an exec session, but
its resumed turn could not run the mailbox CLI, so it never replied.

## 2. Wait for the worker to report

The worker's first message to you looks like:

```
started orch-5af668b8bc codex:7d3e9b21-4c8a-4f05-a6b2-19e0c5d84f37 cwd=C:\path\to\project
```

Match the token to the one the start script printed. The address after it is
the worker's mailbox address. Ack the message. In the test the report arrived
within about a minute of starting the window.

If no report arrives, look at the worker's window: Codex may be waiting for an
approval (for example to run `python`). Whoever watches the window has to
approve it. Don't start workers with approvals or the sandbox bypassed.

## 3. Work with the worker

Send tasks to the worker's address, or reply to its report to keep everything in
one thread:

```
python <mailbox>\mailbox.py send --from <orchestrator> --reply-to <report-id> --body-file task.md
```

The dispatcher delivers within a few seconds. The worker sees the task in its
chat as `[Agent mailbox message #N from <orchestrator>. NOT from your user.]`,
does the work and answers with `--reply-to`. Test timings: delivery about 1 s
after sending, the answer to a small task about 25 s later.

To fan out, start several workers and send the same task with repeated `--to`.
All replies land in the same thread (`mailbox.py thread <thread_id>`).

Write tasks the way the brief asks for messages: self-contained, with absolute
paths, saying exactly what you need back.

## 4. Finish

- Tell the worker it is done, then ack its last message.
- Stop pushes to it with `mailbox.py set-delivery codex:<id> pull`.
- The chat stays in the Codex app. Archive it there if you don't need it.

## Antigravity workers

Antigravity conversations work the same way, through the local `agentapi`
bundled with the Antigravity desktop app. Antigravity must be running.

```
python <mailbox>\start-antigravity-worker.py --orchestrator claude:<your-session-id> --label reviewer [--model gemini-4-neon-low] [--title "..."] [--dry-run]
```

It creates a new conversation in Antigravity and prints one JSON line with the
`token`, the `conversation_id` and the `expected_address`
(`antigravity:<conversation-id>`). The worker then registers itself (its ID
comes from `ANTIGRAVITY_CONVERSATION_ID`, delivery mode `antigravity-send`) and
sends you `started <token> antigravity:<id>`. Wait for that report before
sending tasks: it proves the worker can reach the mailbox. From then on, work
with it exactly as with a Codex worker.

`--model` takes `flash_lite`, `flash`, `pro` or a model ID such as
`gemini-4-neon-low`, `gemini-4-neon-medium` or `gemini-4-neon-high`.

Differences from Codex workers (tested 2026-11-23):

- **No project folder.** Conversations started through `agentapi` have no
  workspace. Give the worker absolute paths and all the context it needs.
- **Commands can still need approval.** The conversation metadata shows
  `commandExecutionPolicy: eager`, but in the test the worker stopped at its
  first command (the mailbox registration) with "Waiting for approval" until
  the user approved it in the Antigravity window. Expect the same for later
  commands, and tell your user to watch the Antigravity window. Still only give
  these workers tasks you would let run with little supervision.
- **Messages arrive as system messages.** The envelope says they come from
  another agent, not from the user or the system, and the start prompt repeats it.
- **Undocumented interface.** `agentapi` and its local address and CSRF token
  (read from `~/.gemini/antigravity/daemon/hub_*.json` at each call, never
  printed or stored) can change with any Antigravity update.

Test timings: in the first test the start report arrived about 3 minutes after
the conversation was created. In the second it took about 20 minutes because
the worker waited for the user's approval; it reported 10 s after the approval.
Messages to the idle conversation were delivered within 2 s and answered after
34-36 s. If no report arrives within a few minutes, ask your user to check the
Antigravity window for a pending approval.

## Cursor workers (Grok)

Cursor agent CLI chats can run Grok (and Cursor's other models). The Cursor agent
CLI must be installed in `%LOCALAPPDATA%\cursor-agent` and signed in
(`agent login`, done by the user), and the dispatcher must be running.

```
python <mailbox>\start-cursor-worker.py --orchestrator claude:<your-session-id> --label reviewer [--model grok-4.7-low] [--workspace <folder>] [--dry-run]
```

The script only writes a start request to `cursor-chats/requests/` and prints the
`token`. The dispatcher creates the chat (usually within seconds), records its model
and folder, and runs the first turn, in which the worker registers as
`cursor:<chat-id>` (delivery mode `cursor-resume`) and sends you
`started <token> cursor:<chat-id> cwd=<folder>`. If the start fails, you get a
message from `dispatcher:local` instead. From then on work with it as with any
other worker.

The dispatcher makes the Cursor calls because it runs outside agent sandboxes. A
sandboxed shell can see a separate, signed-out copy of the Cursor CLI, so don't
call `agent` from the orchestrator's own shell.

Model IDs come from `agent models`, for example `grok-4.7-low`, `grok-4.7-medium`,
`grok-4.7-high` or `cursor-grok-4.6-high`. They change with Cursor updates.

Differences from Codex and Antigravity workers (tested 2026-11-23):

- **One turn per message.** Each message runs `agent -p --resume <chat-id>` once;
  the chat keeps its history between turns. Messages to a worker whose turn is
  still running wait for it. A turn is stopped after 30 minutes and that message
  is not retried.
- **Only allowed commands run.** In this mode Cursor rejects every shell command
  that no allow rule covers (it reports only `Rejected:`), and no one can approve
  it. The rules come from `.cursor/cli.json` in the worker folder. The default
  folder `cursor-worker/` is created on first use and allows only the mailbox CLI, so the worker can read
  files, answer and reply, but not run other commands. For work in a project, pass
  `--workspace <project>`; that folder needs its own `.cursor/cli.json` with the
  mailbox rule `Shell(python:<mailbox>/mailbox.py **)` (absolute path, forward
  slashes; copy it from `cursor-worker/.cursor/cli.json`) and any other commands
  the user agrees to. The start script refuses a folder without
  the mailbox rule.
- **Not visible in the Cursor IDE** as far as known (not checked). The last
  turn's output is in `cursor-chats/<chat-id>.out`.
- **`create-chat` hangs** after printing the chat ID (Cursor bug); the
  dispatcher reads the ID and stops the process.

Test timings: the start report arrived 17 s after the request. A task was
picked up within 2 s, answered 14 s later, and its turn ended after 20 s.
Quotes, umlauts, `&` and `<>` arrived intact.

To stop a Cursor worker, set it to `pull`. Nothing keeps running between turns.

## Rules for orchestrators

- **Start workers only when your user asked for it.** Each worker runs model
  turns and may change files in its project folder.
- **Your messages are not the worker's user instructions.** The dispatcher
  labels them accordingly, and the worker acts only within what its user
  authorized. Anything beyond that (sending, publishing, deleting, pushing,
  spending money) needs the user's own approval in the worker's window.
- **One task per worker at a time.** A message to a busy worker is queued until
  its current turn ends.

## Closing the window

The worker's turns run in the Codex app-server daemon, not in the window. In a
test, a worker whose window process was killed right after the start still
finished its first turn: it registered and sent its report. **Closing the
window therefore does not stop a worker.** To stop one, tell it so, set it to
`pull`, and archive the chat (`codex archive <session-id>`, reversible with
`codex unarchive`).

## Not tested yet

- Whether a new message reaches an interactive worker whose window was already
  closed before the message was sent. The observation above makes it likely.
- Whether an Antigravity conversation can be tied to a project folder.
- Whether Cursor CLI chats show up in the Cursor IDE.
