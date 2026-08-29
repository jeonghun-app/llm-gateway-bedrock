#!/usr/bin/env bash
#
# 리포지토리에 시크릿이 섞여 들어갔는지 검사한다.
#
# 퍼블릭 리포지토리라 한 번 푸시된 값은 되돌릴 수 없다고 가정해야 한다.
# 커밋 전과 CI 에서 이 검사를 돌려 유출을 막는다.
#
# 검사 대상
#   1. 워킹트리의 추적 파일
#   2. 전체 커밋 히스토리 (--history)
#   3. 커밋 메시지
#   4. .gitignore 로 제외돼야 하는 파일이 추적되고 있는지
#
# 사용법
#   ./scripts/scan_secrets.sh              # 워킹트리와 커밋 메시지
#   ./scripts/scan_secrets.sh --history    # 전체 히스토리까지 (느리다)
set -uo pipefail

SCAN_HISTORY="no"
[[ "${1:-}" == "--history" ]] && SCAN_HISTORY="yes"

FAIL_COUNT=0

pass() { printf '  [통과] %s\n' "$*"; }
fail() { printf '  [실패] %s\n' "$*" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }

# 여러 줄 문자열을 들여쓰기해 stderr 로 출력한다.
indent() {
    local line
    while IFS= read -r line; do
        printf '        %s\n' "${line}" >&2
    done
}

# 검사 규칙: 이름|정규식
# 계정 ID 처럼 자리수만으로 판별되는 값은 오탐이 많아 문서용 플레이스홀더
# (123456789012)를 예외로 둔다.
RULES=(
  "AWS 액세스 키|\b(AKIA|ASIA)[0-9A-Z]{16}\b"
  "AWS 시크릿 키 할당|aws_secret_access_key[[:space:]]*=[[:space:]]*[A-Za-z0-9/+]{40}"
  "GitHub 토큰|\bgh[pousr]_[A-Za-z0-9]{30,}"
  "GitHub PAT(신형)|\bgithub_pat_[A-Za-z0-9_]{50,}"
  "Slack 토큰|\bxox[baprs]-[0-9A-Za-z-]{10,}"
  "OpenAI 키|\bsk-[A-Za-z0-9]{40,}"
  "게이트웨이 API 키(평문)|\bsk-llmgw-[a-z0-9]+-[A-Za-z0-9_-]{25,}"
  "Private key 블록|BEGIN[[:space:]]+(RSA|EC|OPENSSH|PGP|DSA)?[[:space:]]*PRIVATE KEY"
  "Bedrock API 키|\bABSK[A-Za-z0-9+/=]{20,}"
  "JWT 토큰|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."
)

echo "════════ 1. 워킹트리 추적 파일 ════════"
for rule in "${RULES[@]}"; do
    name="${rule%%|*}"
    pattern="${rule#*|}"
    hits="$(git grep -nIE "${pattern}" -- . 2>/dev/null || true)"
    if [[ -z "${hits}" ]]; then
        pass "${name}"
    else
        fail "${name} 발견:"
        echo "${hits}" | head -5 | indent
    fi
done

echo
echo "════════ 2. 실제 AWS 계정 ID 하드코딩 ════════"
# 12자리 숫자 중 문서용 플레이스홀더를 제외한 것만 문제로 본다.
account_hits="$(git grep -nIoE "\b[0-9]{12}\b" -- . 2>/dev/null \
    | grep -v ":123456789012$" || true)"
if [[ -z "${account_hits}" ]]; then
    pass "실제 계정 ID 없음 (플레이스홀더만 사용)"
else
    fail "계정 ID 형태의 숫자 발견:"
    echo "${account_hits}" | head -5 | indent
fi

echo
echo "════════ 3. 계정 ID 포함 ARN ════════"
arn_hits="$(git grep -nIE "arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]{12}:" -- . 2>/dev/null \
    | grep -v "123456789012" || true)"
if [[ -z "${arn_hits}" ]]; then
    pass "하드코딩된 ARN 없음"
else
    fail "계정 ID 를 포함한 ARN 발견:"
    echo "${arn_hits}" | head -5 | indent
fi

