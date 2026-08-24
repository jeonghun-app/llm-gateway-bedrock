#!/usr/bin/env bash
#
# LLM Gateway 원커맨드 배포.
#
# AWS 자격증명만 있으면 VPC 부터 DynamoDB 까지 전부 만든다. 콘솔 작업이나
# 사전 리소스 준비가 필요 없다.
#
# 진행 순서
#   1. 사전 점검 (도구, 자격증명, Bedrock 접근, 고아 테이블)
#   2. ECR 스택 배포
#   3. 이미지 빌드와 푸시 (같은 태그가 이미 있으면 건너뛴다)
#   4. 애플리케이션 스택 배포
#   5. 데모 데이터 시드 (--no-seed 로 생략)
#   6. 스모크 테스트
#   7. 접속 정보 출력
#
# 사용법
#   ./scripts/deploy.sh --allowed-cidr 1.2.3.4/32
#   ./scripts/deploy.sh --allowed-cidr 1.2.3.4/32 --env stg --region us-west-2
#
# 여러 번 실행해도 안전하다. CloudFormation 변경 집합과 ECR 태그 확인으로
# 이미 반영된 작업은 건너뛴다.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT

# ---------------------------------------------------------------------------
# 기본값
# ---------------------------------------------------------------------------
PROJECT_NAME="llmgw"
ENVIRONMENT="dev"
# 리전 기본값. 호스트가 us-east-1 에 있고 Bedrock 모델 가용성이 가장 넓다.
AWS_REGION="${AWS_REGION:-us-east-1}"
OWNER="platform-team"
ALLOWED_CIDR_1=""
ALLOWED_CIDR_2=""
ALLOWED_CIDR_3=""
CERTIFICATE_ARN=""
DESIRED_COUNT="1"
TASK_CPU="512"
TASK_MEMORY="1024"
LOG_LEVEL="INFO"
DEFAULT_ALLOWED_MODELS=""
ALARM_EMAIL=""
RUN_SEED="yes"
RUN_SMOKE="yes"
SHOW_ADMIN_TOKEN="no"
DOCKER_CMD="docker"

# 색 없는 출력. CI 로그에서 제어문자가 섞이지 않게 한다.
log()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '   [경고] %s\n' "$*" >&2; }
die()  { printf '\n[실패] %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
사용법: ./scripts/deploy.sh --allowed-cidr <CIDR> [옵션]

필수
  --allowed-cidr <CIDR>     ALB 접근을 허용할 CIDR. 0.0.0.0/0 은 거부된다.
                            접속할 단말의 공인 IP /32 를 권장한다.

선택
  --allowed-cidr-2 <CIDR>   추가 허용 CIDR
  --allowed-cidr-3 <CIDR>   추가 허용 CIDR
  --project <이름>          리소스 이름 접두어 (기본 llmgw)
  --env <dev|stg|prod>      배포 환경 (기본 dev)
  --region <리전>           배포 리전 (기본 us-east-1 또는 $AWS_REGION)
  --owner <소유자>          Owner 태그 값 (기본 platform-team)
  --certificate-arn <ARN>   ACM 인증서. 지정하면 HTTPS 로 서비스한다.
  --desired-count <N>       상시 태스크 수 (기본 1, prod 는 2 이상 권장)
  --task-cpu <N>            태스크 CPU 유닛 (기본 512)
  --task-memory <N>         태스크 메모리 MiB (기본 1024)
  --log-level <레벨>        DEBUG|INFO|WARNING|ERROR (기본 INFO)
  --allowed-models <목록>   기본 허용 모델. 쉼표 구분
  --alarm-email <메일>      알람 수신 이메일
  --no-seed                 데모 데이터 시드를 건너뛴다
  --no-smoke                스모크 테스트를 건너뛴다
  --show-admin-token        관리 토큰을 화면에 출력한다 (기본은 조회 명령만)
  -h, --help                이 도움말

예시
  ./scripts/deploy.sh --allowed-cidr "$(curl -s https://checkip.amazonaws.com)/32"
USAGE
}

