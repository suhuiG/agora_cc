"""도구 실행 compute(lambda|fargate) 분류 (SP-2).

등록 시 Bedrock LLM이 도구 성격을 보고 compute를 추론하고, 실패 시 규칙으로 폴백해요
(등록이 절대 안 터지게). BedrockReportGenerator 패턴을 그대로 복제했어요.
"""
from __future__ import annotations

import json
from typing import Protocol


class ComputeClassifierPort(Protocol):
    def classify(self, *, name: str, area: str, exec_kind: str,
                 invoke_cmd: str, repo: str) -> dict: ...  # {"compute","rationale"}


class RuleComputeClassifier:
    """규칙 기반 폴백. 무거운 실행(서비스형·컨테이너/이미지 스캔)은 fargate, 그 외는 lambda."""

    _HEAVY_AREAS = {"container", "sbom_cve"}

    def classify(self, *, name: str, area: str, exec_kind: str, invoke_cmd: str, repo: str) -> dict:
        if exec_kind == "service" or area in self._HEAVY_AREAS:
            return {"compute": "fargate",
                    "rationale": f"{exec_kind}/{area}는 실행 시간·리소스가 커 격리 태스크(Fargate)가 안전해요."}
        return {"compute": "lambda",
                "rationale": f"{name}는 짧은 바이너리 실행({exec_kind})이라 cold start·비용 면에서 Lambda가 유리해요."}


class BedrockComputeClassifier:
    """Bedrock Claude로 도구 성격 → compute 추론. invoke_model 직접 호출."""

    def __init__(self, model_id: str, region: str, client=None):
        self._model_id = model_id
        self._region = region
        self._client = client

    def _rt(self):
        if self._client is None:
            from ...shared.deps import get_bedrock_runtime_client

            self._client = get_bedrock_runtime_client(self._region)
        return self._client

    def classify(self, *, name: str, area: str, exec_kind: str, invoke_cmd: str, repo: str) -> dict:
        system = (
            "당신은 AWS 아키텍트예요. 스캔 도구의 성격을 보고 AWS에서 Lambda로 실행할지 "
            "Fargate로 실행할지 결정해요. 기준: 짧은 단일 바이너리·수초~수분 실행·작은 디스크는 "
            "lambda(빠른 cold start·저비용). 15분 초과 가능·큰 디스크·이미지/컨테이너 스캔·대용량 "
            ' 메모리는 fargate. JSON만 출력: {"compute":"lambda|fargate","rationale":"한국어 근거 1-2문장"}'
        )
        user = (f"도구: {name}\n영역(area): {area}\n실행형태(exec_kind): {exec_kind}\n"
                f"호출: {invoke_cmd}\n저장소: {repo}\n\n이 도구의 compute를 결정해줘.")
        body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 512,
                "system": system, "messages": [{"role": "user", "content": user}]}
        resp = self._rt().invoke_model(modelId=self._model_id, body=json.dumps(body))
        out = json.loads(resp["body"].read())
        text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
        data = json.loads(text)
        compute = data.get("compute")
        if compute not in ("lambda", "fargate"):
            raise ValueError(f"잘못된 compute: {compute}")
        return {"compute": compute, "rationale": str(data.get("rationale", ""))}


class WithFallback:
    """primary.classify 실패 시 fallback으로 흡수 — 등록이 절대 안 터지게."""

    def __init__(self, primary, fallback):
        self._primary = primary
        self._fallback = fallback

    def classify(self, **kwargs) -> dict:
        try:
            return self._primary.classify(**kwargs)
        except Exception:
            return self._fallback.classify(**kwargs)
