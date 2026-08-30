#!/usr/bin/env bash
#
# LLM Gateway 리소스 삭제.
#
# 기본 동작은 애플리케이션 스택만 삭제한다. ECR 리포지토리와 그 안의
# 이미지는 남는다. 이미지를 다시 빌드하지 않고 재배포할 수 있게 하기
# 위해서다.
#
# 사용법
#   ./scripts/teardown.sh --env dev --region us-east-1
#   ./scripts/teardown.sh --env dev --purge-data     # 앱 스택 + ECR 까지
#
# 되돌릴 수 없는 작업이므로 실행 전에 삭제 대상을 보여주고 확인을 받는다.
# --yes 로 확인을 생략할 수 있지만 자동화에서만 쓴다.
set -euo pipefail

PROJECT_NAME="llmgw"
ENVIRONMENT="dev"
AWS_REGION="${AWS_REGION:-us-east-1}"
PURGE_DATA="no"
ASSUME_YES="no"

log()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\n[실패] %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
사용법: ./scripts/teardown.sh [옵션]

옵션
  --project <이름>      리소스 이름 접두어 (기본 llmgw)
  --env <환경>          배포 환경 (기본 dev)
  --region <리전>       리전 (기본 us-east-1 또는 $AWS_REGION)
  --purge-data          ECR 리포지토리와 이미지, 남은 DynamoDB 테이블까지
                        삭제한다. 사용량 이력이 영구히 사라진다.
  --yes                 확인 프롬프트를 생략한다 (자동화 전용)
  -h, --help            이 도움말
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project)    PROJECT_NAME="${2:-}"; shift 2 ;;
        --env)        ENVIRONMENT="${2:-}"; shift 2 ;;
        --region)     AWS_REGION="${2:-}"; shift 2 ;;
        --purge-data) PURGE_DATA="yes"; shift ;;
        --yes)        ASSUME_YES="yes"; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            usage; die "알 수 없는 인자: $1" ;;
    esac
done

readonly ECR_STACK="${PROJECT_NAME}-${ENVIRONMENT}-ecr"
readonly APP_STACK="${PROJECT_NAME}-${ENVIRONMENT}-app"

aws_cli() { aws --region "${AWS_REGION}" "$@"; }

stack_exists() {
    aws_cli cloudformation describe-stacks --stack-name "$1" \
        >/dev/null 2>&1
}

log "삭제 대상"
info "리전     ${AWS_REGION}"
info "환경     ${ENVIRONMENT}"
if stack_exists "${APP_STACK}"; then
    info "스택     ${APP_STACK} (VPC, ALB, ECS, DynamoDB 3개, IAM, 알람)"
else
    info "스택     ${APP_STACK} — 없음"
fi
if [[ "${PURGE_DATA}" == "yes" ]]; then
    if stack_exists "${ECR_STACK}"; then
        info "스택     ${ECR_STACK} (ECR 리포지토리와 모든 이미지)"
    fi
    info "주의     사용량 이력과 계정·키 정보가 영구히 삭제된다."
else
    info "유지     ${ECR_STACK} (ECR 이미지). --purge-data 로 함께 삭제 가능"
fi

if [[ "${ASSUME_YES}" != "yes" ]]; then
    printf '\n계속하려면 "delete" 를 입력한다: '
    read -r answer
    [[ "${answer}" == "delete" ]] || die "취소했다."
fi

# ---------------------------------------------------------------------------
# 애플리케이션 스택
# ---------------------------------------------------------------------------
if stack_exists "${APP_STACK}"; then
    log "애플리케이션 스택 삭제 (5~10분 소요)"
    aws_cli cloudformation delete-stack --stack-name "${APP_STACK}"
    if aws_cli cloudformation wait stack-delete-complete \
        --stack-name "${APP_STACK}" 2>/dev/null; then
        info "삭제 완료"
    else
        printf '\n[경고] 스택 삭제가 완료되지 않았다. 실패 이벤트:\n' >&2
        # shellcheck disable=SC2016  # 백틱은 JMESPath 리터럴 문법이다.
        aws_cli cloudformation describe-stack-events \
            --stack-name "${APP_STACK}" \
            --query 'StackEvents[?contains(ResourceStatus, `FAILED`)].[LogicalResourceId,ResourceStatusReason]' \
            --output table 2>/dev/null | head -20 >&2 || true
    fi