# ---------------------------------------------------------------------------
# 인자 파싱
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --allowed-cidr)     ALLOWED_CIDR_1="${2:-}"; shift 2 ;;
        --allowed-cidr-2)   ALLOWED_CIDR_2="${2:-}"; shift 2 ;;
        --allowed-cidr-3)   ALLOWED_CIDR_3="${2:-}"; shift 2 ;;
        --project)          PROJECT_NAME="${2:-}"; shift 2 ;;
        --env)              ENVIRONMENT="${2:-}"; shift 2 ;;
        --region)           AWS_REGION="${2:-}"; shift 2 ;;
        --owner)            OWNER="${2:-}"; shift 2 ;;
        --certificate-arn)  CERTIFICATE_ARN="${2:-}"; shift 2 ;;
        --desired-count)    DESIRED_COUNT="${2:-}"; shift 2 ;;
        --task-cpu)         TASK_CPU="${2:-}"; shift 2 ;;
        --task-memory)      TASK_MEMORY="${2:-}"; shift 2 ;;
        --log-level)        LOG_LEVEL="${2:-}"; shift 2 ;;
        --allowed-models)   DEFAULT_ALLOWED_MODELS="${2:-}"; shift 2 ;;
        --alarm-email)      ALARM_EMAIL="${2:-}"; shift 2 ;;
        --no-seed)          RUN_SEED="no"; shift ;;
        --no-smoke)         RUN_SMOKE="no"; shift ;;
        --show-admin-token) SHOW_ADMIN_TOKEN="yes"; shift ;;
        -h|--help)          usage; exit 0 ;;
        *)                  usage; die "알 수 없는 인자: $1" ;;
    esac
done

readonly ECR_STACK="${PROJECT_NAME}-${ENVIRONMENT}-ecr"
readonly APP_STACK="${PROJECT_NAME}-${ENVIRONMENT}-app"

aws_cli() { aws --region "${AWS_REGION}" "$@"; }

# ---------------------------------------------------------------------------
# 1. 사전 점검
# ---------------------------------------------------------------------------
log "1/7 사전 점검"

[[ -n "${ALLOWED_CIDR_1}" ]] || { usage; die "--allowed-cidr 는 필수다."; }

# 0.0.0.0/0 은 템플릿 정규식도 거부하지만, 여기서 먼저 막아 오류 메시지를
# 명확하게 만든다. CloudFormation 파라미터 검증 실패 메시지는 원인을 찾기
# 어렵다.
validate_cidr() {
    local cidr="$1" label="$2"
    [[ -n "${cidr}" ]] || return 0
    if [[ "${cidr}" == "0.0.0.0/0" || "${cidr}" == */0 ]]; then
        die "${label} 에 전체 개방 CIDR(${cidr})은 쓸 수 없다. 접속할 단말의 IP /32 를 지정한다."
    fi
    if [[ ! "${cidr}" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/[0-9]{1,2}$ ]]; then
        die "${label} 형식이 올바르지 않다: ${cidr} (예: 1.2.3.4/32)"
    fi
}
validate_cidr "${ALLOWED_CIDR_1}" "--allowed-cidr"
validate_cidr "${ALLOWED_CIDR_2}" "--allowed-cidr-2"
validate_cidr "${ALLOWED_CIDR_3}" "--allowed-cidr-3"

command -v aws >/dev/null 2>&1 || die "aws CLI 가 필요하다."
command -v jq  >/dev/null 2>&1 || die "jq 가 필요하다."

# docker 그룹이 현재 셸에 반영되지 않은 상태를 흔하게 만난다. sg 로 우회한다.
if docker info >/dev/null 2>&1; then
    DOCKER_CMD="docker"
elif command -v sg >/dev/null 2>&1 && sg docker -c "docker info" >/dev/null 2>&1; then
    DOCKER_CMD="sg_docker"
    info "docker 그룹이 현재 셸에 반영되지 않아 sg 로 실행한다."
else
    die "docker 데몬에 접근할 수 없다. 'sudo systemctl start docker' 와 'sudo usermod -aG docker \$USER' 를 확인한다."
fi

# docker 를 직접 실행하거나 sg 로 감싸 실행한다. 인자를 배열로 받아
# 넘기므로 경로에 공백이 있어도 깨지지 않는다.
docker_run() {
    if [[ "${DOCKER_CMD}" == "sg_docker" ]]; then
        # sg 는 명령을 문자열 하나로만 받는다. %q 로 각 인자를 셸 안전하게
        # 인용해 합친다.
        local quoted
        printf -v quoted '%q ' "$@"
        sg docker -c "docker ${quoted}"
    else
        docker "$@"
    fi
}

caller_json="$(aws_cli sts get-caller-identity 2>&1)" \
    || die "AWS 자격증명을 확인할 수 없다: ${caller_json}"
