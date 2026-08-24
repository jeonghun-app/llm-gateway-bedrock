#!/usr/bin/env bash
#
# ALB 접근 허용 단말 관리.
#
# 관리형 프리픽스 리스트의 엔트리를 직접 조작한다. 보안 그룹은 이 리스트
# 하나만 참조하므로 단말이 몇 개든 규칙은 프로토콜당 1개로 유지되고,
# 단말 추가·삭제에 CloudFormation 스택 업데이트가 필요 없다. 반영은 보통
# 수 초 안에 끝난다.
#
# 사용법
#   ./scripts/manage_access.sh list
#   ./scripts/manage_access.sh add 203.0.113.10/32 --label "사무실-맥북"
#   ./scripts/manage_access.sh add-me --label "현재-단말"
#   ./scripts/manage_access.sh remove 203.0.113.10/32
#   ./scripts/manage_access.sh check            # 현재 단말이 허용되는지 확인
#
# 0.0.0.0/0 과 모든 /0 프리픽스는 거부한다. 전체 개방이 필요하다고 판단되면
# 이 스크립트를 우회하지 말고 ALB 앞에 WAF 나 인증을 두는 방향을 검토한다.
set -euo pipefail

PROJECT_NAME="llmgw"
ENVIRONMENT="dev"
AWS_REGION="${AWS_REGION:-us-east-1}"
LABEL=""
COMMAND=""
TARGET_CIDR=""

log()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\n[실패] %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
사용법: ./scripts/manage_access.sh <명령> [인자] [옵션]

명령
  list                    허용된 단말 목록을 보여준다
  add <CIDR>              단말을 추가한다
  add-me                  이 셸이 나가는 공인 IP 를 /32 로 추가한다
  remove <CIDR>           단말을 제거한다
  check [CIDR]            해당 CIDR(생략 시 현재 단말)이 허용 목록에 있는지 확인
  status                  프리픽스 리스트와 보안 그룹 연결 상태를 점검한다

옵션
  --label <설명>          add 시 기록할 설명. 어느 단말인지 알아보기 위해 권장
  --project <이름>        리소스 이름 접두어 (기본 llmgw)
  --env <환경>            배포 환경 (기본 dev)
  --region <리전>         리전 (기본 us-east-1 또는 $AWS_REGION)
  -h, --help              이 도움말

예시
  # 새 노트북에서 접속 가능하게 만들기 (해당 단말에서 실행)
  ./scripts/manage_access.sh add-me --label "재택-노트북"

  # 사무실 대역 추가
  ./scripts/manage_access.sh add 203.0.113.0/28 --label "본사-사무실"

  # 퇴사자 단말 제거
  ./scripts/manage_access.sh remove 198.51.100.5/32
USAGE
}

[[ $# -gt 0 ]] || { usage; exit 1; }

COMMAND="$1"; shift
case "${COMMAND}" in
    add|remove|check)
        if [[ $# -gt 0 && "$1" != --* ]]; then
            TARGET_CIDR="$1"; shift
        fi
        ;;
    list|add-me|status) ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "알 수 없는 명령: ${COMMAND}" ;;
esac

while [[ $# -gt 0 ]]; do
    case "$1" in
        --label)   LABEL="${2:-}"; shift 2 ;;
        --project) PROJECT_NAME="${2:-}"; shift 2 ;;
        --env)     ENVIRONMENT="${2:-}"; shift 2 ;;
        --region)  AWS_REGION="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; die "알 수 없는 인자: $1" ;;
    esac
done

readonly APP_STACK="${PROJECT_NAME}-${ENVIRONMENT}-app"

aws_cli() { aws --region "${AWS_REGION}" "$@"; }

command -v jq >/dev/null 2>&1 || die "jq 가 필요하다."

# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------

# CIDR 형식을 검증하고 전체 개방을 거부한다.
validate_cidr() {
    local cidr="$1"
    [[ -n "${cidr}" ]] || die "CIDR 을 지정한다. 예: 203.0.113.10/32"
    if [[ "${cidr}" == "0.0.0.0/0" || "${cidr}" == */0 ]]; then
        die "전체 개방 CIDR(${cidr})은 허용하지 않는다. 단말 IP /32 를 쓴다."
    fi
    if [[ ! "${cidr}" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/[0-9]{1,2}$ ]]; then
        die "CIDR 형식이 올바르지 않다: ${cidr} (예: 203.0.113.10/32)"
    fi
    local prefix="${cidr##*/}"
    if (( prefix < 8 )); then
        die "프리픽스 /${prefix} 는 너무 넓다. /8 이상으로 좁힌다."
    fi
}

