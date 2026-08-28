# 변경 이력

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 를 따르고,
버전은 [유의적 버전](https://semver.org/lang/ko/)을 쓴다.

## [1.2.0] - 2026-08-28

관리 API 를 완전한 CRUD 로 확장하고, 대시보드에 계정·팀·사용자·API 키를
직접 만들고 고치는 관리 화면을 얹었다. 이전에는 `curl` 로만 가능하던 관리
작업을 브라우저에서 처리할 수 있다.

### 추가

- **관리 API CRUD 완성.**
  - 계정: `PATCH /admin/accounts/{id}`(이름·예산 수정), `DELETE`(하위 리소스
    없을 때만).
  - 팀: `GET`/`PATCH`/`POST .../status`(활성 토글)/`DELETE` 추가.
  - 사용자: `GET`/`PATCH`/`POST .../status`/`DELETE` 추가.
  - API 키: `GET`/`PATCH`(이름·허용 모델·예산)/`POST .../status`/`POST
    .../rotate`(재발급) 추가.
  - 삭제는 참조 무결성을 강제한다. 하위 팀·사용자·키가 남아 있으면 `409`
    로 거부한다.
  - 부분 수정은 요청에 실린 필드만 반영한다. `monthly_budget_usd` 를 `null`
    로 보내면 예산을 무제한으로 되돌린다.
- **관리 대시보드 화면.** `/ui/` 에 "모니터링 / 관리" 전환을 추가했다. 관리
  화면에서 계정·팀·사용자·키를 폼으로 만들고 수정·삭제·상태토글·재발급한다.
  재발급·발급 시 평문 키를 한 번만 보여 준다. 기존과 같이 외부 라이브러리와
  빌드 단계 없이 동작한다.

### 수정

- **팀·사용자 비활성화 경로가 실제로 열렸다.** 인증기는 팀·사용자의 비활성
  상태를 검사하고 있었지만, 정작 이들을 비활성으로 만들 관리 API 가 없었다.
  status 토글 엔드포인트를 추가해 이 방어 로직에 도달할 수 있게 했다.

## [1.1.0] - 2026-08-24

접근 통제를 다중 단말 운영에 맞게 재설계하고, 비용 집계의 조용한 누락을
드러나게 만들었다. Claude 연동과 Bedrock 엔드포인트 선택 근거를 문서로 남겼다.

0.1.0 에서 바로 1.1.0 으로 올렸다. 접근 통제 방식이 바뀌어 배포 인터페이스가
달라졌고, 사용량 집계에 필드가 추가되어 기능 증분이 패치 수준을 넘는다.

### 변경 (BREAKING)

- **접근 허용 CIDR 을 관리형 프리픽스 리스트로 관리한다.** ALB 보안 그룹이
  프리픽스 리스트 하나만 참조하므로 단말이 몇 개든 규칙은 프로토콜당 1개다.
  **단말 추가·삭제에 스택 재배포가 필요 없고 수 초 안에 반영된다.**
  - CloudFormation 파라미터 `AllowedIngressCidr1/2/3` 이 **제거**되었다.
    대신 `AccessListMaxEntries`(기본 20)가 추가됐다.
  - `deploy.sh` 의 `--allowed-cidr-2`, `--allowed-cidr-3` 옵션이 **제거**되었다.
    `--allowed-cidr` 를 여러 번 지정한다.
  - 기존 스택은 재배포 시 보안 그룹 규칙이 교체된다. 배포 직후 프리픽스
    리스트가 채워지므로 접근이 유지되지만, 배포 중 짧은 단절이 있을 수 있다.
  - 프리픽스 리스트는 **빈 상태로 생성**된다. 아무도 접근할 수 없는 상태에서
    시작하고 명시적으로 추가한 단말만 통과한다.
- **`0.0.0.0/0` 차단 지점이 이동했다.** 이전에는 CloudFormation 파라미터 정규식이
  막았다. 프리픽스 리스트에는 파라미터 제약을 걸 수 없어, 차단이 세 곳으로
  옮겨졌다: `deploy.sh` 검증, `manage_access.sh` 검증, 그리고 매 배포마다
  실행되는 `smoke_test.sh` 의 전체 개방 규칙 검사. `/8` 보다 넓은 대역도
  스크립트가 거부한다.
- **집계에 `unpriced_requests` 필드가 추가됐다.** `/analytics/*` 응답에
  `unpriced_requests` 와 `cost_complete` 가 포함된다. 응답 스키마에 필드가
  늘어난 것이므로 기존 클라이언트는 영향받지 않는다. 1.0 에서 기록된 집계
  행에는 이 속성이 없고 0으로 읽힌다.

### 추가

- **`scripts/manage_access.sh`** — 접근 단말 관리. `list`, `add`, `add-me`,
  `remove`, `check`, `status` 서브명령. 라벨로 어느 단말인지 기록한다.
  `check` 는 목록 확인과 실제 HTTP 호출을 함께 수행한다.
- **`scripts/scan_secrets.sh`** — 시크릿 검사. AWS 키, GitHub 토큰, Slack 토큰,
  OpenAI 키, 게이트웨이 API 키 평문, Bedrock API 키, private key, JWT, 실제 계정
  ID, 계정 ID 포함 ARN, 추적돼서는 안 되는 경로를 검사한다. `--history` 로 전체
  커밋 히스토리까지 확인한다. CI 잡으로 추가했다.
- **`scripts/sync_pricing.py`** — AWS Price List API 와 단가 표를 대조해 불일치·
  누락·미확인을 보고한다. `--apply` 로 **API 가 확인한 값만** 반영한다.
  확인되지 않은 모델의 단가를 추측해 넣지 않는다.
- **단가 미등록 사용량 가시화.** 단가 표에 없는 모델로 처리된 요청이
  `unpriced_requests` 로 집계되고, 대시보드 총비용 카드에
  `USD — 단가 미등록 N건 제외됨` 경고가 표시된다. 이전에는 비용이 0으로 조용히
  누락됐다.
- **`docs/models-claude.md`** — Claude 연동 상세. 현행 Claude 가 전부
  `INFERENCE_PROFILE` 전용이라 기반 모델 ID 로는 호출되지 않는다는 점,
  `us.` 와 `global.` 선택 기준, 허용 목록 정규화가 프로파일 접두어를 무시한다는
  점과 그 보안 함의, LEGACY/EOL 처리, 단가 갭의 현재 상태와 메우는 방법,
  미지원 기능(비전·도구 사용·프롬프트 캐싱 등) 목록, 실측 검증 결과.
- **`docs/bedrock-endpoints.md`** — `bedrock-runtime` 과 `bedrock-mantle` 전체
  비교(API·기능·인증·처리량·요금), 게이트웨이가 `bedrock-runtime` + Converse 를
  고른 이유, **AWS 가 이미 OpenAI 호환 API 를 제공하는데 이 게이트웨이가 필요한
  이유와 필요 없는 경우**, Mantle 전용 기능이 필요할 때의 경로, Guardrails 도입
  설계 지점, PrivateLink 손익분기 계산.
- **`SECURITY.md`** — 시크릿 관리 원칙, 검사 절차, 유출 시 대응 순서, 접근 통제
  3계층, 알려진 위험 4건, IAM 와일드카드 사유, 데이터 보호 설정, 프로덕션 전환
  보안 체크리스트.
- **`tests/test_static_charts.py`** — 대시보드 SVG 차트 렌더링 검증 15건.
  최소 DOM 셰임(`tests/js/charts_harness.js`)을 Node 로 실행한다. 경계 케이스
  (빈 데이터, 데이터 1건, 값 전부 0, 조각 1개 도넛)와 접근성 속성을 확인한다.
  Node 가 없으면 건너뛴다. npm 의존성은 추가하지 않았다.
- **모델 단가 17종 추가.** Price List API 로 확인된 값만 반영했다.
  DeepSeek v3.2, MiniMax M2 계열, Kimi K2 계열, Nemotron 3, GPT-OSS Safeguard,
  Qwen3 계열, GLM 4.7/5, Palmyra Vision 등. 단가 표가 23 → 40개가 됐다.

### 검증

- 단가 표 대조 결과 **기존 23개 항목이 AWS Price List API 값과 불일치 0건**.
  손으로 넣은 스냅샷이 정확했음이 확인됐다.
- 프리픽스 리스트 설계는 실제 스택 업데이트로 검증했다. `Entries` 속성을
  템플릿에서 생략하면 CloudFormation 이 스택 밖에서 추가한 엔트리를 되돌리지
  않는다.
- 시크릿 검사: 워킹트리·전체 커밋 히스토리·커밋 메시지·원격 브랜치 모두 클린.

### 알려진 제약 (변경 사항)

- **현행 Claude 모델의 단가가 등록되지 않았다.** 이 계정의 Price List API 에는
  레거시 Claude 5종만 존재해 자동 확인이 불가능했다. Sonnet 4.5~5,
  Opus 4.5~5, Haiku 4.5, Fable 5 의 비용이 0으로 집계된다. 추측한 값을 넣지
  않은 것은 의도적이다. 이 상태는 `unpriced_requests` 와 대시보드 경고로
  드러난다. **단가가 없는 모델에 예산을 걸면 예산이 작동하지 않는다.**
  메우는 방법은 `docs/models-claude.md` 5절에 있다.
- 프리픽스 리스트 엔트리는 CloudFormation 이 관리하지 않는다. 재배포 없이
  단말을 바꿀 수 있는 이유이면서, AWS CLI 로 직접 넓은 대역을 넣는 것이
  가능하다는 뜻이다. 스모크 테스트의 검사가 마지막 그물이다.
- `AccessListMaxEntries` 는 생성 후 늘릴 수만 있고 줄일 수 없다.

## [0.1.0] - 2026-08-24

첫 릴리스.

### 추가

**게이트웨이**

- OpenAI 호환 `POST /v1/chat/completions`. 비스트리밍과 SSE 스트리밍 모두 지원
- `GET /v1/models`. API 키의 허용 목록으로 필터링해 반환
- OpenAI ↔ Bedrock Converse 변환. 시스템 메시지 분리, 연속 동일 역할 병합,
  `finish_reason` 매핑
- `X-Request-Id` 기반 멱등성. 재시도해도 사용량이 중복 집계되지 않는다

**접근 통제**

- API 키 인증. 평문은 저장하지 않고 SHA-256 해시로만 조회
- 계정 → 팀 → 사용자 → 키 4단 계층
- 키별 모델 허용 목록. 추론 프로파일 접두어(`us.` 등)를 정규화해 비교
- 계정·팀·사용자·키 4단계 월 예산. 하나라도 초과하면 429
- 계정·팀·사용자·키 활성 상태 전환

**사용량 기록**

- 요청 단위 원본 기록과 5축(전체/팀/사용자/모델/키) × 2기간(일/월) 사전 집계
- 원본 쓰기와 집계 갱신을 단일 `TransactWriteItems` 로 처리. 멱등성이 저장소
  제약으로 보장된다
- 모델 단가 표 기반 비용 계산. 표에 없는 모델은 비용 0과 `pricing_known=false`
  플래그로 기록하고 경고 로그를 남긴다

**관리 API**

- 계정·팀·사용자·API 키 생성과 조회, 키 삭제, 계정 상태 변경
- `GET /admin/models`. 단가 인지 여부를 함께 반환

**집계 조회 API**

- `/analytics/summary`, `/timeseries`, `/breakdown`, `/requests`, `/accounts`
- `/analytics/dashboard`. 대시보드 한 화면 데이터를 한 번에 반환

**대시보드 UI**

- KPI 카드 5개, 차트 4개, 상세 표 6개 탭 (계정/팀/사용자/모델/키/최근 요청)
- 기간 프리셋과 임의 구간 조회, 30초 자동 새로고침
- 외부 의존성 없는 SVG 차트 자체 구현
- 관리 토큰은 `sessionStorage` 에만 저장

**인프라**

- CloudFormation 2스택. VPC, ALB, ECS Fargate, DynamoDB 3개, IAM, Secrets
  Manager, CloudWatch 알람 4개, SNS, 오토스케일링
- 접근 CIDR 필수 파라미터. `0.0.0.0/0` 과 모든 `/0` 프리픽스를 정규식으로 거부
- 배포 서킷 브레이커 + 자동 롤백
- S3·DynamoDB 게이트웨이 엔드포인트 (무료). NAT Gateway 미사용
- 관리 토큰을 CloudFormation 이 생성해 Secrets Manager 에 저장

**운영 도구**

- `scripts/deploy.sh`. 사전 점검부터 스모크 테스트까지 원커맨드 배포
- `scripts/teardown.sh`. 단계별 삭제와 `--purge-data`
- `scripts/seed_demo_data.py`. 데모 조직 구조 생성과 실제 사용량 발생
- `scripts/smoke_test.sh`. 인증 경계, 스트리밍, 멱등성, 집계 축, 비용 계산 검증
- `scripts/export_openapi.py` + 드리프트 검증 테스트

**관측**

- JSON 구조화 로그. 모든 줄에 `correlation_id` 자동 첨부
- EMF 커스텀 메트릭 6종 (`Requests`, `Errors`, `InputTokens`, `OutputTokens`,
  `CostUsd`, `LatencyMs`) + `UsageWriteFailures`
- `/healthz`(얕음, ALB 용)와 `/readyz`(의존성 확인) 분리

### 알려진 제약

- **분산 트레이싱이 없다.** 구간별 지연 분해가 불가능하다. 근거와 도입 절차는
  [ADR 0004](docs/adr/0004-region-and-observability.md) 에 있다
- **DynamoDB `DeletionPolicy` 가 `Delete` 다.** 프로덕션 전환 시 `Retain` 으로
  바꿔야 한다
- **인증서 없이 배포하면 HTTP 로만 서비스한다.** API 키가 평문 전송된다
- **집계 축이 고정이다.** 5축 외의 조합(예: 팀 내 사용자별 모델 분포)은 원본을
  읽어야 한다
- **최대·백분위 지연이 집계 테이블에 없다.** `ADD` 로 최댓값을 누적할 수 없어
  평균만 두고 백분위는 CloudWatch 에서 본다
- **한 계정의 쓰기 처리량에 상한이 있다.** 집계 파티션이 `계정#기간` 이라 파티션당
  1,000 WCU 제약을 받는다
- **모델 단가는 스냅샷이다.** `src/llmgw/pricing.json` 을 주기적으로 대조해야 한다
- **이미지가 x86_64 다.** arm64(Graviton)가 비용 효율이 낫지만 빌드 호스트
  아키텍처에 맞췄다
- **메타데이터 캐시 TTL 30초.** 계정·팀·사용자 상태 변경이 그만큼 지연 반영된다.
  키 삭제는 즉시 적용된다
