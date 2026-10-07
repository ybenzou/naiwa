import io
import json

from PySide6.QtWidgets import QApplication, QDialog, QLineEdit

from naiwa import askpass, remote
from naiwa.connections import ConnectionsDialog


def test_dialog_masks_input_and_cancel_sends_nothing(monkeypatch):
    app = QApplication.instance() or QApplication([])
    dialog = askpass.create_dialog("example-server", "Password:")
    assert dialog.credential.echoMode() == QLineEdit.EchoMode.Password
    dialog.credential.setText("synthetic-test-value")
    monkeypatch.setattr(askpass, "create_dialog", lambda *args: dialog)
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    sent = []
    monkeypatch.setattr(askpass, "write_answer", sent.append)
    assert askpass.main() == 1
    assert not sent and not dialog.credential.text()


def test_gui_authentication_answers_only_to_ssh_and_clears_field(monkeypatch):
    app = QApplication.instance() or QApplication([])
    dialog = askpass.create_dialog("example-server", "Password:")
    dialog.credential.setText("synthetic-test-value")
    monkeypatch.setattr(askpass, "create_dialog", lambda *args: dialog)
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    sent = []
    monkeypatch.setattr(askpass, "write_answer", sent.append)
    assert askpass.main() == 0
    assert sent == ["synthetic-test-value"]
    assert not dialog.credential.text()


def test_gui_receiver_does_not_reprompt_when_login_cancelled(tmp_path, monkeypatch):
    calls = []
    class Child:
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        def wait(self, **kwargs):
            return 255
        def poll(self):
            return 255
    monkeypatch.setattr(askpass, "helper_path", lambda: tmp_path/"askpass.exe")
    monkeypatch.setattr(remote, "console_visible", lambda *args: (_ for _ in ()).throw(AssertionError("Console opened")))
    monkeypatch.setattr(remote, "link_event", lambda *args: None)
    monkeypatch.setattr(remote.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)) or Child())
    remote.receive("example-server", tmp_path, interactive=True, gui=True)
    assert len(calls) == 1
    args, options = calls[0]
    assert "NumberOfPasswordPrompts=1" in args
    assert options["creationflags"] == remote.subprocess.CREATE_NO_WINDOW
    assert options["env"]["SSH_ASKPASS_REQUIRE"] == "force"
    assert options["stderr"] == remote.subprocess.DEVNULL
    health = next(tmp_path.glob("remote-*-health.json")).read_text()
    assert json.loads(health)["state"] == "stopped"
    assert "synthetic-test-value" not in health


def test_connection_card_retries_only_selected_host(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    (tmp_path/"remotes.json").write_text(json.dumps({"hosts": [
        {"host": "example-server"}, {"host": "other-server"}]}), encoding="utf-8")
    calls = []
    monkeypatch.setattr("naiwa.connections.start_configured", lambda root, only_host=None: calls.append(only_host))
    dialog = ConnectionsDialog(tmp_path)
    dialog.hosts.setCurrentIndex(1)
    dialog.connect_host()
    assert calls == ["other-server"]
    dialog.timer.stop()
    dialog.close()


def test_legacy_listener_switch_is_an_explicit_action(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    (tmp_path/"remotes.json").write_text(json.dumps({"hosts": [{"host": "example-server", "interactive": True}]}))
    (tmp_path/("remote-"+remote.host_id("example-server")+"-health.json")).write_text(json.dumps({
        "state": "connected", "ts": __import__('time').time(), "host": "example-server", "pid": 123}))
    class Held:
        def __init__(self,*args,**kwargs):
            pass
        def __enter__(self):
            raise TimeoutError
        def __exit__(self,*args):
            pass
    monkeypatch.setattr("naiwa.connections.BusLock", Held)
    calls = []
    monkeypatch.setattr("naiwa.connections.stop_local_receiver", lambda root, host: calls.append(("stop", host)))
    monkeypatch.setattr("naiwa.connections.start_configured", lambda root, only_host=None: calls.append(("start", only_host)))
    dialog = ConnectionsDialog(tmp_path)
    assert not calls and dialog.retry.text() == "切换图形登录"
    dialog.connect_host()
    assert calls == [("stop", "example-server"), ("start", "example-server")]
    dialog.timer.stop()
    dialog.close()
