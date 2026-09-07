"""Connection이 가리키는 downstream 리소스의 구조화 식별자.

(2) policy compiler가 이 필드들로 IAM ARN을 조립해요. parse_target은 결정적
순수 함수로, 자유 문자열 target을 3-kind(aws/uri/opaque) ResourceRef로 바꿔요.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

CURRENT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ResourceRef:
    kind: str                       # "aws" | "uri" | "opaque"
    service: str = ""
    region: str = ""
    account: str = ""
    resource_type: str = ""
    resource_id: str = ""
    raw: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ResourceRef":
        fields = ("kind", "service", "region", "account",
                  "resource_type", "resource_id", "raw")
        return cls(**{k: data.get(k, "") for k in fields})

    def to_arn(self) -> str | None:
        """kind=aws일 때만 ARN 문자열을 조립. 그 외는 None(IAM 대상 아님)."""
        if self.kind != "aws":
            return None
        if self.raw.startswith("arn:"):
            return self.raw
        resource = self.resource_id
        if self.resource_type:
            resource = f"{self.resource_type}/{self.resource_id}"
        return f"arn:aws:{self.service}:{self.region}:{self.account}:{resource}"


def parse_target(s: str) -> ResourceRef:
    """자유 문자열 target → ResourceRef. 결정적, 예외 없음."""
    text = s.strip()
    if text.startswith("arn:"):
        # arn:partition:service:region:account:resource(/id 또는 :id)
        parts = text.split(":", 5)
        if len(parts) == 6:
            _, _partition, service, region, account, resource = parts
            resource_type = ""
            resource_id = resource
            if "/" in resource:
                resource_type, resource_id = resource.split("/", 1)
            elif ":" in resource:
                resource_type, resource_id = resource.split(":", 1)
            return ResourceRef(
                kind="aws", service=service, region=region, account=account,
                resource_type=resource_type, resource_id=resource_id, raw=text,
            )
        return ResourceRef(kind="opaque", raw=text)
    if "://" in text:
        scheme, rest = text.split("://", 1)
        # http(s)는 외부 API — IAM 리소스가 아니라 Token Vault 경로(opaque)로 둬요.
        # ddb://·s3:// 같은 커스텀 스킴만 AWS 리소스 축약(uri)으로 취급해요.
        if scheme and scheme not in ("http", "https"):
            return ResourceRef(
                kind="uri", service=scheme, resource_id=rest, raw=text,
            )
    return ResourceRef(kind="opaque", raw=text)
