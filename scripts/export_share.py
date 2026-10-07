"""Export an allowlisted source tree, without local config or Git history."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tomllib
import zipfile

from audit_share import audit, local_markers

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ("README.md", "pyproject.toml", ".gitignore", "Start-Naiwa.ps1", "naiwa.bat")
SHARE_SCRIPTS = ("build_release.py", "audit_share.py", "export_share.py", "publish_github.py")
RETIRED = {"motion-contours.json", "articulated-parts.json", "rigid-body.json", "rigid-body.png"}


def export(destination: Path) -> dict:
    destination = destination.resolve()
    if destination == ROOT or destination in ROOT.parents:
        raise ValueError("Destination must be a separate source directory")
    if destination.exists():
        raise ValueError("Destination already exists; nothing overwritten")
    sources = [ROOT/name for name in ROOT_FILES]
    sources += [ROOT/"scripts"/name for name in SHARE_SCRIPTS]
    sources += list((ROOT/"tests").glob("*.py"))
    sources += list((ROOT/"hooks").glob("*.py"))
    for source in (ROOT/"src"/"naiwa").rglob("*"):
        if not source.is_file() or source.is_symlink() or "__pycache__" in source.parts:
            continue
        relative = source.relative_to(ROOT/"src"/"naiwa")
        if len(relative.parts) == 1 and source.suffix == ".py":
            sources.append(source)
        elif relative.parts[0] == "assets" and source.suffix in {".png", ".json", ".ps1"}:
            if source.name not in RETIRED and not source.name.startswith("rig-"):
                sources.append(source)
    for source in sources:
        target = destination/source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    result = audit(destination, local_markers())
    if not result["clean"]:
        raise ValueError("Share audit failed: "+json.dumps(result["findings"]))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    version = tomllib.loads((ROOT/"pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    destination = args.output or ROOT/"artifacts"/"github-ready"/("naiwa-"+version)
    result = export(destination)
    archive = destination.with_name(destination.name + ".source.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for source in sorted(destination.rglob("*")):
            if source.is_file():
                bundle.write(source, "naiwa/"+source.relative_to(destination).as_posix())
    print(json.dumps({"source": str(destination), "archive": str(archive), **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
