"""로컬 개발용 배포 폴러 ON/OFF 스위치 — **임시 기능이에요.**

## 왜 있나

배포 job 폴러는 `AGORA_ROLE` 하나당 하나만 돌아야 해요(RT-02). 포털과 로컬이 같은 dev
자원을 동시에 폴링하면 어느 쪽이 단계를 전진시켰는지 비결정적이 되고, 미머지 코드로 한
검증이 무의미해져요. 그래서 `server.py:_poller_enabled()` 가 `AGORA_ROLE=local` +
`AGORA_POLLER_ENABLED=1` 조합을 `AGORA_POLLER_FORCE=1` 없이는 거부해요.

그 규율은 맞는데, 로컬에서 실 AWS e2e 를 할 때마다 **백엔드를 재기동**해야 폴러를 켜고
끌 수 있었어요. 2026-08-29 에 그 왕복이 실제로 시간을 먹었고, 더 나쁜 건 "job 이 왜 안
도는지" 가 화면에 안 보여서 원인을 찾는 데 시간이 들었다는 점이에요.

이 모듈은 **로컬 백엔드의 폴러 task 만** 시작·취소해요. 포털 ECS `desiredCount` 는 절대
건드리지 않아요 — 그건 AWS 변경이고 화면에서 할 일이 아니에요.

## GA 때 지우는 방법

정식 GA 에서는 이 파일과 `domains/runtime/dev_poller_router.py`, `server.py` 의
`register_dev_switch(...)` 호출 한 줄, 그리고 web 의 `DevPollerToggle` 을 지우면 끝이에요.
프로덕션 경로(`_poller_enabled()` 로 기동되는 포털 폴러)는 이 모듈을 거치지 않아요.

## 왜 플래그가 아니라 task 시작·취소인가

`run_poller` 는 `while should_run():` 이라 `should_run()` 이 False 를 돌려주면 루프가
**종료**돼요 — 플래그만 내리면 다시 켤 수 없어요. 그래서 실제로 task 를 만들고 취소해요.
"폴러 ON/OFF" 라는 표현과도 동작이 일치해요.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Iterable

_log = logging.getLogger(__name__)

#: 로컬에서 폴러 task 들을 만드는 팩토리. `server.py` 가 lifespan 에서 등록해요.
_start_tasks: Callable[[], Iterable[asyncio.Task]] | None = None
#: 이 스위치가 만든 task 들. 프로덕션 경로가 만든 task 는 여기 안 들어와요.
_tasks: list[asyncio.Task] = []
#: 부팅 시점에 프로덕션 경로가 이미 폴러를 켰는지. 그러면 스위치는 읽기 전용이에요.
_owned_by_env = False


def register(
    start_tasks: Callable[[], Iterable[asyncio.Task]],
    *,
    already_running: bool,
) -> None:
    """lifespan 에서 한 번 호출해요. `already_running` 이면 스위치는 읽기 전용이에요."""
    global _start_tasks, _owned_by_env
    _start_tasks = start_tasks
    _owned_by_env = already_running


def reset() -> None:
    """테스트 격리용. 등록과 task 를 비워요."""
    global _start_tasks, _owned_by_env
    for task in _tasks:
        task.cancel()
    _tasks.clear()
    _start_tasks = None
    _owned_by_env = False


def role() -> str:
    return os.getenv("AGORA_ROLE", "").lower()


def togglable() -> tuple[bool, str]:
    """토글 가능 여부와 그 이유. 이유를 화면에 그대로 보여줘요."""
    current = role()
    if current != "local":
        return False, (
            f"AGORA_ROLE={current or '(미설정)'} 에서는 바꿀 수 없어요 — "
            "폴러 소유권은 배포된 포털에 있어요."
        )
    if _owned_by_env:
        return False, (
            "환경변수로 이미 켠 폴러예요(AGORA_POLLER_ENABLED=1 + FORCE). "
            "이 스위치는 상태만 보여줘요."
        )
    if _start_tasks is None:
        return False, "폴러 팩토리가 등록되지 않았어요(서버 기동 경로 확인 필요)."
    return True, ""


def running() -> bool:
    """이 스위치가 켠 폴러가 돌고 있는지. 환경변수로 켠 경우는 True 로 봐요."""
    if _owned_by_env:
        return True
    return any(not t.done() for t in _tasks)


def state() -> dict:
    can, reason = togglable()
    return {
        "role": role(),
        "running": running(),
        "togglable": can,
        "reason": reason,
        "owned_by_env": _owned_by_env,
    }


def enable() -> dict:
    can, reason = togglable()
    if not can:
        raise PermissionError(reason)
    if running():
        return state()
    assert _start_tasks is not None
    _tasks.extend(_start_tasks())
    _log.warning(
        "dev poller switch: 로컬 백엔드가 공유 dev 자원을 폴링해요. "
        "포털 폴러가 함께 돌면 job 전진 소유자가 비결정적이에요(RT-02)."
    )
    return state()


def disable() -> dict:
    can, reason = togglable()
    if not can:
        raise PermissionError(reason)
    for task in _tasks:
        task.cancel()
    _tasks.clear()
    _log.info("dev poller switch: 로컬 폴러를 멈췄어요.")
    return state()


def disable_for_shutdown() -> None:
    """서버 종료 경로 전용 — `togglable()` 게이트 없이 task 만 정리해요.

    `disable()` 은 역할 게이트를 통과해야 해서 종료에는 쓸 수 없어요(포털 역할에서는
    `PermissionError` 예요). 종료는 사용자 요청이 아니라 프로세스 정리라서 게이트가
    필요 없어요.
    """
    for task in _tasks:
        task.cancel()
    _tasks.clear()
