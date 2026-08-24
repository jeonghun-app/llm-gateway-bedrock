# 운영 런북

명령의 `llmgw-dev` 와 `us-east-1` 은 실제 프로젝트명·환경·리전으로 바꿔 쓴다.

```bash
# 이 문서의 명령에서 공통으로 쓰는 값
export REGION=us-east-1
export STACK=llmgw-dev-app
export LOG_GROUP=/ecs/llmgw-dev
export CLUSTER=llmgw-dev
export SERVICE=llmgw-dev

# 게이트웨이 URL 과 관리 토큰
export GATEWAY_URL="$(aws cloudformation describe-stacks --region $REGION \
  --stack-name $STACK --query "Stacks[0].Outputs[?OutputKey=='GatewayUrl'].OutputValue" \
  --output text)"
export ADMIN_TOKEN="$(aws secretsmanager get-secret-value --region $REGION \
  --secret-id llmgw/dev/admin-token --query SecretString --output text | jq -r .admin_token)"
```

---

## 1. 배포

### 정상 배포

```bash
./scripts/deploy.sh --allowed-cidr "$(curl -s https://checkip.amazonaws.com)/32"
```

여러 번 실행해도 안전하다. 변경이 없으면 CloudFormation 이 빈 변경 집합으로
끝내고, 같은 이미지 태그가 있으면 빌드와 푸시를 건너뛴다.

### 코드만 바꿔 재배포

`deploy.sh` 를 그대로 다시 실행한다. 커밋 SHA 가 바뀌면 새 태그로 푸시되고 ECS
롤링 업데이트가 시작된다. 작업 트리에 커밋되지 않은 변경이 있으면 태그에
타임스탬프가 붙어 유일성이 보장된다.

### 배포 진행 상황 확인

```bash
# 스택 이벤트
aws cloudformation describe-stack-events --region $REGION --stack-name $STACK \
  --query 'StackEvents[0:15].[Timestamp,LogicalResourceId,ResourceStatus]' --output table

# 서비스 배포 상태
aws ecs describe-services --region $REGION --cluster $CLUSTER --services $SERVICE \
  --query 'services[0].deployments[*].[status,rolloutState,desiredCount,runningCount]' \
  --output table

# 태스크 중단 이유 (기동 실패 진단에 가장 유용)
aws ecs describe-tasks --region $REGION --cluster $CLUSTER \
  --tasks $(aws ecs list-tasks --region $REGION --cluster $CLUSTER \
    --desired-status STOPPED --query 'taskArns[0:3]' --output text) \
  --query 'tasks[*].[stoppedReason,containers[0].reason]' --output table
```

---

## 2. 롤백

배포 서킷 브레이커가 켜져 있어 새 태스크가 반복 실패하면 ECS 가 **자동으로 이전
태스크 정의로 되돌린다.** 수동 개입 없이 끝나는 경우가 대부분이다.

### 수동 롤백 (자동 롤백이 동작하지 않았을 때)

```bash
# 1. 이전 태스크 정의 리비전 확인
aws ecs list-task-definitions --region $REGION --family-prefix llmgw-dev \
  --sort DESC --max-items 5 --query 'taskDefinitionArns' --output table

# 2. 그 리비전으로 서비스 강제 전환
aws ecs update-service --region $REGION --cluster $CLUSTER --service $SERVICE \
  --task-definition llmgw-dev:<이전리비전> --force-new-deployment

# 3. 안정화 대기
aws ecs wait services-stable --region $REGION --cluster $CLUSTER --services $SERVICE
```

### 이미지 태그로 롤백 (권장)

CloudFormation 상태와 실제 배포를 일치시키려면 이미지 URI 를 명시해 스택을 다시
배포한다.

```bash
# 사용 가능한 이미지 태그 목록 (최신순)
aws ecr describe-images --region $REGION --repository-name llmgw-dev \
  --query 'sort_by(imageDetails,&imagePushedAt)[-10:].[imageTags[0],imagePushedAt]' \
  --output table

# 특정 태그로 스택 재배포
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
aws cloudformation deploy --region $REGION --stack-name $STACK \
  --template-file infra/app.yaml --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "ImageUri=${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/llmgw-dev:<되돌릴태그>" \
    "AllowedIngressCidr1=<현재CIDR>" \
    "EcrRepositoryArn=arn:aws:ecr:${REGION}:${ACCOUNT_ID}:repository/llmgw-dev"
```

`deploy.sh` 로 배포했던 나머지 파라미터는 CloudFormation 이 이전 값을 유지한다.
명시하지 않은 파라미터는 기본값이 아니라 **이전 값**이 쓰인다.

### 스택이 `ROLLBACK_COMPLETE` 인 경우

