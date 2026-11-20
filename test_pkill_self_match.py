"""Tests for pkill-self-match.py. Run: python3 -m pytest -q test_pkill_self_match.py"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pkill-self-match.py")
if not os.path.exists(HOOK):
    HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pkill_self_match.py")
spec = importlib.util.spec_from_file_location("hook", HOOK)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

FUNCS = {"grep", "find", "pkill"}     # what the Bash tool's snapshot defines as functions


def verdict(cmd):
    deny, notes = hook.judge(cmd, FUNCS)
    return "deny" if deny else "note" if notes else "ok"


@pytest.mark.parametrize("cmd", [
    # 1. the wrapper shell itself
    "pkill -f worker.py",
    "cd /srv && pkill -f worker.py; echo done",
    "kill $(pgrep -f worker.py)",
    "pgrep -f worker.py | xargs -r kill",
    "pidwait -f worker.py",
    "bash -c 'cd /srv && pkill -f worker.py'",
    "bash <<'EOF'\ncd /srv\npkill -f worker.py\nEOF",
    # 2. -A, but a forked sibling stage carries the text
    "pgrep -A -f worker.py | grep -q . && echo running",
    "pgrep -A -f worker.py | while read p; do kill $p; done",
    "pgrep -A -f worker.py | sort | while read p; do echo $p; done",
    "pgrep -A -f worker.py | { read p; kill $p; }",
    "{ sleep 0; } | pgrep -A -f worker.py",
    # 3. wait loops on an unanchored pattern find other tool shells
    "until ! pgrep -A -f '[w]orker.py'; do sleep 5; done",
    "while pgrep -f '[w]orker.py' >/dev/null; do sleep 5; done",
    "pidwait -A -f '[w]orker.py'",
])
def test_denied(cmd):
    assert verdict(cmd) == "deny", cmd


@pytest.mark.parametrize("cmd", [
    "pkill -A -f worker.py",
    "pkill -f '[w]orker.py'",
    "kill $(pgrep -A -f worker.py)",
    "pgrep -A -f worker.py | xargs -r kill",
    "pgrep -A -f worker.py | head -1",
    "pgrep -A -f worker.py | /usr/bin/grep -q . && echo running",
    "pgrep -f '^python3 worker.py' | grep -q . && echo running",
    "pgrep -f '^python3 worker.py' | while read p; do kill $p; done",
    "while pgrep -f '^python3 worker.py' >/dev/null; do sleep 5; done",
    "pidwait -f '^python3 worker.py'",
    "while kill -0 $PID; do sleep 5; done",
    "pkill worker",                               # no -f: matches the process name only
    "cat > /tmp/stop.sh <<'EOF'\npkill -f worker.py\nEOF",
    "echo 'pkill -f worker.py' >> notes.txt",
    "grep -rn 'pgrep -f' ~/code",
    "ls -la",
])
def test_allowed(cmd):
    assert verdict(cmd) == "ok", cmd


def test_display_only_pgrep_gets_a_note():
    assert verdict("pgrep -af worker.py") == "note"


def test_deny_message_leads_with_the_anchored_pattern():
    out = run_hook("pgrep -A -f worker.py | grep -q . && echo up")
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "fork of the wrapper" in reason and reason.index("^python3") < reason.index("-A only")


def test_hook_is_silent_and_fails_open():
    assert run_hook("ls -la") is None
    p = subprocess.run([sys.executable, HOOK], input="not json", capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout == ""


def run_hook(cmd):
    p = subprocess.run([sys.executable, HOOK], input=json.dumps({"tool_input": {"command": cmd}}),
                       capture_output=True, text=True, check=True)
    return json.loads(p.stdout) if p.stdout.strip() else None
