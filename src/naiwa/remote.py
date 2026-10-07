"""SSH transport for one Cursor workspace. OpenSSH keeps the credentials."""
from __future__ import annotations

import argparse
import base64
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import zipfile

from naiwa.bus import BusLock, append_event, append_events, atomic_json, read_snapshot, runtime_dir
from naiwa.schema import BusEvent
from naiwa.workspace import display_name

HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def host_id(host):
    if not HOST.fullmatch(host):
        raise ValueError("Use an SSH config host alias")
    return hashlib.sha256(host.encode()).hexdigest()[:16]


def bundle_bytes():
    result = io.BytesIO()
    base = Path(__file__).parent
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", "from naiwa.remote_agent import main\nmain()\n")
        for name in ("__init__", "remote_agent", "adapt", "allow", "hook_input", "schema", "workspace", "phase"):
            archive.writestr("naiwa/"+name+".py", (base/(name+".py")).read_bytes())
    return result.getvalue()


def imported_event(host, raw, *, received_at=None):
    event = BusEvent.from_dict(raw)
    if event.source != "cursor":
        raise ValueError("Remote collector accepts only Cursor")
    prefix = "ssh-"+host_id(host)+":"
    ids = {name: prefix+hashlib.sha256(getattr(event, name).encode()).hexdigest()[:32] if getattr(event, name) else ""
           for name in ("session_id", "turn_id", "tool_call_id", "subagent_id", "parent_id")}
    workspace = "ws-"+hashlib.sha256((prefix+event.workspace_id).encode()).hexdigest()[:24]
    # UI durations and debounce must use the receiving machine's clock.
    # No assumption about NTP or the server's time is required.
    return replace(event, **ids, seq=0, ts=received_at if received_at is not None else event.ts,
                   origin="ssh-"+host_id(host),
                   workspace_name=display_name((event.workspace_name or "工作区未知")+" · "+host),
                   workspace_id=workspace)


def console_visible(visible):
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetConsoleWindow.restype = wintypes.HWND
    user = ctypes.WinDLL("user32")
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    window = kernel.GetConsoleWindow()
    if window:
        user.ShowWindow(window, 5 if visible else 0)


BOOTSTRAP = """import base64,json,os,runpy,sys
from pathlib import Path
request=json.loads(sys.stdin.readline())
root=Path.home()/'.agent-pet/naiwa-remote'
root.mkdir(parents=True,exist_ok=True)
os.chmod(root,0o700)
bundle=root/'collector.pyz'
temporary=root/'collector.pyz.tmp'
temporary.write_bytes(base64.b64decode(request['bundle']))
os.chmod(temporary,0o600)
temporary.replace(bundle)
argv=[str(bundle), request.get('command') or 'stream', '--after', str(request.get('seq',0)), '--epoch', request.get('epoch','')]
if request.get('workspace'):
    argv += ['--workspace', request['workspace']]
if request.get('root'):
    argv += ['--root', request['root']]
sys.argv=argv
runpy.run_path(str(bundle),run_name='__main__')
"""


