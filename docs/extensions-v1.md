# 확장점 v1

게이트웨이의 요청 처리 경로에 자체 코드를 끼워 넣는 방법이다. 첫 릴리스는
**요청 필터(`RequestFilter`)** 하나만 공개한다.

## 신뢰 경계 — 먼저 읽어야 할 것

**확장은 샌드박스가 아니다.** 게이트웨이 프로세스 안에서 완전히 신뢰된 코드로
동작한다. 활성화한 확장은 다음을 할 수 있다.

- 모든 요청의 프롬프트와 응답 본문 읽기
- `LLMGW_ADMIN_TOKEN` 환경변수 읽기
- ECS 태스크 역할 자격증명 획득 (registry·usage 테이블 읽기·변조, Bedrock 호출)
- 인터넷으로 데이터 내보내기
- 다른 모듈 교체(monkey patch), 프로세스 종료

`--task-subnet-mode private-nat` 도 이것을 막지 못한다. 경로만 NAT 로 바뀔 뿐
인터넷 이그레스는 그대로다. 컨테이너가 비root 로 도는 것은 호스트·파일 권한
방어에 도움이 되지만 같은 프로세스의 메모리와 환경변수는 보호하지 못한다.

BYOC 라서 "고객이 자기 계정에 자기가 설치한 코드" 라는 점은 **책임 소재**를
분명히 하지만 **기술적 영향 범위를 줄이지는 않는다.** 탈취된 확장 패키지는
고객의 AWS 권한과 프롬프트를 동시에 얻는다.

따라서:

- 설치와 활성화를 분리했다. 설치만으로는 동작하지 않는다.
- 확장 버전과 wheel 해시, 기반 이미지 다이제스트를 고정한다.
- 실행 시점 `pip install` 을 하지 않는다.
- 코드를 읽고 공급망을 검증한 확장만 활성화한다.

파이썬 수준의 샌드박싱(제한된 import, `RestrictedPython`, 별도 스레드)은
보안 경계가 아니다. 실질적인 격리가 필요하면 별도 ECS 서비스나 Lambda 로
분리하고 인증된 내부 호출을 써야 한다. 그 구조는 이 확장점의 범위가 아니다.

## 계약

공개 API 는 `llmgw.extensions.v1` 에만 있다. 내부 모듈(`schemas`, `domain`,
`services`)은 노출하지 않는다.

```python
from llmgw.extensions import v1


class MyFilter:
    def filter_request(
        self, payload: v1.RequestPayload, *, context: v1.RequestContext
    ) -> v1.RequestPayload:
        if "금지어" in payload.messages[-1].content:
            raise v1.RequestRejectedError("정책 위반")
        return payload
```

**동기 함수여야 한다.** 게이트웨이의 요청 핸들러가 모두 동기다(boto3 가 동기
라이브러리다). Starlette 이 threadpool 에서 실행한다.

### 호출 시점

```
인증 → 레이트리밋 → 단가정책 → 모델권한 → 예산
     → [요청 필터]  ← 여기
     → Bedrock 요청 변환 → Bedrock 호출 → 사용량 기록
```

확장은 이미 인증되고 권한이 확인된 요청만 본다. 그리고 **Bedrock 호출 전**이라
거부해도 비용이 발생하지 않는다.

### 확장이 바꿀 수 없는 것

| 항목 | 이유 |
|---|---|
| `model_id` | 모델 허용 목록과 단가 정책을 이미 통과한 값이다. 바꿀 수 있으면 권한 검사를 우회한다 |
| `streamed` | 응답 형식은 클라이언트와의 계약이다 |
| 토큰 수·비용 | Bedrock 이 실제 청구한 값을 기록한다 |

둘 다 `RequestContext` 에만 있고 반환 `RequestPayload` 에는 없다. 타입 차원에서
막았다.

프롬프트를 늘리면 **그만큼 입력 토큰과 비용이 늘고, 그것이 정확한 기록이다.**

### 반환과 거부

