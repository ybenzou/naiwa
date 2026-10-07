"""Exercise privacy checks with generated, fictional metadata only."""
import importlib.util
import json
from pathlib import Path
import zipfile


SPEC = importlib.util.spec_from_file_location(
    "share_audit", Path(__file__).resolve().parents[1]/"scripts"/"audit_share.py")
share = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(share)


def test_source_and_wheel_checks_reject_private_contents_without_echoing(tmp_path):
    marker = "fictional-connection"
    secret = "gh" + "p_" + "A"*40
    address = ".".join(map(str, [10, 1, 2, 3]))
    payload = f"{marker}\n{secret}\n{address}".encode()
    source = tmp_path/"source"
    source.mkdir()
    (source/"config.py").write_bytes(payload)
    wheel = tmp_path/"example.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("package/config.py", payload)
    for path in (source, wheel):
        report = share.audit(path, [marker])
        assert not report["clean"]
        assert {f["kind"] for f in report["findings"]} == {
            "access_token", "private_ip", "local_private_metadata"}
        encoded = json.dumps(report)
        assert secret not in encoded and address not in encoded and marker not in encoded


def test_runtime_files_rejected_even_if_empty(tmp_path):
    wheel = tmp_path/"example.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("package/remotes.json", "{}")
    report = share.audit(wheel)
    assert not report["clean"]
    assert report["findings"][0]["kind"] == "private_or_generated_file"


def test_clean_package_passes(tmp_path):
    (tmp_path/"app.py").write_text("print('Hello')", encoding="utf-8")
    report = share.audit(tmp_path, ["fictional-connection"])
    assert report == {"clean": True, "files_checked": 1, "findings": []}