# 스택 출력에서 프리픽스 리스트 ID 를 얻는다.
resolve_prefix_list() {
    local pl_id
    pl_id="$(aws_cli cloudformation describe-stacks --stack-name "${APP_STACK}" \
        --query "Stacks[0].Outputs[?OutputKey=='AccessPrefixListId'].OutputValue" \
        --output text 2>/dev/null || true)"
    if [[ -z "${pl_id}" || "${pl_id}" == "None" ]]; then
        die "스택 ${APP_STACK} 에서 AccessPrefixListId 를 찾을 수 없다. 먼저 ./scripts/deploy.sh 로 배포한다."
    fi
    printf '%s' "${pl_id}"
}

current_version() {
    aws_cli ec2 describe-managed-prefix-lists --prefix-list-ids "$1" \
        --query 'PrefixLists[0].Version' --output text
}

my_public_ip() {
    local ip
    ip="$(curl -s --max-time 8 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]')"
    [[ -n "${ip}" ]] || die "공인 IP 를 확인할 수 없다. --label 없이 add <CIDR> 로 직접 지정한다."
    printf '%s' "${ip}"
}

# 프리픽스 리스트가 사용 가능한 상태가 될 때까지 기다린다. 수정 직후에는
# modify-in-progress 상태라 연속 호출이 실패한다.
wait_until_ready() {
    local pl_id="$1" state
    for _ in $(seq 1 30); do
        state="$(aws_cli ec2 describe-managed-prefix-lists \
            --prefix-list-ids "${pl_id}" \
            --query 'PrefixLists[0].State' --output text 2>/dev/null || echo unknown)"
        case "${state}" in
            create-complete|modify-complete|restore-complete) return 0 ;;
            *-failed) die "프리픽스 리스트가 실패 상태다: ${state}" ;;
        esac
        sleep 2
    done
    die "프리픽스 리스트가 준비되지 않았다."
}

print_entries() {
    local pl_id="$1" json
    json="$(aws_cli ec2 get-managed-prefix-list-entries --prefix-list-id "${pl_id}" \
        --query 'Entries' --output json)"
    local count
    count="$(echo "${json}" | jq 'length')"
    local max
    max="$(aws_cli ec2 describe-managed-prefix-lists --prefix-list-ids "${pl_id}" \
        --query 'PrefixLists[0].MaxEntries' --output text)"
    if [[ "${count}" == "0" ]]; then
        info "허용된 단말이 없다. 아무도 ALB 에 접근할 수 없다."
        info "추가: ./scripts/manage_access.sh add-me --label \"내-단말\""
    else
        printf '   %-22s %s\n' "CIDR" "설명"
        printf '   %-22s %s\n' "----------------------" "------------------------"
        # jq 는 탭으로 구분만 하고, 정렬과 들여쓰기는 awk 가 한 번에 처리한다.
        echo "${json}" \
            | jq -r '.[] | "\(.Cidr)\t\(.Description // "-")"' \
            | awk -F'\t' '{printf "   %-22s %s\n", $1, $2}'
    fi
    info ""
    info "사용 ${count} / 최대 ${max}"
}

# ---------------------------------------------------------------------------
# 명령
# ---------------------------------------------------------------------------