이 상태에서는 업데이트할 수 없다. 삭제 후 재생성해야 한다.

```bash
./scripts/teardown.sh --env dev --region $REGION
./scripts/deploy.sh --allowed-cidr <CIDR>
```

`deploy.sh` 가 이 상태를 감지하면 위 절차를 안내하며 중단한다.

---

## 3. 접근 CIDR 변경

IP 가 바뀌어 접속이 안 될 때 가장 흔한 상황이다.

```bash
# 현재 내 IP
curl -s https://checkip.amazonaws.com

# 현재 허용된 CIDR 확인
aws cloudformation describe-stacks --region $REGION --stack-name $STACK \
  --query "Stacks[0].Parameters[?starts_with(ParameterKey,'AllowedIngressCidr')]" \
  --output table

# CIDR 을 바꿔 재배포 (최대 3개)
./scripts/deploy.sh \
  --allowed-cidr 203.0.113.10/32 \
  --allowed-cidr-2 198.51.100.5/32 \
  --no-seed
```

**보안 그룹을 콘솔에서 직접 고치지 않는다.** 다음 배포에서 되돌아간다. 항상
파라미터로 변경한다.

`0.0.0.0/0` 은 스크립트와 CloudFormation 파라미터 정규식 양쪽에서 거부된다.
정말 광범위한 접근이 필요하면 ALB 앞에 WAF 를 두거나 Cognito 인증을 붙이는
방향을 검토한다. 파라미터 제약을 우회하는 방식은 쓰지 않는다.

---

## 4. 알람 대응

### `llmgw-*-alb-5xx` — 5xx 응답 발생

```bash
# 1. 에러 로그 확인
aws logs filter-log-events --region $REGION --log-group-name $LOG_GROUP \
  --start-time $(( ($(date +%s) - 900) * 1000 )) \
  --filter-pattern '{ $.level = "ERROR" }' \
  --query 'events[*].message' --output text | head -20
```

원인별 조치:

| 로그에 보이는 것 | 원인 | 조치 |
|---|---|---|
| `storage_unavailable`, `ResourceNotFoundException` | 테이블 없음 | 스택 상태 확인. 테이블이 지워졌다면 재배포 |
| `storage_unavailable`, `AccessDeniedException` | 태스크 역할 권한 | `TaskRole` 정책 확인 |
| `ProvisionedThroughputExceeded` | DynamoDB 스로틀 | 온디맨드는 자동 확장되지만 급증 시 일시 발생. 반복되면 파티션 분포 확인 |
| `upstream_error`, Bedrock 코드 | Bedrock 장애 또는 모델 문제 | AWS Health Dashboard 확인 |
| 스택트레이스 (`처리되지 않은 예외`) | 애플리케이션 버그 | `correlation_id` 로 해당 요청 전체 추적 후 롤백 검토 |

### `llmgw-*-latency-p99` — p99 지연 초과

```bash
# 모델별 지연 비교 (어느 모델이 느린지)
aws cloudwatch get-metric-statistics --region $REGION \
  --namespace LLMGateway --metric-name LatencyMs \
  --dimensions Name=Environment,Value=dev \
  --start-time "$(date -u -d '2 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 300 --extended-statistics p50 p99

# CPU 포화 여부
aws cloudwatch get-metric-statistics --region $REGION \
  --namespace AWS/ECS --metric-name CPUUtilization \
  --dimensions Name=ClusterName,Value=$CLUSTER Name=ServiceName,Value=$SERVICE \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 300 --statistics Average Maximum
```

- CPU 가 60% 를 넘으면 오토스케일링이 태스크를 늘린다. `MaxCount` 에 걸렸는지
  확인한다.
- CPU 가 낮은데 지연이 높으면 Bedrock 응답 자체가 느린 것이다. 긴 출력 요청이
  늘었거나 모델이 혼잡한 상태다. 애플리케이션 조치로 해결되지 않는다.
- 즉시 완화가 필요하면 태스크 수를 늘린다:
  `./scripts/deploy.sh --allowed-cidr <CIDR> --desired-count 3 --no-seed --no-smoke`

### `llmgw-*-unhealthy-targets` — 비정상 타깃

```bash
# 타깃 상태와 실패 이유
TG_ARN="$(aws elbv2 describe-target-groups --region $REGION \
  --names llmgw-dev-tg --query 'TargetGroups[0].TargetGroupArn' --output text)"
aws elbv2 describe-target-health --region $REGION --target-group-arn "$TG_ARN" \
  --query 'TargetHealthDescriptions[*].[Target.Id,TargetHealth.State,TargetHealth.Reason,TargetHealth.Description]' \
  --output table

# 최근 태스크 중단 이유
aws ecs describe-tasks --region $REGION --cluster $CLUSTER \
  --tasks $(aws ecs list-tasks --region $REGION --cluster $CLUSTER \
    --desired-status STOPPED --query 'taskArns[0:3]' --output text) \
  --query 'tasks[*].[stoppedReason,containers[0].reason,containers[0].exitCode]' \
  --output table
```

