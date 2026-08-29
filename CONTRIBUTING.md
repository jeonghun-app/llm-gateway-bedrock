# 기여 가이드

LLM Gateway 에 관심을 가져 주어 고맙다. 이 문서는 개발 환경 구성부터
변경을 제출하기까지의 절차를 설명한다.

행동 규범은 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) 를, 보안 취약점
신고는 [`SECURITY.md`](SECURITY.md) 를 본다.

---

## 시작하기 전에

- **작은 수정**(오타, 문서, 명백한 버그)은 바로 PR 을 열어도 된다.
- **기능 추가나 동작 변경**은 먼저 이슈를 열어 방향을 맞춘다. 구현을
  마친 뒤 설계가 맞지 않아 되돌리는 일을 줄이기 위해서다.
- 설계 판단의 배경은 [`docs/adr/`](docs/adr/) 에 있다. 큰 변경을 제안하기
  전에 관련 ADR 을 읽으면 맥락을 빠르게 잡을 수 있다.

---

## 개발 환경

Python 3.13 이 필요하다. 로컬 개발과 테스트에만 쓰이고, 배포 자체에는
필요 없다.

```bash
git clone <이 리포지토리>
cd llm-gateway-bedrock

python3.13 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt

# 실제 브라우저 UI 테스트를 돌리려면 최초 한 번 Chromium 을 설치한다.
./.venv/bin/python -m playwright install chromium
```

유닛 테스트는 실제 AWS 를 호출하지 않는다. DynamoDB 는 `moto`, Bedrock 은
`botocore.stub.Stubber` 로 대체하므로 자격증명 없이 전부 실행된다.

---

## 변경을 제출하기까지

### 1. 브랜치

`main` 에서 갈라진 토픽 브랜치에서 작업한다.

```bash
git switch -c fix/짧은-설명
```

### 2. 커밋 전 전체 검증

CI 가 실행하는 것과 같은 검증을 로컬에서 먼저 돌린다. 하나라도 실패하면
CI 도 실패한다.

```bash
# 포맷 (Google 스타일: 인덴트 4, 줄 길이 80)
./.venv/bin/isort src tests scripts
./.venv/bin/black src tests scripts

# 린트와 타입 체크
./.venv/bin/ruff check src tests scripts
./.venv/bin/mypy

# 유닛·Node 하네스 테스트 (커버리지 85% 미만이면 실패)
./.venv/bin/python -m pytest -m "not browser" \
  --cov=llmgw --cov-report=term-missing --cov-fail-under=85

# 실제 브라우저 관리 UI 회귀
./.venv/bin/python -m pytest -m browser tests/test_ui_playwright.py

# IaC 와 셸 스크립트
./.venv/bin/cfn-lint infra/*.yaml
shellcheck scripts/*.sh
```

API 스키마에 영향을 주는 변경이라면 스펙을 재생성하고, 그 결과를
커밋에 포함한다. `tests/test_openapi_export.py` 가 코드와 스펙의 일치를
검증하므로, 갱신하지 않으면 CI 가 실패한다.

```bash
./.venv/bin/python scripts/export_openapi.py
```

런타임 의존성을 바꿨다면 lock 파일도 재생성한다.

```bash
./scripts/lock_requirements.sh
```

### 3. 커밋 메시지

한 줄 제목은 명령형으로, 무엇을 왜 바꿨는지 본문에 적는다. 형식을
강제하지는 않지만 아래를 권장한다.

```
auth: 삭제된 팀을 참조하는 고아 키를 거부한다

팀이 None 이면 통과시키던 fail-open 경로가 있었다. 삭제된 팀을 참조하는
키가 계속 인증되는 문제를 fail-closed 로 바꾼다.
```

- 제목은 50자 내외, 마침표 없이.
- 관련 이슈가 있으면 본문에 `#123` 으로 참조한다.
- 하나의 논리적 변경은 하나의 커밋으로 유지한다.

### 4. Pull Request

- `main` 을 대상으로 연다.
- PR 템플릿의 항목(요약, 변경 이유, 테스트 방법)을 채운다.
- CI(포맷·린트·타입·유닛·브라우저·cfn-lint·shellcheck·시크릿 스캔·이미지
  빌드)가 전부 통과해야 병합한다.
- 사용자에게 보이는 변경이면 [`CHANGELOG.md`](CHANGELOG.md) 의
  `## [Unreleased]` 절(없으면 새로 만든다)에 항목을 추가한다.

---

## 코드 스타일

- **포맷터가 정답이다.** `black` 과 `isort`(`--profile google`) 의 결과를
  그대로 따른다. 스타일을 두고 논쟁하지 않는다.
- **주석은 "무엇"이 아니라 "왜" 를 적는다.** 코드가 무엇을 하는지는 코드가
  말한다. 그 판단을 왜 했는지, 어떤 실패를 막으려 했는지를 남긴다.
- **타입은 엄격하게.** `mypy --strict` 를 통과해야 한다.
- **테스트 이름은 한국어로** 검증 내용을 서술한다. 기존 테스트를 참고한다.
- **레이어 경계를 지킨다.** `routers/` 는 HTTP 경계, 도메인 서비스는 AWS
  SDK 를 직접 다루지 않고, boto3 호출은 `repository`/`bedrock` 어댑터에만
  둔다. 자세한 구조는 [`docs/architecture.md`](docs/architecture.md) 를 본다.

---

## 테스트 원칙

- 새 기능과 버그 수정에는 테스트를 함께 낸다. 버그 수정은 그 버그를
  재현하는 테스트를 먼저 추가하면 회귀를 막을 수 있다.
- 유닛 테스트는 실제 AWS 를 호출하지 않는다. 새 AWS 상호작용을 추가하면
  `moto` 나 `Stubber` 로 경계를 대체한다.
- 커버리지 하한은 85% 다. 단순히 숫자를 맞추기보다 실패 경로(권한, 예산,
  스트리밍 중단 등)를 검증하는 데 집중한다.

---

## 라이선스

기여한 코드는 이 프로젝트의 [MIT 라이선스](LICENSE) 로 배포된다. PR 을
제출하면 그 조건에 동의한 것으로 본다.
