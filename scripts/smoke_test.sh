#!/usr/bin/env bash
#
# 배포된 게이트웨이에 대한 종단간 스모크 테스트.
#
# 유닛 테스트가 검증하지 못하는 것을 확인한다. 실제 ALB 경유, 실제 IAM 역할,
# 실제 Bedrock 호출, 실제 DynamoDB 집계다.
#
# 사용법
#   LLMGW_BASE_URL=http://alb-dns LLMGW_ADMIN_TOKEN=... ./scripts/smoke_test.sh
#
# 데모 키 파일(LLMGW_DEMO_KEY_FILE)이 있으면 그 키로 채팅 호출까지 검증한다.
# 없으면 관리 API로 임시 계정과 키를 만들어 검증한 뒤 키를 삭제한다.
set -uo pipefail

BASE_URL="${LLMGW_BASE_URL:-}"
ADMIN_TOKEN="${LLMGW_ADMIN_TOKEN:-}"
DEMO_KEY_FILE="${LLMGW_DEMO_KEY_FILE:-}"

if [[ -z "${BASE_URL}" || -z "${ADMIN_TOKEN}" ]]; then
    echo "LLMGW_BASE_URL 과 LLMGW_ADMIN_TOKEN 이 필요하다." >&2
    exit 2
fi
command -v jq >/dev/null 2>&1 || { echo "jq 가 필요하다." >&2; exit 2; }

BASE_URL="${BASE_URL%/}"
PASS_COUNT=0
FAIL_COUNT=0

pass() { printf '  [통과] %s\n' "$*"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { printf '  [실패] %s\n' "$*" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }

# HTTP 상태 코드만 확인하는 검사.
check_status() {
    local label="$1" expected="$2" actual
    shift 2
    actual="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$@")"
    if [[ "${actual}" == "${expected}" ]]; then
        pass "${label} (${actual})"
    else
        fail "${label}: 기대 ${expected}, 실제 ${actual}"
    fi
}

echo "대상 ${BASE_URL}"
echo
echo "1. 기본 엔드포인트"
check_status "healthz" 200 "${BASE_URL}/healthz"
check_status "readyz" 200 "${BASE_URL}/readyz"
check_status "대시보드" 200 "${BASE_URL}/ui/"
check_status "OpenAPI 스펙" 200 "${BASE_URL}/openapi.json"

echo
echo "2. 네트워크 접근 통제"
# 템플릿에서 0.0.0.0/0 을 문법으로 막던 보장이 프리픽스 리스트 방식으로
# 옮겨졌다. 매 배포마다 전체 개방 규칙이 없는지 여기서 확인해 보장을 유지한다.
if [[ -n "${LLMGW_STACK_TAG_PROJECT:-}" || -n "${LLMGW_REGION:-}" ]] \
   || command -v aws >/dev/null 2>&1; then
    region="${LLMGW_REGION:-${AWS_REGION:-us-east-1}}"
    project="${LLMGW_STACK_TAG_PROJECT:-llmgw}"
    environment="${LLMGW_STACK_TAG_ENV:-dev}"
    # shellcheck disable=SC2016  # 백틱은 JMESPath 리터럴 문법이다.
    open_rules="$(aws ec2 describe-security-groups --region "${region}" \
        --filters "Name=tag:Project,Values=${project}" \
                  "Name=tag:Environment,Values=${environment}" \
        --query 'SecurityGroups[].IpPermissions[].IpRanges[?CidrIp==`0.0.0.0/0`]' \
        --output json 2>/dev/null | jq '[.[][]?] | length' 2>/dev/null || echo "skip")"
    if [[ "${open_rules}" == "0" ]]; then
        pass "인바운드에 0.0.0.0/0 규칙 없음"
    elif [[ "${open_rules}" == "skip" ]]; then
        echo "  [건너뜀] 보안 그룹 조회 권한이 없어 확인하지 못했다"
    else
        fail "인바운드에 0.0.0.0/0 규칙이 ${open_rules}개 있다"
    fi
fi

echo
echo "3. 인증 경계"
check_status "관리 API 무인증 차단" 401 "${BASE_URL}/admin/accounts"
check_status "관리 API 잘못된 토큰 차단" 401 \
    -H "X-Admin-Token: definitely-wrong" "${BASE_URL}/admin/accounts"
check_status "관리 API 정상 토큰" 200 \
    -H "X-Admin-Token: ${ADMIN_TOKEN}" "${BASE_URL}/admin/accounts"
check_status "채팅 API 무인증 차단" 401 \
    -X POST -H 'Content-Type: application/json' \
    -d '{"model":"amazon.nova-micro-v1:0","messages":[{"role":"user","content":"hi"}]}' \
    "${BASE_URL}/v1/chat/completions"