| 증상 | 원인 | 조치 |
|---|---|---|
| `CannotPullContainerError` | ECR 접근 실패 | 태스크 실행 역할 정책, 이미지 태그 존재 확인 |
| 컨테이너가 즉시 종료 | 기동 시 예외 | 로그 확인. 단가 파일 로드 실패나 설정 검증 오류가 흔하다 |
| `Health checks failed` | `/healthz` 응답 없음 | 컨테이너 로그 확인. 기동이 60초를 넘으면 `HealthCheckGracePeriodSeconds` 조정 |
| `ResourceInitializationError` + secret | 시크릿 읽기 실패 | 태스크 실행 역할의 `secretsmanager:GetSecretValue` 확인 |

### `llmgw-*-usage-write-failures` — 사용량 기록 실패

**집계가 유실되고 있다.** 사용자 요청은 성공하지만 비용·사용량이 대시보드에
반영되지 않는다. 동기 API 라 DLQ 를 걸 큐가 없어 이 알람이 유일한 감지 수단이다.

```bash
aws logs filter-log-events --region $REGION --log-group-name $LOG_GROUP \
  --start-time $(( ($(date +%s) - 3600) * 1000 )) \
  --filter-pattern '"사용량 기록에 실패했다"' \
  --query 'events[*].message' --output text | head -10
```

원인은 대개 DynamoDB 스로틀링이나 태스크 역할 권한이다. 유실된 구간은 자동
복구되지 않는다. 원본 레코드가 TTL 안에 있으면 수동 재집계가 가능하지만 현재
전용 도구는 없다. 알람이 반복되면 원인을 먼저 제거한다.

---

## 5. 일상 운영

### 사용량 확인

```bash
# 계정 목록과 기간 합계
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" "$GATEWAY_URL/analytics/accounts" | jq

# 특정 계정 대시보드 데이터 전체
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \
  "$GATEWAY_URL/analytics/dashboard?account_id=acme&start=2026-08-01&end=2026-08-24" | jq

# 팀별 비용 상위
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \
  "$GATEWAY_URL/analytics/breakdown?account_id=acme&dimension=team" \
  | jq -r '.data[] | "\(.label)\t\(.cost_usd)"'
```

### 키 관리

```bash
# 계정의 키 목록 (마지막 사용 시각 포함)
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \
  "$GATEWAY_URL/admin/accounts/acme/keys" \
  | jq -r '.data[] | "\(.key_id)\t\(.user_id)\t\(.key_prefix)\t\(.last_used_at // "미사용")"'

# 키 폐기 (즉시 적용된다. 키는 캐시하지 않는다)
curl -X DELETE -H "X-Admin-Token: $ADMIN_TOKEN" \
  "$GATEWAY_URL/admin/accounts/acme/keys/<key_id>"
```

### 계정 차단

```bash
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"status":"disabled"}' "$GATEWAY_URL/admin/accounts/acme/status"
```

계정·팀·사용자 상태는 메타데이터 캐시 TTL(기본 30초)만큼 반영이 지연된다.
**즉시 차단이 필요하면 계정 차단과 함께 키를 삭제한다.** 키는 캐시하지 않으므로
삭제가 곧바로 적용된다.

### 관리 토큰 교체

```bash
# 1. 새 값 생성 후 저장
NEW_TOKEN="$(openssl rand -hex 20)"
aws secretsmanager put-secret-value --region $REGION \
  --secret-id llmgw/dev/admin-token \
  --secret-string "{\"description\":\"llmgw admin token\",\"admin_token\":\"$NEW_TOKEN\"}"

# 2. 태스크를 재시작해 새 값을 읽게 한다 (환경변수는 시작 시에만 주입된다)
aws ecs update-service --region $REGION --cluster $CLUSTER --service $SERVICE \
  --force-new-deployment
aws ecs wait services-stable --region $REGION --cluster $CLUSTER --services $SERVICE
```

### 모델 단가 갱신

Bedrock 단가가 바뀌거나 새 모델을 쓰기 시작하면 `src/llmgw/pricing.json` 을
갱신한다. 표에 없는 모델은 비용이 0으로 집계되고 `pricing_known=false` 플래그와
경고 로그가 남는다.

```bash
# 단가를 모르는 모델 확인
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" "$GATEWAY_URL/admin/models" \
  | jq -r '.data[] | select(.pricing_known == false) | .model_id'
```

갱신 후 테스트를 돌리고 재배포한다. 과거에 기록된 비용은 소급 계산되지 않는다.

