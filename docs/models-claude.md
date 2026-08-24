# Claude 모델 연동

이 게이트웨이로 Anthropic Claude 모델을 호출할 때 알아야 할 것을 정리한다.
Nova 나 다른 공급자 모델과 다르게 동작하는 지점이 몇 군데 있고, 그중 하나는
모르고 쓰면 "모델을 찾을 수 없다"는 오류로 바로 부딪힌다.

---

## 1. 가장 먼저 알아야 할 것: 현행 Claude 는 기반 모델 ID 로 호출되지 않는다

`us-east-1` 에서 조회한 실제 상태다.

```
$ aws bedrock list-foundation-models --by-provider anthropic \
    --query 'modelSummaries[].[modelId,inferenceTypesSupported[0],modelLifecycle.status]'

anthropic.claude-3-haiku-20240307-v1:0       ON_DEMAND          LEGACY
anthropic.claude-haiku-4-5-20251001-v1:0     INFERENCE_PROFILE  ACTIVE
anthropic.claude-sonnet-4-5-20250929-v1:0    INFERENCE_PROFILE  ACTIVE
anthropic.claude-opus-4-5-20251101-v1:0      INFERENCE_PROFILE  ACTIVE
anthropic.claude-sonnet-5                    INFERENCE_PROFILE  ACTIVE
anthropic.claude-opus-5                      INFERENCE_PROFILE  ACTIVE
...
```

현재 활성 상태인 Claude 모델은 **전부 `INFERENCE_PROFILE`** 이다. `ON_DEMAND`
인 것은 `claude-3-haiku` 하나뿐이고 그마저 `LEGACY` 다.

이것이 뜻하는 바는 명확하다. **기반 모델 ID 를 그대로 넘기면 실패한다.**

```bash
# 실패한다
-d '{"model":"anthropic.claude-sonnet-4-5-20250929-v1:0", ...}'
# → 404 model_not_found

# 성공한다 (추론 프로파일 ID)
-d '{"model":"us.anthropic.claude-sonnet-4-5-20250929-v1:0", ...}'
```

게이트웨이의 `GET /v1/models` 는 호출 가능한 ID 만 노출하므로, 어떤 ID 를
써야 하는지 확인하는 가장 빠른 방법은 그 목록을 보는 것이다.

```bash
curl -s -H "Authorization: Bearer $API_KEY" "$GATEWAY_URL/v1/models" \
  | jq -r '.data[].id' | grep anthropic
```

---

## 2. `us.` 와 `global.` 중 무엇을 쓸 것인가

같은 모델에 두 종류의 프로파일이 존재한다.

| 프로파일 | 예시 | 라우팅 범위 |
|---|---|---|
| 지역(geographic) | `us.anthropic.claude-sonnet-5` | 미국 리전들 사이에서 라우팅 |
| 글로벌(global) | `global.anthropic.claude-sonnet-5` | 전 세계 리전 사이에서 라우팅 |

선택 기준:

- **데이터 처리 위치에 제약이 있으면 `us.` 를 쓴다.** `global.` 은 요청이 어느
  리전에서 처리될지 넓게 열려 있다.
- **처리량이 우선이고 위치 제약이 없으면 `global.` 이 유리하다.** 가용 용량 풀이
  넓어 스로틀링을 덜 만난다.
- 둘의 **토큰 단가는 같다.**

게이트웨이는 어느 쪽이든 그대로 전달한다. 조직 정책을 API 키의 허용 모델
목록으로 강제하는 것을 권장한다.

```bash
# 미국 리전 라우팅만 허용하는 키
curl -X POST "$GATEWAY_URL/admin/accounts/acme/keys" \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "user_id": "alice",
    "name": "US 라우팅 전용",
    "allowed_models": [
      "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
      "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    ]
  }'
```

### 주의: 허용 목록 정규화가 프로파일 접두어를 무시한다

