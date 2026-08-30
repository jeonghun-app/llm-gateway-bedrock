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
#   ./scripts/deploy.sh --allowed-cidr 203.0.113.10/32
#   ./scripts/deploy.sh --allowed-cidr 203.0.113.10/32 --env stg --region us-west-2
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
# 부트스트랩 시 허용 목록에 넣을 CIDR. 여러 번 지정할 수 있다. 배포 후
# 추가·삭제는 scripts/manage_access.sh 로 하며 재배포가 필요 없다.
ALLOWED_CIDRS=()
CERTIFICATE_ARN=""
DESIRED_COUNT="1"
TASK_CPU="512"
TASK_MEMORY="1024"
LOG_LEVEL="INFO"
DEFAULT_ALLOWED_MODELS=""
# Bedrock 호출 리전. 비우면 스택 리전(--region)을 쓴다. 모델 가용성이
# 리전마다 달라 게이트웨이는 서울에 두고 Bedrock 만 us-east-1 로 부르는
# 구성을 이 옵션으로 만든다.
BEDROCK_REGION=""
# usage 원본 레코드 보존 기간(일). 집계 테이블은 만료되지 않는다.
USAGE_TTL_DAYS=""
# 태스크 역할이 호출할 수 있는 기반 모델 ARN 패턴. 비우면 템플릿 기본값
# (모든 기반 모델)을 쓴다. 특정 모델로 IAM 을 좁히려면 지정한다.
ALLOWED_BEDROCK_MODEL_ARN=""
# 단가 표에 없는 모델 처리 방식: allow | reject | hide
UNPRICED_MODEL_POLICY=""
# 이미 있는 이미지를 쓸 때 지정한다. 지정하면 ECR 스택 생성과 로컬 빌드를
# 건너뛴다. Docker 를 쓸 수 없는 환경에서 설치하는 경로다.
PREBUILT_IMAGE=""
# public | private-nat. private-nat 은 태스크에 공인 IP 를 붙이지 않는 대신
# NAT Gateway 비용이 붙는다.
TASK_SUBNET_MODE=""
# 활성화할 요청 필터 확장. 기본 이미지에는 확장이 없으므로 파생 이미지를
# 쓸 때만 의미가 있다.
REQUEST_FILTERS=""
ALARM_EMAIL=""
RUN_SEED="yes"
RUN_SMOKE="yes"
SHOW_ADMIN_TOKEN="no"
DOCKER_CMD="docker"
ACCESS_MAX_ENTRIES="20"

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
                            여러 번 지정할 수 있다. 예:
                              --allowed-cidr 203.0.113.10/32 --allowed-cidr 198.51.100.5/32
                            배포 후 단말 추가·삭제는 재배포 없이
                            ./scripts/manage_access.sh 로 한다.