- 통과·변형: `RequestPayload` 를 반환한다. 변형하지 않으려면 받은 객체를 그대로
  반환한다. `None` 을 반환하면 안 된다 — "통과" 와 "본문 제거" 를 구분할 수 없다.
- 거부: `v1.RequestRejectedError` 를 던진다. 게이트웨이가 403 `request_rejected`
  로 응답한다.

객체는 모두 frozen 이다. 제자리에서 수정하지 말고 `dataclasses.replace` 로 새
객체를 만든다. 확장이 원본을 수정하면 실행 순서에 따라 결과가 달라지고 원본이
무엇이었는지 알 수 없어 감사가 불가능해진다.

## 실패 정책 — fail-closed

| 상황 | 결과 |
|---|---|
| 확장이 `RequestRejectedError` | 403, Bedrock 호출 없음 |
| 확장이 그 밖의 예외 | **503**, Bedrock 호출 없음 |
| 제한 시간 초과 | **503**, 이후 요청도 즉시 503 |
| 잘못된 형식·빈 대화 반환 | **503** |

고장을 통과시키지 않는다. 개인정보 마스킹이 고장났을 때 요청을 흘려보내면
확장을 켠 의미가 없다.

확장이 던진 예외 문구는 클라이언트로 나가지 않는다. 프롬프트 조각이나
자격증명이 섞일 수 있어서다. 상세 원인은 서버 로그에만 남는다. 확장이 임의
HTTP 상태 코드를 고르는 것도 허용하지 않는다.

### 제한 시간의 한계

확장은 전용 단일 워커 스레드에서 실행되고 `LLMGW_EXTENSION_TIMEOUT_SECONDS`
(기본 1초)까지 기다린다. 넘기면 결과를 버리고 그 확장을 차단 상태로 표시해
이후 요청을 즉시 실패시킨다. 워커가 묶인 상태로 계속 작업을 넣으면 큐만
쌓이기 때문이다.

**파이썬 스레드는 강제로 멈출 수 없다.** 제한 시간은 "늦게 온 결과를 쓰지
않는다" 는 보장일 뿐, 확장 코드가 CPU 를 계속 쓰거나 외부 부작용을 일으키는
것을 막지 못한다. 확장이 네트워크 호출을 한다면 `context.deadline_at` 을 보고
자체 타임아웃을 걸어야 한다.

Bedrock 읽기 타임아웃(기본 300초)과 달리 짧게 잡은 이유는 필터가 지역 검사나
짧은 정책 조회여야 하기 때문이다.

## 활성화

```bash
LLMGW_REQUEST_FILTERS="mypkg.filters:MaskPii,mypkg.filters:RejectLongPrompt"
```

`module:Class` 명세를 쉼표로 구분한다. **적은 순서가 적용 순서**이고, 뒤의
확장은 앞의 확장이 반환한 값을 본다. 순서가 결과를 바꾸므로 순서가 계약의
일부다.

명세한 클래스는 **인자 없이 생성**할 수 있어야 한다.

### 자동 발견을 하지 않는 이유

설치만으로 요청 경로에 코드가 끼어들면 안 된다. 파이썬 모듈은 import 만으로
최상위 코드가 실행되고, 확장은 프롬프트와 자격증명에 접근할 수 있다. 의존성을
하나 추가한 것이 곧 그 권한을 준 것이 되어서는 안 된다.

나열하지 않은 모듈은 **import 조차 하지 않는다.**

### 로딩 실패는 기동 실패다

설정에 적은 확장을 불러오지 못하면 게이트웨이가 시작하지 않는다. 확장 없이
기동하면 운영자는 필터가 동작한다고 믿는 상태에서 필터 없이 트래픽을 받는다.
그것이 가장 나쁜 결과다.

기동 시 활성 확장 목록이 로그에 남는다.

```bash
aws logs filter-log-events --log-group-name /ecs/llmgw-dev \
  --filter-pattern '{ $.message = "요청 필터 확장을 활성화했다" }'
```

## 설치 — Docker 를 다시 써야 한다

