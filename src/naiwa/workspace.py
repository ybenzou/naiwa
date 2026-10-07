"""Public hook workspace metadata reduced to a leaf name and opaque identity.

Never traverse the filesystem or retain the full path. Codex supplies session
cwd, Cursor supplies workspace_roots; neither guarantees an IDE window title.
"""
import hashlib
import posixpath
import re
import unicodedata
from urllib.parse import unquote, urlsplit

MAX_NAME = 80


def opaque_id(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"ws-[0-9a-f]{24}", value) else ""


def display_name(value: object) -> str:
    if not isinstance(value, str) or len(value) > 512 or "/" in value or "\\" in value:
        return ""
    text = unicodedata.normalize("NFC", value)
    text = "".join(c for c in text if unicodedata.category(c) not in {"Cc", "Cf", "Cs", "Zl", "Zp"})
    return " ".join(text.split()).strip()[:MAX_NAME]


def _root(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if any(unicodedata.category(c) in {"Cc", "Cs"} for c in value):
        return None
    path = value.strip().replace("\\", "/")
    authority = ""
    if "://" in path:
        try:
            uri = urlsplit(path)
            if uri.scheme not in {"file", "vscode-remote"}:
                return None
            authority = uri.scheme+"://"+uri.netloc if uri.scheme == "vscode-remote" else uri.netloc
            path = unquote(uri.path)
        except ValueError:
            return None
    if not path.startswith("/") and not re.match(r"^[A-Za-z]:/", path):
        return None
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    path = posixpath.normpath(path)
    name = display_name(posixpath.basename(path))
    if not name or name in {".", ".."} or re.fullmatch(r"[A-Za-z]:", name):
        return None
    # Windows paths are case insensitive, Unix paths preserve their case.
    canonical = path.casefold() if re.match(r"^/?[A-Za-z]:/", path) or path.startswith("//") else path
    return name, authority+canonical


def workspace_identity(source, payload):
    roots = payload.get("workspace_roots") if source == "cursor" else None
    candidates = roots[:32] if isinstance(roots, list) else []
    parsed = [root for value in candidates if (root := _root(value))]
    if not parsed and (root := _root(payload.get("cwd"))):
        parsed = [root]
    if not parsed:
        return "", ""
    unique = dict((canonical, name) for name, canonical in parsed)
    names = list(unique.values())
    name = " + ".join(names[:3])
    if len(names) > 3:
        name += f" (+{len(names)-3})"
    identity = "ws-"+hashlib.sha256("\n".join(sorted(unique)).encode("utf-8")).hexdigest()[:24]
    return display_name(name), identity