선택
  --access-max-entries <N>  허용 목록 최대 항목 수 (기본 20). 생성 후 늘릴
                            수만 있고 줄일 수 없다.
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
  --bedrock-region <리전>   Bedrock 호출 리전 (선택). 비우면 --region 을
                            쓴다. 배포 리전에 원하는 모델이 없을 때 지정한다.
                            예: --region ap-northeast-2 --bedrock-region us-east-1
  --usage-ttl-days <N>      usage 원본 레코드 보존 기간 (기본 90)
  --allowed-model-arn <ARN> 태스크 역할이 호출 가능한 기반 모델 ARN 패턴.
                            비우면 모든 기반 모델. IAM 을 좁히려면 지정한다.
  --unpriced-model-policy <값>
                            단가 표에 없는 모델 처리 (allow|reject|hide).
                            기본 allow. reject 는 비용 귀속을 보장하고,
                            hide 는 /v1/models 에서 감춘다.
  --task-subnet-mode <모드> 태스크를 둘 서브넷 (public|private-nat).
                            기본 public 은 퍼블릭 서브넷 + 공인 IP 로 추가
                            비용이 없다. private-nat 은 공인 IP 를 붙이지
                            않는 대신 NAT Gateway 가 월 약 33 USD 를 더한다.
  --request-filters <명세>  활성화할 요청 필터 확장 (module:Class, 쉼표 구분).
                            확장은 게이트웨이 프로세스 안에서 신뢰된 코드로
                            돈다. 기본 이미지에는 확장이 없으므로 파생
                            이미지와 함께 쓴다. docs/extensions-v1.md 참고.
  --image <URI>             이미 있는 이미지로 배포한다. ECR 스택 생성과
                            로컬 Docker 빌드를 건너뛴다. Docker 를 쓸 수 없는
                            환경에서 쓴다. 예:
                              --image ghcr.io/jeonghun-app/llm-gateway-bedrock:v1.10.0
                            프로덕션에서는 이 이미지를 계정 내 private ECR 로
                            복사한 뒤 그 URI 를 지정하기를 권한다. Fargate 는
                            태스크마다 이미지를 새로 받으므로 외부 레지스트리
                            장애가 스케일아웃을 막는다.
  --alarm-email <메일>      알람 수신 이메일
  --no-seed                 데모 데이터 시드를 건너뛴다
  --no-smoke                스모크 테스트를 건너뛴다
  --show-admin-token        관리 토큰을 화면에 출력한다 (기본은 조회 명령만)
  -h, --help                이 도움말

예시
  # 현재 단말만 열고 배포
  ./scripts/deploy.sh --allowed-cidr "$(curl -s https://checkip.amazonaws.com)/32"

  # 여러 단말을 한 번에 열고 배포
  ./scripts/deploy.sh --allowed-cidr 203.0.113.10/32 \
                      --allowed-cidr 198.51.100.5/32

  # 배포 후 단말 추가 (재배포 없음, 수 초 내 반영)
  ./scripts/manage_access.sh add-me --label "재택-노트북"
  ./scripts/manage_access.sh list
USAGE
}

# ---------------------------------------------------------------------------
# 인자 파싱
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --allowed-cidr)     ALLOWED_CIDRS+=("${2:-}"); shift 2 ;;
        --access-max-entries) ACCESS_MAX_ENTRIES="${2:-}"; shift 2 ;;
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
        --bedrock-region)   BEDROCK_REGION="${2:-}"; shift 2 ;;
        --usage-ttl-days)   USAGE_TTL_DAYS="${2:-}"; shift 2 ;;
        --allowed-model-arn) ALLOWED_BEDROCK_MODEL_ARN="${2:-}"; shift 2 ;;
        --unpriced-model-policy) UNPRICED_MODEL_POLICY="${2:-}"; shift 2 ;;
        --image)            PREBUILT_IMAGE="${2:-}"; shift 2 ;;
        --task-subnet-mode) TASK_SUBNET_MODE="${2:-}"; shift 2 ;;
        --request-filters)  REQUEST_FILTERS="${2:-}"; shift 2 ;;
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