# Normal monitoring must not deploy a collector, create a database, run a
# write-producing self-test, or alter permissions on a sensitive server.
# Reopen immutable SQLite reads each poll: they never create WAL/SHM files and
# see fresh checkpoints after the existing hook writer closes its connection.
READ_ONLY_STREAM = """import json,sqlite3,sys,time
from pathlib import Path
request=json.loads(sys.stdin.readline())
root=Path.home()/'.agent-pet/naiwa-remote'
database=root/'events.sqlite3'
def send(value):
    print(json.dumps(value,ensure_ascii=False),flush=True)
if not database.is_file():
    send({'kind':'error','reason':'missing_collector'})
    raise SystemExit(2)
after=request.get('seq',0)
epoch=request.get('epoch','')
pulse=0
ready=False
while True:
    try:
        connection=sqlite3.connect(database.as_uri()+'?mode=ro&immutable=1',uri=True,timeout=1)
        try:
            current=connection.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
            if current!=epoch:
                epoch=current
                after=0
            rows=connection.execute('SELECT seq,data FROM events WHERE seq>? ORDER BY seq LIMIT 256',(after,)).fetchall()
        finally:
            connection.close()
        if not ready:
            send({'kind':'ready','epoch':epoch,'protocol':1,'python':sys.version.split()[0],'self_test':False,'read_only':True})
            ready=True
        for at in range(0,len(rows),16):
            chunk=[{'seq':seq,'event':json.loads(data)} for seq,data in rows[at:at+16]]
            if len(chunk)==1:
                send(dict(chunk[0],kind='event',epoch=epoch))
            else:
                send({'kind':'batch','epoch':epoch,'records':chunk})
            after=chunk[-1]['seq']
    except (sqlite3.Error,ValueError,TypeError,IndexError):
        pass
    if ready and time.monotonic()-pulse>=2:
        try:
            diagnostic=json.loads((root/'last-hook.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):
            diagnostic={}
        send({'kind':'pulse','epoch':epoch,'seq':after,'last_hook':diagnostic})
        pulse=time.monotonic()
    time.sleep(.2)
"""


def ssh_env():
    """Git Bash rewrites arguments that contain /path. The remote shell must see them unchanged."""
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    env["MSYS2_ARG_CONV_EXCL"] = "*"
    return env


