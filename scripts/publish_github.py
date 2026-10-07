"""Publish checked release assets using credentials held by Git or the environment.

Run from the independent, committed public source directory. No credential is
printed, written to disk or embedded into Git URLs.
"""
from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from audit_share import audit, local_markers


def credential() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    result = subprocess.run(
        ["git", "-c", "credential.interactive=false", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n", text=True, capture_output=True,
        timeout=30, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
    if result.returncode == 0:
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if fields.get("password"):
            return fields["password"]
    raise RuntimeError("GitHub credentials unavailable; sign in with Git Credential Manager")


def api(token: str, url: str, method="GET", payload=None, data=None, content_type=None):
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        content_type = "application/json"
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "User-Agent": "naiwa-release", "X-GitHub-Api-Version": "2022-11-28"}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def validate_assets(wheel: Path, checksum: Path, usage: Path) -> str:
    markers = local_markers()
    for path in (wheel, checksum, usage):
        report = audit(path, markers)
        if not report["clean"]:
            raise ValueError(f"Share audit failed for {path.name}: {report['findings']}")
    digest, name = checksum.read_text(encoding="ascii").strip().split()
    if name != wheel.name or digest != hashlib.sha256(wheel.read_bytes()).hexdigest():
        raise ValueError("Checksum does not match the wheel")
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("Expected one wheel metadata record")
        metadata = Parser().parsestr(archive.read(names[0]).decode("utf-8"))
    if metadata["Name"] != "naiwa":
        raise ValueError("Expected a naiwa wheel")
    return metadata["Version"]


def publish(repo: str, wheel: Path, source: Path) -> str:
    if len(repo.split("/")) != 2 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./" for c in repo):
        raise ValueError("Specify repository as owner/name")
    checksum, usage = wheel.with_suffix(".sha256"), wheel.with_suffix(".usage.txt")
    version = validate_assets(wheel, checksum, usage)
    origin = subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=source, text=True).strip()
    if origin.removesuffix(".git") != "https://github.com/" + repo:
        raise ValueError("Source origin must match the specified repository")
    # Audit the committed public tree; local caches are never release contents.
    with tempfile.TemporaryDirectory(prefix="naiwa-publish-") as temporary:
        archive = Path(temporary)/"tracked.zip"
        subprocess.run(["git", "archive", "--format=zip", "--output", str(archive), "HEAD"], cwd=source, check=True)
        report = audit(archive, local_markers())
        if not report["clean"]:
            raise ValueError(f"Committed source failed share audit: {report['findings']}")
    tag = "v" + version
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    token = credential()
    base = "https://api.github.com/repos/" + repo
    remote_ref = api(token, base + "/git/ref/tags/" + tag)
    if remote_ref["object"]["sha"] != head or remote_ref["object"]["type"] != "commit":
        raise ValueError("Push a lightweight release tag matching the checked source first")
    try:
        release = api(token, base + "/releases/tags/" + tag)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        release = api(token, base + "/releases", "POST", payload={
            "tag_name": tag, "target_commitish": head, "name": "Naiwa " + version,
            "draft": True, "prerelease": False,
            "body": "Windows / Python 3.11+ (64-bit). Animation assets are included.\n\n"
                    "```powershell\npython -m pip install https://github.com/" + repo +
                    "/releases/download/" + tag + "/" + wheel.name +
                    "\nnaiwa install\nnaiwa-desktop\n```\n\n"
                    "Review and trust Codex hooks before use. See README for setup.\n\n"
                    "No default server, personal SSH settings, runtime records, or parent Git history are included."})
    if not release["draft"]:
        raise ValueError("This release is already public; nothing overwritten")
    assets = {asset["name"]: asset for asset in release["assets"]}
    for path in (wheel, checksum, usage):
        if path.name in assets:
            existing = api(token, base + "/releases/assets/" + str(assets[path.name]["id"]))
            if existing.get("digest") != "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest():
                raise ValueError("Existing draft asset differs: " + path.name)
            continue
        upload = release["upload_url"].split("{", 1)[0] + "?" + urllib.parse.urlencode({"name": path.name})
        api(token, upload, "POST", data=path.read_bytes(), content_type="application/octet-stream")
    result = api(token, base + "/releases/" + str(release["id"]), "PATCH",
                 payload={"draft": False, "make_latest": "true"})
    return result["html_url"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--source", default=Path.cwd(), type=Path)
    args = parser.parse_args()
    try:
        print(publish(args.repo, args.wheel.resolve(), args.source.resolve()))
    except urllib.error.HTTPError as error:
        parser.exit(1, f"GitHub API returned HTTP {error.code}; credentials were not logged.\n")
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
