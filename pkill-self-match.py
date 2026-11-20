#!/usr/bin/env python3
"""PreToolUse hook for Bash: stop `pkill/pgrep/pidwait -f` from matching the Bash tool's own shells.

The Bash tool runs each command as `/bin/bash -c "source <snapshot> ... && eval '<command>'"`, so the command text is in
that shell's argv (see anthropics/claude-code#93607). A `-f` pattern that matches the command text therefore finds:

1. the wrapper shell itself (an ancestor): pkill/pidwait kill or wait on it (exit 144, nothing after it runs).
   `-A` (--ignore-ancestors, procps-ng >= 4.0.3) leaves ancestors out;
2. forked copies of the wrapper in the same pipeline: a pipe stage that is a loop, a `{ }`/`( )` group or a shell
   function (the snapshot defines grep, find, pkill as functions) runs in a fork with the same argv. It is a sibling of
   pgrep, so `-A` does not leave it out: `pgrep -A -f X | grep -q .` matches with nothing running;
3. other tool shells (background tasks, Monitor, other agents) whose command text mentions the name: a wait loop
   `until ! pgrep -f X` never ends while one of them runs, and two such loops keep each other alive. Neither `-A`
   nor the `[x]` bracket trick helps against another shell that has the plain name in its text.

What always works: anchor the pattern at the start of the target's command line (`pgrep -f '^python3 worker.py'`).
Every tool shell's argv starts with `/bin/bash -c source`, so an anchored pattern cannot match one. Or wait on a PID.

The hook denies the deadly cases and adds a note for a display-only pgrep. It never blocks anything else; on any parse
problem it lets the command through. Tests: test_pkill_self_match.py next to this file.
"""
import glob
import json
import os
import re
import shlex
import sys

TOOLS = {"pkill", "pgrep", "pidwait"}
ARG_SHORT = set("dgGPstuUFrO")  # short options that take a value
ARG_LONG = {"--delimiter", "--pgroup", "--group", "--parent", "--session", "--terminal", "--euid",
            "--uid", "--pidfile", "--ns", "--nslist", "--signal", "--runstates", "--older", "--env"}
PIPES = {"|", "|&"}
SEPARATORS = {";", "&&", "||", "&", "(", ")", "\n", ";;"} | PIPES
COMPOUND_START = {"while", "until", "for", "if", "case", "select", "{", "("}
COMPOUND_END = {"done", "fi", "esac", "}", ")"}
SHELLS = {"bash", "sh", "zsh", "dash", "ssh"}           # a heredoc fed to these is run, not written to a file
FALLBACK_FUNCTIONS = {"grep", "rg", "find", "pkill"}


def snapshot_functions():
    """Names of the shell functions the Bash tool's snapshot defines (a pipe stage calling one is a fork)."""
    try:
        snaps = glob.glob(os.path.expanduser("~/.claude/shell-snapshots/snapshot-bash-*.sh"))
        with open(max(snaps, key=os.path.getmtime)) as f:
            names = set(re.findall(r"^function ([\w.:-]+)", f.read(), re.M))
        return names or FALLBACK_FUNCTIONS
    except (OSError, ValueError):
        return FALLBACK_FUNCTIONS


def wrapper_argv(command):
    """What the wrapper shell's argv looks like (single quotes are escaped inside eval '...')."""
    return ("/bin/bash -c source /home/user/.claude/shell-snapshots/snapshot-bash-0-x.sh 2>/dev/null || true "
            "&& shopt -u extglob 2>/dev/null || true && eval '" + command.replace("'", "'\"'\"'")
            + "' < /dev/null && pwd -P >| /tmp/claude-cwd")


