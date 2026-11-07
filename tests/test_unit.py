"""Fast tests for claude-persist: no network, no real Claude. Run:  python3 -m unittest discover -s tests -v
A fake "claude" (bash started under the name claude, ignoring its arguments) stands in for an interactive Claude."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr

ROOT = tempfile.mkdtemp(prefix="cp-unit-")
SOCK = f"cpunit-{os.getpid()}"
os.environ.update(CLAUDE_PERSIST_STATE=f"{ROOT}/state", CLAUDE_CONFIG_DIR=f"{ROOT}/claude", CLAUDE_PERSIST_SOCKET=SOCK,
                  CLAUDE_PERSIST_NOTIFY="0", CLAUDE_PERSIST_VERIFY_WAIT="1")
_loader = importlib.machinery.SourceFileLoader("cp", os.path.join(os.path.dirname(__file__), "..", "claude-persist"))
cp = importlib.util.module_from_spec(importlib.util.spec_from_loader("cp", _loader))
_loader.exec_module(cp)


def fake_claude(*args):
    """Start a process whose argv is .../claude -c '...' <args>, like an interactive Claude Code with those flags."""
    d = os.path.join(ROOT, "bin")
    os.makedirs(d, exist_ok=True)
    if not os.path.exists(f"{d}/claude"):
        os.symlink(shutil.which("bash"), f"{d}/claude")
    p = subprocess.Popen([f"{d}/claude", "-c", "sleep 300; :", *args], start_new_session=True)
    time.sleep(0.1)
    return p


def stop(p):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(p.pid, signal.SIGKILL)
    p.wait()


def reset_state():
    shutil.rmtree(cp.STATE, ignore_errors=True)
    shutil.rmtree(cp.CLAUDE_DIR, ignore_errors=True)
    os.makedirs(cp.PIDS)
    os.makedirs(os.path.join(cp.CLAUDE_DIR, "sessions"))


def tmux(*a):
    return subprocess.run(["tmux", "-L", SOCK, *a], capture_output=True, text=True, check=False)


def read(path):
    with open(path) as f:
        return f.read()


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


class Parsing(unittest.TestCase):
    def test_claude_detection(self):
        self.assertEqual(cp.claude_args(["claude", "--model", "x"]), ["--model", "x"])
        self.assertEqual(cp.claude_args(["/home/u/.local/bin/claude"]), [])
        self.assertEqual(cp.claude_args(["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", "-c"]), ["-c"])
        for argv in (["claude", "-p", "hi"], ["claude", "--print"], ["claude", "daemon", "run"], ["claude", "mcp"],
                     ["bash"], [], ["node", "server.js"]):
            self.assertIsNone(cp.claude_args(argv), argv)
        self.assertEqual(cp.claude_argv(["claude", "-p", "x"]), ["-p", "x"])     # any Claude, used by the hook walk

    def test_flags(self):
        kept, dropped = cp.carried_flags(["--dangerously-skip-permissions", "--resume", "abc", "--model", "opus",
                                          "--append-system-prompt", "- be terse", "--add-dir", "a", "b", "--verbose",
                                          "--worktree", "w", "--name", "n", "fix the bug"])
        self.assertEqual(kept, ["--dangerously-skip-permissions", "--model", "opus", "--append-system-prompt", "- be terse",
                                "--add-dir", "a", "b", "--verbose"])
        self.assertEqual(dropped, ["--worktree"])          # --resume/--name are expected to be dropped silently
        self.assertEqual(cp.carried_flags(["--model=haiku", "--tools=Bash"])[0], ["--model=haiku", "--tools=Bash"])


class Decisions(unittest.TestCase):
    def setUp(self):
        reset_state()
        os.makedirs(os.path.join(cp.CLAUDE_DIR, "projects", "-p"))
        write(os.path.join(cp.CLAUDE_DIR, "projects", "-p", "have.jsonl"), "")

    def c(self, sid, pane_command=False):
        return {"session_id": sid, "id_source": "hook", "flags": ["--model", "x y"], "dropped_flags": [], "pane_command": pane_command}

    def test_skip_reason(self):
        self.assertEqual(cp.skip_reason(self.c(None), set()), "session unknown")
        self.assertEqual(cp.skip_reason(self.c("have"), {"have"}), "already running elsewhere")
        self.assertEqual(cp.skip_reason(self.c("gone"), set()), "no saved conversation")
        self.assertIsNone(cp.skip_reason(self.c("have"), set()))

    def test_pane_command(self):
        live = set()
        why, tail = cp.pane_command({"cwd": "/a b", "claude": self.c("have")}, live)
        self.assertIsNone(why)
        outer = shlex.split(tail[0])                        # exec <shell> -lic '<inner>'
        self.assertEqual((outer[0], outer[2]), ("exec", "-lic"))
        self.assertTrue(outer[3].startswith("cd '/a b' && claude --model 'x y' --resume have ; exec "), outer[3])
        self.assertEqual(cp.pane_command({"cwd": "/", "claude": self.c("have")}, live)[0], "already running elsewhere")
        _, tail = cp.pane_command({"cwd": "/", "claude": self.c("have", pane_command=True)}, set())
        self.assertIn(" || exec ", shlex.split(tail[0])[3])    # a pane that was Claude closes on a normal exit
        self.assertEqual(cp.pane_command({"cwd": "/", "claude": None}, set()), (None, []))


class Install(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(dir=ROOT)

    def merge(self, path):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cp.merge_hooks(path, "X")

    def test_merge_refuses_unparseable(self):
        f = os.path.join(self.d, "bad.json")
        write(f, '{"model": "opus",}')
        self.assertFalse(self.merge(f))
        self.assertEqual(read(f), '{"model": "opus",}')

    def test_merge_keeps_and_is_idempotent(self):
        f = os.path.join(self.d, "s.json")
        write(f, json.dumps({"model": "opus", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "other"}]}]}}))
        self.assertTrue(self.merge(f))
        self.assertTrue(self.merge(f))
        d = json.loads(read(f))
        self.assertEqual(d["model"], "opus")
        self.assertEqual([h["command"] for g in d["hooks"]["SessionStart"] for h in g["hooks"]], ["other", "X hook start"])
        self.assertEqual(len(d["hooks"]["SessionEnd"]), 1)

    def test_merge_creates_missing(self):
        f = os.path.join(self.d, "new.json")
        self.assertTrue(self.merge(f))
        self.assertEqual(set(json.loads(read(f))["hooks"]), {"SessionStart", "SessionEnd"})

    def test_units(self):
        cp.write_units(self.d, "/opt/cp")
        snap = read(os.path.join(self.d, "claude-persist-snapshot.service"))
        self.assertIn("ExecStart=/opt/cp snapshot", snap)
        self.assertIn("After=claude-persist-restore.service", snap)
        self.assertIn("ExecStart=/opt/cp restore", read(os.path.join(self.d, "claude-persist-restore.service")))


class Hook(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.proc = fake_claude()
        self.env = dict(os.environ, CLAUDE_PID=str(self.proc.pid))

    def tearDown(self):
        stop(self.proc)

    def hook(self, kind, payload, env=None):
        subprocess.run([sys.executable, _loader.path, "hook", kind], input=json.dumps(payload), text=True,
                       env=env or self.env, check=True)

    def record(self):
        return cp.load_json(os.path.join(cp.PIDS, f"{self.proc.pid}.json"))

    def test_start_then_deliberate_exit(self):
        self.hook("start", {"session_id": "S1", "source": "startup"})
        self.assertEqual(self.record(), {"session_id": "S1", "start": cp.start_ticks(self.proc.pid), "ended": False})
        self.assertEqual(cp.session_of(self.proc.pid), ("S1", "hook", False))
        self.hook("end", {"session_id": "S1", "reason": "other"})                 # killed: keep for restore
        self.assertFalse(self.record()["ended"])
        self.hook("end", {"session_id": "S1", "reason": "prompt_input_exit"})     # /exit: drop
        self.assertTrue(self.record()["ended"])

    def test_clear_follows_new_id(self):
        self.hook("start", {"session_id": "S1", "source": "startup"})
        self.hook("start", {"session_id": "S2", "source": "clear"})
        self.assertEqual(cp.session_of(self.proc.pid)[0], "S2")

    def test_ignores_non_interactive_and_uninstalled(self):
        self.hook("start", {"session_id": "S1"}, env=dict(os.environ, CLAUDE_PID=str(os.getpid())))   # not a Claude
        self.assertEqual(os.listdir(cp.PIDS), [])
        shutil.rmtree(cp.STATE)
        self.hook("start", {"session_id": "S1"})
        self.assertFalse(os.path.exists(cp.STATE))          # the hook never creates state itself

    def test_pid_reuse_guard(self):
        cp.write_json(os.path.join(cp.PIDS, f"{self.proc.pid}.json"), {"session_id": "OLD", "start": "1", "ended": False})
        self.assertIsNone(cp.hook_record(self.proc.pid))


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class SnapshotRestore(unittest.TestCase):
    def setUp(self):
        reset_state()
        tmux("kill-server")
        self.procs = []

    def tearDown(self):
        tmux("kill-server")
        for p in self.procs:
            stop(p)

    def build(self):
        d = tempfile.mkdtemp(dir=ROOT)
        tmux("new-session", "-d", "-s", "u1", "-x", "200", "-y", "50", "-c", d)
        tmux("new-window", "-d", "-t", "u1:2", "-n", "split", "-c", "/tmp")
        tmux("split-window", "-d", "-h", "-t", "u1:2", "-c", d)
        return d

    def snapshot(self):
        with redirect_stdout(io.StringIO()):
            cp.cmd_snapshot()
        return cp.load_json(cp.SNAP)

    def test_snapshot_records_hook_session_and_skips_deliberate_exit(self):
        self.build()
        claude = fake_claude("--model", "opus")
        self.procs.append(claude)
        tmux("new-window", "-d", "-t", "u1:5", "-n", "cl", "exec sleep 300")    # placeholder pane we swap claude into
        pane_pid = int(tmux("display-message", "-p", "-t", "u1:5", "#{pane_pid}").stdout)
        cp.write_json(os.path.join(cp.PIDS, f"{claude.pid}.json"), {"session_id": "S9", "start": cp.start_ticks(claude.pid), "ended": False})
        orig = cp.claude_in_pane
        cp.claude_in_pane = lambda pid: claude.pid if pid == pane_pid else orig(pid)
        try:
            c = next(p["claude"] for _, w, p in cp.iter_panes(self.snapshot()) if w["index"] == 5)
            self.assertEqual((c["session_id"], c["id_source"], c["flags"]), ("S9", "hook", ["--model", "opus"]))
            cp.write_json(os.path.join(cp.PIDS, f"{claude.pid}.json"), {"session_id": "S9", "start": cp.start_ticks(claude.pid), "ended": True})
            self.assertIsNone(next(p["claude"] for _, w, p in cp.iter_panes(self.snapshot()) if w["index"] == 5))
        finally:
            cp.claude_in_pane = orig

    def test_roundtrip_windows_layout_and_guards(self):
        d = self.build()
        layout = tmux("display-message", "-p", "-t", "u1:2", "#{window_layout}").stdout.strip()
        snap = self.snapshot()
        self.assertEqual([w["index"] for w in snap["sessions"][0]["windows"]], [0, 2])
        mtime = os.path.getmtime(cp.SNAP)
        time.sleep(1.1)
        self.snapshot()
        self.assertFalse(os.path.exists(cp.PREV))           # unchanged: not rewritten, no rotation
        self.assertGreater(os.path.getmtime(cp.SNAP), mtime)
        tmux("kill-server")
        self.snapshot()                                     # no server: the good snapshot stays
        self.assertEqual(cp.load_json(cp.SNAP)["sessions"][0]["name"], "u1")
        # a recorded Claude pane with no saved conversation is reported and comes back as a shell
        snap["sessions"][0]["windows"][0]["panes"][0]["claude"] = {"session_id": "GONE", "id_source": "hook", "flags": [],
                                                                  "dropped_flags": [], "pane_command": False}
        cp.write_json(cp.SNAP, snap)
        out = io.StringIO()
        with redirect_stdout(out):
            cp.cmd_restore(False)
        self.assertIn("restored 1 tmux session(s); Claude resumed 0/0", out.getvalue())
        self.assertIn("(no saved conversation)", out.getvalue())
        self.assertEqual(tmux("list-windows", "-t", "u1", "-F", "#{window_index}:#{window_name}").stdout.split(), ["0:" + snap["sessions"][0]["windows"][0]["name"], "2:split"])
        self.assertEqual(geometry(layout), geometry(tmux("display-message", "-p", "-t", "u1:2", "#{window_layout}").stdout))
        self.assertEqual(tmux("display-message", "-p", "-t", "u1:0", "#{pane_current_path}").stdout.strip(), d)
        out = io.StringIO()
        with redirect_stdout(out):
            cp.cmd_restore(True)
        self.assertIn("skip session u1: already exists", out.getvalue())


def geometry(layout):
    """A tmux layout string without its checksum and pane ids: the sizes and positions only."""
    import re  # noqa: PLC0415
    return re.sub(r",\d+([},\]]|$)", r"\1", layout.strip().split(",", 1)[1])


def tearDownModule():
    tmux("kill-server")
    shutil.rmtree(ROOT, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
