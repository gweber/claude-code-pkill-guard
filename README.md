# claude-code-pkill-guard

A Claude Code `PreToolUse` hook that stops `pkill -f`, `pgrep -f` and `pidwait -f` from matching the Bash tool's own
shells (Linux). Background: [anthropics/claude-code#93607](https://github.com/anthropics/claude-code/issues/93607).

## The problem

The Bash tool runs every command as `/bin/bash -c "source <snapshot> ... && eval '<your command>'"`, so your
command text is in that shell's argv. A `-f` pattern that matches your command text then finds Claude Code's own
shells:

1. **The calling shell.** `cd /srv && pkill -f server` kills it: exit 144, nothing after it runs.
   `-A` (`--ignore-ancestors`, procps-ng ≥ 4.0.3) fixes this case, and only this case.
2. **Forked copies of it in the same pipeline.** A pipe stage that is a loop, a `{ }` / `( )` group or a shell
   function runs in a fork of the wrapper with the same argv. In the Bash tool, `grep`, `find` and `pkill` are shell
   functions from the snapshot. The fork is a sibling of `pgrep`, not an ancestor, so `-A` does not leave it out:
   `pgrep -A -f X | grep -q .` reports a match with nothing running.
3. **Other tool shells.** A background task, a Monitor or another agent whose command text contains the name.
   `until ! pgrep -A -f '[w]orker.py'; do sleep 5; done` never ends while one of them runs, and two such wait loops
   keep each other alive (we had two run for 12.5 hours). Neither `-A` nor the `[x]` bracket trick helps here.

What holds up in all three cases: **anchor the pattern at the start of the target's command line**, for example
`pgrep -f '^python3 worker.py'`. Every tool shell's argv starts with `/bin/bash -c source`, so an anchored pattern
cannot match one. Check the target's real argv with `ps -o args= -p PID`. To wait for a job, wait on its PID
(`while kill -0 $PID`) or on a file it writes last.

Measured on Claude Code 2.1.312 with procps-ng 4.0.4 (Ubuntu 24.04):

| command (nothing named X running) | result |
|---|---|
| `pgrep -A -f X \| grep -q .` (grep is a function) | match |
| `pgrep -A -f X \| /usr/bin/grep -q .` | clean |
| `pgrep -A -f X \| while read p; do …; done` | match |
| `pgrep -A -f X \| head -1` | clean |
| `pgrep -f '^X' \| grep -q .` | clean |
| `pgrep -A -f 'zzq[q]_marker'`, another background tool shell holding `zzqq_marker` | match (the other shell) |
| `pgrep -f '^sleep 5'`, same background shell running `sleep 5` | only the `sleep` |

## What the hook does

It reads the Bash command before it runs and **denies**:

- `pkill -f` / `pidwait -f` without `-A` whose pattern matches the command's own text, and `pgrep -f` used the same
  way (`kill $(pgrep -f …)`, `| xargs kill`, in a condition);
- `pgrep`/`pkill -A -f` in a pipeline with a loop, a group or a shell-function stage, when the pattern matches the
  command text (the function names are read from the current shell snapshot);
- wait loops (`while`/`until … pgrep -f`, `pidwait -f`) on a pattern that is not anchored with `^`.

The deny message tells Claude what to do instead (anchored pattern first, then PID). A display-only `pgrep -af X`
gets a short note instead of a deny. Heredoc text written to a file is ignored; commands inside `bash -c '…'`,
`ssh host '…'` and heredocs fed to a shell are checked. Everything else passes untouched, and any parse problem lets
the command through.

## Install

Copy `pkill-self-match.py` to `~/.claude/hooks/`, make it executable, and add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "~/.claude/hooks/pkill-self-match.py", "timeout": 5 }]
      }
    ]
  }
}
```

Needs Python 3.8+ and nothing else. Tests: `python3 -m pytest -q test_pkill_self_match.py`.

This is a workaround. The real fix is in Claude Code: not putting the command text into the wrapper shell's argv
(for example, passing it on stdin or in an environment variable), as proposed in #93607.

## License

MIT
