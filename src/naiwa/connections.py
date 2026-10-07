"""Local-only connection controls. No passwords, remote setup, or shell commands."""
from __future__ import annotations

import json
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from naiwa.askpass import STYLE, card_font
from naiwa.bus import BusLock, runtime_dir
from naiwa.remote import host_id, start_configured, stop_local_receiver


class ConnectionsDialog(QDialog):
    def __init__(self, root):
        super().__init__()
        self.setFont(card_font())
        self.root = runtime_dir(root)
        self.setWindowTitle("奶蛙 · SSH 连接")
        self.setMinimumWidth(400)
        self.setStyleSheet(STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        title = QLabel("奶蛙 / SSH 连接")
        title.setObjectName("heading")
        layout.addWidget(title)
        self.hosts = QComboBox()
        try:
            entries = json.loads((self.root/"remotes.json").read_text(encoding="utf-8")).get("hosts", [])
        except (OSError, ValueError, AttributeError):
            entries = []
        self.password_hosts = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                host_id(entry.get("host", ""))
            except (ValueError, TypeError):
                continue
            self.hosts.addItem(entry["host"])
            if entry.get("interactive") is True:
                self.password_hosts.add(entry["host"])
        layout.addWidget(self.hosts)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setObjectName("hint")
        layout.addWidget(self.hint)
        buttons = QHBoxLayout()
        self.retry = QPushButton("连接 / 重试")
        self.retry.clicked.connect(self.connect_host)
        close = QPushButton("关闭")
        close.setObjectName("cancel")
        close.clicked.connect(self.close)
        buttons.addWidget(self.retry)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.hosts.currentIndexChanged.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self):
        host = self.hosts.currentText()
        if not host:
            self.status.setText("尚未配置 SSH 监听")
            self.retry.setEnabled(False)
            return
        identity = host_id(host)
        running = False
        try:
            with BusLock(self.root/("remote-"+identity+".lock"), timeout=.001):
                pass
        except TimeoutError:
            running = True
        try:
            health = json.loads((self.root/("remote-"+identity+"-health.json")).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            health = {}
        if not isinstance(health, dict):
            health = {}
        self.legacy = running and host in self.password_hosts and health.get("auth_ui") != "gui"
        recent = time.time()-health.get("ts", 0) < 15
        if running and recent and health.get("state") == "connected":
            text = "● 已连接 · 正在监控"
        elif running and health.get("state") == "connecting":
            text = "● 正在连接 · 请查看登录卡片"
        elif running:
            text = "● 监听进程仍在运行"
        else:
            text = "○ 未连接 · 可以重试"
        self.status.setText(text)
        self.retry.setText("切换图形登录" if self.legacy else "连接 / 重试")
        self.retry.setEnabled(not running or self.legacy)
        self.hint.setText("旧监听连接继续保留。点击切换后将重新登录一次，使用奶蛙卡片。" if self.legacy
                          else "需要密码时显示奶蛙登录卡片。取消或断线后，点击连接／重试即可。")

    def connect_host(self):
        self.retry.setEnabled(False)
        try:
            if getattr(self, "legacy", False):
                stop_local_receiver(self.root, self.hosts.currentText())
            start_configured(self.root, only_host=self.hosts.currentText())
            self.status.setText("正在连接…")
        except (OSError, ValueError):
            self.status.setText("启动失败，请检查奶蛙是否完整安装")

