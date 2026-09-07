"""Initializr와 Runtime 배포가 공유하는 Bedrock 모델 계약."""

from __future__ import annotations


MODEL_ID_MAP: dict[str, str] = {
    "sonnet-5": "global.anthropic.claude-sonnet-5",
    "opus-4-8": "global.anthropic.claude-opus-4-8",
    "sonnet-4-6": "global.anthropic.claude-sonnet-4-6",
    "haiku-4-5": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
}

# Initializr code artifacts use one platform-owned value. It is deliberately
# not caller-configurable.
PLATFORM_TEMPERATURE = 0.2

# Anthropic model documentation and migration guide define sampling parameter
# support. New models default to unsupported until their capability is verified.
MODEL_CAPABILITIES: dict[str, dict[str, bool]] = {
    "sonnet-5": {"sampling_params": False},
    "opus-4-8": {"sampling_params": False},
    "sonnet-4-6": {"sampling_params": True},
    "haiku-4-5": {"sampling_params": True},
}

_MODEL_KEY_BY_ID = {
    bedrock_model_id: model
    for model, bedrock_model_id in MODEL_ID_MAP.items()
}


def model_supports_sampling_params(model: str) -> bool:
    """Agora 모델 키나 Bedrock ID의 sampling parameter 지원 여부를 반환해요."""
    model_key = model if model in MODEL_ID_MAP else _MODEL_KEY_BY_ID.get(model)
    return bool(
        model_key
        and MODEL_CAPABILITIES.get(model_key, {}).get("sampling_params", False)
    )


def validate_model_id(model: str) -> str:
    """지원 모델 ID만 통과시켜 임의 Bedrock 모델 주입을 막아요."""
    if model not in MODEL_ID_MAP:
        raise ValueError(f"지원하지 않는 모델이에요: {model}")
    return model


def model_metadata(model: str) -> dict[str, str]:
    """표시용 ID와 실제 Bedrock 호출 ID를 함께 보존해요."""
    model = validate_model_id(model)
    return {"model": model, "bedrockModelId": MODEL_ID_MAP[model]}


def is_model_metadata(model: object, bedrock_model_id: object) -> bool:
    """Registry에서 읽은 모델 metadata가 현재 계약과 일치하는지 확인해요."""
    return (
        isinstance(model, str)
        and bedrock_model_id == MODEL_ID_MAP.get(model)
    )
