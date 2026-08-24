# 변경 이력

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 를 따르고,
버전은 [유의적 버전](https://semver.org/lang/ko/)을 쓴다.

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