ACCOUNT_ID="$(echo "${caller_json}" | jq -r .Account)"
info "계정 ${ACCOUNT_ID} / 리전 ${AWS_REGION} / 환경 ${ENVIRONMENT}"

# Bedrock 모델 액세스가 꺼져 있으면 배포는 성공하지만 모든 호출이 실패한다.
# 30분 뒤에 알게 되는 것보다 지금 아는 편이 낫다.
model_count="$(aws_cli bedrock list-foundation-models \
    --by-output-modality TEXT --by-inference-type ON_DEMAND \
    --query 'length(modelSummaries)' --output text 2>/dev/null || echo "0")"
if [[ "${model_count}" == "0" ]]; then
    warn "Bedrock 온디맨드 텍스트 모델이 조회되지 않는다. 콘솔의 Model access 에서 모델을 활성화해야 호출이 성공한다."
else
    info "Bedrock 온디맨드 텍스트 모델 ${model_count}개 확인"
fi

# 스택 없이 테이블만 남은 상태에서 배포하면 "테이블이 이미 있다" 로 실패한다.
# 원인을 미리 알려준다.
app_stack_status="$(aws_cli cloudformation describe-stacks \
    --stack-name "${APP_STACK}" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "NONE")"
if [[ "${app_stack_status}" == "NONE" ]]; then
    for suffix in registry usage usage-agg; do
        table="${PROJECT_NAME}-${ENVIRONMENT}-${suffix}"
        if aws_cli dynamodb describe-table --table-name "${table}" \
            >/dev/null 2>&1; then
            die "스택 ${APP_STACK} 는 없는데 테이블 ${table} 가 남아 있다. 이전 배포의 잔여 리소스다. './scripts/teardown.sh --env ${ENVIRONMENT} --region ${AWS_REGION} --purge-data' 로 정리한 뒤 다시 실행한다."
        fi
    done
fi
if [[ "${app_stack_status}" == ROLLBACK_COMPLETE ]]; then
    die "스택 ${APP_STACK} 가 ROLLBACK_COMPLETE 상태다. 이 상태에서는 업데이트할 수 없다. './scripts/teardown.sh --env ${ENVIRONMENT} --region ${AWS_REGION}' 로 삭제한 뒤 다시 실행한다."
fi

# ---------------------------------------------------------------------------
# 2. ECR 스택
# ---------------------------------------------------------------------------
log "2/7 ECR 스택 배포"
aws_cli cloudformation deploy \
    --stack-name "${ECR_STACK}" \
    --template-file "${REPO_ROOT}/infra/ecr.yaml" \
    --no-fail-on-empty-changeset \
    --parameter-overrides \
        "ProjectName=${PROJECT_NAME}" \
        "Environment=${ENVIRONMENT}" \
        "Owner=${OWNER}" \
    --tags \
        "Project=${PROJECT_NAME}" \
        "Environment=${ENVIRONMENT}" \
        "Owner=${OWNER}" \
        "ManagedBy=cloudformation" \
    >/dev/null

stack_output() {
    aws_cli cloudformation describe-stacks --stack-name "$1" \
        --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" \
        --output text
}

REPOSITORY_URI="$(stack_output "${ECR_STACK}" RepositoryUri)"
REPOSITORY_ARN="$(stack_output "${ECR_STACK}" RepositoryArn)"
REPOSITORY_NAME="$(stack_output "${ECR_STACK}" RepositoryName)"
info "리포지토리 ${REPOSITORY_URI}"

# ---------------------------------------------------------------------------
# 3. 이미지 빌드와 푸시
# ---------------------------------------------------------------------------
log "3/7 이미지 빌드와 푸시"

# ECR 태그를 불변으로 설정했으므로 태그가 유일해야 한다. 작업 트리가
# 깨끗하면 커밋 SHA 를 그대로 쓴다. 그러면 같은 커밋 재배포 시 푸시를
# 건너뛸 수 있다. 수정 사항이 있으면 타임스탬프를 붙여 유일성을 보장한다.
# 그러지 않으면 변경된 소스가 이전 이미지로 배포되는 사고가 난다.
cd "${REPO_ROOT}"
if git rev-parse --git-dir >/dev/null 2>&1; then
    git_sha="$(git rev-parse --short=12 HEAD 2>/dev/null || echo nogit)"
    if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
        IMAGE_TAG="${git_sha}-dirty-$(date -u +%Y%m%d%H%M%S)"
        warn "작업 트리에 커밋되지 않은 변경이 있다. 태그에 타임스탬프를 붙인다."
    else
        IMAGE_TAG="${git_sha}"
    fi