echo
echo "4. Bedrock 모델 조회"
models_json="$(curl -s --max-time 30 -H "X-Admin-Token: ${ADMIN_TOKEN}" \
    "${BASE_URL}/admin/models")"
model_count="$(echo "${models_json}" | jq -r '.data | length' 2>/dev/null || echo 0)"
if [[ "${model_count}" -gt 0 ]]; then
    pass "모델 ${model_count}개 조회"
else
    fail "모델 목록이 비어 있다. Bedrock 모델 액세스와 태스크 역할 권한을 확인한다."
fi

# 검증에 사용할 API 키를 확보한다.
API_KEY=""
TEMP_ACCOUNT=""
TEMP_KEY_ID=""
if [[ -n "${DEMO_KEY_FILE}" && -f "${DEMO_KEY_FILE}" ]]; then
    API_KEY="$(jq -r '.[0].api_key // empty' "${DEMO_KEY_FILE}" 2>/dev/null)"
    ACCOUNT_ID="$(jq -r '.[0].account_id // empty' "${DEMO_KEY_FILE}" 2>/dev/null)"
fi
if [[ -z "${API_KEY}" ]]; then
    echo
    echo "   데모 키가 없어 임시 계정과 키를 만든다."
    TEMP_ACCOUNT="smoke-$(date -u +%s)"
    ACCOUNT_ID="${TEMP_ACCOUNT}"
    curl -s -o /dev/null -X POST -H "X-Admin-Token: ${ADMIN_TOKEN}" \
        -H 'Content-Type: application/json' \
        -d "{\"account_id\":\"${TEMP_ACCOUNT}\",\"name\":\"Smoke Test\"}" \
        "${BASE_URL}/admin/accounts"
    curl -s -o /dev/null -X POST -H "X-Admin-Token: ${ADMIN_TOKEN}" \
        -H 'Content-Type: application/json' \
        -d '{"user_id":"smoke","name":"Smoke User"}' \
        "${BASE_URL}/admin/accounts/${TEMP_ACCOUNT}/users"
    key_json="$(curl -s -X POST -H "X-Admin-Token: ${ADMIN_TOKEN}" \
        -H 'Content-Type: application/json' \
        -d '{"user_id":"smoke","name":"smoke key"}' \
        "${BASE_URL}/admin/accounts/${TEMP_ACCOUNT}/keys")"
    API_KEY="$(echo "${key_json}" | jq -r '.api_key // empty')"
    TEMP_KEY_ID="$(echo "${key_json}" | jq -r '.key_id // empty')"
fi

if [[ -z "${API_KEY}" ]]; then
    fail "검증용 API 키를 확보하지 못했다. 이후 검사를 건너뛴다."
