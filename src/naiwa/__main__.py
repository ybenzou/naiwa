"""Desktop, isolated preview, hooks installation, and diagnostics CLI."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from naiwa import __version__
from naiwa.bus import runtime_dir
from naiwa.install import (default_backup_dir, default_codex_path, default_cursor_path,
    install_codex, install_cursor, preview_install, uninstall_codex, uninstall_cursor)


def _install(root: Path | None, dry_run: bool, source: str = "all") -> int:
    # Validate every requested source before changing an existing config.
    sources = ("cursor", "codex") if source == "all" else (source,)
    executable = sys.executable
    paths = {"cursor":default_cursor_path(), "codex":default_codex_path()}
    plans = {s:preview_install(paths[s], s, executable, root) for s in sources}
    if dry_run:
        print(json.dumps(plans, ensure_ascii=False, indent=2))
        return 0
    backup = default_backup_dir()
    if "cursor" in sources:
        install_cursor(default_cursor_path(), executable, backup, root)
    if "codex" in sources:
        install_codex(default_codex_path(), executable, backup, root)
    for selected in sources:
        print(f"{selected.title()}: {paths[selected]}")
    print(f"事件总线: {runtime_dir(root)}")
    if "codex" in sources:
        print("Codex 的用户级钩子必须在 /hooks 中审查并信任。之后用 probe --watch 60 验证真实扩展回合。")
    return 0


def _detach_desktop(argv: list[str]) -> bool:
    """Leave the launching terminal free. The password console is a separate window."""
    if os.name != "nt" or os.environ.get("NAIWA_DETACHED") == "1":
        return False
    env = os.environ.copy()
    env["NAIWA_DETACHED"] = "1"
    subprocess.Popen(
        [sys.executable, "-m", "naiwa", *argv],
        env=env,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def _run(root: Path | None, placeholder: bool, zoom: int, demo: bool) -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QActionGroup, QIcon, QPixmap
    from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
    from naiwa.demo import SCENARIOS, demo_machine
    from naiwa.sprites import pose_frames
    from naiwa.window import NaiwaWindow, _image

    base = runtime_dir(root)
    if os.environ.get("NAIWA_DETACHED") != "1":
        from PySide6.QtCore import QLockFile
        held = QLockFile(str(base / ("demo.lock" if demo else "desktop.lock")))
        held.setStaleLockTime(0)
        if not held.tryLock(50):
            print("奶娃已经在运行")
            return 0
        held.unlock()
        if _detach_desktop(sys.argv[1:]):
            print("奶娃已在后台运行，这个终端可以继续用。弹出的窗口里再输入一次服务器密码。")
            return 0
    app = QApplication([sys.argv[0]])
    app.setApplicationName("奶娃")
    app.setQuitOnLastWindowClosed(False)
    # QLockFile arbitrates startup as well as the running process, avoiding a socket race.
    from PySide6.QtCore import QLockFile
    lock = QLockFile(str(base / ("demo.lock" if demo else "desktop.lock")))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        print("奶娃已经在运行")
        return 0
    from naiwa.motion import prepare_animation
    prepare_animation()
    window = NaiwaWindow(root=base, placeholder=placeholder, zoom=zoom, demo=demo)
    app.aboutToQuit.connect(window.close)
    tray = QSystemTrayIcon(QIcon(QPixmap.fromImage(_image(pose_frames("idle")[0], 2))), app)
    menu = QMenu()
    menu.addAction("奶娃说两句", window.toggle_details)
    menu.addAction("回到主屏", window.reset_position)
    sizes = menu.addMenu("大小")
    group = QActionGroup(sizes)
    for size in (1, 2, 3):
        action = sizes.addAction(f"{size} 倍")
        action.setCheckable(True)
        action.setChecked(window._zoom == size)
        group.addAction(action)
        action.triggered.connect(lambda checked=False, value=size: window.set_zoom(value))
        window.scaleChanged.connect(lambda value, a=action, own=size: a.setChecked(value == own))
    menu.addSeparator()
    menu.addAction("退出", app.quit)
    tray.setContextMenu(menu)
    tray.setToolTip("奶娃 · 演示" if demo else "奶娃")
    tray.activated.connect(lambda reason: window.toggle_details() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()
    if demo:
        state = [0]
        def advance_demo():
            phase = SCENARIOS[state[0] % len(SCENARIOS)]
            window.machine = demo_machine(phase)
            window._sync()
            state[0] += 1
        demo_timer = QTimer(app)
        demo_timer.setInterval(3600)
        demo_timer.timeout.connect(advance_demo)
        demo_timer.start()
        advance_demo()
    if not demo and os.environ.get("NAIWA_NO_REMOTE_START") != "1":
        from naiwa.remote import start_configured
        start_configured(base)
    window.show()
    code = app.exec()
    lock.unlock()
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="奶娃：观察 Cursor 和 VS Code Codex 的像素桌宠")
    parser.add_argument("--version", action="version", version=f"naiwa {__version__}")
    parser.add_argument("--data-dir", type=Path, help="默认 %%USERPROFILE%%/.agent-pet 或 NAIWA_HOME")
    commands = parser.add_subparsers(dest="command")
    for name in ("run", "demo"):
        sub = commands.add_parser(name)
        sub.add_argument("--placeholder", action="store_true")
        sub.add_argument("--zoom", type=int, choices=(1, 2, 3), default=1)
        sub.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS)
    install = commands.add_parser("install")
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--source", choices=("all", "cursor", "codex"), default="all")
    install.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS)
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--source", choices=("all", "cursor", "codex"), default="all")
    for name in ("probe", "doctor"):
        sub = commands.add_parser(name)
        sub.add_argument("--watch", type=int, default=0)
        sub.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS)
    sheet = commands.add_parser("sheet")
    sheet.add_argument("--output", type=Path, default=Path.cwd()/"previews")
    remote = commands.add_parser("remote")
    remote.add_argument("remote_command", nargs="?", choices=("install", "uninstall"))
    remote.add_argument("--host", default="")
    remote.add_argument("--workspace", default="")
    remote.add_argument("--root", default="")
    remote.add_argument("--interactive", action="store_true")
    remote.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--placeholder", action="store_true")
    parser.add_argument("--zoom", type=int, choices=(1, 2, 3), default=1)
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            return _install(args.data_dir, args.dry_run, args.source)
        if args.command == "uninstall":
            if args.source in {"all", "cursor"}:
                uninstall_cursor(default_cursor_path())
            if args.source in {"all", "codex"}:
                uninstall_codex(default_codex_path())
            print("已移除奶娃的观察钩子；其他钩子保留。")
            return 0
        if args.command in {"probe", "doctor"}:
            from naiwa.probe import report, watch, write_environment
            print(write_environment(args.data_dir))
            if not 0 <= args.watch <= 3600:
                parser.error("--watch must be between 0 and 3600 seconds")
            print(json.dumps(watch(args.data_dir, args.watch) if args.watch else report(args.data_dir), ensure_ascii=False, indent=2))
            return 0
        if args.command == "remote":
            from naiwa.remote import main as remote_main
            forwarded = [args.remote_command] if args.remote_command else []
            if args.host:
                forwarded += ["--host", args.host]
            if args.workspace:
                forwarded += ["--workspace", args.workspace]
            if args.root:
                forwarded += ["--root", args.root]
            if args.interactive:
                forwarded.append("--interactive")
            data_dir = getattr(args, "data_dir", None)
            if data_dir is not None:
                forwarded += ["--data-dir", str(data_dir)]
            return remote_main(forwarded)
        if args.command == "sheet":
            from naiwa.sprites import export_assets
            export_assets(args.output)
            print(args.output.resolve()/"contact.png")
            return 0
        root = args.data_dir
        if args.command == "demo" and root is None:
            root = runtime_dir()/"demo"
        return _run(root, args.placeholder, args.zoom, args.command == "demo")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
