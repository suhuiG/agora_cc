"""Agora 카탈로그 레지스트리 패키지.

RegistryPort 뒤에 Aws 어댑터를 두는 구조. 레이어 코드는 Port에만 의존해요.
"""

from .aws_adapter import AwsRegistryAdapter
from .models import (
    DescriptorType,
    InvalidStateTransition,
    RecordAlreadyExists,
    RecordNotFound,
    RecordStatus,
    RegistryError,
    RegistryRecord,
    SearchHit,
    ValidationError,
)
from .port import RegistryPort

__all__ = [
    "RegistryPort",
    "AwsRegistryAdapter",
    "RegistryRecord",
    "SearchHit",
    "DescriptorType",
    "RecordStatus",
    "RegistryError",
    "RecordAlreadyExists",
    "RecordNotFound",
    "InvalidStateTransition",
    "ValidationError",
]