def strip_heredocs(command):
    """The command without heredoc bodies that are written somewhere (cat > f <<EOF): they are text, not commands."""
    out, ends = [], []
    for line in command.splitlines():
        if ends:
            if line.strip() == ends[0][0]:
                ends.pop(0)
            elif ends[0][1]:
                out.append(line)        # a heredoc fed to a shell is run: keep its lines
            continue
        out.append(line)
        for m in re.finditer(r"<<-?\s*(['\"]?)([\w.-]+)\1", line):
            before = re.split(r"\|\|?|&&|;", line[:m.start()])[-1].split()
            runs = bool(before) and before[0].rsplit("/", 1)[-1] in SHELLS
            ends.append((m.group(2), runs))
    return "\n".join(out)


def tokenize(text):
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()")
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def tokens_of(command):
    try:
        return tokenize(command.replace("\n", " ; "))
    except ValueError:
        tokens = []
        for line in command.splitlines():   # unbalanced quotes somewhere: keep the lines that parse
            try:
                tokens += tokenize(line) + [";"]
            except ValueError:
                tokens.append(";")
        return tokens


def parse_call(tokens, i):
    """flags, pattern and the index after the call starting at tokens[i] (the tool name)."""
    flags, pattern, i = set(), None, i + 1
    while i < len(tokens) and tokens[i] not in SEPARATORS and not re.fullmatch(r"\d*[<>].*", tokens[i]):
        t = tokens[i]
        if t.startswith("--"):
            key = t.split("=", 1)[0]
            flags.add(key)
            if key in ARG_LONG and "=" not in t:
                i += 1
        elif t.startswith("-") and len(t) > 1 and pattern is None:
            if not (re.fullmatch(r"-(\d+|[A-Z]+[0-9+-]*)", t) and t != "-A"):  # else a signal: -9, -KILL
                for j, ch in enumerate(t[1:]):
                    flags.add("-" + ch)
                    if ch in ARG_SHORT:
                        if j == len(t) - 2:
                            i += 1
                        break
        elif pattern is None:
            pattern = t
        i += 1
    return flags, pattern, i


def forked_neighbours(tokens, start, end, functions):
    """True if the pipeline around tokens[start:end] has a stage that runs in a fork of the wrapper shell."""
    j = end                                   # stages after the call
    while j < len(tokens) and tokens[j] in PIPES:
        k = j + 1
        while k < len(tokens) and tokens[k] in {"!", "time"}:
            k += 1
        if k < len(tokens) and (tokens[k] in COMPOUND_START or tokens[k] in functions):
            return True
        j = k
        while j < len(tokens) and tokens[j] not in SEPARATORS:
            j += 1
    j = start - 1                             # stages before the call
    while j >= 0 and tokens[j] in PIPES:
        if j > 0 and tokens[j - 1] in COMPOUND_END:
            return True
        k = j - 1
        while k >= 0 and tokens[k] not in SEPARATORS:
            k -= 1
        if k + 1 < j and tokens[k + 1] in functions:
            return True
        j = k
    return False


def in_wait_loop(tokens, start):
    """True if the call is the condition of a while/until loop (`until ! pgrep -f X; do ...`)."""
    k = start - 1
    while k >= 0 and tokens[k] == "!":
        k -= 1
    return k >= 0 and tokens[k] in {"while", "until"}


def result_used(tokens, start, end, command):
    """pgrep's output or exit status decides something (not just printed)."""
    before = tokens[start - 1] if start else ";"
    after = tokens[end] if end < len(tokens) else ";"
    return (before in {"if", "while", "until", "!", "&&", "||"} or after in {"&&", "||"} | PIPES
            or bool(re.search(r"\bkill\b|\bxargs\b", command)))


def runs_as_command(tokens, head, i):
    """True if tokens[i] is the command string of `bash -c '...'` or the remote command of `ssh host '...'`."""
    words = [w.rsplit("/", 1)[-1] for w in tokens[head:i]]
    while words and words[0] in {"sudo", "timeout", "nice", "nohup", "env", "command", "exec", "time"}:
        words.pop(0)
    if not words:
        return False
    if words[0] in SHELLS - {"ssh"}:
        return bool(re.fullmatch(r"-\w*c", words[-1]))
    return words[0] == "ssh"


