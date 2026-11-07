"""Fast tests for claude-persist: no network, no real Claude. Run:  python3 -m unittest discover -s tests -v
A fake "claude" (bash started under the name claude, ignoring its arguments) stands in for an interactive Claude."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = tempfile.mkdtemp(prefix="cp-unit-")
SOCK = f"cpunit-{os.getpid()}"
os.environ.update(CLAUDE_PERSIST_STATE=f"{ROOT}/state", CLAUDE_CONFIG_DIR=f"{ROOT}/claude", CLAUDE_PERSIST_SOCKET=SOCK,
                  CLAUDE_PERSIST_MARKER=f"{ROOT}/restored", CLAUDE_PERSIST_NOTIFY="0", CLAUDE_PERSIST_VERIFY_WAIT="1")
_loader = importlib.machinery.SourceFileLoader("cp", os.path.join(os.path.dirname(__file__), "..", "claude-persist"))
cp = importlib.util.module_from_spec(importlib.util.spec_from_loader("cp", _loader))
_loader.exec_module(cp)


def fake_claude_cmd(session_id=None, *args):
    """Command line of a stand-in Claude: bash run under the name `claude`, ignoring its arguments, optionally calling
    the real SessionStart hook for itself first (as Claude Code would)."""
    d = os.path.join(ROOT, "bin")
    os.makedirs(d, exist_ok=True)
    if not os.path.exists(f"{d}/claude"):
        os.symlink(shutil.which("bash"), f"{d}/claude")
    hook = (f"CLAUDE_PID=$$ {shlex.quote(sys.executable)} {shlex.quote(_loader.path)} hook start "
            f"<<< {shlex.quote(json.dumps({'session_id': session_id}))}; " if session_id else "")
    return [f"{d}/claude", "-c", f"{hook}sleep 300; :", *args]


def fake_claude(*args):
    p = subprocess.Popen(fake_claude_cmd(None, *args), start_new_session=True)
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
    cp.touch_marker()                                   # as if restore already ran this boot


tmux = cp.tmux                                          # the module already targets the test socket


def captured(fn, *a):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(*a)
    return out.getvalue()


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
        self.assertEqual(cp.any_claude_args(["claude", "-p", "x"]), ["-p", "x"])  # any Claude, used by the hook walk

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
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
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

    def test_unrestored_snapshot_only_before_this_boot(self):
        f = os.path.join(self.d, "snapshot.json")
        write(f, "{}")
        self.assertFalse(cp.unrestored_snapshot(f, ["false"]))          # written this boot: current, not unrestored
        old = cp.boot_time() - 3600
        os.utime(f, (old, old))
        self.assertTrue(cp.unrestored_snapshot(f, ["false"]))           # from before boot, restore unit not active
        self.assertFalse(cp.unrestored_snapshot(f, ["true"]))           # restore unit active this boot
        self.assertFalse(cp.unrestored_snapshot(os.path.join(self.d, "none.json"), ["false"]))

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
        self.assertEqual(cp.hook_record(self.proc.pid), {})


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class SnapshotRestore(unittest.TestCase):
    def setUp(self):
        reset_state()
        tmux("kill-server")

    def tearDown(self):
        tmux("kill-server")

    def build(self):
        d = tempfile.mkdtemp(dir=ROOT)
        tmux("new-session", "-d", "-s", "u1", "-x", "200", "-y", "50", "-c", d)
        tmux("new-window", "-d", "-t", "u1:2", "-n", "split", "-c", "/tmp")
        tmux("split-window", "-d", "-h", "-t", "u1:2", "-c", d)
        return d

    def snapshot(self):
        captured(cp.cmd_snapshot)
        return cp.load_json(cp.SNAP)

    def pane(self, index):
        return next(p["claude"] for _, w, p in cp.iter_panes(self.snapshot()) if w["index"] == index)

    def wait_record(self, target):
        for _ in range(50):
            cpid, _sid, source = cp.pane_session(cp.pane_pid_of(target))
            if source == "hook":
                return cpid
            time.sleep(0.1)
        raise self.failureException(f"no hook record for the stand-in Claude in {target}")

    def test_finds_claude_in_panes_both_ways(self):
        """No mocking: the stand-in runs inside real panes and calls the real hook."""
        self.build()
        tmux("new-window", "-d", "-t", "u1:5", "-n", "typed", "bash --norc --noprofile -i")
        tmux("send-keys", "-t", "u1:5", shlex.join(fake_claude_cmd("S5", "--model", "opus")), "Enter")
        tmux("new-window", "-d", "-t", "u1:6", "-n", "cmd", shlex.join(fake_claude_cmd("S6")))
        typed = self.wait_record("u1:5")
        self.wait_record("u1:6")
        c5, c6 = self.pane(5), self.pane(6)
        self.assertEqual((c5["session_id"], c5["id_source"], c5["flags"], c5["pane_command"]), ("S5", "hook", ["--model", "opus"], False))
        self.assertEqual((c6["session_id"], c6["pane_command"]), ("S6", True))
        subprocess.run([sys.executable, _loader.path, "hook", "end"], input='{"reason": "prompt_input_exit"}', text=True,
                       env=dict(os.environ, CLAUDE_PID=str(typed)), check=True)
        self.assertIsNone(self.pane(5))                 # a deliberate /exit is not brought back

    def test_roundtrip_windows_layout_and_guards(self):
        d = self.build()
        layout = tmux("display-message", "-p", "-t", "u1:2", "#{window_layout}").stdout
        snap = self.snapshot()
        self.assertEqual([w["index"] for w in snap["sessions"][0]["windows"]], [0, 2])
        mtime = os.path.getmtime(cp.SNAP)
        time.sleep(1.1)
        self.snapshot()
        self.assertFalse(os.path.exists(cp.PREV))       # unchanged: not rewritten, no rotation
        self.assertGreater(os.path.getmtime(cp.SNAP), mtime)
        tmux("kill-server")
        os.remove(cp.MARKER)
        self.snapshot()                                 # restore has not run this boot: the snapshot is kept
        self.assertEqual(cp.load_json(cp.SNAP)["sessions"][0]["name"], "u1")
        # a recorded Claude pane with no saved conversation is reported and comes back as a shell
        snap["sessions"][0]["windows"][0]["panes"][0]["claude"] = {"session_id": "GONE", "id_source": "hook", "flags": [],
                                                                  "dropped_flags": [], "pane_command": False}
        cp.write_json(cp.SNAP, snap)
        out = captured(cp.cmd_restore, False)
        self.assertTrue(os.path.exists(cp.MARKER))      # restore enables snapshots for this boot
        self.assertIn("restored 1 tmux session(s); Claude resumed 0/0", out)
        self.assertIn("(no saved conversation)", out)
        self.assertEqual(tmux("list-windows", "-t", "u1", "-F", "#{window_index}:#{window_name}").stdout.split(),
                         [f"{w['index']}:{w['name']}" for w in snap["sessions"][0]["windows"]])
        self.assertEqual(geometry(layout), geometry(tmux("display-message", "-p", "-t", "u1:2", "#{window_layout}").stdout))
        self.assertEqual(tmux("display-message", "-p", "-t", "u1:0", "#{pane_current_path}").stdout.strip(), d)
        self.assertIn("skip session u1: already exists", captured(cp.cmd_restore, True))
        tmux("kill-server")
        self.assertEqual(self.snapshot()["sessions"], [])   # after restore, closing everything is remembered


def geometry(layout):
    """A tmux layout string without its checksum and pane ids: the sizes and positions only."""
    return re.sub(r",\d+([},\]]|$)", r"\1", layout.strip().split(",", 1)[1])


def tearDownModule():
    tmux("kill-server")
    shutil.rmtree(ROOT, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
