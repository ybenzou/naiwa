"""Build and validate a clean wheel without collecting local runtime files.

Usage: python scripts/build_release.py
Requires: python -m pip install ".[release]"
"""
from __future__ import annotations

import hashlib
from email.parser import Parser
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

from audit_share import audit, local_markers

ROOT = Path(__file__).resolve().parents[1]


def check_wheel(wheel: Path, version: str) -> None:
    with zipfile.ZipFile(wheel) as archive:
        files = set(archive.namelist())
        required = {
            "naiwa/assets/cursor-input.ps1", "naiwa/assets/physical-clips.json",
            "naiwa/assets/motion-loops.json", "naiwa/assets/whole-book-15.png",
            "naiwa/assets/whole-signals-7.png",
        }
        if required - files:
            raise ValueError(f"Missing runtime resources: {sorted(required - files)}")
        for path in files:
            if path.startswith("naiwa/"):
                relative = path.removeprefix("naiwa/")
                if not (relative.endswith(".py") or relative.startswith("assets/")):
                    raise ValueError(f"Unexpected package file: {path}")
                filename = Path(relative).name
                if filename.startswith(("rig-", "rigid-body")) or filename in {
                    "motion-contours.json", "articulated-parts.json",
                }:
                    raise ValueError(f"Retired animation resource: {path}")
            elif not path.startswith(f"naiwa-{version}.dist-info/"):
                raise ValueError(f"Unexpected wheel file: {path}")
        metadata = Parser().parsestr(archive.read(f"naiwa-{version}.dist-info/METADATA").decode("utf-8"))
        if metadata["Version"] != version or not any(
                requirement.lower().startswith("pyside6") for requirement in metadata.get_all("Requires-Dist", [])):
            raise ValueError("Incorrect version or missing Qt dependency")
        entry = archive.read(f"naiwa-{version}.dist-info/entry_points.txt").decode("utf-8")
        if "naiwa = naiwa.__main__:main" not in entry or "naiwa-desktop = naiwa.__main__:main" not in entry:
            raise ValueError("Missing desktop entry points")
        print(f"Validated {sum(p.endswith('.png') for p in files)} PNG assets and CLI/GUI entry points")
    report = audit(wheel, local_markers())
    if not report["clean"]:
        raise ValueError(f"Share audit failed: {report['findings']}")
    print(f"Privacy check passed for {report['files_checked']} wheel entries")


def main() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    build_dir = (ROOT / "build").resolve()
    output = (ROOT / "artifacts").resolve()
    # The temporary tree will be removed: verify its absolute parent first.
    if not build_dir.is_relative_to(ROOT) or not output.is_relative_to(ROOT):
        raise ValueError("Build/output paths must remain inside the project")
    build_dir.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wheel-", dir=build_dir) as temporary:
        stage = Path(temporary).resolve()
        if not stage.is_relative_to(build_dir):
            raise ValueError("Temporary build escaped project")
        for filename in ("pyproject.toml", "README.md", "LICENSE", "LICENSE.txt"):
            if (ROOT / filename).is_file():
                shutil.copy2(ROOT / filename, stage / filename)
        shutil.copytree(ROOT / "src", stage / "src",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
        subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation",
                        "--outdir", str(stage / "dist"), str(stage)], check=True)
        built = list((stage / "dist").glob("*.whl"))
        if len(built) != 1:
            raise ValueError("Expected exactly one wheel")
        check_wheel(built[0], version)
        subprocess.run([sys.executable, "-m", "twine", "check", "--strict", str(built[0])], check=True)
        wheel = output / built[0].name
        shutil.copy2(built[0], wheel)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wheel.with_suffix(".sha256").write_text(f"{digest}  {wheel.name}\n", encoding="ascii")
    wheel.with_suffix(".usage.txt").write_text(
        f"奶娃 {version} / Windows / Python 3.11+ (64 位)\n\n"
        "在 wheel 文件所在目录打开 PowerShell，依次运行：\n"
        f"python -m pip install ./{wheel.name}\n"
        "naiwa install\nnaiwa-desktop\n\n"
        "pip 自动安装依赖，需要联网。无需源码、conda 或单独的动画资源。\n"
        "仅接 Cursor：naiwa install --source cursor\n"
        "仅接 Codex：naiwa install --source codex\n"
        "Codex 钩子需审查并信任；接入结果以真实扩展回合的新事件为准。\n"
        "找不到 naiwa 命令时，用 python -m naiwa install 接入、python -m naiwa 启动。\n"
        "demo 预览：naiwa demo；排查：naiwa doctor；版本：naiwa --version\n"
        "退出：右键系统托盘中的奶娃，选择退出。\n"
        "移除钩子：naiwa uninstall；卸载包：python -m pip uninstall naiwa\n"
        "升级前先退出，再 pip install --upgrade 新 wheel，重新 naiwa install 并启动。\n"
        "位置和显示偏好保存在用户目录 .agent-pet，升级保留。\n",
        encoding="utf-8-sig")
    print(f"\nReady: {wheel}\nSHA256: {digest}")


if __name__ == "__main__":
    main()
