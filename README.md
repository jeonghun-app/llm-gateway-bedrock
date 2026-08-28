# LLM Gateway

Amazon Bedrock 앞단에 놓이는 **OpenAI 호환 API 게이트웨이**다. 조직이 여러 팀과
사용자에게 LLM 접근을 나눠줄 때 생기는 세 가지 문제를 해결한다.

1. **누가 얼마를 썼는지 모른다.** Bedrock 청구서는 계정 단위로만 나온다. 팀별,
   사용자별로 쪼갤 수 없다.
2. **접근을 통제할 수 없다.** IAM 자격증명을 나눠주면 모델 제한이나 예산 한도를
   걸기 어렵다.
3. **기존 코드를 고쳐야 한다.** OpenAI SDK 로 짠 애플리케이션을 Bedrock 으로
   옮기려면 요청/응답 형식을 다시 맞춰야 한다.

이 게이트웨이는 OpenAI 호환 엔드포인트를 제공해 `base_url` 만 바꾸면 붙고,
요청마다 **계정 / 팀 / 사용자 / 모델** 축으로 토큰·비용·지연·에러를 기록하고,
API 키 단위로 모델 허용 목록과 월 예산을 강제한다.

AWS 자격증명만 있으면 명령 하나로 VPC 부터 DynamoDB 까지 전부 생성된다.

---

## 목차

