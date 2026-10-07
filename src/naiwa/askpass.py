"""OpenSSH askpass companion: a small Naiwa card, stdout only to SSH's pipe."""
from __future__ import annotations

import os
from pathlib import Path
import sys

STYLE = """
QDialog { background: #17272c; color: #eee2bc; }
QLabel { color: #eee2bc; font-size: 13px; }
QLabel#heading { color: #66cec1; font-size: 19px; font-weight: bold; }
QLabel#hint { color: #a4b9b8; font-size: 12px; }
QLineEdit, QComboBox { background: #24363b; color: #eee2bc;
    border: 2px solid #657773; padding: 8px; font-size: 15px; }
QPushButton { background: #66cec1; color: #14272b;
    border: 2px solid #a4b9b8; padding: 7px 18px; font-weight: bold; }
QPushButton:disabled { background: #35464a; color: #879a9a; }
QPushButton#cancel { background: #263a3f; color: #eee2bc; }
"""


def card_font():
    from PySide6.QtGui import QFont, QFontDatabase
    if os.name == "nt" and "Microsoft YaHei" not in QFontDatabase.families():
        font = Path(os.environ.get("WINDIR", "C:/Windows"))/"Fonts"/"msyh.ttc"
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
    return QFont("Microsoft YaHei", 10)


def helper_path() -> Path:
    """Use the GUI launcher installed by pip, not a shell command or batch file."""
    filename = "naiwa-askpass.exe" if os.name == "nt" else "naiwa-askpass"
    directories = [Path(sys.executable).parent, Path(sys.prefix)/"Scripts", Path(sys.prefix)/"bin"]
    for directory in directories:
        candidate = directory/filename
        if candidate.is_file():
            return candidate
    raise OSError("请重新安装奶蛙，以安装图形登录组件 naiwa-askpass")


def environment(host: str, base: dict | None = None) -> dict:
    values = dict(os.environ if base is None else base)
    values.update(SSH_ASKPASS=str(helper_path()), SSH_ASKPASS_REQUIRE="force",
                  NAIWA_AUTH_HOST=host)
    return values


def write_answer(answer: str) -> None:
    data = (answer + "\n").encode("utf-8")
    if len(data) > 1000 or "\n" in answer or "\r" in answer:
        raise ValueError("Invalid authentication response")
    if os.name == "nt":
        # pythonw deliberately has no sys.stdout. OpenSSH supplies a native
        # anonymous output pipe; write to that handle without opening a console.
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [w.DWORD]
        kernel.GetStdHandle.restype = w.HANDLE
        kernel.WriteFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD,
                                    ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        handle = kernel.GetStdHandle(w.DWORD(-11))
        written = w.DWORD()
        buffer = ctypes.create_string_buffer(data)
        try:
            if not kernel.WriteFile(handle, buffer, len(data), ctypes.byref(written), None) or written.value != len(data):
                raise OSError("OpenSSH response pipe unavailable")
        finally:
            ctypes.memset(buffer, 0, len(buffer))
    else:
        os.write(1, data)


def create_dialog(host: str, prompt: str, confirm=False):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                                  QPushButton, QVBoxLayout)
    dialog = QDialog()
    dialog.setFont(card_font())
    dialog.setWindowTitle("奶蛙 · SSH 登录")
    dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
    dialog.setStyleSheet(STYLE)
    dialog.setMinimumWidth(420)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 18, 20, 18)
    heading = QLabel("奶蛙 / 连接确认" if confirm else "奶蛙 / SSH 登录")
    heading.setObjectName("heading")
    layout.addWidget(heading)
    label = QLabel("连接：" + host)
    label.setTextFormat(Qt.TextFormat.PlainText)
    layout.addWidget(label)
    request = QLabel(prompt[:2048])
    request.setTextFormat(Qt.TextFormat.PlainText)
    request.setWordWrap(True)
    request.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    layout.addWidget(request)
    field = QLineEdit()
    field.setObjectName("credential")
    field.setEchoMode(QLineEdit.EchoMode.Password)
    field.setMaxLength(240)
    field.setPlaceholderText("请输入密码或私钥口令")
    if not confirm:
        layout.addWidget(field)
    hint = QLabel("仅用于这次连接；不保存、不记入日志。取消后可在奶蛙里重试。")
    hint.setObjectName("hint")
    hint.setWordWrap(True)
    layout.addWidget(hint)
    buttons = QHBoxLayout()
    buttons.addStretch()
    cancel = QPushButton("取消")
    cancel.setObjectName("cancel")
    cancel.clicked.connect(dialog.reject)
    submit = QPushButton("确认连接" if confirm else "登录")
    submit.setDefault(True)
    submit.clicked.connect(dialog.accept)
    if not confirm:
        submit.setEnabled(False)
        field.textChanged.connect(lambda text: submit.setEnabled(bool(text)))
        field.returnPressed.connect(lambda: dialog.accept() if field.text() else None)
    buttons.addWidget(cancel)
    buttons.addWidget(submit)
    layout.addLayout(buttons)
    dialog.credential = field
    if not confirm:
        field.setFocus()
    return dialog


def main() -> int:
    # Never print diagnostics: stdout is the authentication response channel.
    if os.environ.get("SSH_ASKPASS_PROMPT") == "none":
        return 1
    from PySide6.QtWidgets import QApplication, QDialog
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("奶蛙")
    prompt = sys.argv[1] if len(sys.argv) > 1 else "OpenSSH 请求身份验证"
    confirm = os.environ.get("SSH_ASKPASS_PROMPT") == "confirm" or "are you sure" in prompt.casefold()
    dialog = create_dialog(os.environ.get("NAIWA_AUTH_HOST", "SSH"), prompt, confirm)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return 1
        answer = "yes" if confirm else dialog.credential.text()
        dialog.credential.clear()
        write_answer(answer)
        del answer
        return 0
    except (OSError, ValueError):
        return 1
    finally:
        dialog.credential.clear()
        dialog.close()


if __name__ == "__main__":
    raise SystemExit(main())
