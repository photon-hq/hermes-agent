from __future__ import annotations

from types import SimpleNamespace

from gateway import status


def test_collect_descendant_pids_from_ps_fallback(monkeypatch):
    root_pid = 987654

    def fake_run(cmd, **kwargs):
        assert cmd == ["ps", "-eo", "pid=,ppid="]
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f"{root_pid} 1\n"
                f"{root_pid + 1} {root_pid}\n"
                f"{root_pid + 2} {root_pid + 1}\n"
                f"{root_pid + 3} 1\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(status, "_IS_WINDOWS", False)
    monkeypatch.setattr(status.subprocess, "run", fake_run)

    assert status._collect_descendant_pids(root_pid) == [root_pid + 1, root_pid + 2]


def test_terminate_process_tree_force_kills_children_before_parent(monkeypatch):
    calls = []

    monkeypatch.setattr(status, "_IS_WINDOWS", False)
    monkeypatch.setattr(status, "_collect_descendant_pids", lambda pid: [11, 12])
    monkeypatch.setattr(
        status,
        "terminate_pid",
        lambda pid, force=False: calls.append((pid, force)),
    )

    status.terminate_process_tree(10, force=True)

    assert calls == [(12, True), (11, True), (10, True)]


def test_terminate_process_tree_non_force_keeps_single_pid_semantics(monkeypatch):
    calls = []

    monkeypatch.setattr(status, "_IS_WINDOWS", False)
    monkeypatch.setattr(status, "_collect_descendant_pids", lambda pid: [11, 12])
    monkeypatch.setattr(
        status,
        "terminate_pid",
        lambda pid, force=False: calls.append((pid, force)),
    )

    status.terminate_process_tree(10, force=False)

    assert calls == [(10, False)]