fi

# ---------------------------------------------------------------------------
# 데이터 정리
# ---------------------------------------------------------------------------
if [[ "${PURGE_DATA}" == "yes" ]]; then
    # 앱 스택이 테이블을 이미 지웠지만, 스택 없이 남은 잔여 테이블도
    # 정리한다. 남아 있으면 다음 배포가 "이미 존재한다" 로 실패한다.
    log "잔여 DynamoDB 테이블 확인"
    for suffix in registry usage usage-agg; do
        table="${PROJECT_NAME}-${ENVIRONMENT}-${suffix}"
        if aws_cli dynamodb describe-table --table-name "${table}" \
            >/dev/null 2>&1; then
            aws_cli dynamodb delete-table --table-name "${table}" \
                >/dev/null
            info "${table} 삭제 요청"
        fi
    done

    # 템플릿 버킷은 CloudFormation 이 관리하지 않는다. 스택을 만들기 위해
    # 필요한 버킷을 그 스택이 만들 수는 없어서다. 여기서 지운다.
    log "템플릿 버킷 확인"
    account_id="$(aws_cli sts get-caller-identity --query Account --output text)"
    bucket="${PROJECT_NAME}-${ENVIRONMENT}-cfn-${account_id}-${AWS_REGION}"
    if aws_cli s3api head-bucket --bucket "${bucket}" >/dev/null 2>&1; then
        # 객체가 남아 있으면 버킷 삭제가 실패한다.
        aws_cli s3 rm "s3://${bucket}" --recursive >/dev/null 2>&1 || true
        # A && B || C 는 if-then-else 가 아니다. B 가 실패하면 C 도 돈다.
        if aws_cli s3api delete-bucket --bucket "${bucket}" >/dev/null 2>&1
        then
            info "${bucket} 삭제"
        else
            info "${bucket} 삭제 실패 (수동 확인 필요)"
        fi
    fi

    log "잔여 시크릿 확인"
    secret_name="${PROJECT_NAME}/${ENVIRONMENT}/admin-token"
    if aws_cli secretsmanager describe-secret --secret-id "${secret_name}" \
        >/dev/null 2>&1; then
        # 복구 기간을 두지 않고 즉시 삭제한다. 두면 같은 이름으로 재배포가
        # 실패한다.
        aws_cli secretsmanager delete-secret --secret-id "${secret_name}" \
            --force-delete-without-recovery >/dev/null
        info "${secret_name} 즉시 삭제"
    fi

    if stack_exists "${ECR_STACK}"; then
        log "ECR 이미지 삭제"
        repo_name="${PROJECT_NAME}-${ENVIRONMENT}"
        image_ids="$(aws_cli ecr list-images --repository-name "${repo_name}" \
            --query 'imageIds[*]' --output json 2>/dev/null || echo '[]')"
        if [[ "${image_ids}" != "[]" && -n "${image_ids}" ]]; then
            aws_cli ecr batch-delete-image --repository-name "${repo_name}" \
                --image-ids "${image_ids}" >/dev/null 2>&1 || true
            info "이미지 삭제 요청"
        fi

        log "ECR 스택 삭제"
        # 리포지토리는 DeletionPolicy=Retain 이라 스택만 지우면 남는다.
        # 명시적으로 지운 뒤 스택을 삭제한다.
        aws_cli ecr delete-repository --repository-name "${repo_name}" \
            --force >/dev/null 2>&1 || true
        aws_cli cloudformation delete-stack --stack-name "${ECR_STACK}"
        aws_cli cloudformation wait stack-delete-complete \
            --stack-name "${ECR_STACK}" 2>/dev/null || true
        info "삭제 완료"
    fi
fi

log "정리 결과"
for stack in "${APP_STACK}" "${ECR_STACK}"; do
    if stack_exists "${stack}"; then
        info "${stack} — 남아 있음"
    else
        info "${stack} — 삭제됨"
    fi
done

cat <<'NOTE'

   CloudWatch 로그 그룹은 스택과 함께 삭제된다. 로그를 보관해야 한다면
   삭제 전에 내보내야 한다.

   남아 있을 수 있는 것: 로컬 .deploy/ 디렉터리의 데모 키 파일.
   필요 없으면 지운다: rm -rf .deploy
NOTE