case "${COMMAND}" in
    list)
        PL_ID="$(resolve_prefix_list)"
        log "접근 허용 단말 (${APP_STACK})"
        info "프리픽스 리스트 ${PL_ID}"
        info ""
        print_entries "${PL_ID}"
        ;;

    add|add-me)
        PL_ID="$(resolve_prefix_list)"
        if [[ "${COMMAND}" == "add-me" ]]; then
            TARGET_CIDR="$(my_public_ip)/32"
            info "현재 단말 IP: ${TARGET_CIDR}"
        fi
        validate_cidr "${TARGET_CIDR}"

        wait_until_ready "${PL_ID}"
        existing="$(aws_cli ec2 get-managed-prefix-list-entries \
            --prefix-list-id "${PL_ID}" \
            --query "Entries[?Cidr=='${TARGET_CIDR}'].Cidr" --output text)"
        if [[ -n "${existing}" && "${existing}" != "None" ]]; then
            log "이미 등록됨"
            info "${TARGET_CIDR} 은 이미 허용 목록에 있다."
            print_entries "${PL_ID}"
            exit 0
        fi

        count="$(aws_cli ec2 get-managed-prefix-list-entries --prefix-list-id "${PL_ID}" \
            --query 'length(Entries)' --output text)"
        max="$(aws_cli ec2 describe-managed-prefix-lists --prefix-list-ids "${PL_ID}" \
            --query 'PrefixLists[0].MaxEntries' --output text)"
        if (( count >= max )); then
            die "허용 목록이 가득 찼다 (${count}/${max}). 쓰지 않는 항목을 remove 하거나, AccessListMaxEntries 를 늘려 재배포한다."
        fi

        description="${LABEL:-added $(date -u +%Y-%m-%dT%H:%M:%SZ)}"
        log "단말 추가"
        aws_cli ec2 modify-managed-prefix-list \
            --prefix-list-id "${PL_ID}" \
            --current-version "$(current_version "${PL_ID}")" \
            --add-entries "Cidr=${TARGET_CIDR},Description=${description}" \
            >/dev/null
        wait_until_ready "${PL_ID}"
        info "${TARGET_CIDR} 추가 완료 (${description})"
        info ""
        print_entries "${PL_ID}"
        ;;

    remove)
        PL_ID="$(resolve_prefix_list)"
        validate_cidr "${TARGET_CIDR}"
        wait_until_ready "${PL_ID}"
        existing="$(aws_cli ec2 get-managed-prefix-list-entries \
            --prefix-list-id "${PL_ID}" \
            --query "Entries[?Cidr=='${TARGET_CIDR}'].Cidr" --output text)"
        if [[ -z "${existing}" || "${existing}" == "None" ]]; then
            die "${TARGET_CIDR} 은 허용 목록에 없다. list 로 확인한다."
        fi
        log "단말 제거"
        aws_cli ec2 modify-managed-prefix-list \
            --prefix-list-id "${PL_ID}" \
            --current-version "$(current_version "${PL_ID}")" \
            --remove-entries "Cidr=${TARGET_CIDR}" \
            >/dev/null
        wait_until_ready "${PL_ID}"
        info "${TARGET_CIDR} 제거 완료"
        info ""
        print_entries "${PL_ID}"
        ;;

    check)
        PL_ID="$(resolve_prefix_list)"
        if [[ -z "${TARGET_CIDR}" ]]; then
            TARGET_CIDR="$(my_public_ip)/32"
            info "현재 단말 IP: ${TARGET_CIDR}"
        fi
        log "허용 여부 확인"
        # 정확한 CIDR 일치만 확인한다. 상위 대역에 포함되는 경우까지
        # 판정하려면 서브넷 계산이 필요해 여기서는 다루지 않는다.
        hit="$(aws_cli ec2 get-managed-prefix-list-entries --prefix-list-id "${PL_ID}" \
            --query "Entries[?Cidr=='${TARGET_CIDR}'].Description" --output text)"
        if [[ -n "${hit}" && "${hit}" != "None" ]]; then
            info "${TARGET_CIDR} → 허용됨 (${hit})"
        else
            info "${TARGET_CIDR} → 정확히 일치하는 항목이 없다."
            info "상위 대역으로 포함될 수 있으니 아래 목록을 확인한다."
            info ""
            print_entries "${PL_ID}"
        fi

        gateway_url="$(aws_cli cloudformation describe-stacks --stack-name "${APP_STACK}" \
            --query "Stacks[0].Outputs[?OutputKey=='GatewayUrl'].OutputValue" \
            --output text 2>/dev/null || true)"
        if [[ -n "${gateway_url}" && "${gateway_url}" != "None" ]]; then
            info ""
            code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
                "${gateway_url}/healthz" 2>/dev/null || echo "000")"
            if [[ "${code}" == "200" ]]; then
                info "실제 접속 테스트: 성공 (HTTP 200)"
            else
                info "실제 접속 테스트: 실패 (HTTP ${code})"
                info "000 이면 네트워크 차단, 그 외는 애플리케이션 오류다."
            fi
        fi
        ;;

    status)
        PL_ID="$(resolve_prefix_list)"
        log "접근 통제 상태"
        aws_cli ec2 describe-managed-prefix-lists --prefix-list-ids "${PL_ID}" \
            --query 'PrefixLists[0].[PrefixListName,State,Version,MaxEntries]' \
            --output text | awk '{printf "   이름 %s\n   상태 %s\n   버전 %s\n   최대 %s\n", $1, $2, $3, $4}'
        info ""
        print_entries "${PL_ID}"
        info ""
        info "이 리스트를 참조하는 보안 그룹 규칙:"
        aws_cli ec2 describe-security-groups \
            --filters "Name=tag:Project,Values=${PROJECT_NAME}" \
                      "Name=tag:Environment,Values=${ENVIRONMENT}" \
            --query "SecurityGroups[].IpPermissions[?PrefixListIds[?PrefixListId=='${PL_ID}']].[FromPort,ToPort]" \
            --output text | awk 'NF {printf "     tcp %s-%s\n", $1, $2}'
        info ""
        info "0.0.0.0/0 인바운드 규칙 검사:"
        # shellcheck disable=SC2016  # 백틱은 JMESPath 리터럴 문법이다.
        open_count="$(aws_cli ec2 describe-security-groups \
            --filters "Name=tag:Project,Values=${PROJECT_NAME}" \
                      "Name=tag:Environment,Values=${ENVIRONMENT}" \
            --query 'SecurityGroups[].IpPermissions[].IpRanges[?CidrIp==`0.0.0.0/0`]' \
            --output json | jq '[.[][]?] | length')"
        if [[ "${open_count}" == "0" ]]; then
            info "     없음 (정상)"
        else
            info "     ${open_count}개 발견 — 확인이 필요하다"
        fi
        ;;
esac