게이트웨이는 허용 목록을 비교할 때 `us.` / `global.` / `eu.` / `apac.` 접두어를
제거하고 대조한다. 접두어까지 손으로 관리하는 부담을 없애려는 설계지만,
**결과적으로 `us.` 만 등록해도 `global.` 호출이 통과한다.**

```
허용 목록: ["us.anthropic.claude-sonnet-5"]
  us.anthropic.claude-sonnet-5      → 통과
  global.anthropic.claude-sonnet-5  → 통과  (접두어가 무시되므로)
  us.anthropic.claude-opus-5        → 거부
```

라우팅 범위를 기술적으로 강제해야 한다면 허용 목록만으로는 부족하다. 태스크
역할의 `AllowedBedrockModelArn` 파라미터를 좁혀 IAM 수준에서 막는다.

```yaml
# global 프로파일을 IAM 에서 차단하는 예
AllowedBedrockModelArn: "arn:aws:bedrock:us-*::foundation-model/*"
```

관련 구현은 `src/llmgw/pricing.py` 의 `normalize_model_id` 와
`src/llmgw/auth.py` 의 `enforce_model` 에 있다.

---

## 3. 모델 액세스 활성화

Bedrock 은 계정별로 모델 사용 승인이 필요하다. 활성화하지 않으면 **배포는
성공하고 게이트웨이도 정상 기동하지만 모든 Claude 호출이 403 으로 실패**한다.

```
AWS 콘솔 → Amazon Bedrock → Model access → Modify model access
→ Anthropic 항목 선택 → Submit
```

게이트웨이가 반환하는 오류는 이렇게 보인다.

```json
{"error":{"message":"모델에 접근할 수 없다. Bedrock 모델 액세스를 확인한다: us.anthropic.claude-sonnet-5","type":"invalid_request_error","code":"model_not_allowed"}}
```

배포 스크립트가 시작 시 모델 목록을 조회해 경고하지만, 목록 조회 권한과 모델
호출 승인은 별개다. 목록이 보여도 호출이 거부될 수 있다.

---

## 4. LEGACY 와 EOL 처리

모델에는 수명 주기가 있고 게이트웨이는 이를 숨기지 않는다.

| 상태 | 의미 | 게이트웨이 동작 |
|---|---|---|
| `ACTIVE` | 정상 | 호출 성공 |
| `LEGACY` | 지원 종료 예정 | 호출은 성공. 이전 계획을 세워야 한다 |
| EOL(목록에서 제거) | 종료됨 | `404 model_not_found` |

실제로 만난 사례다.

```
$ aws bedrock-runtime converse --model-id anthropic.claude-3-5-sonnet-20241022-v2:0 ...
ResourceNotFoundException: This model version has reached the end of its life.
```

게이트웨이는 이것을 `404 model_not_found` 로 변환한다. 클라이언트가 하드코딩한
모델 ID 가 어느 날 갑자기 실패하는 상황이므로, **API 키의 허용 목록을 주기적으로
점검**하는 것이 필요하다.

```bash
# 허용 목록에 있지만 더 이상 호출할 수 없는 모델 찾기
AVAILABLE="$(curl -s -H "X-Admin-Token: $ADMIN_TOKEN" "$GATEWAY_URL/admin/models" \
  | jq -r '.data[].model_id' | sort)"
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" "$GATEWAY_URL/admin/accounts/acme/keys" \
  | jq -r '.data[] | .key_id + " " + (.allowed_models | join(","))' \
  | while read -r kid models; do
      for m in ${models//,/ }; do
        echo "$AVAILABLE" | grep -qx "$m" || echo "  $kid → 사용 불가: $m"
      done
    done
```

---

## 5. 비용 집계와 단가 표의 현재 한계

**이 절이 Claude 사용 시 가장 주의할 부분이다.**

