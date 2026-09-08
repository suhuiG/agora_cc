import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))


class FakeLambda:
    def __init__(self, payload): self._payload = payload; self.calls = []
    def invoke(self, **kw):
        self.calls.append(kw)
        import io
        return {"Payload": io.BytesIO(json.dumps(self._payload).encode())}


class FakeEcs:
    """스캐너 컨테이너 계약은 「항상 exit 0」이에요(scan-runner/entrypoint.py).

    `stopped` 를 주면 컨테이너가 못 뜬 상황(예: ECR 이미지 미push)을 흉내내요.
    """
    def __init__(self, stopped=None):
        self.calls = []
        self._stopped = stopped or {
            "lastStatus": "STOPPED", "containers": [{"exitCode": 0}],
        }
    def run_task(self, **kw):
        self.calls.append(kw)
        return {"tasks": [{"taskArn": "arn:task/1"}], "failures": []}
    def describe_tasks(self, **kw):
        return {"tasks": [self._stopped]}


class FakeS3Result:
    def __init__(self, body): self._body = body
    def get_object(self, **kw):
        import io
        return {"Body": io.BytesIO(json.dumps(self._body).encode())}


def _event(**over):
    e = {"tool_id": "gitleaks", "area": "secret", "compute": "lambda",
         "image_ref": "uri/gitleaks", "scan_id": "s1", "bucket": "b",
         "input_prefix": "input/s1/", "output_prefix": "output/s1/"}
    e.update(over)
    return e


def test_no_image_ref_returns_not_run():
    import dispatch_handler as h
    out = h.handler(_event(tool_id="snyk-agent-scan", area="sbom_cve", image_ref="", compute="fargate"), None)
    assert out["tool_id"] == "snyk-agent-scan"
    assert out["scanned_areas"] == []          # area 미포함 → gate가 not_run
    assert out["risk"] == "none" and out["findings"] == []


def test_lambda_compute_invokes_tool_function(monkeypatch):
    import dispatch_handler as h
    monkeypatch.setenv("AGORA_STAGE", "dev")
    fake = FakeLambda({"scan_id": "s1", "area": "secret", "risk": "high",
                       "findings": [{"code": "SECRET_X", "severity": "high"}], "scanned_areas": ["secret"]})
    out = h.handler(_event(), None, clients={"lambda": fake})
    assert fake.calls[0]["FunctionName"] == "agora-tool-gitleaks-dev"
    assert out["risk"] == "high" and out["scanned_areas"] == ["secret"]


def test_dispatch_passes_model_alias_to_lambda(monkeypatch):
    import dispatch_handler as h
    monkeypatch.setenv("AGORA_STAGE", "dev")
    fake = FakeLambda({"risk": "none", "findings": [], "scanned_areas": ["agent_intent"]})
    h.handler(_event(tool_id="llm-judge", area="agent_intent", image_ref="uri/llm-judge",
                     model_alias="sonnet-5"), None, clients={"lambda": fake})
    assert json.loads(fake.calls[0]["Payload"])["model_alias"] == "sonnet-5"


def test_dispatch_omits_model_alias_when_absent(monkeypatch):
    import dispatch_handler as h
    monkeypatch.setenv("AGORA_STAGE", "dev")
    fake = FakeLambda({"risk": "none", "findings": [], "scanned_areas": ["secret"]})
    h.handler(_event(), None, clients={"lambda": fake})
    assert "model_alias" not in json.loads(fake.calls[0]["Payload"])


def test_fargate_compute_runs_task_and_reads_s3(monkeypatch):
    import dispatch_handler as h
    monkeypatch.setenv("AGORA_STAGE", "dev")
    monkeypatch.setenv("SCAN_CLUSTER", "c"); monkeypatch.setenv("SCAN_SUBNETS", "sub1")
    monkeypatch.setenv("SCAN_SG", "sg1")
    s3 = FakeS3Result({"risk": "none", "findings": [], "scanned_areas": ["sast"]})
    out = h.handler(_event(tool_id="semgrep", area="sast", compute="fargate", image_ref="uri/semgrep"),
                    None, clients={"ecs": FakeEcs(), "s3": s3}, sleep=lambda *_: None)
    assert out["scanned_areas"] == ["sast"]
    assert out["risk"] == "none" and out["findings"] == []


def test_fargate_container_never_started_reports_stop_reason(monkeypatch):
    """ECR 이미지 미push 를 `NoSuchKey` 로 가리지 않아요.

    컨테이너가 못 뜨면 결과 객체가 없어서 S3 조회가 `NoSuchKey` 로 떨어지는데, 그 메시지만
    보면 운영자가 원인(이미지 미push)을 알 수 없어요. 태스크 stop 이유를 실어요.
    """
    import dispatch_handler as h
    monkeypatch.setenv("AGORA_STAGE", "dev")
    monkeypatch.setenv("SCAN_CLUSTER", "c"); monkeypatch.setenv("SCAN_SUBNETS", "sub1")
    monkeypatch.setenv("SCAN_SG", "sg1")
    ecs = FakeEcs(stopped={
        "lastStatus": "STOPPED",
        "stoppedReason": "CannotPullContainerError: … agora-tool-semgrep-dev:latest: not found",
        "containers": [{}],
    })

    class BoomS3:
        def get_object(self, **kw):
            raise AssertionError("stop 이유가 있으면 S3 를 읽지 않아요")

    out = h.handler(_event(tool_id="semgrep", area="sast", compute="fargate", image_ref="uri/semgrep"),
                    None, clients={"ecs": ecs, "s3": BoomS3()}, sleep=lambda *_: None)
    assert out["risk"] == "high"
    detail = out["findings"][0]["detail"]
    assert "CannotPullContainerError" in detail and "NoSuchKey" not in detail


def test_tool_error_fail_closed(monkeypatch):
    import dispatch_handler as h
    monkeypatch.setenv("AGORA_STAGE", "dev")
    class Boom:
        def invoke(self, **kw): raise RuntimeError("lambda down")
    out = h.handler(_event(), None, clients={"lambda": Boom()})
    assert out["risk"] == "high"
    assert any(f.get("code") == "SCANNER_ERROR" for f in out["findings"])
    assert out["tool_id"] == "gitleaks"