def calls(command, functions, depth=0):
    """Yield one dict per pkill/pgrep/pidwait call, including those inside `bash -c '...'` / `ssh host '...'`."""
    tokens = tokens_of(strip_heredocs(command))
    i, head = 0, 0                            # head: where the current simple command starts
    while i < len(tokens):
        t = tokens[i]
        if t in SEPARATORS:
            head = i + 1
        name = t.rsplit("/", 1)[-1]
        if name in TOOLS:
            flags, pattern, end = parse_call(tokens, i)
            yield {"tool": name, "flags": flags, "pattern": pattern, "inner": command if depth else None,
                   "forked": forked_neighbours(tokens, i, end, functions),
                   "wait": in_wait_loop(tokens, i) or name == "pidwait",
                   "used": result_used(tokens, i, end, command)}
            i = end
            continue
        if depth < 2 and " " in t and re.search(r"\b(pkill|pgrep|pidwait)\b", t) and runs_as_command(tokens, head, i):
            yield from calls(t, functions, depth + 1)
        i += 1


def judge(command, functions=None):
    """(deny_reasons, notes) for a Bash command."""
    if not re.search(r"\b(pkill|pgrep|pidwait)\b", command):
        return [], []
    functions = snapshot_functions() if functions is None else functions
    argv = wrapper_argv(command)
    deny, notes = [], []
    for c in calls(command, functions):
        tool, flags, pattern = c["tool"], c["flags"], c["pattern"]
        if not pattern or not ({"-f", "--full"} & flags):
            continue
        skips_ancestors = bool({"-A", "--ignore-ancestors"} & flags)
        shown = f"`{tool} {'-A ' if skips_ancestors else ''}-f {pattern}`"
        try:
            own = bool(re.search(pattern, argv)) or bool(c["inner"] and re.search(pattern, c["inner"]))
        except re.error:
            continue
        if own and not skips_ancestors:
            if tool != "pgrep" or c["used"] or c["wait"]:
                deny.append(f"{shown} matches this command's own text, which is in the Bash tool's wrapper shell argv, "
                            f"so it would {'kill' if tool == 'pkill' else 'find/wait on'} this shell (pkill: exit 144, "
                            "nothing after it runs).")
            else:
                notes.append(f"{shown} also lists the Bash tool's own wrapper shell (a different PID on every call); "
                             "an anchored pattern ('^name ...') or -A leaves it out.")
        elif own and c["forked"]:
            deny.append(f"{shown} is in a pipeline with a loop, a {{ }}/( ) group or a shell-function stage "
                        f"(the Bash tool defines {', '.join(sorted(functions)) or 'grep, find'} as functions). "
                        "That stage runs in a fork of the wrapper shell with this command's text in its argv; it is not an "
                        "ancestor, so -A does not leave it out and the pattern matches it even with nothing running.")
        elif c["wait"] and not pattern.startswith("^"):
            deny.append(f"{shown} waits on a pattern that is not anchored. Any other tool shell whose command text "
                        "contains the name (a background task, a Monitor, another wait loop) matches too, -A and the "
                        "[x] bracket trick notwithstanding, and the loop never ends.")
    return deny, notes


FIX = ("Anchor the pattern at the start of the target's command line, e.g. `pgrep -f '^python3 worker.py'` "
       "(check the real argv with `ps -o args= -p PID`): every tool shell's argv starts with `/bin/bash -c`, so an "
       "anchored pattern matches only the target. To wait for a job, prefer its PID (`while kill -0 $PID`), a file it "
       "writes last, or run_in_background and its completion notice. -A only leaves out the calling shell itself.")


def main():
    try:
        data = json.load(sys.stdin)
        command = data.get("tool_input", {}).get("command") or ""
        deny, notes = judge(command)
    except Exception:
        return
    if deny:
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": " ".join(deny) + " " + FIX}}
    elif notes:
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": " ".join(notes)}}
    else:
        return
    print(json.dumps(out))


if __name__ == "__main__":
    main()