**정직하게 적는다.** v1.11.0 에서 없앤 "Docker 불필요" 이점은 **자체 확장을
쓸 때는 유지되지 않는다.** 임의의 파이썬 코드를 이미 만들어진 이미지에
런타임으로 붙이는 방법은 없다.

| 경로 | Docker | 설명 |
|---|---|---|
| 확장 없음 | 불필요 | 공개 이미지 그대로 |
| 자체 확장 | **필요** | 공개 이미지를 기반으로 파생 이미지 빌드 |

```dockerfile
# 다이제스트로 고정한다. 태그는 다른 이미지를 가리킬 수 있다.
FROM ghcr.io/jeonghun-app/llm-gateway-bedrock@sha256:<다이제스트>

USER root
COPY wheels/ /tmp/wheels/
# --no-index 로 사설망에서도 빌드되게 하고 공급망을 고정한다.
RUN /opt/venv/bin/pip install --no-index --find-links=/tmp/wheels \
      "my-llmgw-extension==1.0.0" \
    && rm -rf /tmp/wheels
USER 10001:10001
```

```bash
./scripts/deploy.sh --allowed-cidr <IP>/32 \
  --image <계정ID>.dkr.ecr.<리전>.amazonaws.com/llmgw-custom:1.0.0 \
  --request-filters "my_ext.filters:MaskPii"
```

**태스크 시작 시 `pip install` 은 하지 않는다.** 빌드가 재현되지 않고, 공급망이
실행 시점에 바뀌며, 사설망에서 PyPI 에 닿지 않는다.

## 버전 정책

애플리케이션 버전과 확장 계약 버전을 분리한다. 계약은 `llmgw.extensions.v1`
이다.

- v1 에 기본값 있는 필드를 추가하는 것은 허용한다.
- Protocol 에 필수 메서드를 추가하지 않는다.
- 의미·호출 순서·실패 정책을 바꾸면 `v2` 를 새로 만들고 전환 기간 동안 둘을
  함께 지원한다.

`Message.content` 가 문자열인 것은 현재 계약이 **텍스트 Converse 범위**라는
뜻이다. 멀티모달은 v2 가 필요하다.

## 예제

[`examples/extensions/request_filters.py`](../examples/extensions/request_filters.py)
에 두 개가 있다. 하나는 변형 경로(주민등록번호 마스킹), 하나는 거부 경로
(프롬프트 길이 상한)를 보여준다.

**예제는 프로덕션용이 아니다.** 정규식 기반 개인정보 탐지는 오탐과 미탐이
모두 많다. 실제로는 Amazon Bedrock Guardrails 처럼 전용 기능을 쓰는 편이 낫다.

## 아직 없는 것

`ResponseFilter` 와 `UsageSink` 는 계약을 확정하지 않았다.

`ResponseFilter` 는 스트리밍에서 근본적인 문제가 있다. **이미 보낸 청크는
회수할 수 없다.** "제 번호는 900101-" 까지 전송한 뒤 다음 청크에서 패턴이
완성되면 이미 늦었다. lookahead 윈도우로 일부를 잡을 수 있지만 일반적인
필터에 대해 보장할 수 있는 크기가 없다. 현재 방향은 응답 필터가 활성이면
스트리밍 요청을 **명시적으로 거부**하는 것이다. 조용히 비스트리밍으로
격하하면 클라이언트 SDK 가 예측할 수 없게 깨진다.

`UsageSink` 는 전달 보장 수준을 먼저 정해야 한다. 사용량의 authoritative
source 는 DynamoDB 트랜잭션이고 Sink 는 보조 전송이다. 신뢰할 수 있는 외부
전달이 필요하면 메모리 큐가 아니라 트랜잭션에 outbox 항목을 함께 쓰고 별도
워커가 재시도해야 한다.

`UsageSink` 를 구현할 때 멱등 키는 **`usage_id`** 다. `request_id`
(`X-Request-Id`)를 쓰면 안 된다. 클라이언트가 같은 값으로 재시도하면 호출
횟수만큼 별도로 집계되는 상관관계 ID 이기 때문이다.