def ssh_arguments(host, interactive=False, *, read_only=False):
    """Batch mode never prompts. Interactive mode lets OpenSSH ask on its own console."""
    host_id(host)
    args = ["ssh", "-T", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=2", "-o", "LogLevel=ERROR"]
    if not interactive:
        args += ["-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey"]
    args += [host, "python3 -u -c " + shlex.quote(READ_ONLY_STREAM if read_only else BOOTSTRAP)]
    return args


def link_event(root, host, lost):
    from datetime import datetime, timezone
    from naiwa.phase import ACTIVE, PhaseMachine
    from naiwa.bus import EventReader
    machine = PhaseMachine.from_snapshot(read_snapshot(root))
    machine = EventReader(root).poll(machine)
    prefix = "ssh-"+host_id(host)+":"
    for turn in machine.turns.values():
        if turn.session_id.startswith(prefix) and turn.phase in ACTIVE:
            append_event(root, BusEvent("cursor", turn.session_id, turn.turn_id,
                        "connection_lost" if lost else "connection_restored",
                        ts=datetime.now(timezone.utc).isoformat()))


def refresh_connection_health(root, machine, now=None):
    """Reflect a stopped receiver even if it was killed before writing a stop.

    Only the local collector's health files are read. No SSH calls or writes.
    """
    from naiwa.phase import ACTIVE
    now = time.time() if now is None else now
    states = {}
    for turn in machine.turns.values():
        if turn.phase not in ACTIVE or not turn.session_id.startswith("ssh-"):
            continue
        identity = turn.session_id.split(":", 1)[0].removeprefix("ssh-")
        if not re.fullmatch(r"[0-9a-f]{16}", identity):
            continue
        if identity not in states:
            try:
                data = json.loads((runtime_dir(root)/f"remote-{identity}-health.json").read_text(encoding="utf-8"))
                stamp = data.get("ts")
                if not isinstance(data, dict) or type(stamp) not in (int, float):
                    continue
                states[identity] = data.get("state") != "connected" or not -1 <= now-stamp <= 15
            except (OSError, ValueError, AttributeError):
                continue
        turn.disconnected = states[identity]


def _request(command, cursor=None, workspace="", explicit_root=""):
    cursor = cursor or {}
    return {"bundle": base64.b64encode(bundle_bytes()).decode(), "command": command,
            "seq": cursor.get("seq", 0), "epoch": cursor.get("epoch", ""),
            "workspace": workspace, "root": explicit_root}


def receive(host, root, interactive=False, gui=False):
    base = runtime_dir(root)
    identity = host_id(host)
    checkpoint = base/("remote-"+identity+"-cursor.json")
    health = base/("remote-"+identity+"-health.json")
    health_fields = {}
    started_at = time.time()
    def status(state, **extra):
        health_fields.update(extra)
        atomic_json(health, {"host": host, "pid": os.getpid(), "state": state, "ts": time.time(),
                            "started_at": started_at, "auth_ui": "gui" if gui else "terminal" if interactive else "batch",
                            **health_fields})
    try:
        lock = BusLock(base/("remote-"+identity+".lock"), timeout=.05)
        lock.__enter__()
    except TimeoutError:
        return
    child = None
    try:
        while True:
            console_visible(True) if interactive and not gui else None
            status("connecting")
            try:
                cursor = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
                environment = ssh_env()
                arguments = ssh_arguments(host, interactive, read_only=True)
                if gui:
                    from naiwa.askpass import environment as gui_environment
                    environment = gui_environment(host, environment)
                    arguments[1:1] = ["-o", "NumberOfPasswordPrompts=1"]
                child = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL if gui else None, env=environment,
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" and (gui or not interactive) else 0)
                child.stdin.write((json.dumps({"seq": cursor.get("seq", 0), "epoch": cursor.get("epoch", "")})+"\n").encode())
                child.stdin.close()
                for line in iter(child.stdout.readline, b""):
                    if len(line) > 65536:
                        raise ValueError("Invalid remote event")
                    message = json.loads(line)
                    if message.get("kind") == "ready":
                        if message.get("protocol") != 1:
                            raise ValueError("Remote protocol mismatch")
                        link_event(base, host, False)
                        status("connected", remote_python=message.get("python", ""), self_test=message.get("self_test", False),
                               read_only=message.get("read_only", False))
                        if interactive and not gui:
                            console_visible(False)
                    elif message.get("kind") in {"event", "batch"}:
                        epoch = message["epoch"]
                        records = [message] if message["kind"] == "event" else message.get("records")
                        if (not isinstance(epoch, str) or not re.fullmatch(r"[0-9a-f]{32}", epoch)
                                or not isinstance(records, list) or not 1 <= len(records) <= 16):
                            raise ValueError("Invalid remote sequence")
                        from datetime import datetime, timezone
                        pending = []
                        seq = cursor.get("seq", 0) if epoch == cursor.get("epoch") else 0
                        skipped = 0
                        packet_seq = 0
                        for record in records:
                            incoming_seq = record.get("seq") if isinstance(record, dict) else None
                            if type(incoming_seq) is not int or incoming_seq <= packet_seq:
                                raise ValueError("Invalid remote sequence")
                            packet_seq = incoming_seq
                            if incoming_seq <= seq:
                                continue
                            seq = incoming_seq
                            try:
                                pending.append(imported_event(host, record["event"],
                                               received_at=datetime.now(timezone.utc).isoformat()))
                            except (ValueError, KeyError, TypeError):
                                skipped = seq
                        if seq == cursor.get("seq", 0) and epoch == cursor.get("epoch"):
                            continue
                        while pending:
                            try:
                                if len(pending) == 1:
                                    append_event(base, pending[0])
                                else:
                                    append_events(base, pending)
                                break
                            except TimeoutError:
                                # Local compaction/another IDE hook can briefly
                                # hold the bus lock. Retain the event and login.
                                time.sleep(.05)
                        cursor = {"epoch": epoch, "seq": seq}
                        atomic_json(checkpoint, cursor)
                        if skipped:
                            health_fields.update(skipped_remote_seq=skipped, last_rejection="invalid_event")
                        status("connected", last_remote_seq=seq, last_event=time.time())
                    elif message.get("kind") == "pulse":
                        status("connected", last_remote_seq=cursor.get("seq", 0), last_hook=message.get("last_hook", {}))
                child.wait()
                raise OSError("SSH connection closed")
            except (OSError, ValueError, KeyError, TypeError) as error:
                known = {"SSH connection closed", "Invalid remote event", "Remote protocol mismatch", "Invalid remote sequence"}
                status("disconnected", last_error=str(error) if str(error) in known else type(error).__name__,
                       error_file=Path(error.filename2 or error.filename).name if isinstance(error, OSError) and (error.filename2 or error.filename) else "",
                       ssh_exit_code=child.poll() if child is not None else None)
                link_event(base, host, True)
                if child and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
                if gui:
                    # Cancelling or losing a connection must not cause repeated
                    # password popups. Retry is an explicit action in the pet.
                    return
                if interactive:
                    console_visible(True)
                    print(f"{host}：连接已断开。按 Enter 后在 OpenSSH 提示里重新输入密码，Ctrl+C 停止。", flush=True)
                    input()
                else:
                    time.sleep(5)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        if child and child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        status("stopped")
        link_event(base, host, True)
        lock.__exit__(None, None, None)


def _exchange(host, request, interactive=False):
    payload = (json.dumps(request)+"\n").encode()
    if interactive and os.name == "nt":
        from naiwa.askpass import environment
        arguments = ssh_arguments(host, True)
        arguments[1:1] = ["-o", "NumberOfPasswordPrompts=1"]
        completed = subprocess.run(arguments, input=payload, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=180,
                                   env=environment(host, ssh_env()), creationflags=subprocess.CREATE_NO_WINDOW)
    elif interactive:
        completed = subprocess.run(ssh_arguments(host, True), input=payload,
                                   stdout=subprocess.PIPE, stderr=None, timeout=180, env=ssh_env())
    else:
        completed = subprocess.run(ssh_arguments(host), input=payload,
                                   capture_output=True, timeout=40, env=ssh_env())
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    message = None
    if lines:
        try:
            message = json.loads(lines[-1])
        except ValueError:
            message = None
    if isinstance(message, dict) and message.get("kind") == "error":
        if message.get("reason") == "count":
            raise ValueError(f"没有唯一的目录（{message.get('count', 0)} 个）。请用 --root 指定。")
        if message.get("reason") == "hooks":
            raise ValueError("远端 hooks.json 有内容但不是合法 JSON，没有覆盖。")
        raise ValueError("远端目录不符合要求")
    if isinstance(message, dict) and message.get("kind") == "ready":
        return message
    if completed.returncode != 0 or message is None:
        if interactive:
            raise OSError("SSH 连接失败。密码只输给 OpenSSH，奶蛙不保存。")
        raise OSError("SSH 连接失败。没有提示或保存密码；请先为该主机配置密钥。")
    raise ValueError("远端目录不符合要求")


def remember(root, host, workspace, interactive=False):
    path = runtime_dir(root)/"remotes.json"
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
    hosts = [item for item in current.get("hosts", []) if isinstance(item, dict) and item.get("host") != host]
    item = {"host": host, "workspace": workspace}
    if interactive:
        item["interactive"] = True
    hosts.append(item)
    atomic_json(path, {"hosts": hosts})


def install_host(host, workspace="", explicit_root="", root=None, interactive=False):
    host_id(host)
    if not workspace:
        raise ValueError("Specify the remote workspace name explicitly")
    _exchange(host, _request("prepare", workspace=workspace, explicit_root=explicit_root), interactive)
    remember(root, host, workspace, interactive)


def uninstall_host(host, root=None, interactive=False):
    host_id(host)
    _exchange(host, _request("uninstall"), interactive)
    path = runtime_dir(root)/"remotes.json"
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        hosts = [item for item in current.get("hosts", []) if isinstance(item, dict) and item.get("host") != host]
        atomic_json(path, {"hosts": hosts})


def start_configured(root, only_host=None):
    path = runtime_dir(root)/"remotes.json"
    if not path.exists():
        return
    config = json.loads(path.read_text(encoding="utf-8"))
    for entry in config.get("hosts", []):
        if not isinstance(entry, dict):
            continue
        host = entry.get("host", "")
        if only_host is not None and host != only_host:
            continue
        try:
            identity = host_id(host)
        except ValueError:
            continue
        try:
            with BusLock(runtime_dir(root)/("remote-"+identity+".lock"), timeout=.01):
                pass
        except TimeoutError:
            continue
        python = str(Path(sys.executable).with_name("python.exe")) if os.name == "nt" else sys.executable
        args = [python, "-m", "naiwa.remote", "--host", host, "--data-dir", str(runtime_dir(root))]
        interactive = entry.get("interactive") is True
        if interactive:
            args += ["--interactive"]
            if os.name == "nt":
                args += ["--gui"]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.Popen(args, creationflags=flags)


def stop_local_receiver(root, host):
    """Explicit UI action: stop only a verified local listener and its SSH child.

    Validate process creation time and executable against the local health record
    before terminating, so a stale PID cannot kill a later unrelated process.
    """
    if os.name != "nt":
        raise OSError("This control requires Windows")
    import ctypes
    from ctypes import wintypes as w
    from naiwa.hosts import _processes
    record = json.loads((runtime_dir(root)/("remote-"+host_id(host)+"-health.json")).read_text(encoding="utf-8"))
    pid, stamp = record.get("pid"), record.get("ts")
    if record.get("host") != host or type(pid) is not int or pid <= 0 or pid == os.getpid() or type(stamp) not in (int, float):
        raise OSError("Listener identity unavailable")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.GetProcessTimes.argtypes = [w.HANDLE]+[ctypes.POINTER(w.FILETIME)]*4
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x101001, False, pid)
    if not handle:
        return
    try:
        created, exited, system, user = (w.FILETIME() for _ in range(4))
        image, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
            raise OSError("Cannot verify listener process")
        birth = ((created.dwHighDateTime << 32)+created.dwLowDateTime)/10_000_000-11_644_473_600
        if birth > stamp+1 or not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            raise OSError("Listener process no longer matches its record")
        if Path(image.value).resolve() != Path(sys.executable).with_name("python.exe").resolve():
            raise OSError("Listener executable does not match")
        for child_pid, (parent, name) in _processes().items():
            if parent != pid or name != "ssh.exe":
                continue
            child = kernel.OpenProcess(0x101001, False, child_pid)
            if child:
                try:
                    kernel.TerminateProcess(child, 0)
                    kernel.WaitForSingleObject(child, 500)
                finally:
                    kernel.CloseHandle(child)
        if not kernel.TerminateProcess(handle, 0):
            raise OSError("Could not stop listener")
        kernel.WaitForSingleObject(handle, 1000)
    finally:
        kernel.CloseHandle(handle)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("remote_command", nargs="?", choices=("install", "uninstall"))
    parser.add_argument("--host", required=True, help="An alias from your own SSH config")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--root", default="")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--gui", action="store_true", help="Use Naiwa's OpenSSH login card")
    args = parser.parse_args(argv)
    if args.remote_command == "install":
        if not args.workspace:
            parser.error("remote install requires --workspace")
        install_host(args.host, args.workspace, args.root, args.data_dir, interactive=args.interactive)
        print(f"已准备 {args.host} 上的 {args.workspace}。监听随奶蛙启动，不保存服务器路径。")
        if args.interactive:
            print("需要认证时会显示奶蛙登录卡片。连接状态和重试入口在托盘菜单“SSH 连接”中。")
        return 0
    if args.remote_command == "uninstall":
        uninstall_host(args.host, args.data_dir, interactive=args.interactive)
        print(f"已移除 {args.host} 上奶蛙自己的钩子。")
        return 0
    receive(args.host, args.data_dir, args.interactive or args.gui,
            gui=args.gui or (os.name == "nt" and args.interactive))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
