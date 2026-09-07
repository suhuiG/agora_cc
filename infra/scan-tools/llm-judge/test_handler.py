"""LLM-judge Lambda 테스트 (Bedrock 완전 스텁 — 실 AWS 불필요)."""
import importlib.util, json, os, sys
import boto3
from moto import mock_aws

# shared + llm-judge 경로 등록 (normalize, rubric 임포트용)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
sys.path.insert(0, os.path.dirname(__file__))


def _load_handler():
    """llm-judge/handler.py를 경로로 직접 로드해요 (동일 모듈명 충돌 방지)."""
    spec = importlib.util.spec_from_file_location(
        "llm_judge_handler",
        os.path.join(os.path.dirname(__file__), "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 공통 fake_s3 픽스처 헬퍼
# ---------------------------------------------------------------------------

def _make_s3():
    s3 = boto3.client("s3", region_name="ap-northeast-2")
    s3.create_bucket(
        Bucket="scan-bucket",
        CreateBucketConfiguration={"LocationConstraint": "ap-northeast-2"},
    )
    return s3


# ---------------------------------------------------------------------------
# 테스트 1: Bedrock 판정 → normalize → S3 기록
# ---------------------------------------------------------------------------

@mock_aws
def test_handler_invokes_bedrock_and_normalizes(monkeypatch):
    h = _load_handler()
    fake_s3 = _make_s3()
    fake_s3.put_object(Bucket="scan-bucket", Key="input/s1/app.py", Body=b"def tool(): ...")

    fake_verdict = {
        "findings": [{"code": "THREAT_TOOL_POISONING", "severity": "high", "detail": "x", "location": "h.py"}],
        "risk": "high",
    }
    monkeypatch.setattr(h, "_bedrock_judge", lambda src_text, model_id: fake_verdict)
    monkeypatch.setattr(h, "_download_text", lambda s3, b, p: "def tool(): ...")

    result = h._scan(
        fake_s3,
        "s3://scan-bucket/input/s1/",
        "s3://scan-bucket/output/s1/llm-judge.json",
        model_alias="sonnet-5",
        scan_id="s1",
    )
    assert result["scanned_areas"] == ["agent_intent"]
    assert result["findings"][0]["code"] == "THREAT_TOOL_POISONING"

    # S3에도 기록됐는지 확인
    body = json.loads(
        fake_s3.get_object(Bucket="scan-bucket", Key="output/s1/llm-judge.json")["Body"].read()
    )
    assert set(body.keys()) >= {"risk", "findings", "scanned_areas"}


# ---------------------------------------------------------------------------
# 테스트 2: 별칭 → Bedrock 모델 ID 매핑
# ---------------------------------------------------------------------------

def test_model_alias_maps_to_bedrock_id():
    # 실측(2026-07-17): global. inference profile ID여야 on-demand 호출 가능.
    h = _load_handler()
    assert h._model_id("sonnet-5") == "global.anthropic.claude-sonnet-5"
    assert h._model_id("haiku-4-5") == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert h._model_id("unknown") == "global.anthropic.claude-haiku-4-5-20251001-v1:0"  # 폴백


# ---------------------------------------------------------------------------
# 테스트 3: Bedrock 오류 → fail_closed (SCANNER_ERROR)
# ---------------------------------------------------------------------------

@mock_aws
def test_bad_json_from_model_fails_closed(monkeypatch):
    h = _load_handler()
    fake_s3 = _make_s3()
    fake_s3.put_object(Bucket="scan-bucket", Key="input/s1/app.py", Body=b"x")

    monkeypatch.setattr(
        h, "_bedrock_judge",
        lambda s, m: (_ for _ in ()).throw(ValueError("bad json")),
    )
    monkeypatch.setattr(h, "_download_text", lambda s3, b, p: "x")

    result = h._scan(
        fake_s3,
        "s3://scan-bucket/input/s1/",
        "s3://scan-bucket/output/s1/llm-judge.json",
        model_alias="haiku-4-5",
        scan_id="s1",
    )
    assert any(f["code"] == "SCANNER_ERROR" for f in result["findings"])
    # S3에도 기록됐는지 확인
    body = json.loads(
        fake_s3.get_object(Bucket="scan-bucket", Key="output/s1/llm-judge.json")["Body"].read()
    )
    assert any(f.get("code") == "SCANNER_ERROR" for f in body["findings"])


# ---------------------------------------------------------------------------
# 테스트 4: handler(event, context) 래퍼
# ---------------------------------------------------------------------------

@mock_aws
def test_handler_wrapper(monkeypatch):
    h = _load_handler()
    fake_s3_client = _make_s3()
    fake_s3_client.put_object(Bucket="scan-bucket", Key="input/s2/app.py", Body=b"code")

    fake_verdict = {"findings": [], "risk": "none"}
    monkeypatch.setattr(h, "_bedrock_judge", lambda src_text, model_id: fake_verdict)
    monkeypatch.setattr(h, "_download_text", lambda s3, b, p: "code")
    # boto3.client를 fake_s3_client로 교체
    monkeypatch.setattr(h.boto3, "client", lambda *a, **kw: fake_s3_client)

    out = h.handler(
        {
            "input_uri": "s3://scan-bucket/input/s2/",
            "output_uri": "s3://scan-bucket/output/s2/llm-judge.json",
            "scan_id": "s2",
            "model_alias": "haiku-4-5",
        },
        None,
    )
    assert out["scan_id"] == "s2"
    assert out["area"] == "agent_intent"
    assert "risk" in out


def test_extract_json_strips_code_fence():
    # 실측(2026-07-17): Claude가 ```json 코드펜스 + 설명 산문을 섞어 반환 → 추출 필요.
    h = _load_handler()
    fenced = "```json\n{\"findings\":[],\"risk\":\"none\"}\n```\n\n제공된 데이터는 안전합니다."
    assert h._extract_json(fenced) == {"findings": [], "risk": "none"}


def test_extract_json_slices_prose_wrapped():
    h = _load_handler()
    prose = "분석 결과는 다음과 같습니다: {\"findings\":[],\"risk\":\"low\"} 이상입니다."
    assert h._extract_json(prose) == {"findings": [], "risk": "low"}


def test_extract_json_plain():
    h = _load_handler()
    assert h._extract_json("{\"risk\":\"high\",\"findings\":[]}") == {"risk": "high", "findings": []}
