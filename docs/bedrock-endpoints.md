# Bedrock 엔드포인트 선택: `bedrock-runtime` 과 `bedrock-mantle`

Amazon Bedrock 은 추론 엔드포인트를 두 개 제공한다. 이 게이트웨이는 그중
`bedrock-runtime` 의 Converse API 를 쓴다. 이 문서는 두 엔드포인트의 차이,
게이트웨이가 한쪽을 고른 이유, 그리고 **AWS 가 이미 OpenAI 호환 API 를
제공하는데도 이 게이트웨이가 필요한 이유**를 정리한다.

마지막 항목이 가장 중요하다. 이 프로젝트를 도입할지 판단하는 근거다.

출처: [Endpoints supported by Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html),
[Chat Completions API](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions-mantle.html).
내용은 라이선스 준수를 위해 재구성했다.

---

## 1. 두 엔드포인트

| | `bedrock-runtime` | `bedrock-mantle` |
|---|---|---|
| 호스트 | `bedrock-runtime.{region}.amazonaws.com` | `bedrock-mantle.{region}.api.aws` |
| AWS 권장 | 신규 애플리케이션 권장 | 기존 사용은 계속 지원 |
| IAM 서비스 접두어 | `bedrock` | `bedrock-mantle` |

먼저 흔한 오해를 정리한다. **두 엔드포인트 모두 Mantle 추론 엔진 위에서
동작한다.** `bedrock-mantle` 이라는 이름은 두 엔드포인트 표면 중 하나를
가리키는 것이고, Mantle 엔진을 쓰는지 여부를 뜻하지 않는다. 따라서 Mantle
엔진의 zero operator access(ZOA) 설계는 양쪽 모두에 적용된다.

### API 지원

| API | `bedrock-runtime` | `bedrock-mantle` |
|---|---|---|
| `InvokeModel` | O | X |
| `Converse` / `ConverseStream` | O | X |
| Chat Completions (OpenAI 호환) | O (`/openai/v1` 경로) | O (`/v1` 경로) |
| Responses API (OpenAI 호환) | O | O |
| Messages API (Anthropic 네이티브) | O | O |

### 기능 차이

| 기능 | `bedrock-runtime` | `bedrock-mantle` |
|---|---|---|
| 크로스리전 추론 (지역·글로벌 프로파일) | O | X |
| Guardrails | O | X |
| 지능형 프롬프트 라우팅 | O | X |
| 프롬프트 캐싱 | O | 모델에 따라 다름 |
| 서버 사이드 도구 사용 | X | O |
| 사전 구성 도구 (웹 검색 등) | X | O |
| 비동기·장시간 추론 (`background=true`) | X | O |
| 상태 유지 대화 관리 | O | O |
| 클라이언트 사이드 도구 사용 | O | O |
| Projects / Workspaces | 기본 프로젝트만 | O |
| 구조화 출력 (Messages API) | O | X (400 오류) |

### 인증과 사용량 귀속

| 항목 | `bedrock-runtime` | `bedrock-mantle` |
|---|---|---|
| SigV4 | O | O |
| Bedrock API 키 (OpenAI SDK 호환) | O | O |
| 사용량 귀속 수단 | IAM 프린시펄, 요청별 메타데이터 태깅, 애플리케이션 추론 프로파일 | Projects, Workspaces |

### 처리량 모델

두 엔드포인트의 접근 방식이 다르다.

- **`bedrock-runtime`** — 모델별로 고정된 RPM/TPM 쿼터가 있고 증액을 신청한다.
  전통적인 계정별 쿼터 방식이다.
- **`bedrock-mantle`** — 스케줄링과 작업 큐잉으로 공정 분배를 하면서 초기
  처리량 한도를 더 높게 준다. 대부분 즉시 처리되지만, 진행 중인 작업이 끝날
  때까지 요청이 잠깐 대기할 수 있다.

### 요금

**같은 모델의 토큰 단가는 두 엔드포인트에서 동일하다.** 비용으로 고르는 것이
아니라 필요한 API 와 기능으로 고른다.

---

## 2. 게이트웨이가 `bedrock-runtime` + Converse 를 고른 이유

### 크로스리전 추론 프로파일이 필수다

가장 결정적인 이유다. [Claude 연동 문서](models-claude.md) 1절에 적었듯이,
현행 Claude 모델은 전부 `INFERENCE_PROFILE` 전용이고 `us.` / `global.` 프로파일
ID 로만 호출된다. 크로스리전 추론은 **`bedrock-runtime` 에서만** 지원된다.

`bedrock-mantle` 을 쓰면 이 게이트웨이의 주된 사용 대상인 최신 Claude 모델을
호출할 수 없다. 다른 어떤 장점보다 이 제약이 앞선다.

### Converse 가 모델별 스키마 차이를 흡수한다

`InvokeModel` 은 Anthropic, Nova, Llama, Mistral 마다 요청·응답 본문이 다르다.
게이트웨이가 그 차이를 직접 다루면 새 모델이 나올 때마다 코드를 고쳐야 한다.
Converse 는 단일 스키마로 통일해 준다.

