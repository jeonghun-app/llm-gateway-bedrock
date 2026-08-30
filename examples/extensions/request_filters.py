"""요청 필터 확장 예제.

**이 코드는 예제다. 프로덕션에서 그대로 쓰지 않는다.** 정규식 기반 개인정보
탐지는 오탐과 미탐이 모두 많다. 실제로는 Amazon Bedrock Guardrails 처럼
전용 기능을 쓰는 편이 낫다.

이 모듈이 있는 이유는 확장점 계약을 실제 코드로 검증하기 위해서다. 두 가지
경로를 각각 보여준다.

- `MaskKoreanIdFilter`: 요청을 **변형**한다. 반환 payload 가 원본과 다르다.
- `RejectLongPromptFilter`: 요청을 **거부**한다. 예외를 던진다.

활성화 방법:

```
LLMGW_REQUEST_FILTERS=examples.extensions.request_filters:MaskKoreanIdFilter
```

표준 라이브러리만 쓴다. 확장이 새 런타임 의존성을 들여오면 게이트웨이의
의존성 최소 원칙이 깨지고 버전 충돌이 생긴다.
"""

from __future__ import annotations

import dataclasses
import re

from llmgw.extensions import v1

# 한국 주민등록번호 형태. 앞 6자리 생년월일 + 뒤 7자리.
# 검증(체크섬)은 하지 않는다. 예제이므로 형태만 본다.
_KOREAN_ID = re.compile(r"\b\d{6}[-\s]?\d{7}\b")

_MASK = "[주민등록번호 삭제됨]"

# 프롬프트 길이 상한 기본값. 실제 값은 배포마다 다르므로 생성자로 받는다.
_DEFAULT_MAX_CHARS = 20_000


class MaskKoreanIdFilter:
    """요청 본문에서 주민등록번호 형태를 지운다.

    사용자 메시지만 검사한다. `system` 메시지는 운영자가 넣은 지시문이므로
    건드리지 않는다.
    """

    def filter_request(
        self, payload: v1.RequestPayload, *, context: v1.RequestContext
    ) -> v1.RequestPayload:
        """개인정보를 가린 요청을 반환한다.

        Args:
            payload: 원본 요청 본문.
            context: 요청 컨텍스트. 이 필터는 쓰지 않는다.

        Returns:
            마스킹된 요청. 바꿀 것이 없으면 받은 객체를 그대로 반환한다.
        """
        del context  # 이 필터는 주체나 모델에 따라 다르게 동작하지 않는다.
        masked: list[v1.Message] = []
        changed = False
        for message in payload.messages:
            if message.role != "user":
                masked.append(message)
                continue
            replaced = _KOREAN_ID.sub(_MASK, message.content)
            if replaced != message.content:
                changed = True
            masked.append(v1.Message(role=message.role, content=replaced))

        if not changed:
            # 원본을 그대로 반환하면 게이트웨이가 "변형 없음" 으로 기록한다.
            return payload
        return dataclasses.replace(payload, messages=tuple(masked))


class RejectLongPromptFilter:
    """프롬프트가 너무 길면 요청을 거부한다.

    입력 토큰이 비용의 상당 부분을 차지하므로, 실수로 대용량 문서를 붙여
    넣는 것을 예산 소진 전에 막는 용도다.
    """

    def __init__(self, max_chars: int = _DEFAULT_MAX_CHARS) -> None:
        """상한을 설정한다.

        Args:
            max_chars: 모든 메시지 길이의 합에 대한 상한.
        """
        self._max_chars = max_chars

    def filter_request(
        self, payload: v1.RequestPayload, *, context: v1.RequestContext
    ) -> v1.RequestPayload:
        """상한을 넘지 않으면 요청을 그대로 통과시킨다.

        Args:
            payload: 원본 요청 본문.
            context: 요청 컨텍스트. 이 필터는 쓰지 않는다.

        Returns:
            받은 요청 그대로.

        Raises:
            RequestRejectedError: 총 길이가 상한을 넘은 경우.
        """
        del context
        total = sum(len(message.content) for message in payload.messages)
        if total > self._max_chars:
            # 실제 길이를 메시지에 넣는다. 프롬프트 내용은 넣지 않는다.
            raise v1.RequestRejectedError(
                f"프롬프트가 너무 길다: {total}자 (상한 {self._max_chars}자)"
            )
        return payload
