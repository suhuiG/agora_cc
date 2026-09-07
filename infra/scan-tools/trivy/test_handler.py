"""trivy Lambda handler 테스트 — trivy 실행은 monkeypatch, S3는 stub."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
sys.path.insert(0, os.path.dirname(__file__))

import handler as h


class _FakeS3:
    def __init__(self):
        self.put_calls = []

    def get_paginator(self, _op):
        class _P:
            def paginate(self, **kw):
                return [{"Contents": [{"Key": "input/x/requirements.txt"}]}]
        return _P()

    def download_file(self, bucket, key, local):
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "w") as fh:
            fh.write("flask==2.0.0\n")

    def put_object(self, **kw):
        self.put_calls.append(kw)


def test_handler_clean_scan(monkeypatch):
    monkeypatch.setattr(h.boto3, "client", lambda *a, **k: _FakeS3())
    monkeypatch.setattr(h, "_run_trivy_raw", lambda src, scan_id="": {"Results": []})
    out = h.handler({"input_uri": "s3://b/input/x/", "output_uri": "s3://b/output/x/trivy.json",
                     "scan_id": "x"}, None)
    assert out["area"] == "sbom_cve"
    assert out["risk"] == "none"
    assert out["scanned_areas"] == ["sbom_cve"]


def test_handler_vuln_scan(monkeypatch):
    monkeypatch.setattr(h.boto3, "client", lambda *a, **k: _FakeS3())
    monkeypatch.setattr(h, "_run_trivy_raw", lambda src, scan_id="": {"Results": [
        {"Target": "requirements.txt", "Vulnerabilities": [
            {"VulnerabilityID": "CVE-2023-1", "PkgName": "flask", "Severity": "HIGH"}]}]})
    out = h.handler({"input_uri": "s3://b/input/x/", "output_uri": "s3://b/output/x/trivy.json",
                     "scan_id": "x"}, None)
    assert out["risk"] == "high"
    assert out["findings"][0]["code"] == "CVE_VULNERABILITY"


def test_handler_fail_closed_on_error(monkeypatch):
    monkeypatch.setattr(h.boto3, "client", lambda *a, **k: _FakeS3())
    def _boom(src, scan_id=""):
        raise RuntimeError("trivy exploded")
    monkeypatch.setattr(h, "_run_trivy_raw", _boom)
    out = h.handler({"input_uri": "s3://b/input/x/", "output_uri": "s3://b/output/x/trivy.json",
                     "scan_id": "x"}, None)
    assert out["risk"] == "high"
    assert out["findings"][0]["code"] == "SCANNER_ERROR"