실측으로도 확인됐다. 게이트웨이는 코드 변경 없이 Nova 3종, Claude, GPT-OSS 를
같은 경로로 중계한다.

### Guardrails 를 붙일 여지를 남긴다

현재 v1.1 은 Guardrails 를 연동하지 않는다. 하지만 조직 게이트웨이에서
입출력 필터링은 자연스러운 다음 단계다. Guardrails 는 `bedrock-runtime` 에만
있으므로, 여기에 있으면 나중에 추가할 수 있고 `bedrock-mantle` 로 갔으면
불가능해진다.

### boto3 로 호출 가능하다

Converse 는 AWS SDK 로 호출한다. SigV4 서명, 재시도, 자격증명 갱신, 커넥션
풀링을 SDK 가 처리한다. `bedrock-mantle` 은 REST + Bedrock API 키 방식이라
HTTP 클라이언트와 키 수명 관리를 직접 다뤄야 한다. ECS 태스크 역할로 자격증명
관리를 위임하는 현재 구조와 맞지 않는다.

### 탈락시킨 선택지

**`bedrock-mantle` 로 중계** — 크로스리전 프로파일 미지원으로 탈락. 위 참고.

**`bedrock-runtime` 의 `/openai/v1/chat/completions` 로 중계** — OpenAI 형식을
그대로 넘길 수 있어 변환 코드가 필요 없다는 장점이 있다. 그런데 게이트웨이는
어차피 요청을 파싱해 모델 허용 검사와 토큰 집계를 해야 하므로 변환 비용이
절감되지 않는다. 그리고 boto3 대신 직접 HTTP 를 다뤄야 하는 단점이 남는다.

**양쪽을 모두 지원하는 어댑터** — 실질적 이득 대비 유지 비용이 크다. 두 배의
오류 경로와 두 배의 테스트가 필요하고, 어느 경로로 처리됐는지에 따라 사용량
집계 의미가 달라진다. v1.1 범위에서 제외했다.

---

## 3. AWS 가 OpenAI 호환 API 를 제공하는데 이 게이트웨이가 왜 필요한가

이 질문에 정직하게 답해야 한다. 두 엔드포인트 모두 **base URL 과 API 키만
바꾸면 기존 OpenAI SDK 코드를 쓸 수 있다.** 즉 "OpenAI 호환" 자체는 이
게이트웨이의 가치가 아니다. AWS 가 이미 제공한다.

게이트웨이가 실제로 더하는 것은 **조직 단위 거버넌스 계층**이다.

| 필요한 것 | Bedrock 기본 제공 | 이 게이트웨이 |
|---|---|---|
| OpenAI SDK 호환 | O | O (동일) |
| 사용량 귀속 | IAM 프린시펄 / Projects / Workspaces | **계정 → 팀 → 사용자 → 키** 4단 계층 |
| 사용자별 자격증명 | IAM 사용자·역할 또는 Bedrock API 키 | 게이트웨이 발급 키. IAM 을 나눠주지 않는다 |
| 예산 강제 | 없음 (Budgets 는 사후 알림) | **요청 차단**. 4단계 각각에 월 한도 |
| 모델 접근 제어 | IAM 정책 | 키별 허용 목록. 발급이 API 한 번 |
| 조직 관점 대시보드 | CloudWatch, Cost Explorer | 팀·사용자별 비용·토큰·지연·에러율 UI |
| 사용자 추가 | IAM 프린시펄 생성 | 관리 API 호출 |

핵심 차이는 두 가지다.

**첫째, IAM 을 나눠주지 않는다.** Bedrock 의 귀속 수단은 모두 AWS 측 구성물이다.
IAM 프린시펄로 귀속하려면 사용자마다 IAM 자격증명을 발급해야 하고, Projects 나
Workspaces 는 AWS 리소스라 생성·회수에 AWS 권한이 필요하다. 사내 개발자
50명에게 IAM 자격증명을 나눠주는 것과 게이트웨이 API 키를 발급하는 것은
운영 부담이 다르다.

**둘째, 예산을 사후 알림이 아니라 사전 차단으로 처리한다.** AWS Budgets 는
임계값을 넘으면 알려주지만 호출을 막지 않는다. 게이트웨이는 이번 달 누적치를
확인해 한도를 넘으면 `429` 로 거부한다. 팀별·사용자별 상한을 실제로 강제할 수
있다.

### 게이트웨이가 필요 없는 경우

솔직하게, 아래에 해당하면 이 프로젝트를 쓰지 않는 편이 낫다. 홉이 하나
줄어들고 운영 대상이 하나 사라진다.

- 애플리케이션이 하나이고 사용자별 분리가 필요 없다
- 사용량 귀속을 IAM 프린시펄 단위로 하면 충분하다
- 팀별 예산 **강제**가 필요 없고 사후 리포트로 충분하다
- 도구 사용, 비전, 프롬프트 캐싱, Guardrails 같은 기능을 써야 한다
  (게이트웨이가 중계하지 않는다)
- 게이트웨이 한 홉의 지연과 가용성 의존을 받아들일 수 없다

### 두 개를 함께 쓸 수도 있다