---

## 6. 프로덕션 전환 체크리스트

기본 구성은 dev·데모 검증을 1순위로 맞춰져 있다. 실사용 전에 다음을 반드시
처리한다.

- [ ] **HTTPS 를 켠다.** ACM 인증서를 발급해 `--certificate-arn` 으로 배포한다.
      인증서가 없으면 API 키와 관리 토큰이 평문으로 전송된다. 이건 선택이 아니다.
- [ ] **DynamoDB `DeletionPolicy` 를 `Retain` 으로 바꾼다.** `infra/app.yaml` 의
      `RegistryTable`, `UsageTable`, `UsageAggTable` 세 곳이다. 기본값 `Delete` 는
      dev 재배포 편의를 위한 것이다.
- [ ] **`DesiredCount` 를 2 이상으로 한다.** 1이면 AZ 장애나 태스크 교체 중 가용성이
      끊긴다.
- [ ] **`--alarm-email` 을 지정하고 확인 메일을 승인한다.** 승인하지 않으면 알람이
      발생해도 통보되지 않는다.
- [ ] **접근 CIDR 을 최소로 좁힌다.** 사무실 대역이나 VPN 출구 IP 만 남긴다.
- [ ] **로그 보존을 검토한다.** 기본 30일이다. 감사 요건이 있으면 늘린다.
- [ ] **예산 한도를 설정한다.** 계정·팀·사용자 어느 축에도 예산이 없으면 지출 상한이
      없다.
- [ ] **`AllowedBedrockModelArn` 을 좁힌다.** 기본값은 모든 기반 모델을 허용한다.
- [ ] **네트워크 강화를 검토한다.** 프라이빗 서브넷 전환 절차는
      [ADR 0003](adr/0003-network-and-exposure.md) 의 마지막 절에 있다.
- [ ] **단가 표를 AWS 요금 페이지와 대조한다.** `pricing.json` 은 스냅샷이다.
- [ ] **CI/CD 를 OIDC 로 연결한다.** 아래 절 참고.

---

## 7. GitHub Actions 에서 배포하기 (OIDC)

장기 액세스 키를 시크릿에 넣지 않고 OIDC 임시 자격증명을 쓴다.

```bash
# 1. GitHub OIDC 공급자 등록 (계정당 한 번)
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1

# 2. 배포용 역할을 만든다. 신뢰 정책에서 리포지토리와 브랜치를 못박는다.
cat > /tmp/trust.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::<계정ID>:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "token.actions.githubusercontent.com:sub": "repo:<소유자>/<리포>:ref:refs/heads/main"
      }
    }
  }]
}
JSON
aws iam create-role --role-name llmgw-github-deploy \
  --assume-role-policy-document file:///tmp/trust.json
```

역할에 붙일 권한은 CloudFormation, ECR, EC2(VPC), ELBv2, ECS, DynamoDB, IAM,
Secrets Manager, CloudWatch, SNS, Application Auto Scaling 이다. `*FullAccess`
관리형 정책을 붙이지 말고 필요한 액션만 인라인 정책으로 정의한다.

워크플로에서는 다음과 같이 쓴다.

```yaml
permissions:
  id-token: write
  contents: read
steps:
  - uses: aws-actions/configure-aws-credentials@v4
    with:
      role-to-assume: arn:aws:iam::<계정ID>:role/llmgw-github-deploy
      aws-region: us-east-1
  - run: ./scripts/deploy.sh --allowed-cidr ${{ vars.ALLOWED_CIDR }} --no-seed
```

`prod` 배포는 GitHub Environments 의 필수 리뷰어 기능으로 수동 승인을 건다.

---

## 8. 전체 삭제

```bash
# 앱 스택만 (ECR 이미지 유지)
./scripts/teardown.sh --env dev --region $REGION

# 전부 (사용량 이력이 영구 삭제된다)
./scripts/teardown.sh --env dev --region $REGION --purge-data
```

확인 프롬프트에 `delete` 를 입력해야 진행된다.

삭제 전에 데이터를 보관해야 하면:

```bash
# 로그 내보내기 (S3 버킷이 미리 있어야 한다)
aws logs create-export-task --region $REGION --log-group-name $LOG_GROUP \
  --from $(( ($(date +%s) - 30*86400) * 1000 )) --to $(( $(date +%s) * 1000 )) \
  --destination <버킷> --destination-prefix llmgw-dev-logs

# 사용량 집계 백업
aws dynamodb scan --region $REGION --table-name llmgw-dev-usage-agg \
  --output json > usage-agg-backup.json
```

삭제 후에도 남는 것: 로컬 `.deploy/` 디렉터리의 데모 키 파일. 필요 없으면
`rm -rf .deploy` 로 지운다.
