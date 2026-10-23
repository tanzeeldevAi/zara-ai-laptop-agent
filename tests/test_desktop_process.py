"""AgentProcess: state transitions with a fake child and a real sleeper."""

import subprocess
import sys
import time

from desktop.agent_process import AgentProcess


class FakeProc:
    def __init__(self, pid=4321):
        self.pid = pid
        self._rc = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._rc

    def terminate(self):
        self.terminated = True
        self._rc = -15

    def kill(self):
        self.killed = True
        self._rc = -9

    def wait(self, timeout=None):
        return self._rc


def _factory(proc, captured):
    def popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return proc
    return popen


def test_start_runs_runpy_and_tracks_state(tmp_path):
    captured = {}
    proc = FakeProc()
    agent = AgentProcess(root=tmp_path, popen=_factory(proc, captured))
    assert agent.state == "stopped"
    assert agent.start() is True
    assert captured["cmd"][1] == "run.py"
    assert str(captured["kwargs"]["cwd"]) == str(tmp_path)
    assert agent.pid == 4321
    assert agent.state == "running"
    # second start is a no-op
    assert agent.start() is False


def test_stop_terminates_and_records_exit(tmp_path):
    proc = FakeProc()
    agent = AgentProcess(root=tmp_path, popen=_factory(proc, {}))
    agent.start()
    assert agent.stop() is True
    assert proc.terminated is True
    assert agent.exit_code == -15
    assert agent.state == "stopped"
    assert agent.stop() is False  # already stopped


def test_child_exit_is_detected_by_poll(tmp_path):
    proc = FakeProc()
    agent = AgentProcess(root=tmp_path, popen=_factory(proc, {}))
    agent.start()
    proc._rc = 3
    assert agent.poll() == "stopped"
    assert agent.exit_code == 3


def test_real_sleeper_child(tmp_path):
    def popen(cmd, **kwargs):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"),
        )

    agent = AgentProcess(root=tmp_path, popen=popen)
    assert agent.start() is True
    assert agent.state == "running"
    started = time.time()
    assert agent.stop(timeout=3) is True
    assert time.time() - started < 10
    assert agent.state == "stopped"
    assert (tmp_path / ".runtime" / "agent-shell.log").exists()
