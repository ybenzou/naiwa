"""Check a share tree or wheel; never print matched secrets or private values."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import zipfile

PATTERNS = {
    "private_ip": re.compile(rb"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"),
    "access_token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|pypi-[A-Za-z0-9_-]{30,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,})"),
    "private_key": re.compile(rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "credential_url": re.compile(rb"https?://[^/\s:]+:[^@\s/]+@"),
}
BLOCKED_DIRS = {".ssh", ".agent-pet", "runtime", "previews", "art", "build", "dist", "__pycache__", ".venv", ".pytest_cache"}
BLOCKED_NAMES = {"remotes.json", ".pypirc", "events.jsonl", "snapshot.json", "reader-health.json", "id_rsa", "id_ed25519"}
BLOCKED_SUFFIXES = {".jsonl", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx", ".pyc", ".log"}


def local_markers() -> list[str]:
    """Read connection metadata locally, never keys, passwords or file contents on SSH."""
    home = Path.home()
    markers = [str(home), home.as_posix()]
    aliases = []
    try:
        config = json.loads((home/".agent-pet"/"remotes.json").read_text(encoding="utf-8"))
        for item in config.get("hosts", []):
            aliases.append(item.get("host", ""))
            markers.extend((item.get("host", ""), item.get("workspace", "")))
    except (OSError, ValueError, AttributeError):
        pass
    try:
        selected = False
        for line in (home/".ssh"/"config").read_text(encoding="utf-8").splitlines():
            try:
                parts = shlex.split(line, comments=True, posix=False)
            except ValueError:
                continue
            if not parts:
                continue
            if parts[0].lower() == "host":
                selected = any(alias.casefold() in {p.casefold() for p in parts[1:]} for alias in aliases)
            elif selected and parts[0].lower() in {"hostname", "user"}:
                markers.extend(parts[1:])
    except OSError:
        pass
    markers.extend(os.environ.get("NAIWA_PRIVATE_MARKERS", "").splitlines())
    examples = {"example-server", "other-server", "sample_project", "localhost"}
    return sorted({value for value in markers if isinstance(value, str) and len(value) >= 6 and value.casefold() not in examples})


def inspect_blob(name: str, data: bytes, markers=()) -> list[dict]:
    findings = []
    for kind, pattern in PATTERNS.items():
        if pattern.search(data):
            findings.append({"file": name, "kind": kind})
    folded = data.lower()
    if any(marker.encode("utf-8").lower() in folded for marker in markers):
        findings.append({"file": name, "kind": "local_private_metadata"})
    return findings


def inspect_name(name: str) -> bool:
    parts = Path(name.replace("\\", "/")).parts
    basename = parts[-1] if parts else ""
    return (any(part in BLOCKED_DIRS or part.endswith(".egg-info") for part in parts)
            or basename in BLOCKED_NAMES or basename.startswith((".env", "id_rsa", "id_ed25519"))
            or Path(basename).suffix in BLOCKED_SUFFIXES)


def audit(path: Path, markers=()) -> dict:
    findings, count = [], 0
    if path.is_file() and path.suffix in {".whl", ".zip"}:
        with zipfile.ZipFile(path) as archive:
            entries = [(name, archive.read(name)) for name in archive.namelist() if not name.endswith("/")]
    elif path.is_dir():
        entries = []
        for item in sorted(path.rglob("*")):
            relative = item.relative_to(path)
            if ".git" in relative.parts:
                continue
            if item.is_symlink():
                findings.append({"file": relative.as_posix(), "kind": "symlink"})
            elif item.is_file():
                entries.append((relative.as_posix(), item.read_bytes()))
    else:
        entries = [(path.name, path.read_bytes())]
    for name, data in entries:
        count += 1
        if inspect_name(name):
            findings.append({"file": name, "kind": "private_or_generated_file"})
        findings.extend(inspect_blob(name, data, markers))
    return {"clean": not findings, "files_checked": count, "findings": findings}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    markers = local_markers()
    results = [{"artifact": path.name, **audit(path, markers)} for path in args.paths]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(result["clean"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