배타적 선택이 아니다. 실제로 이런 구성이 합리적이다.

- **일반 사내 사용자** → 게이트웨이 (키 발급, 예산, 팀별 집계)
- **에이전트 워크로드** → `bedrock-mantle` 직접 (서버 사이드 도구, 비동기)
- **Guardrails 필요 서비스** → `bedrock-runtime` 직접

---

## 4. Mantle 전용 기능이 필요할 때

게이트웨이를 통해서는 쓸 수 없다. 애플리케이션이 직접 호출한다.

```python
from openai import OpenAI

# bedrock-mantle 직접 호출. Bedrock API 키가 필요하다.
client = OpenAI(
    base_url="https://bedrock-mantle.us-east-1.api.aws/v1",
    api_key="<Bedrock API 키>",
)
```

이 경로를 쓰면 게이트웨이의 집계·예산·모델 제한이 적용되지 않는다는 점을
분명히 인지해야 한다. 지출 가시성이 그만큼 사라진다.

완화책으로 다음을 권장한다.

- Mantle 은 **Projects / Workspaces** 로 워크로드를 분리하고 애플리케이션 단위
  비용을 추적할 수 있다. 게이트웨이 집계와 별개로 이쪽을 설정한다.
- Mantle 을 쓰는 IAM 역할을 게이트웨이 태스크 역할과 분리해, 어느 경로의
  지출인지 태그와 Cost Explorer 로 구분한다.

### Guardrails 를 쓰려면

Guardrails 는 `bedrock-runtime` 에 있으므로 게이트웨이에 추가하는 것이
가능하다. Chat Completions 경로에서는 `X-Amzn-Bedrock-GuardrailIdentifier` 와
`X-Amzn-Bedrock-GuardrailVersion` 헤더로 지정한다. Converse 경로에서는
`guardrailConfig` 파라미터를 쓴다.

게이트웨이에 도입할 때의 설계 지점은 다음과 같다.

1. Guardrail ID/버전을 어디에 둘 것인가 — 전역 설정, 계정별, 키별 중 선택.
   조직 정책 성격이라 **계정 단위**가 자연스럽다.
2. Guardrail 개입 시 응답 처리 — `stopReason` 이 `guardrail_intervened` 로
   오므로 이미 `content_filter` 로 매핑되어 있다.
3. 집계 축 추가 — 어느 요청이 차단됐는지 보려면 `blocked_requests` 카운터가
   필요하다. `unpriced_requests` 와 같은 방식으로 추가할 수 있다.

v1.1 에는 포함하지 않았다. 관심사가 다른 기능이라 별도 릴리스로 분리하는 것이
검증하기 쉽다.

---

## 5. 비용 관련 운영 권고

문서에 명시된 사항 하나를 반영해 둘 필요가 있다. VPC 안에서 Bedrock 을 호출할
때 **VPC 인터페이스 엔드포인트(AWS PrivateLink)** 를 쓰면 트래픽이 AWS 네트워크
안에 머물러 NAT 게이트웨이나 인터넷 게이트웨이에 따르는 데이터 송신 요금을
피할 수 있다.

현재 이 게이트웨이는 태스크를 퍼블릭 서브넷에 두고 IGW 로 나간다. 판단 근거는
[ADR 0003](adr/0003-network-and-exposure.md) 에 있다. **토큰 처리량이 커지면
이 선택을 재검토해야 한다.** 인터페이스 엔드포인트의 시간 요금(엔드포인트·AZ당
약 $0.01/시간)보다 송신 요금이 커지는 지점이 존재한다.

대략적인 손익분기:

```
인터페이스 엔드포인트 1개 × 2 AZ ≈ 월 $14.6 (시간 요금만)
IGW 데이터 송신 ≈ $0.09/GB (첫 10TB 구간)

→ 월 약 160GB 이상 Bedrock 트래픽이 발생하면 엔드포인트가 유리해진다
```

응답 토큰이 많은 워크로드에서는 생각보다 빨리 도달한다. CloudWatch 의
`OutputTokens` 메트릭으로 실제 트래픽을 추정해 판단한다.

---

## 6. 요약

| 질문 | 답 |
|---|---|
| 게이트웨이는 어느 엔드포인트를 쓰는가 | `bedrock-runtime` 의 Converse API |
| 왜 `bedrock-mantle` 이 아닌가 | 크로스리전 추론 프로파일 미지원. 현행 Claude 를 호출할 수 없다 |
| 두 엔드포인트 중 어느 쪽이 빠른가 | 엔진이 같다. 처리량 관리 방식이 다르다 |
| 요금 차이가 있는가 | 같은 모델이면 토큰 단가 동일 |
| AWS 가 OpenAI 호환을 제공하는데 왜 게이트웨이인가 | 호환성이 아니라 계정·팀·사용자 단위 귀속과 예산 강제 때문 |
| 도구 사용·웹 검색이 필요하면 | `bedrock-mantle` 을 직접 호출한다. 게이트웨이 집계에서 빠진다 |
| Guardrails 를 쓰려면 | `bedrock-runtime` 에 있으므로 게이트웨이에 추가 가능. v1.1 미포함 |
