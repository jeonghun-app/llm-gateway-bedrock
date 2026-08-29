# LLM Gateway 컨테이너 이미지.
#
# 2단계 빌드다. builder 에서 의존성을 가상환경에 설치하고, runtime 에는 그
# 가상환경과 소스만 복사한다. 컴파일러와 pip 캐시가 최종 이미지에 남지
# 않아 크기와 취약점 표면이 줄어든다.
#
# 베이스 이미지는 태그와 다이제스트를 함께 고정한다. 태그만 쓰면 같은
# 태그가 재발행됐을 때 빌드 결과가 조용히 달라진다.
#
# 아키텍처는 x86_64 다. Graviton(arm64)이 같은 성능당 비용에서 더 유리하지만
# 빌드 호스트가 x86_64 라 QEMU 에뮬레이션이 필요해진다. 에뮬레이션 빌드는
# 느리고 실패 양상이 진단하기 어려워, 원커맨드 배포의 신뢰성을 우선했다.
# 전환 시에는 이 파일과 infra/app.yaml 의 CpuArchitecture 를 함께 바꾼다.

# ---------------------------------------------------------------------------
# 1단계: 의존성 설치
# ---------------------------------------------------------------------------
FROM python:3.14.7-slim-bookworm@sha256:416f0db2a2b561945630cef9877a7ea0581b27449eb9fd9df42f03e1b74b5b63 AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 전이 의존성까지 고정된 lock 파일만 사용한다. requirements.txt 를 쓰면
# 전이 의존성이 빌드 시점마다 달라질 수 있다.
COPY requirements.lock ./

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade "pip==25.2" \
    && /opt/venv/bin/pip install --require-virtualenv -r requirements.lock

# ---------------------------------------------------------------------------
# 2단계: 런타임
# ---------------------------------------------------------------------------
FROM python:3.14.7-slim-bookworm@sha256:416f0db2a2b561945630cef9877a7ea0581b27449eb9fd9df42f03e1b74b5b63 AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    LLMGW_PORT=8080

# 비root 사용자로 실행한다. UID 를 고정해 볼륨 권한이 환경마다 달라지지
# 않게 한다.
RUN groupadd --system --gid 10001 llmgw \
    && useradd --system --uid 10001 --gid llmgw --no-create-home llmgw

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=llmgw:llmgw src/ ./src/

USER llmgw:llmgw

EXPOSE 8080

# ECS 는 태스크 정의의 healthCheck 또는 ALB 타깃 그룹 체크를 쓴다. 이
# HEALTHCHECK 는 `docker run` 으로 로컬 검증할 때를 위한 것이다. slim
# 이미지에 curl 이 없어 표준 라이브러리로 확인한다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]

# --no-access-log: 액세스 로그는 상관관계 ID 를 포함한 자체 미들웨어가
#   JSON 으로 남긴다. uvicorn 기본 로그는 평문이라 중복이고 파싱이 안 된다.
# --timeout-keep-alive 65: ALB 기본 유휴 타임아웃(60초)보다 길게 잡는다.
#   앱이 먼저 keep-alive 연결을 닫으면 ALB 가 그 연결에 요청을 보내다
#   502 를 내는 경합이 생긴다.
# --workers 1: 태스크당 0.5 vCPU 기준이다. 처리량은 태스크 수로 늘린다.
CMD ["uvicorn", "llmgw.app:create_app", \
     "--factory", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--no-access-log", \
     "--timeout-keep-alive", "65"]