게이트웨이는 Bedrock 이 비용을 돌려주지 않으므로 토큰 수 × 자체 단가 표로
비용을 계산한다. 단가 표는 `src/llmgw/pricing.json` 이다.

`scripts/sync_pricing.py` 로 AWS Price List API 와 대조한 결과, 이 계정의
Price List 데이터에는 **레거시 Claude 5종만** 존재한다.

```
$ ./.venv/bin/python scripts/sync_pricing.py --region us-east-1
── 불일치: 0건
── 누락(API 확인됨): 17건        ← Claude 아님 (Qwen, DeepSeek, GLM 등)
── 미확인·누락: 78건             ← 여기에 현행 Claude 전부가 포함된다
```

따라서 **Claude Sonnet 4.5/4.6/5, Opus 4.5~5, Haiku 4.5, Fable 5 의 비용이
0으로 집계된다.** 단가를 추측해 넣지 않은 것은 의도적이다. 틀린 단가는 조용히
잘못된 청구 근거를 만들어, 값이 없어서 드러나는 상태보다 위험하다.

### 이 갭이 어떻게 드러나는가

세 곳에서 보인다.

1. **usage 레코드**에 `pricing_known: false` 가 남는다.
2. **집계에 `unpriced_requests` 카운터**가 쌓인다.
3. **대시보드 총비용 카드**에 경고가 표시된다.
   `USD — 단가 미등록 14건 제외됨`

```bash
# 단가 미등록 요청이 얼마나 되는지
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \
  "$GATEWAY_URL/analytics/summary?account_id=acme" \
  | jq '{cost_usd: .totals.cost_usd,
         unpriced: .totals.unpriced_requests,
         complete: .totals.cost_complete}'

# 단가를 모르는 모델 목록
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" "$GATEWAY_URL/admin/models" \
  | jq -r '.data[] | select(.pricing_known == false) | .model_id' | grep anthropic
```

### 갭을 메우는 방법