[[ ${#ALLOWED_CIDRS[@]} -gt 0 ]] || { usage; die "--allowed-cidr 는 최소 한 번 지정해야 한다."; }

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
        die "${label} 형식이 올바르지 않다: ${cidr} (예: 203.0.113.10/32)"
    fi
}
for cidr in "${ALLOWED_CIDRS[@]}"; do
    validate_cidr "${cidr}" "--allowed-cidr"
done
if [[ ${#ALLOWED_CIDRS[@]} -gt ${ACCESS_MAX_ENTRIES} ]]; then
    die "지정한 CIDR 수(${#ALLOWED_CIDRS[@]})가 --access-max-entries(${ACCESS_MAX_ENTRIES})보다 많다."
fi

command -v aws >/dev/null 2>&1 || die "aws CLI 가 필요하다."
command -v jq  >/dev/null 2>&1 || die "jq 가 필요하다."

# 사전 빌드 이미지를 쓰면 로컬 빌드가 없으므로 docker 를 요구하지 않는다.
# 기업에서 개발자 PC 의 이미지 빌드를 막는 경우가 많아 이 경로가 필요하다.
if [[ -n "${TASK_SUBNET_MODE}" && "${TASK_SUBNET_MODE}" != "public" \
      && "${TASK_SUBNET_MODE}" != "private-nat" ]]; then
    die "--task-subnet-mode 는 public 또는 private-nat 이어야 한다: ${TASK_SUBNET_MODE}"
fi
if [[ "${TASK_SUBNET_MODE}" == "private-nat" ]]; then
    info "프라이빗 서브넷 모드. NAT Gateway 가 월 약 33 USD 를 더한다."
fi

if [[ -n "${PREBUILT_IMAGE}" ]]; then
    info "사전 빌드 이미지를 사용한다. docker 검사와 빌드를 건너뛴다."
    DOCKER_CMD="skip"
# docker 그룹이 현재 셸에 반영되지 않은 상태를 흔하게 만난다. sg 로 우회한다.
elif docker info >/dev/null 2>&1; then
    DOCKER_CMD="docker"
elif command -v sg >/dev/null 2>&1 && sg docker -c "docker info" >/dev/null 2>&1; then
    DOCKER_CMD="sg_docker"
    info "docker 그룹이 현재 셸에 반영되지 않아 sg 로 실행한다."
else
    die "docker 데몬에 접근할 수 없다. 'sudo systemctl start docker' 와 'sudo usermod -aG docker \$USER' 를 확인한다. 또는 --image 로 사전 빌드 이미지를 지정한다."
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
# 30분 뒤에 알게 되는 것보다 지금 아는 편이 낫다. Bedrock 을 다른 리전에서
# 부르도록 --bedrock-region 을 준 경우, 그 리전에서 확인해야 의미가 있다.
bedrock_check_region="${BEDROCK_REGION:-${AWS_REGION}}"
model_count="$(aws --region "${bedrock_check_region}" bedrock list-foundation-models \
    --by-output-modality TEXT --by-inference-type ON_DEMAND \
    --query 'length(modelSummaries)' --output text 2>/dev/null || echo "0")"
if [[ "${model_count}" == "0" ]]; then
    warn "Bedrock 온디맨드 텍스트 모델이 ${bedrock_check_region} 에서 조회되지 않는다. 콘솔의 Model access 에서 모델을 활성화해야 호출이 성공한다."
else
    info "Bedrock 온디맨드 텍스트 모델 ${model_count}개 확인 (${bedrock_check_region})"
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
if [[ -n "${PREBUILT_IMAGE}" ]]; then
    log "2-3/7 ECR 스택과 이미지 빌드 건너뜀"
    IMAGE_URI="${PREBUILT_IMAGE}"
    # 사전 빌드 이미지는 우리 계정 밖(GHCR 등)에 있을 수 있다. 그 경우 태스크
    # 실행 역할에 ECR pull 권한을 주지 않는다. 공개 레지스트리는 익명 pull 이라
    # 권한이 필요 없고, 주지 않는 편이 최소권한에 맞다.
    REPOSITORY_ARN=""
    info "이미지 ${IMAGE_URI}"
else
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
fi

# ---------------------------------------------------------------------------
# 4. 애플리케이션 스택
# ---------------------------------------------------------------------------
log "4/7 애플리케이션 스택 배포 (5~10분 소요)"

# 값이 있을 때만 넘길 파라미터. 빈 값을 넘기면 CloudFormation 기본값 대신
# 빈 문자열이 들어가 UsageTtlDays(Number)는 검증 실패하고,
# AllowedBedrockModelArn 은 IAM 이 아무 모델도 허용하지 않게 된다.
# BedrockRegion 은 템플릿이 빈 값을 HasBedrockRegion 조건으로 처리하므로
# 항상 넘겨도 안전하다.
optional_overrides=()
[[ -n "${USAGE_TTL_DAYS}" ]] \
    && optional_overrides+=("UsageTtlDays=${USAGE_TTL_DAYS}")
[[ -n "${ALLOWED_BEDROCK_MODEL_ARN}" ]] \
    && optional_overrides+=("AllowedBedrockModelArn=${ALLOWED_BEDROCK_MODEL_ARN}")
[[ -n "${UNPRICED_MODEL_POLICY}" ]] \
    && optional_overrides+=("UnpricedModelPolicy=${UNPRICED_MODEL_POLICY}")
# 값이 비어도 반드시 넘긴다. 넘기지 않으면 CloudFormation 이 기존 값을
# 유지하므로, private ECR 로 배포했던 스택을 공개 이미지로 옮길 때 태스크 실행
# 역할에 ECR pull 권한이 남는다. 최소권한이 깨지는 것을 실측으로 확인했다.
optional_overrides+=("EcrRepositoryArn=${REPOSITORY_ARN}")
[[ -n "${TASK_SUBNET_MODE}" ]] \
    && optional_overrides+=("TaskSubnetMode=${TASK_SUBNET_MODE}")
[[ -n "${REQUEST_FILTERS}" ]] \
    && optional_overrides+=("RequestFilters=${REQUEST_FILTERS}")

# CloudFormation 은 본문으로 직접 넘기는 템플릿을 51,200 바이트로 제한한다.
# app.yaml 이 그 한도에 도달했다(v1.12.1 에서 여유가 75 바이트였다). S3 를
# 경유하면 한도가 1MB 로 올라가므로 앞으로 기능을 더해도 다시 막히지 않는다.
#
# 버킷은 CloudFormation 이 관리하지 않는다. 스택을 만들기 위해 필요한
# 버킷을 그 스택이 만들 수는 없다. 이름에 계정 ID 와 리전을 넣어 전역
# 유일성을 확보한다.
TEMPLATE_BUCKET="${PROJECT_NAME}-${ENVIRONMENT}-cfn-${ACCOUNT_ID}-${AWS_REGION}"
if ! aws_cli s3api head-bucket --bucket "${TEMPLATE_BUCKET}" >/dev/null 2>&1; then
    info "템플릿 버킷 생성 ${TEMPLATE_BUCKET}"
    # us-east-1 은 LocationConstraint 를 받지 않는다. AWS API 의 예외다.
    if [[ "${AWS_REGION}" == "us-east-1" ]]; then
        aws_cli s3api create-bucket --bucket "${TEMPLATE_BUCKET}" >/dev/null
    else
        aws_cli s3api create-bucket --bucket "${TEMPLATE_BUCKET}" \
            --create-bucket-configuration "LocationConstraint=${AWS_REGION}" \
            >/dev/null
    fi
    aws_cli s3api put-public-access-block --bucket "${TEMPLATE_BUCKET}" \
        --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
        >/dev/null
    aws_cli s3api put-bucket-encryption --bucket "${TEMPLATE_BUCKET}" \
        --server-side-encryption-configuration \
        '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' \
        >/dev/null
    # 템플릿 이력은 오래 둘 이유가 없다. 30일 뒤 지워 저장 비용을 없앤다.
    aws_cli s3api put-bucket-lifecycle-configuration \
        --bucket "${TEMPLATE_BUCKET}" \
        --lifecycle-configuration \
        '{"Rules":[{"ID":"expire-templates","Status":"Enabled","Filter":{"Prefix":""},"Expiration":{"Days":30}}]}' \
        >/dev/null
fi

aws_cli cloudformation deploy \
    --stack-name "${APP_STACK}" \
    --template-file "${REPO_ROOT}/infra/app.yaml" \
    --s3-bucket "${TEMPLATE_BUCKET}" \
    --s3-prefix "${APP_STACK}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset \
    --parameter-overrides \
        "ProjectName=${PROJECT_NAME}" \
        "Environment=${ENVIRONMENT}" \
        "Owner=${OWNER}" \
        "ImageUri=${IMAGE_URI}" \
        "AccessListMaxEntries=${ACCESS_MAX_ENTRIES}" \
        "CertificateArn=${CERTIFICATE_ARN}" \
        "DesiredCount=${DESIRED_COUNT}" \
        "TaskCpu=${TASK_CPU}" \
        "TaskMemory=${TASK_MEMORY}" \
        "LogLevel=${LOG_LEVEL}" \
        "DefaultAllowedModels=${DEFAULT_ALLOWED_MODELS}" \
        "BedrockRegion=${BEDROCK_REGION}" \
        "AlarmEmail=${ALARM_EMAIL}" \
        ${optional_overrides[@]+"${optional_overrides[@]}"} \
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
# 4b. 접근 허용 목록 시딩
# ---------------------------------------------------------------------------
# 보안 그룹은 프리픽스 리스트 하나만 참조한다. CloudFormation 은 리스트
# 리소스만 만들고 엔트리는 관리하지 않으므로(그래야 재배포 없이 단말을
# 추가·삭제할 수 있다) 여기서 초기 엔트리를 채운다. 이미 있는 CIDR 은
# 건너뛰어 재실행이 안전하다.
log "접근 허용 목록 설정"
PREFIX_LIST_ID="$(stack_output "${APP_STACK}" AccessPrefixListId)"
info "프리픽스 리스트 ${PREFIX_LIST_ID}"

wait_prefix_list_ready() {
    local state
    for _ in $(seq 1 30); do
        state="$(aws_cli ec2 describe-managed-prefix-lists \
            --prefix-list-ids "${PREFIX_LIST_ID}" \
            --query 'PrefixLists[0].State' --output text 2>/dev/null || echo unknown)"
        case "${state}" in
            create-complete|modify-complete|restore-complete) return 0 ;;
            *-failed) die "프리픽스 리스트가 실패 상태다: ${state}" ;;
        esac
        sleep 2
    done
    die "프리픽스 리스트가 준비되지 않았다."
}

for cidr in "${ALLOWED_CIDRS[@]}"; do
    wait_prefix_list_ready
    if [[ -n "$(aws_cli ec2 get-managed-prefix-list-entries \
            --prefix-list-id "${PREFIX_LIST_ID}" \
            --query "Entries[?Cidr=='${cidr}'].Cidr" --output text)" ]]; then
        info "${cidr} 이미 등록됨"
        continue
    fi
    aws_cli ec2 modify-managed-prefix-list \
        --prefix-list-id "${PREFIX_LIST_ID}" \
        --current-version "$(aws_cli ec2 describe-managed-prefix-lists \
            --prefix-list-ids "${PREFIX_LIST_ID}" \
            --query 'PrefixLists[0].Version' --output text)" \
        --add-entries "Cidr=${cidr},Description=bootstrap by deploy.sh" \
        >/dev/null
    info "${cidr} 추가"
done
wait_prefix_list_ready

ALLOWED_SUMMARY="$(aws_cli ec2 get-managed-prefix-list-entries \
    --prefix-list-id "${PREFIX_LIST_ID}" \
    --query 'Entries[].Cidr' --output text | tr '\t' ' ')"
info "현재 허용: ${ALLOWED_SUMMARY:-(없음)}"

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
    warn "현재 허용 목록: ${ALLOWED_SUMMARY:-(없음)}"
    warn "이 단말을 추가: ./scripts/manage_access.sh add-me --env ${ENVIRONMENT} --region ${AWS_REGION}"
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

 접근 허용 목록  ${ALLOWED_SUMMARY:-(없음)}
 단말 관리 (재배포 불필요, 수 초 내 반영)
   ./scripts/manage_access.sh list
   ./scripts/manage_access.sh add-me --label "내-노트북"
   ./scripts/manage_access.sh add 203.0.113.10/32 --label "사무실"
   ./scripts/manage_access.sh remove 203.0.113.10/32
   ./scripts/manage_access.sh check

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