else
    IMAGE_TAG="notgit-$(date -u +%Y%m%d%H%M%S)"
fi
IMAGE_URI="${REPOSITORY_URI}:${IMAGE_TAG}"
info "태그 ${IMAGE_TAG}"

if aws_cli ecr describe-images --repository-name "${REPOSITORY_NAME}" \
    --image-ids "imageTag=${IMAGE_TAG}" >/dev/null 2>&1; then
    info "같은 태그의 이미지가 이미 있다. 빌드와 푸시를 건너뛴다."
else
    docker_run build -t "${IMAGE_URI}" "${REPO_ROOT}"
    aws_cli ecr get-login-password \
        | docker_run login --username AWS --password-stdin \
            "${REPOSITORY_URI%%/*}" >/dev/null
    docker_run push "${IMAGE_URI}" >/dev/null
    info "푸시 완료"
fi

# ---------------------------------------------------------------------------
# 4. 애플리케이션 스택
# ---------------------------------------------------------------------------
log "4/7 애플리케이션 스택 배포 (5~10분 소요)"
aws_cli cloudformation deploy \
    --stack-name "${APP_STACK}" \
    --template-file "${REPO_ROOT}/infra/app.yaml" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset \
    --parameter-overrides \
        "ProjectName=${PROJECT_NAME}" \
        "Environment=${ENVIRONMENT}" \
        "Owner=${OWNER}" \
        "ImageUri=${IMAGE_URI}" \
        "EcrRepositoryArn=${REPOSITORY_ARN}" \
        "AllowedIngressCidr1=${ALLOWED_CIDR_1}" \
        "AllowedIngressCidr2=${ALLOWED_CIDR_2}" \
        "AllowedIngressCidr3=${ALLOWED_CIDR_3}" \
        "CertificateArn=${CERTIFICATE_ARN}" \
        "DesiredCount=${DESIRED_COUNT}" \
        "TaskCpu=${TASK_CPU}" \
        "TaskMemory=${TASK_MEMORY}" \
        "LogLevel=${LOG_LEVEL}" \
        "DefaultAllowedModels=${DEFAULT_ALLOWED_MODELS}" \
        "AlarmEmail=${ALARM_EMAIL}" \
    --tags \
        "Project=${PROJECT_NAME}" \
        "Environment=${ENVIRONMENT}" \
        "Owner=${OWNER}" \
        "ManagedBy=cloudformation" \
    || {
        printf '\n[실패] 스택 배포가 실패했다. 최근 실패 이벤트:\n' >&2
        # shellcheck disable=SC2016  # 백틱은 JMESPath 리터럴 문법이다.
        aws_cli cloudformation describe-stack-events \
            --stack-name "${APP_STACK}" \
            --query 'StackEvents[?contains(ResourceStatus, `FAILED`)].[Timestamp,LogicalResourceId,ResourceStatusReason]' \
            --output table 2>/dev/null | head -40 >&2
        exit 1
    }

GATEWAY_URL="$(stack_output "${APP_STACK}" GatewayUrl)"
DASHBOARD_URL="$(stack_output "${APP_STACK}" DashboardUrl)"
SECRET_ARN="$(stack_output "${APP_STACK}" AdminTokenSecretArn)"
LOG_GROUP="$(stack_output "${APP_STACK}" LogGroupName)"
CLUSTER_NAME="$(stack_output "${APP_STACK}" ClusterName)"
SERVICE_NAME="$(stack_output "${APP_STACK}" ServiceName)"
info "게이트웨이 ${GATEWAY_URL}"

ADMIN_TOKEN="$(aws_cli secretsmanager get-secret-value \
    --secret-id "${SECRET_ARN}" --query SecretString --output text \
    | jq -r .admin_token)"
[[ -n "${ADMIN_TOKEN}" && "${ADMIN_TOKEN}" != "null" ]] \
    || die "관리 토큰을 읽지 못했다."

# ---------------------------------------------------------------------------
# 5. 서비스 준비 대기
# ---------------------------------------------------------------------------
log "5/7 서비스 기동 대기"
ready="no"
for attempt in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
        "${GATEWAY_URL}/healthz" 2>/dev/null || echo "000")"
    if [[ "${code}" == "200" ]]; then
        ready="yes"
        info "healthz 200 응답 확인 (${attempt}회 시도)"
        break
    fi
    sleep 5