else
    # 실제로 호출 가능한 모델을 목록에서 고른다. 하드코딩하면 리전이나
    # 계정 활성화 상태에 따라 스모크 테스트가 잘못 실패한다.
    TEST_MODEL="$(echo "${models_json}" | jq -r '
        [.data[].model_id]
        | (map(select(test("nova-micro"))) + map(select(test("nova-lite"))) + .)
        | .[0] // empty')"

    echo
    echo "5. OpenAI 호환 API (모델 ${TEST_MODEL})"
    check_status "v1/models" 200 \
        -H "Authorization: Bearer ${API_KEY}" "${BASE_URL}/v1/models"

    chat_json="$(curl -s --max-time 90 -X POST \
        -H "Authorization: Bearer ${API_KEY}" \
        -H 'Content-Type: application/json' \
        -H "X-Request-Id: smoke-nonstream-$(date -u +%s)" \
        -d "{\"model\":\"${TEST_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: OK\"}],\"max_tokens\":16,\"temperature\":0}" \
        "${BASE_URL}/v1/chat/completions")"
    chat_content="$(echo "${chat_json}" | jq -r '.choices[0].message.content // empty')"
    chat_tokens="$(echo "${chat_json}" | jq -r '.usage.total_tokens // 0')"
    if [[ -n "${chat_content}" && "${chat_tokens}" -gt 0 ]]; then
        pass "비스트리밍 채팅 (토큰 ${chat_tokens}, 응답 '${chat_content:0:24}')"
    else
        fail "비스트리밍 채팅 실패: $(echo "${chat_json}" | head -c 300)"
    fi

    stream_body="$(curl -s --max-time 90 -N -X POST \
        -H "Authorization: Bearer ${API_KEY}" \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"${TEST_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Count 1 to 3.\"}],\"max_tokens\":48,\"temperature\":0,\"stream\":true}" \
        "${BASE_URL}/v1/chat/completions")"
    if echo "${stream_body}" | grep -q 'chat.completion.chunk' \
        && echo "${stream_body}" | grep -q 'data: \[DONE\]'; then
        chunk_count="$(echo "${stream_body}" | grep -c '^data: {')"
        pass "스트리밍 채팅 (청크 ${chunk_count}개, DONE 수신)"
    else
        fail "스트리밍 채팅 실패: $(echo "${stream_body}" | head -c 300)"
    fi

    echo
    echo "6. 정책 적용"
    check_status "허용되지 않은 역할로 시작하면 400" 400 \
        -X POST -H "Authorization: Bearer ${API_KEY}" \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"${TEST_MODEL}\",\"messages\":[{\"role\":\"assistant\",\"content\":\"hi\"}]}" \
        "${BASE_URL}/v1/chat/completions"
    check_status "잘못된 API 키 차단" 401 \
        -X POST -H "Authorization: Bearer sk-llmgw-dev-invalid" \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"${TEST_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}" \
        "${BASE_URL}/v1/chat/completions"

    echo
    echo "7. 멱등성 (같은 X-Request-Id 2회)"
    idem_id="smoke-idem-$(date -u +%s)"
    for _ in 1 2; do
        curl -s -o /dev/null --max-time 90 -X POST \
            -H "Authorization: Bearer ${API_KEY}" \
            -H 'Content-Type: application/json' \
            -H "X-Request-Id: ${idem_id}" \
            -d "{\"model\":\"${TEST_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: OK\"}],\"max_tokens\":16,\"temperature\":0}" \
            "${BASE_URL}/v1/chat/completions"
    done
    today="$(date -u +%F)"
    idem_count="$(curl -s --max-time 30 -H "X-Admin-Token: ${ADMIN_TOKEN}" \
        "${BASE_URL}/analytics/requests?account_id=${ACCOUNT_ID}&date=${today}&limit=200" \
        | jq -r --arg rid "${idem_id}" \
            '[.data[] | select(.request_id == $rid)] | length')"
    if [[ "${idem_count}" == "1" ]]; then
        pass "중복 요청이 1건으로 기록됨"
    else
        fail "멱등성 위반: 같은 request_id 가 ${idem_count}건 기록됐다"
    fi

    echo
    echo "8. 집계와 대시보드 데이터"
    dash="$(curl -s --max-time 60 -H "X-Admin-Token: ${ADMIN_TOKEN}" \
        "${BASE_URL}/analytics/dashboard?account_id=${ACCOUNT_ID}")"
    requests_total="$(echo "${dash}" | jq -r '.totals.requests // 0')"
    if [[ "${requests_total}" -gt 0 ]]; then
        pass "계정 집계 요청 ${requests_total}건"
    else
        fail "계정 집계가 비어 있다. 사용량 기록 경로를 확인한다."
    fi

    for axis in team user model key; do
        axis_count="$(echo "${dash}" | jq -r --arg a "${axis}" \
            '.breakdowns[$a] | length')"
        if [[ "${axis_count}" -gt 0 ]]; then
            pass "${axis} 축 ${axis_count}개 항목"
        else
            fail "${axis} 축이 비어 있다"
        fi
    done

    cost="$(echo "${dash}" | jq -r '.totals.cost_usd // 0')"
    if awk -v c="${cost}" 'BEGIN { exit !(c > 0) }'; then
        pass "비용 계산됨 (${cost} USD)"
    else
        fail "비용이 0이다. 단가 표에 테스트 모델이 없을 수 있다 (모델: ${TEST_MODEL})"
    fi

    tokens="$(echo "${dash}" | jq -r '.totals.total_tokens // 0')"
    if [[ "${tokens}" -gt 0 ]]; then
        pass "토큰 집계됨 (${tokens})"
    else
        fail "토큰 집계가 0이다"
    fi

    accounts_view="$(curl -s --max-time 60 -H "X-Admin-Token: ${ADMIN_TOKEN}" \
        "${BASE_URL}/analytics/accounts")"
    accounts_count="$(echo "${accounts_view}" | jq -r '.data | length')"
    if [[ "${accounts_count}" -gt 0 ]]; then
        pass "계정 목록 뷰 ${accounts_count}개"
    else
        fail "계정 목록 뷰가 비어 있다"
    fi
fi

# 임시로 만든 리소스를 정리한다. 계정은 남지만 키는 지워 유출 경로를 줄인다.
if [[ -n "${TEMP_KEY_ID}" && -n "${TEMP_ACCOUNT}" ]]; then
    curl -s -o /dev/null -X DELETE -H "X-Admin-Token: ${ADMIN_TOKEN}" \
        "${BASE_URL}/admin/accounts/${TEMP_ACCOUNT}/keys/${TEMP_KEY_ID}"
    echo
    echo "   임시 스모크 키를 삭제했다. 임시 계정 ${TEMP_ACCOUNT} 는 집계 이력 때문에 남긴다."
fi

echo
echo "────────────────────────────────────────────"
printf ' 통과 %d · 실패 %d\n' "${PASS_COUNT}" "${FAIL_COUNT}"
echo "────────────────────────────────────────────"
[[ "${FAIL_COUNT}" -eq 0 ]]
