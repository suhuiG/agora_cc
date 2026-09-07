import json, os, sys
import boto3
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
sys.path.insert(0, os.path.dirname(__file__))


@mock_aws
def test_handler_writes_result_json(monkeypatch):
    import handler as h
    s3 = boto3.client("s3", region_name="ap-northeast-2")
    s3.create_bucket(Bucket="scan-bucket", CreateBucketConfiguration={"LocationConstraint": "ap-northeast-2"})
    s3.put_object(Bucket="scan-bucket", Key="input/scan1/app.py", Body=b"secret = 'AKIAIOSFODNN7EXAMPLE'")

    # gitleaks 바이너리 실행을 fake — normalize가 처리할 leaks JSON을 리포트 파일에 씀.
    def fake_run(src, outdir, scan_id=""):
        return [{"RuleID": "generic-api-key", "File": "app.py", "StartLine": 1,
                 "Description": "x", "Match": "secret"}]
    monkeypatch.setattr(h, "_run_gitleaks_raw", fake_run)

    out = h.handler({"input_uri": "s3://scan-bucket/input/scan1/",
                     "output_uri": "s3://scan-bucket/output/scan1/gitleaks.json",
                     "scan_id": "scan1"}, None)
    assert out["scan_id"] == "scan1"
    assert out["area"] == "secret"
    # result.json이 S3에 기록됨
    body = json.loads(s3.get_object(Bucket="scan-bucket", Key="output/scan1/gitleaks.json")["Body"].read())
    assert set(body.keys()) >= {"risk", "findings", "scanned_areas"}


@mock_aws
def test_handler_fail_closed_on_error(monkeypatch):
    import handler as h
    s3 = boto3.client("s3", region_name="ap-northeast-2")
    s3.create_bucket(Bucket="scan-bucket", CreateBucketConfiguration={"LocationConstraint": "ap-northeast-2"})
    s3.put_object(Bucket="scan-bucket", Key="input/scan2/app.py", Body=b"x")

    def boom(src, outdir, scan_id=""):
        raise RuntimeError("gitleaks crashed")
    monkeypatch.setattr(h, "_run_gitleaks_raw", boom)

    out = h.handler({"input_uri": "s3://scan-bucket/input/scan2/",
                     "output_uri": "s3://scan-bucket/output/scan2/gitleaks.json",
                     "scan_id": "scan2"}, None)
    assert out["risk"] == "high"  # fail-closed
    body = json.loads(s3.get_object(Bucket="scan-bucket", Key="output/scan2/gitleaks.json")["Body"].read())
    assert any(f.get("code") == "SCANNER_ERROR" for f in body["findings"])