[Amazon Bedrock 요금 페이지](https://aws.amazon.com/bedrock/pricing/)에서 값을
확인해 직접 추가한다. 단위는 **1,000 토큰당 USD** 이고, 요금 페이지는 보통
100만 토큰 기준이므로 **1,000으로 나눠야 한다.**

```jsonc
// src/llmgw/pricing.json
"anthropic.claude-sonnet-5": {
  // 요금 페이지가 $3 / 1M tokens 이면 → 0.003 / 1K tokens
  "input_per_1k_usd": "0.003",
  "output_per_1k_usd": "0.015"
}
```

키는 **정규화된 기반 모델 ID** 로 쓴다. `us.` / `global.` 접두어를 붙이지
않는다. 게이트웨이가 조회 시 접두어를 제거하므로 한 항목이 모든 프로파일에
적용된다.

추가 후 확인:

```bash
./.venv/bin/python -m pytest tests/test_pricing.py
./.venv/bin/python scripts/sync_pricing.py --region us-east-1   # 불일치 재확인
```

이미 기록된 과거 비용은 소급 계산되지 않는다. 단가 표는 기록 시점에 적용된다.

---

## 6. 요청 변환에서 Claude 관련 주의점

게이트웨이는 OpenAI 형식을 Bedrock Converse API 로 변환한다. Claude 는
Converse 규칙이 엄격해 아래 처리가 필요했고, 모두 게이트웨이가 자동으로 한다.

| OpenAI 입력 | Claude/Converse 요구사항 | 게이트웨이 처리 |
|---|---|---|
| `system` 역할 메시지 | `messages` 가 아닌 별도 `system` 파라미터 | 분리해서 전달 |
| `developer` 역할 | 지원하지 않음 | `system` 으로 처리 |
| 같은 역할 연속 메시지 | user/assistant 교대 필수 | 개행으로 병합 |
| `assistant` 로 시작하는 대화 | user 로 시작해야 함 | `400 invalid_request` |
| 빈 문자열 콘텐츠 | 빈 콘텐츠 블록 거부 | 해당 메시지 제외 |
| `max_tokens` | `inferenceConfig.maxTokens` | 매핑 |
| `stop` (문자열/배열) | `stopSequences` (배열) | 배열로 정규화 |
| `n > 1` | Converse 는 후보 1개만 | `400` 으로 명시적 거부 |

`stopReason` 은 OpenAI `finish_reason` 으로 변환된다.

```
end_turn, stop_sequence → stop
max_tokens             → length
tool_use               → tool_calls
content_filtered       → content_filter
guardrail_intervened   → content_filter
```

구현은 `src/llmgw/translate.py` 에 있고 `tests/test_translate.py` 가 각 규칙을
검증한다.

### 지원하지 않는 것

게이트웨이가 v1.1 에서 중계하지 **않는** Claude 기능이다.

| 기능 | 상태 | 우회 |
|---|---|---|
| 이미지 입력(비전) | 텍스트 블록만 전달. 이미지 블록은 무시된다 | Bedrock 을 직접 호출 |
| 도구 사용(tool use) | 요청의 `tools` 를 전달하지 않는다 | Bedrock 직접 호출 |
| 확장 사고(extended thinking) | 전용 파라미터를 전달하지 않는다 | Bedrock 직접 호출 |
| 프롬프트 캐싱 | 캐시 포인트를 지정하지 않는다 | Bedrock 직접 호출 |
| Guardrails | 연동하지 않는다 | 아래 참고 |
| 구조화 출력 | 전달하지 않는다 | Bedrock 직접 호출 |

이 기능들이 필요하면 Bedrock 네이티브 API 나 AWS 가 제공하는 OpenAI 호환
엔드포인트를 직접 쓰는 편이 낫다. 판단 기준은
[`bedrock-endpoints.md`](bedrock-endpoints.md) 에 정리했다.

---

## 7. 실측 검증 결과

`us-east-1` 에서 게이트웨이를 통해 확인한 내용이다.

| 항목 | 결과 |
|---|---|
| `us.anthropic.claude-3-haiku-20240307-v1:0` 비스트리밍 | 성공 |
| 기반 모델 ID 직접 호출 (`anthropic.claude-sonnet-...`) | `404 model_not_found` |
| EOL 모델 (`claude-3-5-sonnet-20241022-v2:0`) | `404 model_not_found` |
| 시스템 메시지 분리 전달 | 정상 (`system` 파라미터로 전달됨) |
| 허용 목록에 기반 모델 등록 → `us.` 호출 | 통과 (정규화 동작 확인) |
| `claude-3-haiku` 비용 집계 | 정상 (단가 표에 있음) |
| 현행 Claude 비용 집계 | 0 + `pricing_known=false` (단가 미등록) |

---

## 8. 권장 설정

조직에서 Claude 를 쓸 때의 기본 형태다.

```bash
# 1. 팀별로 등급을 나눈 키 발급
#    일상 작업은 Haiku, 어려운 작업만 Opus 로 제한
curl -X POST "$GATEWAY_URL/admin/accounts/acme/keys" \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "user_id": "alice",
    "name": "일상 작업",
    "allowed_models": ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
    "monthly_budget_usd": 50
  }'

# 2. 고가 모델은 별도 키 + 낮은 예산으로 분리
curl -X POST "$GATEWAY_URL/admin/accounts/acme/keys" \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "user_id": "alice",
    "name": "Opus 제한 사용",
    "allowed_models": ["us.anthropic.claude-opus-4-5-20251101-v1:0"],
    "monthly_budget_usd": 20
  }'
```

**단가가 등록되지 않은 모델에 예산을 걸면 예산이 작동하지 않는다.** 비용이 0으로
누적되어 한도에 도달하지 않는다. 고가 모델에 예산을 걸기 전에 단가 표를 먼저
채워야 한다. 이것이 5절의 갭을 메워야 하는 실질적인 이유다.
