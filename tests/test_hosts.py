from naiwa import hosts


def test_host_identity_excludes_cli_and_non_codex_editor_children():
    processes = {10: (0, "code.exe"), 20: (10, "codex.exe"), 30: (10, "powershell.exe"),
                 40: (30, "codex.exe"), 50: (10, "python.exe"), 60: (10, "code.exe")}
    assert hosts._is_host(20, processes)
    assert not hosts._is_host(40, processes)
    assert not hosts._is_host(50, processes)
    assert not hosts._is_host(60, processes)


def test_each_instance_needs_its_own_observation_and_pid_reuse_is_not_verified(monkeypatch):
    first = {"pid": 20, "started_at": 10, "extension_version": "v1"}
    second = {"pid": 30, "started_at": 20, "extension_version": "v2"}
    monkeypatch.setattr(hosts, "running_hosts", lambda: [first, second])
    rows = [{"source": "codex", "origin": "vscode", "observed": True,
             "producer": first, "workspace_name": "workspace-a", "ts": "now"},
            {"source": "codex", "origin": "vscode", "observed": True,
             "producer": {**second, "started_at": 5}, "workspace_name": "old", "ts": "before"}]
    result = hosts.coverage(rows)
    assert result[0]["observer_seen"] and result[0]["workspaces"] == ["workspace-a"]
    assert not result[1]["observer_seen"] and not result[1]["workspaces"]