- [아키텍처](#아키텍처)
- [사전 요구사항](#사전-요구사항)
- [배포](#배포)
- [접근 통제 (다중 단말)](#접근-통제-다중-단말)
- [사용법](#사용법)
- [모니터링 대시보드](#모니터링-대시보드)
- [로컬 실행](#로컬-실행)
- [테스트](#테스트)
- [환경변수](#환경변수)
- [배포 파라미터](#배포-파라미터)
- [비용](#비용)
- [관측 지점과 트러블슈팅](#관측-지점과-트러블슈팅)
- [삭제](#삭제)
- [보안상 알아야 할 점](#보안상-알아야-할-점)
- [문서](#문서)

---

## 아키텍처

```mermaid
flowchart LR
  client["OpenAI SDK / curl"]
  browser["브라우저 (대시보드)"]

  subgraph aws["AWS (기본 us-east-1)"]
    alb["Application Load Balancer<br/>접근 CIDR 제한"]

    subgraph vpc["전용 VPC / 퍼블릭 서브넷 2개"]
      task["ECS Fargate<br/>FastAPI + uvicorn"]
    end

    bedrock["Bedrock Runtime<br/>Converse / ConverseStream"]
    registry[("DynamoDB<br/>registry")]
    usage[("DynamoDB<br/>usage")]
    agg[("DynamoDB<br/>usage-agg")]
    logs["CloudWatch Logs<br/>JSON 로그 + EMF 메트릭"]
    secret["Secrets Manager<br/>관리 토큰"]
  end

  client -->|"POST /v1/chat/completions"| alb
  browser -->|"GET /ui/"| alb
  alb --> task
  task --> bedrock
  task --> registry
  task --> usage
  task --> agg
  task --> logs
  secret -.->|"태스크 시작 시 주입"| task
```

**요청 처리 흐름**

```mermaid
sequenceDiagram
  participant C as 클라이언트
  participant G as 게이트웨이
  participant D as DynamoDB
  participant B as Bedrock

  C->>G: POST /v1/chat/completions (Bearer sk-llmgw-...)
  G->>D: 키 해시로 조회 → 계정/팀/사용자/예산
  G->>G: 모델 허용 목록 검사
  G->>D: 이번 달 누적 비용 조회 (예산이 설정된 경우만)
  G->>B: Converse / ConverseStream
  B-->>G: 응답 + 토큰 사용량
  G->>D: TransactWriteItems<br/>원본 1건 + 집계 10건 (원자적)
  G-->>C: OpenAI 형식 응답
```

사용량 원본 쓰기와 집계 갱신은 **하나의 트랜잭션**이다. 원본 쓰기에
`attribute_not_exists` 조건이 걸려 있어, 같은 `X-Request-Id` 로 재시도하면
트랜잭션 전체가 취소되고 집계가 두 번 더해지지 않는다.

**사용 중인 AWS 서비스**

| 용도 | 서비스 |
|---|---|
| 컴퓨트 | ECS Fargate |
| 진입점 | Application Load Balancer |
| LLM | Amazon Bedrock (Converse API) |
| 데이터 | DynamoDB 3개 (온디맨드, PITR, SSE-KMS) |
| 시크릿 | Secrets Manager (토큰 자동 생성) |
| 레지스트리 | ECR (스캔 온 푸시, 태그 불변) |
| 관측 | CloudWatch Logs, EMF 커스텀 메트릭, 알람 4개, SNS |
| 네트워크 | 전용 VPC, IGW, S3/DynamoDB 게이트웨이 엔드포인트 |
| IaC | CloudFormation 2스택 |

설계 판단의 근거는 [`docs/adr/`](docs/adr/) 에 있다.

---

## 사전 요구사항

| 항목 | 버전 | 용도 |
|---|---|---|
| AWS 자격증명 | - | 배포 대상 계정 |
| AWS CLI | v2 | 스택 배포, 시크릿 조회 |
| Docker | 20+ | 이미지 빌드 |
| `jq` | 1.6+ | 배포 스크립트의 JSON 처리 |
| `git` | 2.x | 이미지 태그 생성 |
| Python | 3.13 | 로컬 개발과 테스트 (배포에는 불필요) |

**Bedrock 모델 액세스를 먼저 켜야 한다.** AWS 콘솔 → Bedrock → Model access 에서
사용할 모델을 활성화한다. 활성화하지 않으면 배포는 성공하지만 모든 LLM 호출이
`AccessDeniedException` 으로 실패한다. 배포 스크립트가 시작 시 확인해 경고한다.

배포에 필요한 IAM 권한은 CloudFormation, EC2(VPC), ELBv2, ECS, ECR, DynamoDB,
IAM, Secrets Manager, CloudWatch, SNS, Application Auto Scaling 에 대한 생성·수정
권한이다.

---

## 배포

```bash
git clone <이 리포지토리>
cd LLMGateway

# 접속할 단말의 공인 IP 만 열고 배포한다.
./scripts/deploy.sh --allowed-cidr "$(curl -s https://checkip.amazonaws.com)/32"
```

`--allowed-cidr` 는 **필수**이고 여러 번 지정할 수 있다. `0.0.0.0/0` 과 모든
`/0` 프리픽스는 거부된다.

배포 후 단말 추가·삭제는 **스택 재배포 없이** 수 초 안에 끝난다.

```bash
./scripts/manage_access.sh add-me --label "재택-노트북"
./scripts/manage_access.sh add 203.0.113.0/28 --label "본사-사무실"
./scripts/manage_access.sh list
./scripts/manage_access.sh remove 198.51.100.5/32
./scripts/manage_access.sh check     # 지금 이 단말이 접근 가능한지
```

자세한 내용은 [접근 통제](#접근-통제-다중-단말) 절을 본다.

스크립트가 하는 일:

1. 도구·자격증명·Bedrock 접근 확인, 잔여 리소스 검사
2. ECR 스택 생성
3. 이미지 빌드와 푸시 (같은 태그가 이미 있으면 건너뛴다)
4. 애플리케이션 스택 생성 (VPC, ALB, ECS, DynamoDB, IAM, 알람)
5. `/healthz` 가 200 을 반환할 때까지 대기
6. 데모 계정 2개·팀 4개·사용자 6명·키 6개 생성 후 실제 Bedrock 호출로 사용량 생성
7. 스모크 테스트 (인증 경계, 스트리밍, 멱등성, 집계 축 4개, 비용 계산)
8. 접속 정보 출력

소요 시간은 처음 배포 시 8~12분이다. 여러 번 실행해도 안전하다.

주요 옵션:

```bash
# 프로덕션 형태 (HTTPS + 태스크 2개 + 알람 메일)
./scripts/deploy.sh \
  --allowed-cidr 203.0.113.10/32 \
  --env prod --desired-count 2 \
  --certificate-arn arn:aws:acm:us-east-1:<계정ID>:certificate/<ID> \
  --alarm-email ops@example.com

# 여러 단말을 한 번에 열고 배포
./scripts/deploy.sh --allowed-cidr 203.0.113.10/32 \
                    --allowed-cidr 198.51.100.5/32

# 허용 모델을 좁힌 기본 정책
./scripts/deploy.sh --allowed-cidr 203.0.113.10/32 \
  --allowed-models "amazon.nova-lite-v1:0,amazon.nova-pro-v1:0"
```

전체 옵션은 `./scripts/deploy.sh --help` 로 확인한다.

---

## 접근 통제 (다중 단말)

ALB 보안 그룹은 **관리형 프리픽스 리스트 하나**만 참조한다. 단말이 몇 개든
보안 그룹 규칙은 프로토콜당 1개로 유지되고, 단말을 추가·삭제해도
CloudFormation 스택 업데이트가 필요 없다. 반영은 보통 수 초다.

프리픽스 리스트는 **빈 상태로 생성**된다. 즉 아무도 접근할 수 없는 상태에서
시작하고, 명시적으로 추가한 단말만 통과한다.

```bash
# 지금 이 단말 추가 (공인 IP 를 자동으로 /32 로)
./scripts/manage_access.sh add-me --label "재택-노트북"

# 특정 CIDR 추가
./scripts/manage_access.sh add 203.0.113.10/32 --label "사무실-맥북"
./scripts/manage_access.sh add 198.51.100.0/28 --label "본사-대역"

# 목록 확인
./scripts/manage_access.sh list

# 제거
./scripts/manage_access.sh remove 203.0.113.10/32

# 지금 이 단말이 접근 가능한지 (실제 HTTP 호출까지 확인)
./scripts/manage_access.sh check

# 전체 상태 점검 (0.0.0.0/0 규칙 존재 여부 포함)
./scripts/manage_access.sh status
```

출력 예시:

```
== 접근 허용 단말 (llmgw-dev-app)
   프리픽스 리스트 pl-0abc123def456

   CIDR                   설명
   ---------------------- ------------------------
   192.0.2.10/32          bootstrap by deploy.sh
   203.0.113.10/32        사무실-맥북
   198.51.100.0/28        본사-대역

   사용 3 / 최대 20
```

### 제약과 주의점

- 한도는 `AccessListMaxEntries`(기본 20)다. **생성 후 늘릴 수만 있고 줄일 수
  없다.** 늘리려면 파라미터를 바꿔 재배포한다.
- `0.0.0.0/0` 과 `/8` 보다 넓은 대역은 스크립트가 거부한다.
- 프리픽스 리스트 엔트리는 CloudFormation 이 관리하지 않는다. 이것이
  재배포 없이 단말을 바꿀 수 있는 이유이고, 동시에 AWS CLI 로 직접 넓은 대역을
  넣는 것이 가능하다는 뜻이다. `smoke_test.sh` 가 매 배포마다 `0.0.0.0/0`
  인바운드 규칙이 없는지 검사한다.
- 보안 그룹 규칙을 콘솔에서 직접 고치지 않는다. 다음 배포에서 되돌아간다.


## 사용법

배포 후 출력된 URL 과 관리 토큰을 쓴다. 토큰은 CloudFormation 이 생성해
Secrets Manager 에 저장하며, 사람이 정하지 않는다.

```bash
GATEWAY_URL="http://<alb-dns>"
ADMIN_TOKEN="$(aws secretsmanager get-secret-value --region us-east-1 \
  --secret-id llmgw/dev/admin-token --query SecretString --output text | jq -r .admin_token)"
```

### 계정 · 팀 · 사용자 · 키 만들기

```bash
# 계정 (월 예산 500 USD)
curl -X POST "$GATEWAY_URL/admin/accounts" \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"account_id":"acme","name":"Acme Corp","monthly_budget_usd":500}'

# 팀 (월 예산 200 USD)
curl -X POST "$GATEWAY_URL/admin/accounts/acme/teams" \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"team_id":"platform","name":"플랫폼팀","monthly_budget_usd":200}'

# 사용자
curl -X POST "$GATEWAY_URL/admin/accounts/acme/users" \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"user_id":"alice","name":"김앨리스","team_id":"platform","monthly_budget_usd":100}'

# API 키 (평문 키는 이 응답에서만 볼 수 있다)
curl -X POST "$GATEWAY_URL/admin/accounts/acme/keys" \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"user_id":"alice","name":"노트북","allowed_models":["amazon.nova-lite-v1:0"]}'
```

예산은 계정·팀·사용자·키 네 단계에 각각 걸 수 있고, **하나라도 초과하면**
`429 insufficient_quota` 로 차단된다. 예산을 지정하지 않으면 무제한이고, 그
경우 예산 확인용 DynamoDB 조회도 발생하지 않는다.

### OpenAI SDK 로 호출

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://<alb-dns>/v1",
    api_key="sk-llmgw-dev-...",  # 위에서 발급한 키
)

response = client.chat.completions.create(
    model="amazon.nova-lite-v1:0",
    messages=[{"role": "user", "content": "안녕하세요"}],
    max_tokens=256,
)
print(response.choices[0].message.content)

# 스트리밍
for chunk in client.chat.completions.create(
    model="amazon.nova-lite-v1:0",
    messages=[{"role": "user", "content": "1부터 5까지 세어줘"}],
    stream=True,
):
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
```

### curl 로 호출

```bash
curl -X POST "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer sk-llmgw-dev-..." \
  -H 'Content-Type: application/json' \
  -H "X-Request-Id: $(uuidgen)" \
  -d '{
    "model": "amazon.nova-lite-v1:0",
    "messages": [{"role":"user","content":"안녕하세요"}],
    "max_tokens": 256
  }'
```

`X-Request-Id` 를 보내면 그 값이 **멱등성 키**가 된다. 타임아웃 후 같은 ID 로
재시도해도 사용량이 두 번 집계되지 않는다.

### 지원하는 엔드포인트

| 메서드 | 경로 | 인증 | 설명 |
|---|---|---|---|
| `POST` | `/v1/chat/completions` | API 키 | 채팅 완성 (스트리밍 지원) |
| `GET` | `/v1/models` | API 키 | 키가 쓸 수 있는 모델 목록 |
| `GET` | `/healthz` | 없음 | 얕은 헬스 체크 (ALB 용) |
| `GET` | `/readyz` | 없음 | DynamoDB·Bedrock 접근 확인 |
| `GET` | `/ui/` | 관리 토큰(브라우저 입력) | 모니터링 대시보드 |
| `GET` | `/docs`, `/openapi.json` | 없음 | API 스펙 |
| `*` | `/admin/*` | `X-Admin-Token` | 계정·팀·사용자·키 관리 |
| `GET` | `/analytics/*` | `X-Admin-Token` | 사용량 집계 조회 |

전체 스펙은 [`docs/openapi.json`](docs/openapi.json) 에 있고, 배포된 게이트웨이의
`/docs` 에서 대화형으로 볼 수 있다.

OpenAI 스펙 중 Bedrock Converse 에 대응이 없는 필드(`presence_penalty`,
`logit_bias` 등)는 받아들이되 무시한다. 결과가 달라지는 `n > 1` 은 명시적으로
거부한다.

---

## 모니터링 대시보드

`http://<alb-dns>/ui/` 에 접속해 관리 토큰을 입력한다. 토큰은
`sessionStorage` 에만 저장되어 탭을 닫으면 사라진다.

보여주는 것:

- **KPI** — 요청 수(성공/실패), 총 토큰(입력/출력), 총 비용, 평균 지연, 에러율
- **일별 비용과 요청 수** 추이 (좌우 축 분리)
- **팀별 비용** 막대 그래프
- **사용자별 비용** 상위 10 막대 그래프
- **모델별 요청 비중** 도넛 차트
- **상세 표 6개 탭** — 계정 / 팀 / 사용자 / 모델 / API 키 / 최근 요청

기간 프리셋(오늘/7일/30일/90일)과 임의 구간 조회, 30초 자동 새로고침을
지원한다. 조회 범위 상한은 93일이다.

차트는 외부 라이브러리 없이 SVG 로 직접 그린다. CDN 접근이 막힌 사설망에서도
그대로 동작하고, 컨테이너 빌드에 npm 툴체인이 필요 없다.

대시보드가 읽는 데이터는 모두 사전 집계 테이블에서 나온다. 요청 수가 늘어도
대시보드 응답 시간이 변하지 않는다.

---

## 로컬 실행

```bash
python3.13 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt

export LLMGW_ENV=local
export AWS_REGION=us-east-1
export LLMGW_ADMIN_TOKEN=local-dev-token
export LLMGW_REGISTRY_TABLE=llmgw-dev-registry
export LLMGW_USAGE_TABLE=llmgw-dev-usage
export LLMGW_USAGE_AGG_TABLE=llmgw-dev-usage-agg
export LLMGW_BIND_HOST=127.0.0.1

PYTHONPATH=src ./.venv/bin/python -m llmgw
```

`http://127.0.0.1:8080/ui/` 로 접속한다. DynamoDB 와 Bedrock 은 실제 AWS 를
호출하므로 유효한 자격증명과 배포된 테이블이 필요하다.

컨테이너로 실행:

```bash
docker build -t llmgw:local .
docker run --rm -p 8080:8080 \
  -e AWS_REGION=us-east-1 \
  -e LLMGW_ADMIN_TOKEN=local-dev-token \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
  llmgw:local
```

---

## 테스트

유닛 테스트는 실제 AWS 를 호출하지 않는다. DynamoDB 는 `moto`, Bedrock 은
`botocore.stub.Stubber` 와 대역 객체로 대체한다. 대시보드 차트와 관리 UI 는
`tests/js/` 의 Node 하네스로 검증하며, Node 가 없으면 해당 테스트만 건너뛴다.

```bash
# 전체 (382개, 약 55초)
./.venv/bin/python -m pytest

# 커버리지
./.venv/bin/python -m pytest --cov=llmgw --cov-report=term-missing

# 특정 파일
./.venv/bin/python -m pytest tests/test_usage_store.py -v
```

커밋 전 전체 검증:

```bash
./.venv/bin/isort src tests scripts
./.venv/bin/black src tests scripts
./.venv/bin/ruff check src tests scripts
./.venv/bin/mypy
./.venv/bin/python -m pytest
./.venv/bin/cfn-lint infra/*.yaml
shellcheck scripts/*.sh
./.venv/bin/python scripts/export_openapi.py   # 스펙 갱신
```

배포된 환경에 대한 종단간 테스트:

```bash
LLMGW_BASE_URL="$GATEWAY_URL" LLMGW_ADMIN_TOKEN="$ADMIN_TOKEN" \
  ./scripts/smoke_test.sh
```

---

## 환경변수

컨테이너가 읽는 값이다. 모두 `LLMGW_` 접두어를 쓴다. **값은 이 표에 적지
않는다.** CloudFormation 이 태스크 정의에 설정하므로 직접 지정할 일은 로컬
실행뿐이다.

| 이름 | 필수 | 기본값 | 용도 |
|---|---|---|---|
| `LLMGW_ENV` | 아니오 | `dev` | 환경 식별자. 리소스 이름과 API 키 접두어에 쓰인다 |
| `LLMGW_AWS_REGION` / `AWS_REGION` | 아니오 | `us-east-1` | DynamoDB 등 호출 리전 |
| `LLMGW_BEDROCK_REGION` | 아니오 | (`AWS_REGION`) | Bedrock 호출 리전 |
| `LLMGW_REGISTRY_TABLE` | 아니오 | `llmgw-dev-registry` | 계정·팀·사용자·키 테이블 |
| `LLMGW_USAGE_TABLE` | 아니오 | `llmgw-dev-usage` | 요청 단위 사용량 테이블 |
| `LLMGW_USAGE_AGG_TABLE` | 아니오 | `llmgw-dev-usage-agg` | 집계 테이블 |
| `LLMGW_ADMIN_TOKEN` | **예** | (빈 값) | 관리 API·대시보드 토큰. 비면 관리 API가 503 |
| `LLMGW_DEFAULT_ALLOWED_MODELS` | 아니오 | (빈 값) | 키에 허용 목록이 없을 때 적용. 쉼표 구분. 비면 전체 허용 |
| `LLMGW_USAGE_TTL_DAYS` | 아니오 | `90` | usage 원본 보존 기간 |
| `LLMGW_PRICING_FILE` | 아니오 | 패키지 내 `pricing.json` | 모델 단가 표 경로 |
| `LLMGW_LOG_LEVEL` | 아니오 | `INFO` | 로그 레벨 |
| `LLMGW_SERVICE_NAME` | 아니오 | `llmgw` | 로그의 `service` 필드 |
| `LLMGW_METRICS_NAMESPACE` | 아니오 | `LLMGateway` | EMF 메트릭 네임스페이스 |
| `LLMGW_REQUEST_TIMEOUT_SECONDS` | 아니오 | `300` | Bedrock 읽기 타임아웃 |
| `LLMGW_BIND_HOST` | 아니오 | `0.0.0.0` | 로컬 실행 시 바인드 주소 |
| `LLMGW_PORT` | 아니오 | `8080` | 로컬 실행 시 포트 |

`LLMGW_ADMIN_TOKEN` 이 비어 있으면 관리 API 는 **통과시키지 않고 503 을
반환**한다. 토큰 미설정을 무인증 허용으로 해석하면 관리 API가 인터넷에 열린다.

---

## 배포 파라미터

`infra/app.yaml` 의 주요 파라미터다. 전체는 템플릿의 `Parameters` 절을 본다.

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `AccessListMaxEntries` | `20` | 접근 허용 목록 최대 항목 수. 생성 후 늘릴 수만 있다 |
| `CertificateArn` | 빈 값 | ACM 인증서. 주면 HTTPS + HTTP→HTTPS 리다이렉트 |
| `DesiredCount` | `1` | 상시 태스크 수. prod 는 2 이상 권장 |
| `MaxCount` | `4` | 오토스케일링 상한 (CPU 60% 목표 추적) |
| `TaskCpu` / `TaskMemory` | `512` / `1024` | 0.5 vCPU / 1 GiB |
| `VpcCidr` | `10.60.0.0/16` | 새로 만들 VPC 대역 |
| `LogRetentionDays` | `30` | 로그 보존. 무한 보존은 선택할 수 없다 |
| `UsageTtlDays` | `90` | usage 원본 TTL |
| `AllowedBedrockModelArn` | `arn:aws:bedrock:*::foundation-model/*` | 태스크 역할이 호출 가능한 모델 범위 |
| `AlarmEmail` | 빈 값 | 알람 수신 메일. 확인 메일 승인 필요 |

---

## 비용

`us-east-1` 기준 대략치다. 실제 요금은 [AWS 요금 페이지](https://aws.amazon.com/pricing/)
로 확인한다.

| 항목 | 과금 단위 | 기본 구성 월 예상 |
|---|---|---|
| ALB | 시간 + LCU | 약 $17 |
| Fargate | vCPU·초 + GB·초 | 0.5 vCPU / 1 GiB × 1 태스크 ≈ 약 $18 |
| DynamoDB | 요청당 (온디맨드) | 유휴 시 거의 $0 |
| ECR | 저장 용량 | 이미지 몇 개 기준 약 $0.1 |
| Secrets Manager | 시크릿당 | 약 $0.40 |
| CloudWatch Logs | 수집 + 저장 | 트래픽에 비례, 소규모 약 $1 |
| VPC 게이트웨이 엔드포인트 | 없음 | $0 |
| **합계 (유휴)** | | **약 $37** |
| Bedrock | 토큰 | 사용량에 비례. 별도 |

요청당 추가되는 DynamoDB 쓰기는 트랜잭션 11개 항목이라 약 22 WRU 다. 100만
요청이면 대략 $27 수준이다. 자세한 근거는 [`docs/adr/0002`](docs/adr/0002-datastore-dynamodb.md) 를 본다.

NAT Gateway 를 쓰지 않아 월 $40 를 절약한다. 대신 태스크에 공인 IP 가 붙는다.
판단 근거는 [`docs/adr/0003`](docs/adr/0003-network-and-exposure.md) 에 있다.

`./scripts/teardown.sh` 로 전부 삭제하면 과금이 멈춘다.

---

## 관측 지점과 트러블슈팅

### 로그

```bash
# 실시간
aws logs tail /ecs/llmgw-dev --region us-east-1 --follow

# 특정 요청 추적 (모든 로그에 correlation_id 가 붙는다)
aws logs filter-log-events --region us-east-1 \
  --log-group-name /ecs/llmgw-dev \
  --filter-pattern '{ $.correlation_id = "abc-123" }'

# 에러만
aws logs filter-log-events --region us-east-1 \
  --log-group-name /ecs/llmgw-dev \
  --filter-pattern '{ $.level = "ERROR" }'
```

로그는 전부 JSON 한 줄이다. `service`, `level`, `correlation_id`, `location` 이
항상 포함된다.

### 메트릭

CloudWatch 커스텀 네임스페이스 `LLMGateway`:

| 메트릭 | 차원 | 의미 |
|---|---|---|
| `Requests` | `Environment`, `Model` | 요청 수 |
| `Errors` | `Environment`, `Model` | 실패 수 |
| `InputTokens`, `OutputTokens` | `Environment`, `Model` | 토큰 수 |
| `CostUsd` | `Environment`, `Model` | 계산된 비용 |
| `LatencyMs` | `Environment`, `Model` | 처리 시간 (p50/p95/p99 산출 가능) |
| `UsageWriteFailures` | `Environment` | 집계 유실 감지 |

메트릭 차원에 계정 ID 를 넣지 않는다. 차원 조합마다 별도 과금되어 테넌트 수에
비례해 비용이 늘기 때문이다. 계정·팀·사용자별 수치는 대시보드에서 본다.

### 알람

| 알람 | 조건 |
|---|---|
| `llmgw-dev-alb-5xx` | 5분간 타깃 5xx > 5건 |
| `llmgw-dev-latency-p99` | p99 응답 시간 > 30초, 2회 연속 |
| `llmgw-dev-unhealthy-targets` | 비정상 타깃 > 0, 3회 연속 |
| `llmgw-dev-usage-write-failures` | 사용량 기록 실패 > 0 |

### 자주 만나는 문제

| 증상 | 원인과 조치 |
|---|---|
| `/healthz` 에 연결되지 않음 | 이 단말이 허용 목록에 없다. `./scripts/manage_access.sh check` 로 확인하고 `add-me` 로 추가한다 (재배포 불필요) |
| `503 storage_unavailable` | DynamoDB 테이블이 없거나 태스크 역할 권한 부족. 응답 메시지의 AWS 코드를 확인 |
| `403 model_not_allowed` | 키의 `allowed_models` 에 없는 모델. `GET /v1/models` 로 사용 가능 목록 확인 |
| `403` + "Bedrock 모델 액세스" | 콘솔 Bedrock → Model access 에서 모델 활성화 |
| `429 insufficient_quota` | 계정/팀/사용자/키 중 하나가 월 예산 초과. 대시보드에서 어느 축인지 확인 |
| `404 model_not_found` | 모델이 EOL 이거나 해당 리전에 없다. `GET /admin/models` 로 확인 |
| 대시보드 비용이 0 또는 '단가 미등록 N건' 경고 | 단가 표에 없는 모델이다. `./.venv/bin/python scripts/sync_pricing.py` 로 점검한다. 현행 Claude 가 여기 해당한다 ([상세](docs/models-claude.md#5-비용-집계와-단가-표의-현재-한계)) |
| 태스크가 계속 재시작 | `aws logs tail /ecs/llmgw-dev --since 15m` 확인. 배포 서킷 브레이커가 자동 롤백한다 |

더 자세한 절차는 [`docs/runbook.md`](docs/runbook.md) 를 본다.

---

## 삭제

```bash
# 애플리케이션 스택만 삭제 (ECR 이미지는 유지)
./scripts/teardown.sh --env dev --region us-east-1

# ECR·시크릿·잔여 테이블까지 전부 삭제
./scripts/teardown.sh --env dev --region us-east-1 --purge-data
```

확인 프롬프트에 `delete` 를 입력해야 진행된다. `--purge-data` 는 사용량 이력을
영구히 삭제한다.

---

## 보안상 알아야 할 점

- **ALB 는 인터넷에 노출된다.** 접근 허용 목록은 빈 상태로 시작하고 명시적으로
  추가한 단말만 통과한다. `/0` 과 `/8` 보다 넓은 대역은 스크립트가 거부하지만,
  `/8`~`/16` 수준의 넓은 대역은 여전히 지정할 수 있으니 최소 범위로 유지한다.
- **인증서를 주지 않으면 HTTP 로만 서비스한다.** API 키와 관리 토큰이 평문으로
  전송된다. 검증 목적으로만 쓰고, 실사용 전에 ACM 인증서를 발급해
  `--certificate-arn` 으로 재배포한다.
- **평문 API 키는 저장되지 않는다.** SHA-256 해시만 보관하고, 발급 응답에서 한
  번만 노출된다. 분실하면 새로 발급해야 한다.
- **관리 토큰은 CloudFormation 이 생성한다.** 코드·파라미터·이미지에 평문으로
  남지 않고 Secrets Manager 에만 존재한다.
- **IAM 은 리소스 단위로 좁혔다.** 와일드카드가 불가피한 세 곳
  (`ecr:GetAuthorizationToken`, `bedrock:List*`, 기반 모델 ARN)에는 템플릿에 사유를
  주석으로 남겼다.
- **`.deploy/` 는 커밋하지 않는다.** 데모 키 평문이 들어 있고 `.gitignore` 에
  포함되어 있다.
- 프로덕션에서는 DynamoDB 테이블의 `DeletionPolicy` 를 `Retain` 으로 바꿔야
  한다. 기본값은 dev 편의를 위한 `Delete` 다. 절차는 런북의 "프로덕션 전환
  체크리스트" 에 있다.

---

## 문서

| 문서 | 내용 |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | 컴포넌트, 데이터 모델, 요청 흐름, 확장 한계 |
| [`docs/models-claude.md`](docs/models-claude.md) | Claude 모델 연동. 추론 프로파일 필수 조건, 단가 갭, 미지원 기능 |
| [`docs/bedrock-endpoints.md`](docs/bedrock-endpoints.md) | `bedrock-runtime` vs `bedrock-mantle`, 네이티브 OpenAI API 와의 관계 |
| [`SECURITY.md`](SECURITY.md) | 시크릿 관리, 접근 통제, IAM, 데이터 보호 |
| [`docs/runbook.md`](docs/runbook.md) | 배포·롤백·알람 대응·프로덕션 전환 절차 |
| [`docs/adr/0001-compute-ecs-fargate.md`](docs/adr/0001-compute-ecs-fargate.md) | 컴퓨트로 Fargate 를 고른 이유 |
| [`docs/adr/0002-datastore-dynamodb.md`](docs/adr/0002-datastore-dynamodb.md) | DynamoDB 선택과 단일 트랜잭션 집계 설계 |
| [`docs/adr/0003-network-and-exposure.md`](docs/adr/0003-network-and-exposure.md) | 퍼블릭 서브넷 + 공인 IP 판단 |
| [`docs/adr/0004-region-and-observability.md`](docs/adr/0004-region-and-observability.md) | 리전 선택과 분산 트레이싱 보류 |
| [`docs/openapi.json`](docs/openapi.json) | API 스펙 (코드와 일치 여부를 테스트가 검증) |
| [`CHANGELOG.md`](CHANGELOG.md) | 릴리스 이력 |

---

## 라이선스

[MIT](LICENSE)