done
if [[ "${ready}" != "yes" ]]; then
    warn "5분 안에 healthz 가 200 을 반환하지 않았다. 접근 CIDR 이 현재 단말 IP 와 다를 수 있다."
    warn "현재 단말 공인 IP: $(curl -s --max-time 5 https://checkip.amazonaws.com 2>/dev/null || echo '확인 실패')"
    warn "허용 CIDR: ${ALLOWED_CIDR_1} ${ALLOWED_CIDR_2} ${ALLOWED_CIDR_3}"
    warn "태스크 로그: aws logs tail ${LOG_GROUP} --region ${AWS_REGION} --since 10m"
fi

# ---------------------------------------------------------------------------
# 6. 데모 데이터 시드
# ---------------------------------------------------------------------------
if [[ "${RUN_SEED}" == "yes" && "${ready}" == "yes" ]]; then
    log "6/7 데모 데이터 시드"
    python_bin="python3"
    [[ -x "${REPO_ROOT}/.venv/bin/python" ]] \
        && python_bin="${REPO_ROOT}/.venv/bin/python"
    LLMGW_BASE_URL="${GATEWAY_URL}" \
    LLMGW_ADMIN_TOKEN="${ADMIN_TOKEN}" \
        "${python_bin}" "${REPO_ROOT}/scripts/seed_demo_data.py" \
        --output "${REPO_ROOT}/.deploy/demo-keys-${ENVIRONMENT}.json" \
        || warn "시드가 실패했다. 배포 자체는 완료됐다."
else
    log "6/7 데모 데이터 시드 건너뜀"
fi

# ---------------------------------------------------------------------------
# 7. 스모크 테스트
# ---------------------------------------------------------------------------
if [[ "${RUN_SMOKE}" == "yes" && "${ready}" == "yes" ]]; then
    log "7/7 스모크 테스트"
    LLMGW_BASE_URL="${GATEWAY_URL}" \
    LLMGW_ADMIN_TOKEN="${ADMIN_TOKEN}" \
    LLMGW_DEMO_KEY_FILE="${REPO_ROOT}/.deploy/demo-keys-${ENVIRONMENT}.json" \
        "${REPO_ROOT}/scripts/smoke_test.sh" \
        || die "스모크 테스트가 실패했다. 위 출력을 확인한다."
else
    log "7/7 스모크 테스트 건너뜀"
fi

# ---------------------------------------------------------------------------
# 결과
# ---------------------------------------------------------------------------
cat <<SUMMARY

────────────────────────────────────────────────────────────────────
 배포 완료
────────────────────────────────────────────────────────────────────
 게이트웨이      ${GATEWAY_URL}
 대시보드        ${DASHBOARD_URL}
 OpenAI base_url ${GATEWAY_URL}/v1

 ECS             ${CLUSTER_NAME} / ${SERVICE_NAME}
 로그            aws logs tail ${LOG_GROUP} --region ${AWS_REGION} --follow

 관리 토큰 조회
   aws secretsmanager get-secret-value --region ${AWS_REGION} \\
     --secret-id ${SECRET_ARN} --query SecretString --output text | jq -r .admin_token
SUMMARY

if [[ "${SHOW_ADMIN_TOKEN}" == "yes" ]]; then
    printf ' 관리 토큰       %s\n' "${ADMIN_TOKEN}"
fi

cat <<SUMMARY

 대시보드는 위 토큰을 입력해야 데이터가 보인다.

 접근 허용 CIDR  ${ALLOWED_CIDR_1} ${ALLOWED_CIDR_2} ${ALLOWED_CIDR_3}
 다른 단말에서 접속하려면 --allowed-cidr-2 에 그 단말 IP 를 추가해 다시
 실행한다. 자세한 절차는 docs/runbook.md 를 참고한다.

 전체 삭제
   ./scripts/teardown.sh --env ${ENVIRONMENT} --region ${AWS_REGION} --purge-data
────────────────────────────────────────────────────────────────────
SUMMARY

if [[ -z "${CERTIFICATE_ARN}" ]]; then
    cat <<'NOTICE'

 [보안 안내] 인증서를 지정하지 않아 ALB 가 HTTP 로만 서비스한다. API 키와
 관리 토큰이 평문으로 전송된다. 검증 목적으로만 사용하고, 실사용 전에
 ACM 인증서를 발급해 --certificate-arn 으로 다시 배포한다.
NOTICE
fi