echo
echo "════════ 4. 실제 공인 IP 주소 ════════"
# 문서에 실제 인프라 IP 를 적으면 공격 표면을 노출한다. 예시 주소는
# RFC 5737 의 문서용 대역(192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24)을
# 쓴다. 사설 대역, 루프백, VPC CIDR 은 문제가 아니므로 제외한다.
#
# 링크로컬(169.254.0.0/16)도 제외한다. IMDS(169.254.169.254)와 Fargate 태스크
# 자격증명(169.254.170.2)은 SSRF 방어 대상으로 코드와 문서에 명시해야 하는
# 주소이고, 공개해도 공격 표면이 되지 않는 공표된 고정 주소다.
ip_hits="$(git grep -nIoE "\b([0-9]{1,3}\.){3}[0-9]{1,3}\b" -- . 2>/dev/null \
    | grep -vE ":(10|127)\." \
    | grep -vE ":192\.168\." \
    | grep -vE ":172\.(1[6-9]|2[0-9]|3[01])\." \
    | grep -vE ":(192\.0\.2|198\.51\.100|203\.0\.113)\." \
    | grep -vE ":169\.254\." \
    | grep -vE ":(0\.0\.0\.0|255\.255\.255\.255)$" \
    || true)"
if [[ -z "${ip_hits}" ]]; then
    pass "실제 공인 IP 없음 (문서용 대역만 사용)"
else
    fail "문서용 대역이 아닌 IP 발견 (RFC 5737 주소로 바꾼다):"
    echo "${ip_hits}" | head -5 | indent
fi

echo
echo "════════ 5. 추적돼서는 안 되는 파일 ════════"
FORBIDDEN_PATHS=(".deploy" ".env" "credentials" "id_rsa" "id_ed25519")
for path in "${FORBIDDEN_PATHS[@]}"; do
    tracked="$(git ls-files | grep -iE "(^|/)${path}" || true)"
    if [[ -z "${tracked}" ]]; then
        pass "${path} 추적되지 않음"
    else
        fail "${path} 가 추적되고 있다:"
        echo "${tracked}" | indent
    fi
done

tracked_keys="$(git ls-files | grep -iE "\.(pem|key|p12|pfx|jks|keystore)$" || true)"
if [[ -z "${tracked_keys}" ]]; then
    pass "키 파일 확장자 추적되지 않음"
else
    fail "키 파일이 추적되고 있다:"
    echo "${tracked_keys}" | indent
fi

echo
echo "════════ 6. 커밋 메시지 ════════"
msg_hits=""
for rule in "${RULES[@]}"; do
    pattern="${rule#*|}"
    found="$(git log --all --format='%H %B' 2>/dev/null \
        | grep -inE "${pattern}" || true)"
    [[ -n "${found}" ]] && msg_hits="${msg_hits}${found}"$'\n'
done
if [[ -z "${msg_hits//[[:space:]]/}" ]]; then
    pass "커밋 메시지에 시크릿 없음"
else
    fail "커밋 메시지에서 발견:"
    echo "${msg_hits}" | head -5 | indent
fi

if [[ "${SCAN_HISTORY}" == "yes" ]]; then
    echo
    echo "════════ 7. 전체 커밋 히스토리 ════════"
    revs="$(git rev-list --all 2>/dev/null)"
    if [[ -z "${revs}" ]]; then
        echo "  커밋이 없다."
    else
        for rule in "${RULES[@]}"; do
            name="${rule%%|*}"
            pattern="${rule#*|}"
            # shellcheck disable=SC2086  # revs 는 공백 구분 리비전 목록이다.
            hits="$(git grep -IE "${pattern}" ${revs} -- . 2>/dev/null | head -3 || true)"
            if [[ -z "${hits}" ]]; then
                pass "${name} (히스토리)"
            else
                fail "${name} 히스토리에서 발견:"
                echo "${hits}" | indent
            fi
        done
    fi
fi

echo
echo "────────────────────────────────────────────"
if [[ "${FAIL_COUNT}" -eq 0 ]]; then
    echo " 시크릿 검사 통과"
else
    echo " 실패 ${FAIL_COUNT}건 — 커밋하지 말고 먼저 제거한다."
    echo
    echo " 이미 커밋했다면 히스토리에서 지워야 한다. 푸시까지 됐다면"
    echo " 해당 자격증명을 즉시 폐기하고 재발급한다. 히스토리 정리만으로는"
    echo " 유출된 값이 안전해지지 않는다."
fi
echo "────────────────────────────────────────────"
[[ "${FAIL_COUNT}" -eq 0 ]]
