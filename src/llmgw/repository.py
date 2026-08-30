"""DynamoDB 접근 계층.

boto3 호출은 이 모듈과 `bedrock` 모듈에만 존재한다. 상위 계층은 도메인
객체만 다룬다.

테이블 설계
-----------
`registry` (pk, sk) + GSI `gsi1`(gsi1pk, gsi1sk)

    ACCOUNT#<aid>  META            계정. gsi1pk=ACCOUNTS 로 전체 목록 조회
    ACCOUNT#<aid>  TEAM#<tid>      팀
    ACCOUNT#<aid>  USER#<uid>      사용자
    ACCOUNT#<aid>  AUTH            계정별 외부 인증(OIDC) 설정
    KEY#<hash>     META            API 키. gsi1pk=ACCOUNT#<aid> 로 계정별 조회
    OIDC#<issuer>  META            발급자 -> 계정 역인덱스. 토큰의 iss 로
                                   어느 계정 설정인지 한 번에 찾는다.

`usage` (pk, sk) + LSI `lsi_ts`(pk, ts)

    pk = <aid>#<YYYY-MM-DD>, sk = <usage_id>

    sk 는 서버가 요청마다 만드는 `usage_id` 다. 클라이언트가 지정할 수 있는
    `X-Request-Id` 를 키로 쓰면 같은 값을 계속 보내는 것만으로 집계가 멈추고
    예산 검사가 영원히 통과한다. 재전송 보호는 `ClientRequestToken` 이
    담당한다. 시간순 조회는 LSI 로 한다.

`usage-agg` (pk, sk)

    pk = <aid>#DAY#<YYYY-MM-DD> 또는 <aid>#MONTH#<YYYY-MM>
    sk = TOTAL | TEAM#<tid> | USER#<uid> | MODEL#<mid> | KEY#<kid>

    원자적 ADD 로 누적한다. 대시보드는 이 테이블만 읽으므로 요청 수가
    늘어도 조회 비용이 늘지 않는다.
"""

from __future__ import annotations

from concurrent import futures
import datetime
import decimal
import typing
import uuid

import boto3
from boto3.dynamodb import types as dynamodb_types
import botocore.config
import botocore.exceptions

from llmgw import clock
from llmgw import domain
from llmgw import errors

# 목록 조회 시 무한 페이징을 막는 상한. 관리 화면 용도라 이 정도면 충분하다.
_MAX_PAGES = 20

# 집계 파티션 병렬 조회에 쓰는 최대 스레드 수. botocore 커넥션 풀
# (max_pool_connections=50)보다 작게 잡아 풀 고갈을 피한다.
_MAX_QUERY_WORKERS = 16

_ACCOUNTS_INDEX_PARTITION = "ACCOUNTS"
_GSI1_NAME = "gsi1"
_TS_INDEX_NAME = "lsi_ts"
_META_SORT_KEY = "META"
# 계정별 외부 인증(OIDC) 설정의 정렬 키.
_AUTH_SORT_KEY = "AUTH"
# 분 단위 레이트 리밋 카운터 보존 시간. 분 경계에서 시계가 어긋나도 직전 분이
# 남아 있어야 한다.
_RATE_LIMIT_TTL_SECONDS = 120

_SECONDS_PER_DAY = 86400

_SERIALIZER = dynamodb_types.TypeSerializer()

_JsonDict = dict[str, typing.Any]
_LowLevelItem = dict[str, typing.Any]


# ---------------------------------------------------------------------------
# 키 빌더
# ---------------------------------------------------------------------------


def account_pk(account_id: str) -> str:
    """계정 파티션 키를 만든다."""
    return f"ACCOUNT#{account_id}"


def key_pk(key_hash: str) -> str:
    """API 키 파티션 키를 만든다."""
    return f"KEY#{key_hash}"


def rate_limit_pk(account_id: str, scope: str) -> str:
    """레이트 리밋 카운터의 파티션 키를 만든다.

    호출 주체가 파티션 키에 들어가므로 부하가 키·사용자별로 분산된다. 분을
    파티션 키에 넣으면 그 분의 모든 요청이 한 파티션에 몰린다.

    Args:
        account_id: 계정 ID.
        scope: `KEY#<id>` 또는 `USER#<id>`.

    Returns:
        `RATE#<account_id>#<scope>` 형태의 파티션 키.
    """
    return f"RATE#{account_id}#{scope}"


def issuer_pk(issuer: str) -> str:
    """OIDC 발급자 역인덱스의 파티션 키를 만든다.

    토큰의 `iss` 로 어느 계정 설정인지 한 번의 GetItem 으로 찾기 위한
    포인터 아이템이다.

    Args:
        issuer: OIDC 발급자 URL.

    Returns:
        `OIDC#<issuer>` 형태의 파티션 키.
    """
    return f"OIDC#{issuer}"


def team_sk(team_id: str) -> str:
    """팀 정렬 키를 만든다."""
    return f"TEAM#{team_id}"


def user_sk(user_id: str) -> str:
    """사용자 정렬 키를 만든다."""
    return f"USER#{user_id}"


def usage_pk(account_id: str, day: str) -> str:
    """usage 테이블 파티션 키를 만든다.

    Args:
        account_id: 계정 ID.
        day: `YYYY-MM-DD` 형식의 날짜.

    Returns:
        `<account_id>#<day>` 형태의 파티션 키.
    """
    return f"{account_id}#{day}"


def agg_pk(
    account_id: str, granularity: domain.Granularity, period: str
) -> str:
    """집계 테이블 파티션 키를 만든다.

    Args:
        account_id: 계정 ID.
        granularity: 시간 단위.
        period: `YYYY-MM-DD`(일) 또는 `YYYY-MM`(월).

    Returns:
        `<account_id>#DAY#2026-08-23` 형태의 파티션 키.
    """
    return f"{account_id}#{granularity.name}#{period}"


def dimension_sk(
    dimension: domain.BreakdownDimension | None, value: str
) -> str:
    """집계 테이블 정렬 키를 만든다.

    Args:
        dimension: 집계 축. `None` 이면 전체 합계 행이다.
        value: 축의 값(팀 ID, 사용자 ID 등).

    Returns:
        `TOTAL` 또는 `TEAM#platform` 형태의 정렬 키.
    """
    if dimension is None:
        return "TOTAL"
    return f"{dimension.name}#{value}"


# ---------------------------------------------------------------------------
# 클라이언트 생성
# ---------------------------------------------------------------------------


def boto_config(timeout_seconds: int) -> botocore.config.Config:
    """DynamoDB/Bedrock 공용 botocore 설정을 만든다.

    표준 재시도 모드는 스로틀링과 일시 오류를 지수 백오프로 재시도한다.
    DynamoDB 는 짧은 타임아웃으로도 충분하지만, 같은 설정을 Bedrock 에도
    쓰기 때문에 읽기 타임아웃을 넉넉하게 잡는다.

    Args:
        timeout_seconds: 읽기 타임아웃(초).

    Returns:
        구성된 `botocore.config.Config`.
    """
    return botocore.config.Config(
        retries={"max_attempts": 3, "mode": "standard"},
        connect_timeout=5,
        read_timeout=timeout_seconds,
        # ECS 태스크 하나가 동시에 처리하는 요청 수를 감당할 커넥션 풀.
        max_pool_connections=50,
    )


def create_dynamodb_resource(
    region: str, timeout_seconds: int = 10
) -> typing.Any:
    """DynamoDB 리소스 클라이언트를 만든다.

    Args:
        region: 리전 이름.
        timeout_seconds: 읽기 타임아웃(초).

    Returns:
        boto3 DynamoDB 서비스 리소스.
    """
    return boto3.resource(
        "dynamodb", region_name=region, config=boto_config(timeout_seconds)
    )


def create_dynamodb_client(
    region: str, timeout_seconds: int = 10
) -> typing.Any:
    """DynamoDB 저수준 클라이언트를 만든다.

    리소스 객체의 `meta.client` 를 재사용하면 안 된다. boto3 는 리소스를
    만들 때 그 클라이언트에 문서 변환 핸들러를 등록하는데, 이미
    AttributeValue 형식으로 직렬화한 파라미터를 넘기면 한 번 더 변환되어
    호출이 깨진다. 트랜잭션과 병렬 Query 는 직렬화를 직접 하므로 변환
    핸들러가 붙지 않은 별도 클라이언트가 필요하다.

    Args:
        region: 리전 이름.
        timeout_seconds: 읽기 타임아웃(초).

    Returns:
        boto3 DynamoDB 클라이언트.
    """
    return boto3.client(
        "dynamodb", region_name=region, config=boto_config(timeout_seconds)
    )


# ---------------------------------------------------------------------------
# 변환 헬퍼
# ---------------------------------------------------------------------------


def _optional_int(value: typing.Any) -> int | None:
    """DynamoDB 숫자를 정수로 바꾼다. 없으면 `None`.

    Args:
        value: 아이템에서 읽은 값.

    Returns:
        정수 또는 `None`.
    """
    if value is None:
        return None
    return int(value)


def _optional_decimal(value: typing.Any) -> decimal.Decimal | None:
    """DynamoDB 숫자 값을 `Decimal | None` 으로 변환한다."""
    if value is None:
        return None
    return decimal.Decimal(str(value))


def _as_int(value: typing.Any) -> int:
    """DynamoDB 숫자 값을 `int` 로 변환한다. 없으면 0."""
    if value is None:
        return 0
    return int(decimal.Decimal(str(value)))


def _serialize(value: typing.Any) -> _LowLevelItem:
    """값을 DynamoDB AttributeValue 로 직렬화한다.

    `Decimal` 은 지수 표기로 문자열화될 수 있다. 예를 들어
    `Decimal('0E-10')` 은 `"0E-10"` 이 되고 DynamoDB 는 이를 숫자로 받지
    않는다. 그러면 트랜잭션 전체가 취소되어 집계가 유실된다. 직렬화 직전에
    항상 고정소수점 표기로 바꿔 이 경로를 막는다.

    Args:
        value: 직렬화할 파이썬 값.

    Returns:
        `{"S": ...}` 형태의 AttributeValue.
    """
    if isinstance(value, decimal.Decimal):
        value = decimal.Decimal(format(value, "f"))
    return typing.cast("_LowLevelItem", _SERIALIZER.serialize(value))


def _strip_none(item: _JsonDict) -> _JsonDict:
    """값이 `None` 인 속성을 제거한다.

    DynamoDB 는 NULL 타입을 지원하지만, 속성을 아예 두지 않는 편이
    `attribute_exists` 조건과 스토리지 비용 모두에 유리하다.

    Args:
        item: 정리할 아이템.

    Returns:
        `None` 값이 제거된 새 딕셔너리.
    """
    return {name: value for name, value in item.items() if value is not None}


def _api_key_item(api_key: domain.ApiKey) -> dict[str, typing.Any]:
    """API 키를 DynamoDB 아이템으로 직렬화한다.

    저장 경로가 세 곳(`put_api_key`, `update_api_key`, `rotate_api_key`)이라
    직렬화를 각자 나열하면 필드를 추가할 때 일부만 갱신된다. 실제로 그런 일이
    두 번 있었다. 1.7.0 에서 `update_api_key` 가 `rpm_limit` 과 `expires_at` 을
    저장하지 않았고, 그것을 고친 뒤에도 `rotate_api_key` 는 그대로 남아 키를
    회전하면 레이트리밋과 만료가 사라졌다. 만료된 키를 회전하면 무기한
    유효해지는 통제 우회였다.

    Args:
        api_key: 직렬화할 키.

    Returns:
        `None` 필드가 제거된 아이템.
    """
    return _strip_none(
        {
            "pk": key_pk(api_key.key_hash),
            "sk": _META_SORT_KEY,
            "gsi1pk": account_pk(api_key.account_id),
            "gsi1sk": f"KEY#{api_key.key_id}",
            "entity": "api_key",
            "key_id": api_key.key_id,
            "key_hash": api_key.key_hash,
            "key_prefix": api_key.key_prefix,
            "account_id": api_key.account_id,
            "team_id": api_key.team_id,
            "user_id": api_key.user_id,
            "display_name": api_key.name,
            "allowed_models": list(api_key.allowed_models),
            "monthly_budget_usd": api_key.monthly_budget_usd,
            "rpm_limit": api_key.rpm_limit,
            "expires_at": api_key.expires_at,
            "status": api_key.status.value,
            "created_at": api_key.created_at,
            "last_used_at": api_key.last_used_at,
        }
    )


def _totals_from_item(item: _JsonDict) -> domain.UsageTotals:
    """집계 테이블 아이템을 `UsageTotals` 로 변환한다."""
    return domain.UsageTotals(
        requests=_as_int(item.get("requests")),
        success_requests=_as_int(item.get("success_requests")),
        error_requests=_as_int(item.get("error_requests")),
        input_tokens=_as_int(item.get("input_tokens")),
        output_tokens=_as_int(item.get("output_tokens")),
        cost_usd=decimal.Decimal(str(item.get("cost_usd", "0"))),
        latency_ms_sum=_as_int(item.get("latency_ms_sum")),
        # 1.0 에서 기록된 행에는 이 속성이 없다. 없으면 0으로 읽어 하위
        # 호환을 유지한다.
        unpriced_requests=_as_int(item.get("unpriced_requests")),
    )


# ---------------------------------------------------------------------------
# 레지스트리
# ---------------------------------------------------------------------------


class RegistryRepository:
    """계정·팀·사용자·API 키 저장소."""

    def __init__(self, table: typing.Any, client: typing.Any = None) -> None:
        """저장소를 만든다.

        Args:
            table: boto3 DynamoDB `Table` 리소스.
            client: 저수준 DynamoDB 클라이언트. 트랜잭션(키 재발급)에 쓴다.
                생략하면 트랜잭션 경로만 사용할 수 없고 나머지는 동작한다.
                리소스의 `meta.client` 를 넘기면 안 된다. 이유는
                `create_dynamodb_client` 문서를 참고한다.
        """
        self._table = table
        self._client = client
        # 트랜잭션 아이템은 테이블 이름을 명시해야 한다. 리소스에서 이름을
        # 얻어 두어 호출자가 따로 전달하지 않아도 되게 한다.
        self._table_name = getattr(table, "name", None)

    # -- 계정 ---------------------------------------------------------------

    def put_account(
        self, account: domain.Account, *, overwrite: bool = False
    ) -> None:
        """계정을 저장한다.

        Args:
            account: 저장할 계정.
            overwrite: `True` 면 기존 계정을 덮어쓴다.

        Raises:
            ResourceConflictError: `overwrite` 가 `False` 인데 같은 ID 의
                계정이 이미 있는 경우.
            ResourceNotFoundError: `overwrite` 가 `True` 인데 갱신 직전
                계정이 삭제된 경우.
        """
        item = _strip_none(
            {
                "pk": account_pk(account.account_id),
                "sk": _META_SORT_KEY,
                "gsi1pk": _ACCOUNTS_INDEX_PARTITION,
                "gsi1sk": account_pk(account.account_id),
                "entity": "account",
                "account_id": account.account_id,
                "display_name": account.name,
                "monthly_budget_usd": account.monthly_budget_usd,
                "status": account.status.value,
                "created_at": account.created_at,
            }
        )
        self._put(item, overwrite=overwrite, label="account")

    def get_account(self, account_id: str) -> domain.Account | None:
        """계정을 조회한다.

        Args:
            account_id: 계정 ID.

        Returns:
            찾은 계정. 없으면 `None`.
        """
        item = self._get(account_pk(account_id), _META_SORT_KEY)
        return None if item is None else self._to_account(item)

    def list_accounts(self) -> list[domain.Account]:
        """모든 계정을 반환한다.

        전체 스캔을 피하기 위해 GSI 의 단일 파티션에 계정을 모아 두고
        Query 로 읽는다. 계정 수가 수만 건이 되면 파티션 분할이 필요하지만,
        테넌트 수는 그 규모가 되지 않는 전제다.

        Returns:
            계정 ID 순으로 정렬된 계정 목록.
        """
        items = self._query_index(
            key_condition="gsi1pk = :pk",
            values={":pk": _ACCOUNTS_INDEX_PARTITION},
        )
        return [self._to_account(item) for item in items]

    # -- 팀 -----------------------------------------------------------------

    def put_team(self, team: domain.Team, *, overwrite: bool = False) -> None:
        """팀을 저장한다.

        Args:
            team: 저장할 팀.
            overwrite: `True` 면 기존 팀을 덮어쓴다.

        Raises:
            ResourceConflictError: 중복 생성인 경우.
            ResourceNotFoundError: `overwrite` 가 `True` 인데 갱신 직전
                팀이 삭제된 경우.
        """
        item = _strip_none(
            {
                "pk": account_pk(team.account_id),
                "sk": team_sk(team.team_id),
                "entity": "team",
                "account_id": team.account_id,
                "team_id": team.team_id,
                "display_name": team.name,
                "monthly_budget_usd": team.monthly_budget_usd,
                "status": team.status.value,
                "created_at": team.created_at,
            }
        )
        self._put(item, overwrite=overwrite, label="team")

    # -- 외부 인증(OIDC) 설정 ------------------------------------------------

    def put_auth_config(
        self, config: domain.AccountAuthConfig, *, overwrite: bool = False
    ) -> None:
        """계정의 OIDC 설정을 저장한다.

        설정 본문과 발급자 역인덱스를 하나의 트랜잭션으로 쓴다. 두 쓰기를
        나누면 발급자만 등록되고 설정이 없는 상태(또는 그 반대)가 남아,
        토큰이 계정으로 라우팅됐는데 설정을 읽을 수 없는 경로가 생긴다.

        발급자 역인덱스에는 `attribute_not_exists` 조건을 걸어 다른 계정이
        이미 쓰는 발급자를 가로채지 못하게 한다.

        Args:
            config: 저장할 설정.
            overwrite: `True` 면 같은 계정의 기존 설정을 갱신한다.

        Raises:
            ResourceConflictError: 발급자가 다른 계정에 이미 등록된 경우,
                또는 중복 생성인 경우.
        """
        body = _strip_none(
            {
                "pk": account_pk(config.account_id),
                "sk": _AUTH_SORT_KEY,
                "entity": "auth_config",
                "account_id": config.account_id,
                "issuer": config.issuer,
                "jwks_url": config.jwks_url,
                "audience": config.audience,
                "user_claim": config.user_claim,
                "team_claim": config.team_claim,
                "groups_claim": config.groups_claim,
                "admin_groups": config.admin_groups,
                "auto_provision": config.auto_provision,
                "provision_allowed_models": config.provision_allowed_models,
                "provision_budget_usd": config.provision_budget_usd,
                "status": config.status.value,
                "created_at": config.created_at,
                "updated_at": config.updated_at,
            }
        )
        pointer = {
            "pk": issuer_pk(config.issuer),
            "sk": _META_SORT_KEY,
            "entity": "auth_issuer",
            "issuer": config.issuer,
            "account_id": config.account_id,
        }
        # 역인덱스는 같은 계정이 다시 저장하는 경우에만 덮어쓰기를 허용한다.
        pointer_condition = (
            "attribute_not_exists(pk) OR account_id = :account_id"
        )
        transact_items: list[_LowLevelItem] = [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": {
                        name: _serialize(value) for name, value in body.items()
                    },
                    "ConditionExpression": (
                        "attribute_exists(pk)"
                        if overwrite
                        else "attribute_not_exists(sk)"
                    ),
                }
            },
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": {
                        name: _serialize(value)
                        for name, value in pointer.items()
                    },
                    "ConditionExpression": pointer_condition,
                    "ExpressionAttributeValues": {
                        ":account_id": _serialize(config.account_id)
                    },
                }
            },
        ]
        try:
            self._client.transact_write_items(TransactItems=transact_items)
        except botocore.exceptions.ClientError as exc:
            codes = [
                str(reason.get("Code") or "")
                for reason in (exc.response.get("CancellationReasons") or [])
            ]
            if len(codes) > 1 and codes[1] == "ConditionalCheckFailed":
                raise errors.ResourceConflictError(
                    "이 발급자는 다른 계정에 이미 등록돼 있다:"
                    f" {config.issuer}"
                ) from exc
            if codes and codes[0] == "ConditionalCheckFailed":
                if overwrite:
                    raise errors.ResourceNotFoundError(
                        "갱신할 인증 설정이 없다."
                    ) from exc
                raise errors.ResourceConflictError(
                    "이 계정에는 인증 설정이 이미 있다."
                ) from exc
            raise

    def get_auth_config(
        self, account_id: str
    ) -> domain.AccountAuthConfig | None:
        """계정의 OIDC 설정을 조회한다.

        Args:
            account_id: 계정 ID.

        Returns:
            찾은 설정. 없으면 `None`.
        """
        item = self._get(account_pk(account_id), _AUTH_SORT_KEY)
        return None if item is None else self._to_auth_config(item)

    def find_account_by_issuer(self, issuer: str) -> str:
        """발급자로 계정 ID 를 찾는다.

        Args:
            issuer: 토큰의 `iss` 값.

        Returns:
            매핑된 계정 ID. 등록되지 않은 발급자면 빈 문자열.
        """
        if not issuer:
            return ""
        item = self._get(issuer_pk(issuer), _META_SORT_KEY)
        if item is None:
            return ""
        return str(item.get("account_id") or "")

    def delete_auth_config(self, account_id: str) -> None:
        """계정의 OIDC 설정과 발급자 역인덱스를 함께 삭제한다.

        Args:
            account_id: 계정 ID.

        Raises:
            ResourceNotFoundError: 설정이 없는 경우.
        """
        existing = self.get_auth_config(account_id)
        if existing is None:
            raise errors.ResourceNotFoundError("삭제할 인증 설정이 없다.")
        self._client.transact_write_items(
            TransactItems=[
                {
                    "Delete": {
                        "TableName": self._table_name,
                        "Key": {
                            "pk": _serialize(account_pk(account_id)),
                            "sk": _serialize(_AUTH_SORT_KEY),
                        },
                    }
                },
                {
                    "Delete": {
                        "TableName": self._table_name,
                        "Key": {
                            "pk": _serialize(issuer_pk(existing.issuer)),
                            "sk": _serialize(_META_SORT_KEY),
                        },
                    }
                },
            ]
        )

    @staticmethod
    def _to_auth_config(item: _JsonDict) -> domain.AccountAuthConfig:
        """아이템을 인증 설정 도메인 객체로 변환한다."""
        return domain.AccountAuthConfig(
            account_id=str(item["account_id"]),
            issuer=str(item["issuer"]),
            jwks_url=str(item.get("jwks_url", "")),
            audience=str(item.get("audience", "")),
            user_claim=str(item.get("user_claim", "email")),
            team_claim=str(item.get("team_claim", "")),
            groups_claim=str(item.get("groups_claim", "cognito:groups")),
            admin_groups=str(item.get("admin_groups", "")),
            auto_provision=bool(item.get("auto_provision", False)),
            provision_allowed_models=str(
                item.get("provision_allowed_models", "")
            ),
            provision_budget_usd=_optional_decimal(
                item.get("provision_budget_usd")
            ),
            status=domain.EntityStatus(item.get("status", "active")),
            created_at=str(item.get("created_at", "")),
            updated_at=str(item.get("updated_at", "")),
        )

    def get_team(self, account_id: str, team_id: str) -> domain.Team | None:
        """팀을 조회한다.

        Args:
            account_id: 계정 ID.
            team_id: 팀 ID.

        Returns:
            찾은 팀. 없으면 `None`.
        """
        item = self._get(account_pk(account_id), team_sk(team_id))
        return None if item is None else self._to_team(item)

    def list_teams(self, account_id: str) -> list[domain.Team]:
        """계정의 팀 목록을 반환한다.

        Args:
            account_id: 계정 ID.

        Returns:
            팀 ID 순으로 정렬된 팀 목록.
        """
        items = self._query_table(
            key_condition="pk = :pk AND begins_with(sk, :prefix)",
            values={":pk": account_pk(account_id), ":prefix": "TEAM#"},
        )
        return [self._to_team(item) for item in items]

    # -- 사용자 -------------------------------------------------------------

    def put_user(self, user: domain.User, *, overwrite: bool = False) -> None:
        """사용자를 저장한다.

        Args:
            user: 저장할 사용자.
            overwrite: `True` 면 기존 사용자를 덮어쓴다.

        Raises:
            ResourceConflictError: 중복 생성인 경우.
            ResourceNotFoundError: `overwrite` 가 `True` 인데 갱신 직전
                사용자가 삭제된 경우.
        """
        item = _strip_none(
            {
                "pk": account_pk(user.account_id),
                "sk": user_sk(user.user_id),
                "entity": "user",
                "account_id": user.account_id,
                "user_id": user.user_id,
                "display_name": user.name,
                "email": user.email,
                "team_id": user.team_id,
                "monthly_budget_usd": user.monthly_budget_usd,
                "rpm_limit": user.rpm_limit,
                "status": user.status.value,
                "created_at": user.created_at,
            }
        )
        self._put(item, overwrite=overwrite, label="user")

    def get_user(self, account_id: str, user_id: str) -> domain.User | None:
        """사용자를 조회한다.

        Args:
            account_id: 계정 ID.
            user_id: 사용자 ID.

        Returns:
            찾은 사용자. 없으면 `None`.
        """
        item = self._get(account_pk(account_id), user_sk(user_id))
        return None if item is None else self._to_user(item)

    def list_users(self, account_id: str) -> list[domain.User]:
        """계정의 사용자 목록을 반환한다.

        Args:
            account_id: 계정 ID.

        Returns:
            사용자 ID 순으로 정렬된 사용자 목록.
        """
        items = self._query_table(
            key_condition="pk = :pk AND begins_with(sk, :prefix)",
            values={":pk": account_pk(account_id), ":prefix": "USER#"},
        )
        return [self._to_user(item) for item in items]

    # -- API 키 -------------------------------------------------------------

    def put_api_key(self, api_key: domain.ApiKey) -> None:
        """API 키를 저장한다.

        Args:
            api_key: 저장할 키 메타데이터.

        Raises:
            ResourceConflictError: 같은 해시의 키가 이미 있는 경우. 난수
                충돌은 사실상 발생하지 않지만, 조건부 쓰기를 생략하면 기존
                키를 조용히 덮어쓸 수 있어 방어한다.
        """
        self._put(_api_key_item(api_key), overwrite=False, label="api_key")

    def get_api_key_by_hash(self, key_hash: str) -> domain.ApiKey | None:
        """해시로 API 키를 조회한다.

        인증 경로에서 요청마다 호출되므로 단일 GetItem 으로 끝낸다.

        Args:
            key_hash: 평문 키의 SHA-256 16진수 해시.

        Returns:
            찾은 키. 없으면 `None`.
        """
        item = self._get(key_pk(key_hash), _META_SORT_KEY)
        return None if item is None else self._to_api_key(item)

    def list_api_keys(self, account_id: str) -> list[domain.ApiKey]:
        """계정의 API 키 목록을 반환한다.

        Args:
            account_id: 계정 ID.

        Returns:
            키 ID 순으로 정렬된 키 목록. 해시는 포함되지만 평문은 없다.
        """
        items = self._query_index(
            key_condition="gsi1pk = :pk AND begins_with(gsi1sk, :prefix)",
            values={":pk": account_pk(account_id), ":prefix": "KEY#"},
        )
        return [self._to_api_key(item) for item in items]

    def get_api_key(self, account_id: str, key_id: str) -> domain.ApiKey | None:
        """계정 범위에서 키 ID 로 키 메타데이터를 조회한다.

        키의 파티션 키는 해시라서 GSI 를 거쳐 찾는다. 관리 화면의 조회·수정
        경로에서 쓰고, 인증 경로에서는 쓰지 않는다.

        Args:
            account_id: 계정 ID.
            key_id: 키 ID.

        Returns:
            찾은 키. 없으면 `None`.
        """
        items = self._query_index(
            key_condition="gsi1pk = :pk AND gsi1sk = :sk",
            values={":pk": account_pk(account_id), ":sk": f"KEY#{key_id}"},
        )
        return None if not items else self._to_api_key(items[0])

    def update_api_key(self, api_key: domain.ApiKey) -> None:
        """기존 API 키 메타데이터를 덮어쓴다.

        해시가 파티션 키이므로 해시가 그대로인 수정에만 쓴다. 존재할 때만
        갱신해 삭제된 키가 되살아나지 않게 한다.

        Args:
            api_key: 갱신할 키. 해시는 기존과 같아야 한다.

        Raises:
            ResourceNotFoundError: 해당 해시의 키가 없는 경우.
        """
        try:
            self._table.put_item(
                Item=_api_key_item(api_key),
                ConditionExpression="attribute_exists(pk)",
            )
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                raise errors.ResourceNotFoundError(
                    f"API 키를 찾을 수 없다: {api_key.key_id}"
                ) from exc
            raise

    def delete_api_key(self, account_id: str, key_id: str) -> None:
        """API 키를 삭제한다.

        키 아이템의 파티션 키는 해시라서 키 ID 만으로는 삭제할 수 없다.
        GSI 로 해시를 먼저 찾은 뒤 삭제한다.

        Args:
            account_id: 계정 ID.
            key_id: 삭제할 키 ID.

        Raises:
            ResourceNotFoundError: 해당 키가 없는 경우.
        """
        items = self._query_index(
            key_condition="gsi1pk = :pk AND gsi1sk = :sk",
            values={
                ":pk": account_pk(account_id),
                ":sk": f"KEY#{key_id}",
            },
        )
        if not items:
            raise errors.ResourceNotFoundError(
                f"API 키를 찾을 수 없다: account={account_id} key={key_id}"
            )
        self._conditional_delete(
            partition=str(items[0]["pk"]),
            sort=_META_SORT_KEY,
            not_found=(
                f"API 키를 찾을 수 없다: account={account_id} key={key_id}"
            ),
        )

    def rotate_api_key(
        self,
        old_key_hash: str,
        rotated: domain.ApiKey,
    ) -> None:
        """키를 원자적으로 재발급한다.

        새 해시 아이템 생성과 옛 해시 아이템 삭제를 하나의
        `TransactWriteItems` 로 묶는다. 두 쓰기를 별도 호출로 나누면, 두 번째
        가 실패했을 때 옛 키와 새 키가 모두 유효하고 같은 `key_id` 가 GSI 에
        중복으로 남는다. 트랜잭션은 둘 다 성공하거나 둘 다 취소되게 한다.

        호출마다 새 `ClientRequestToken` 을 만들며, 이는 **같은 SDK 호출의
        내부 재시도**를 보호한다. 네트워크 떨림으로 boto3 가 동일 요청을
        자동 재시도할 때 트랜잭션이 두 번 적용되지 않도록 DynamoDB 가
        결과를 캐시한다(약 10분 창).

        이 보호는 **HTTP `/rotate` 재호출까지 멱등하게 만들지는 않는다.** 응답이
        유실되어 클라이언트가 `/rotate` 를 다시 부르면, 그 호출은 새 평문 키를
        새로 만들므로 트랜잭션 내용과 작업 토큰도 달라진다. HTTP 수준의 완전한
        멱등성이 필요하면 회전 결과를 작업 ID 별로 임시 저장하는 별도 설계가
        필요하다. 그 전까지 재발급은 "다시 부르면 또 새 키가 나온다"는 동작으로
        다룬다.

        Args:
            old_key_hash: 무효화할 옛 키의 해시.
            rotated: 새 해시·접두어를 담은 키. `key_id` 는 그대로다.

        Raises:
            GatewayError: 트랜잭션 클라이언트가 주입되지 않은 경우.
            ResourceConflictError: 새 해시가 이미 존재하는 경우(사실상 없음).
            ResourceNotFoundError: 옛 키가 이미 사라진 경우.
        """
        if self._client is None or self._table_name is None:
            raise errors.GatewayError(
                "재발급에는 트랜잭션 클라이언트가 필요하다."
            )
        new_item = _api_key_item(rotated)
        transact_items = [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": {
                        name: _serialize(value)
                        for name, value in new_item.items()
                    },
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            {
                "Delete": {
                    "TableName": self._table_name,
                    "Key": {
                        "pk": _serialize(key_pk(old_key_hash)),
                        "sk": _serialize(_META_SORT_KEY),
                    },
                    "ConditionExpression": "attribute_exists(pk)",
                }
            },
        ]
        try:
            # 한 HTTP 요청 안의 SDK 재시도는 같은 호출 인자를 재사용하므로
            # 동일 토큰을 쓴다. 다음 HTTP 요청은 여기로 다시 들어와 새 토큰을
            # 받아, 클라이언트가 X-Request-Id 를 재사용해도 서로 충돌하지
            # 않는다.
            self._client.transact_write_items(
                TransactItems=transact_items,
                ClientRequestToken=uuid.uuid4().hex,
            )
        except botocore.exceptions.ClientError as exc:
            reasons = exc.response.get("CancellationReasons") or []
            codes = [reason.get("Code") for reason in reasons]
            # reasons[0] 은 Put(새 해시 충돌), reasons[1] 은 Delete(옛 키 부재).
            if len(codes) > 0 and codes[0] == "ConditionalCheckFailed":
                raise errors.ResourceConflictError(
                    "이미 존재하는 키 해시로 재발급을 시도했다:"
                    f" {rotated.key_id}"
                ) from exc
            if len(codes) > 1 and codes[1] == "ConditionalCheckFailed":
                raise errors.ResourceNotFoundError(
                    f"재발급할 키가 이미 없다: {rotated.key_id}"
                ) from exc
            raise

    # -- 삭제 (참조 무결성) --------------------------------------------------

    def _conditional_delete(
        self, *, partition: str, sort: str, not_found: str
    ) -> None:
        """대상이 아직 존재할 때만 삭제한다.

        조회-후-삭제 사이에 다른 요청이 같은 항목을 지웠을 수 있다. 조건 없이
        지우면 그 경우에도 조용히 204 를 돌려줘, 두 삭제가 모두 성공한 것처럼
        보인다. `attribute_exists` 조건을 걸어, 이미 사라진 항목의 삭제는
        404 로 드러낸다. 이것으로 부모 삭제 쪽 경쟁 창을 좁힌다. 자식이 그
        사이 새로 생기는 방향까지 완전히 막으려면 soft delete 나 참조 카운터가
        필요하다(후속 과제).

        Args:
            partition: 파티션 키.
            sort: 정렬 키.
            not_found: 조건 실패 시 낼 404 메시지.

        Raises:
            ResourceNotFoundError: 삭제 직전 항목이 이미 사라진 경우.
        """
        try:
            self._table.delete_item(
                Key={"pk": partition, "sk": sort},
                ConditionExpression="attribute_exists(pk)",
            )
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                raise errors.ResourceNotFoundError(not_found) from exc
            raise

    def delete_account(self, account_id: str) -> None:
        """계정을 삭제한다.

        하위에 팀·사용자·키가 하나라도 남아 있으면 삭제하지 않는다. 고아
        레코드가 생기면 사용량 집계의 이름 조인이 깨지고, 같은 ID 를 재생성했을
        때 옛 하위 리소스가 되살아나기 때문이다.

        Args:
            account_id: 삭제할 계정 ID.

        Raises:
            ResourceNotFoundError: 계정이 없는 경우.
            ResourceConflictError: 하위 팀·사용자·키 또는 인증 설정이 남아
                있는 경우.
        """
        if self.get_account(account_id) is None:
            raise errors.ResourceNotFoundError(
                f"계정을 찾을 수 없다: {account_id}"
            )
        if self.list_teams(account_id):
            raise errors.ResourceConflictError(
                f"팀이 남아 있어 계정을 삭제할 수 없다: {account_id}"
            )
        if self.list_users(account_id):
            raise errors.ResourceConflictError(
                f"사용자가 남아 있어 계정을 삭제할 수 없다: {account_id}"
            )
        if self.list_api_keys(account_id):
            raise errors.ResourceConflictError(
                f"API 키가 남아 있어 계정을 삭제할 수 없다: {account_id}"
            )
        # 인증 설정을 남긴 채 계정을 지우면 발급자 역인덱스가 고아로 남아
        # 다른 계정이 그 발급자를 등록할 수 없게 된다. 다른 하위 리소스와
        # 같은 정책으로 거부해, 조용한 데이터 유실 대신 명시적 삭제를
        # 요구한다.
        if self.get_auth_config(account_id) is not None:
            raise errors.ResourceConflictError(
                "인증 설정이 남아 있어 계정을 삭제할 수 없다:" f" {account_id}"
            )
        self._conditional_delete(
            partition=account_pk(account_id),
            sort=_META_SORT_KEY,
            not_found=f"계정을 찾을 수 없다: {account_id}",
        )

    def delete_team(self, account_id: str, team_id: str) -> None:
        """팀을 삭제한다.

        그 팀에 소속된 사용자나 키가 남아 있으면 삭제하지 않는다. 소속을
        먼저 옮기거나 지워야 한다.

        Args:
            account_id: 계정 ID.
            team_id: 삭제할 팀 ID.

        Raises:
            ResourceNotFoundError: 팀이 없는 경우.
            ResourceConflictError: 그 팀 소속 사용자·키가 남아 있는 경우.
        """
        if self.get_team(account_id, team_id) is None:
            raise errors.ResourceNotFoundError(
                f"팀을 찾을 수 없다: account={account_id} team={team_id}"
            )
        if any(user.team_id == team_id for user in self.list_users(account_id)):
            raise errors.ResourceConflictError(
                f"소속 사용자가 남아 있어 팀을 삭제할 수 없다: {team_id}"
            )
        if any(
            key.team_id == team_id for key in self.list_api_keys(account_id)
        ):
            raise errors.ResourceConflictError(
                f"소속 API 키가 남아 있어 팀을 삭제할 수 없다: {team_id}"
            )
        self._conditional_delete(
            partition=account_pk(account_id),
            sort=team_sk(team_id),
            not_found=f"팀을 찾을 수 없다: account={account_id} team={team_id}",
        )

    def delete_user(self, account_id: str, user_id: str) -> None:
        """사용자를 삭제한다.

        그 사용자에게 발급된 키가 남아 있으면 삭제하지 않는다. 키를 먼저
        회수해야 한다.

        Args:
            account_id: 계정 ID.
            user_id: 삭제할 사용자 ID.

        Raises:
            ResourceNotFoundError: 사용자가 없는 경우.
            ResourceConflictError: 그 사용자 소유 키가 남아 있는 경우.
        """
        if self.get_user(account_id, user_id) is None:
            raise errors.ResourceNotFoundError(
                f"사용자를 찾을 수 없다: account={account_id} user={user_id}"
            )
        if any(
            key.user_id == user_id for key in self.list_api_keys(account_id)
        ):
            raise errors.ResourceConflictError(
                f"소유 API 키가 남아 있어 사용자를 삭제할 수 없다: {user_id}"
            )
        self._conditional_delete(
            partition=account_pk(account_id),
            sort=user_sk(user_id),
            not_found=(
                f"사용자를 찾을 수 없다: account={account_id} user={user_id}"
            ),
        )

    def touch_api_key(self, key_hash: str, used_at: str) -> None:
        """API 키의 마지막 사용 시각을 갱신한다.

        관리 화면에서 유휴 키를 찾기 위한 부가 정보다. 이 호출이 실패해도
        요청 처리에는 영향이 없어야 하므로 호출자가 예외를 삼킨다.

        Args:
            key_hash: 키 해시.
            used_at: ISO-8601 UTC 시각.
        """
        self._table.update_item(
            Key={"pk": key_pk(key_hash), "sk": _META_SORT_KEY},
            UpdateExpression="SET #last_used_at = :used_at",
            ExpressionAttributeNames={"#last_used_at": "last_used_at"},
            ExpressionAttributeValues={":used_at": used_at},
            # 삭제된 키를 되살리지 않기 위해 존재할 때만 갱신한다.
            ConditionExpression="attribute_exists(pk)",
        )

    # -- 내부 ---------------------------------------------------------------

    def _put(self, item: _JsonDict, *, overwrite: bool, label: str) -> None:
        """생성·갱신을 구분하는 조건부 PutItem 공통 처리.

        생성은 대상이 없어야 하고, 갱신은 대상이 있어야 한다. 갱신 전에
        다른 요청이 항목을 삭제했는데 무조건 Put 하면 삭제된 리소스를
        되살리므로 두 방향 모두 조건을 건다.
        """
        condition = (
            "attribute_exists(pk)" if overwrite else "attribute_not_exists(pk)"
        )
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression=condition,
            )
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                if overwrite:
                    raise errors.ResourceNotFoundError(
                        f"수정할 리소스를 찾을 수 없다: {label} "
                        f"{item['pk']}/{item['sk']}"
                    ) from exc
                raise errors.ResourceConflictError(
                    f"이미 존재하는 {label}: {item['pk']}/{item['sk']}"
                ) from exc
            raise

    def _get(self, partition: str, sort: str) -> _JsonDict | None:
        """단일 GetItem."""
        response = self._table.get_item(Key={"pk": partition, "sk": sort})
        item = response.get("Item")
        return typing.cast("_JsonDict | None", item)

    def _query_table(
        self, *, key_condition: str, values: _JsonDict
    ) -> list[_JsonDict]:
        """기본 테이블 Query. 페이지를 모두 모아 반환한다."""
        return self._query(
            key_condition=key_condition, values=values, index_name=None
        )

    def _query_index(
        self, *, key_condition: str, values: _JsonDict
    ) -> list[_JsonDict]:
        """GSI Query. 페이지를 모두 모아 반환한다."""
        return self._query(
            key_condition=key_condition, values=values, index_name=_GSI1_NAME
        )

    def _query(
        self,
        *,
        key_condition: str,
        values: _JsonDict,
        index_name: str | None,
    ) -> list[_JsonDict]:
        """Query 를 페이지 상한까지 반복 호출한다."""
        collected: list[_JsonDict] = []
        start_key: _JsonDict | None = None
        for _ in range(_MAX_PAGES):
            kwargs: _JsonDict = {
                "KeyConditionExpression": key_condition,
                "ExpressionAttributeValues": values,
            }
            if index_name is not None:
                kwargs["IndexName"] = index_name
            if start_key is not None:
                kwargs["ExclusiveStartKey"] = start_key
            response = self._table.query(**kwargs)
            collected.extend(response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if start_key is None:
                break
        return collected

    @staticmethod
    def _to_account(item: _JsonDict) -> domain.Account:
        """아이템을 계정 도메인 객체로 변환한다."""
        return domain.Account(
            account_id=str(item["account_id"]),
            name=str(item.get("display_name", item["account_id"])),
            monthly_budget_usd=_optional_decimal(
                item.get("monthly_budget_usd")
            ),
            status=domain.EntityStatus(item.get("status", "active")),
            created_at=str(item.get("created_at", "")),
        )

    @staticmethod
    def _to_team(item: _JsonDict) -> domain.Team:
        """아이템을 팀 도메인 객체로 변환한다."""
        return domain.Team(
            account_id=str(item["account_id"]),
            team_id=str(item["team_id"]),
            name=str(item.get("display_name", item["team_id"])),
            monthly_budget_usd=_optional_decimal(
                item.get("monthly_budget_usd")
            ),
            status=domain.EntityStatus(item.get("status", "active")),
            created_at=str(item.get("created_at", "")),
        )

    @staticmethod
    def _to_user(item: _JsonDict) -> domain.User:
        """아이템을 사용자 도메인 객체로 변환한다."""
        return domain.User(
            account_id=str(item["account_id"]),
            user_id=str(item["user_id"]),
            name=str(item.get("display_name", item["user_id"])),
            email=str(item.get("email", "")),
            team_id=str(item.get("team_id", "")),
            monthly_budget_usd=_optional_decimal(
                item.get("monthly_budget_usd")
            ),
            rpm_limit=_optional_int(item.get("rpm_limit")),
            status=domain.EntityStatus(item.get("status", "active")),
            created_at=str(item.get("created_at", "")),
        )

    @staticmethod
    def _to_api_key(item: _JsonDict) -> domain.ApiKey:
        """아이템을 API 키 도메인 객체로 변환한다."""
        return domain.ApiKey(
            key_id=str(item["key_id"]),
            key_hash=str(item["key_hash"]),
            key_prefix=str(item.get("key_prefix", "")),
            account_id=str(item["account_id"]),
            team_id=str(item.get("team_id", "")),
            user_id=str(item["user_id"]),
            name=str(item.get("display_name", "")),
            allowed_models=tuple(
                str(value) for value in item.get("allowed_models", [])
            ),
            monthly_budget_usd=_optional_decimal(
                item.get("monthly_budget_usd")
            ),
            rpm_limit=_optional_int(item.get("rpm_limit")),
            expires_at=str(item.get("expires_at", "")),
            status=domain.EntityStatus(item.get("status", "active")),
            created_at=str(item.get("created_at", "")),
            last_used_at=str(item.get("last_used_at", "")),
        )


# ---------------------------------------------------------------------------
# 사용량 저장소
# ---------------------------------------------------------------------------


class UsageStore:
    """사용량 원본 기록과 집계를 함께 다루는 저장소.

    원본 쓰기와 집계 갱신을 하나의 `TransactWriteItems` 로 묶는다. 이유는
    두 가지다.

    1. 멱등성. 원본 쓰기에 `attribute_not_exists` 조건을 걸어 두면 중복
       요청에서 트랜잭션 전체가 취소되므로 집계가 두 번 더해지지 않는다.
    2. 지연. 집계 축이 10개라 개별 UpdateItem 을 순차 호출하면 왕복이
       10번 발생한다. 트랜잭션은 1회로 끝난다.

    대가로 트랜잭션 쓰기는 일반 쓰기의 2배 용량을 소비한다. 요청당 약
    22 WRU 를 쓰며, 온디맨드 기준 100만 요청에 대략 27 USD 수준이다.
    """

    def __init__(
        self,
        *,
        usage_table: typing.Any,
        agg_table: typing.Any,
        client: typing.Any,
        usage_ttl_days: int,
    ) -> None:
        """저장소를 만든다.

        Args:
            usage_table: usage 테이블 리소스. 편의 조회에 쓴다.
            agg_table: usage-agg 테이블 리소스. 테이블 이름 확인용이다.
            client: 저수준 DynamoDB 클라이언트. 트랜잭션과 병렬 Query 에
                쓴다. 리소스의 `meta.client` 를 넘기면 안 된다. 이유는
                `create_dynamodb_client` 문서를 참고한다.
            usage_ttl_days: 원본 레코드 보존 기간(일).
        """
        self._usage_table = usage_table
        self._usage_ttl_days = usage_ttl_days
        self._client = client
        self._usage_table_name = usage_table.name
        self._agg_table_name = agg_table.name

    def try_consume_rate_limit(
        self,
        *,
        account_id: str,
        scope: str,
        minute: str,
        limit: int,
        now: datetime.datetime,
    ) -> bool:
        """분당 요청 한도를 원자적으로 소비한다.

        `ADD` 와 조건식을 한 번의 `UpdateItem` 으로 묶어, 태스크가 여러 개여도
        정확히 센다. 태스크별 인메모리 카운터를 쓰면 한도가 태스크 수만큼
        늘어나고 오토스케일링으로 그 배수가 계속 바뀐다.

        카운터는 별도 테이블을 만들지 않고 usage 테이블의 다른 파티션
        namespace 를 쓴다. 이 테이블에는 이미 TTL(`expires_at`)이 걸려 있어
        만료된 분 단위 카운터가 자동으로 사라진다. 파티션 키에 호출 주체가
        들어가므로 특정 분에 부하가 몰리는 핫 파티션이 생기지 않는다.

        Args:
            account_id: 계정 ID.
            scope: 카운터 단위. `KEY#<id>` 또는 `USER#<id>`.
            minute: `YYYY-MM-DDTHH:MM` 형태의 분 버킷.
            limit: 분당 허용 요청 수.
            now: 현재 시각. TTL 계산에 쓴다.

        Returns:
            소비에 성공하면 `True`, 한도를 이미 채웠으면 `False`.

        Raises:
            ClientError: 조건 실패 외의 DynamoDB 오류.
        """
        # 분 버킷은 2분 뒤에 지운다. 경계에서 시계가 조금 어긋나도 직전 분
        # 카운터가 남아 있어야 한다.
        expires_at = int(now.timestamp()) + _RATE_LIMIT_TTL_SECONDS
        try:
            self._usage_table.update_item(
                Key={
                    "pk": rate_limit_pk(account_id, scope),
                    "sk": minute,
                },
                UpdateExpression=(
                    "ADD #count :one SET expires_at = :expires_at"
                ),
                ConditionExpression=(
                    "attribute_not_exists(#count) OR #count < :limit"
                ),
                ExpressionAttributeNames={"#count": "request_count"},
                ExpressionAttributeValues={
                    ":one": 1,
                    ":limit": limit,
                    ":expires_at": expires_at,
                },
            )
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def record(self, usage: domain.UsageRecord) -> bool:
        """사용량을 기록하고 집계를 갱신한다.

        Bedrock 호출은 한 번마다 실제 비용이 발생하므로, 호출이 있었다면
        집계는 반드시 늘어야 한다. 그래서 저장소 키는 서버가 만든
        `usage_id` 이고, 클라이언트가 보낸 `request_id` 로는 기록을 건너뛰지
        않는다.

        `ClientRequestToken` 을 함께 넘겨 네트워크 오류로 같은
        `TransactWriteItems` 가 재전송되더라도 집계가 두 번 더해지지 않게
        한다(DynamoDB 가 10분간 같은 토큰의 재요청을 멱등 처리한다).

        Args:
            usage: 기록할 사용량 레코드.

        Returns:
            새로 기록되면 `True`. 같은 `usage_id` 가 이미 있으면 `False`.

        Raises:
            ClientError: 조건 실패 외의 DynamoDB 오류.
        """
        transact_items = [self._usage_put_item(usage)]
        transact_items.extend(self._aggregate_update_items(usage))
        try:
            self._client.transact_write_items(
                TransactItems=transact_items,
                ClientRequestToken=usage.usage_id,
            )
        except botocore.exceptions.ClientError as exc:
            if self._is_duplicate_cancellation(exc):
                return False
            raise
        return True

    def list_records(
        self, account_id: str, day: str, *, limit: int = 50
    ) -> list[_JsonDict]:
        """특정 날짜의 최근 요청 레코드를 시간 역순으로 반환한다.

        Args:
            account_id: 계정 ID.
            day: `YYYY-MM-DD` 날짜.
            limit: 최대 개수.

        Returns:
            LSI 로 시간 역순 정렬된 원본 아이템 목록.
        """
        response = self._usage_table.query(
            IndexName=_TS_INDEX_NAME,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": usage_pk(account_id, day)},
            ScanIndexForward=False,
            Limit=max(1, min(limit, 200)),
        )
        return list(response.get("Items", []))

    def query_totals(
        self,
        account_id: str,
        granularity: domain.Granularity,
        period: str,
    ) -> dict[str, domain.UsageTotals]:
        """한 기간 파티션의 모든 축 집계를 읽는다.

        Args:
            account_id: 계정 ID.
            granularity: 시간 단위.
            period: 기간 키.

        Returns:
            정렬 키(`TOTAL`, `TEAM#...` 등)를 키로 갖는 집계 매핑.
        """
        partition = agg_pk(account_id, granularity, period)
        return self.query_partitions([partition]).get(partition, {})

    def query_partitions(
        self, partition_keys: typing.Sequence[str]
    ) -> dict[str, dict[str, domain.UsageTotals]]:
        """여러 집계 파티션을 병렬로 읽는다.

        대시보드는 30일 범위를 조회할 때 파티션 30개를 읽는다. 순차 호출은
        왕복 지연이 그대로 누적되므로 스레드 풀로 겹친다.

        boto3 의 리소스 객체는 스레드 안전이 보장되지 않는다. 그래서 이
        메서드는 스레드 안전한 저수준 클라이언트만 사용한다.

        Args:
            partition_keys: 집계 테이블 파티션 키 목록.

        Returns:
            파티션 키 → (정렬 키 → 집계) 이중 매핑. 데이터가 없는 파티션은
            빈 딕셔너리로 채워진다.

        Raises:
            ClientError: DynamoDB 호출이 실패한 경우.
        """
        unique_keys = list(dict.fromkeys(partition_keys))
        if not unique_keys:
            return {}
        if len(unique_keys) == 1:
            return {unique_keys[0]: self._query_partition(unique_keys[0])}

        worker_count = min(_MAX_QUERY_WORKERS, len(unique_keys))
        with futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
            submitted = {
                pool.submit(self._query_partition, partition): partition
                for partition in unique_keys
            }
            results: dict[str, dict[str, domain.UsageTotals]] = {}
            for future in futures.as_completed(submitted):
                # 예외는 그대로 전파시킨다. 일부 파티션만 읽힌 결과로
                # 대시보드에 잘못된 합계를 보여주는 것이 더 위험하다.
                results[submitted[future]] = future.result()
        return results

    def _query_partition(self, partition: str) -> dict[str, domain.UsageTotals]:
        """집계 파티션 하나를 페이지 끝까지 읽는다."""
        deserializer = dynamodb_types.TypeDeserializer()
        collected: dict[str, domain.UsageTotals] = {}
        start_key: _LowLevelItem | None = None
        for _ in range(_MAX_PAGES):
            kwargs: _JsonDict = {
                "TableName": self._agg_table_name,
                "KeyConditionExpression": "pk = :pk",
                "ExpressionAttributeValues": {":pk": {"S": partition}},
            }
            if start_key is not None:
                kwargs["ExclusiveStartKey"] = start_key
            response = self._client.query(**kwargs)
            for raw_item in response.get("Items", []):
                item = {
                    name: deserializer.deserialize(value)
                    for name, value in raw_item.items()
                }
                collected[str(item["sk"])] = _totals_from_item(item)
            start_key = response.get("LastEvaluatedKey")
            if start_key is None:
                break
        return collected

    def get_totals(
        self,
        account_id: str,
        granularity: domain.Granularity,
        period: str,
        sort_keys: typing.Sequence[str],
    ) -> dict[str, domain.UsageTotals]:
        """지정한 축들의 집계만 골라 읽는다.

        예산 검사는 전체 축이 아니라 4개 축만 필요하다. 파티션 전체를
        Query 하면 모델·사용자 행까지 다 읽게 되므로 BatchGetItem 으로
        필요한 것만 가져온다.

        Args:
            account_id: 계정 ID.
            granularity: 시간 단위.
            period: 기간 키.
            sort_keys: 읽을 정렬 키 목록.

        Returns:
            존재하는 항목만 담긴 집계 매핑. 없는 축은 키가 빠진다.
        """
        unique_keys = list(dict.fromkeys(sort_keys))
        if not unique_keys:
            return {}
        partition = agg_pk(account_id, granularity, period)
        response = self._client.batch_get_item(
            RequestItems={
                self._agg_table_name: {
                    "Keys": [
                        {
                            "pk": {"S": partition},
                            "sk": {"S": sort_key},
                        }
                        for sort_key in unique_keys
                    ]
                }
            }
        )
        deserializer = dynamodb_types.TypeDeserializer()
        results: dict[str, domain.UsageTotals] = {}
        raw_items = response.get("Responses", {}).get(self._agg_table_name, [])
        for raw_item in raw_items:
            item = {
                name: deserializer.deserialize(value)
                for name, value in raw_item.items()
            }
            results[str(item["sk"])] = _totals_from_item(item)
        return results

    # -- 내부 ---------------------------------------------------------------

    def _usage_put_item(self, usage: domain.UsageRecord) -> _LowLevelItem:
        """원본 레코드 조건부 Put 트랜잭션 항목을 만든다."""
        moment = datetime.datetime.fromisoformat(
            usage.timestamp.replace("Z", "+00:00")
        )
        expires_at = int(moment.timestamp()) + (
            self._usage_ttl_days * _SECONDS_PER_DAY
        )
        item: _JsonDict = {
            "pk": usage_pk(usage.account_id, clock.day_key(moment)),
            "sk": usage.usage_id,
            "ts": usage.timestamp,
            "request_id": usage.request_id,
            "account_id": usage.account_id,
            "team_id": usage.team_id,
            "user_id": usage.user_id,
            "key_id": usage.key_id,
            "model_id": usage.model_id,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "cost_usd": usage.cost_usd,
            "latency_ms": usage.latency_ms,
            "status_code": usage.status_code,
            "error_code": usage.error_code,
            "streamed": usage.streamed,
            "pricing_known": usage.pricing_known,
            "expires_at": expires_at,
        }
        return {
            "Put": {
                "TableName": self._usage_table_name,
                "Item": {
                    name: _serialize(value) for name, value in item.items()
                },
                # 서버가 만든 usage_id 라 정상 흐름에서는 절대 충돌하지
                # 않는다. 같은 키가 이미 있다는 것은 같은 트랜잭션이 두 번
                # 적용되려는 상황이므로 막는다.
                "ConditionExpression": "attribute_not_exists(sk)",
            }
        }

    def _aggregate_update_items(
        self, usage: domain.UsageRecord
    ) -> list[_LowLevelItem]:
        """집계 ADD 트랜잭션 항목들을 만든다.

        일 단위와 월 단위 각각에 대해 전체·팀·사용자·모델·키 축 행을
        갱신한다. 값이 없는 축(팀 미지정 등)은 건너뛴다.

        Args:
            usage: 사용량 레코드.

        Returns:
            트랜잭션 항목 리스트.
        """
        moment = datetime.datetime.fromisoformat(
            usage.timestamp.replace("Z", "+00:00")
        )
        periods = (
            (domain.Granularity.DAY, clock.day_key(moment)),
            (domain.Granularity.MONTH, clock.month_key(moment)),
        )
        axes: list[tuple[domain.BreakdownDimension | None, str]] = [(None, "")]
        if usage.team_id:
            axes.append((domain.BreakdownDimension.TEAM, usage.team_id))
        if usage.user_id:
            axes.append((domain.BreakdownDimension.USER, usage.user_id))
        if usage.model_id:
            axes.append((domain.BreakdownDimension.MODEL, usage.model_id))
        if usage.key_id:
            axes.append((domain.BreakdownDimension.KEY, usage.key_id))

        items: list[_LowLevelItem] = []
        for granularity, period in periods:
            partition = agg_pk(usage.account_id, granularity, period)
            for dimension, value in axes:
                items.append(
                    self._agg_update_item(
                        partition=partition,
                        sort_key=dimension_sk(dimension, value),
                        dimension=dimension,
                        dimension_value=value,
                        usage=usage,
                    )
                )
        return items

    def _agg_update_item(
        self,
        *,
        partition: str,
        sort_key: str,
        dimension: domain.BreakdownDimension | None,
        dimension_value: str,
        usage: domain.UsageRecord,
    ) -> _LowLevelItem:
        """집계 행 하나에 대한 ADD 업데이트 항목을 만든다."""
        success_delta = 1 if usage.is_success else 0
        error_delta = 0 if usage.is_success else 1
        # 단가를 모르는 모델은 비용이 0으로 기록된다. 그 사실을 집계에 남겨
        # 대시보드가 "비용이 실제보다 작다"고 알릴 수 있게 한다.
        unpriced_delta = 0 if usage.pricing_known else 1
        values: _JsonDict = {
            ":one": 1,
            ":success": success_delta,
            ":error": error_delta,
            ":unpriced": unpriced_delta,
            ":input_tokens": usage.input_tokens,
            ":output_tokens": usage.output_tokens,
            ":cost_usd": usage.cost_usd,
            ":latency_ms": usage.latency_ms,
            ":dimension": (
                dimension.value if dimension is not None else "total"
            ),
            ":dimension_value": dimension_value,
            ":updated_at": usage.timestamp,
        }
        return {
            "Update": {
                "TableName": self._agg_table_name,
                "Key": {
                    "pk": {"S": partition},
                    "sk": {"S": sort_key},
                },
                "UpdateExpression": (
                    "SET #dimension = :dimension,"
                    " #dimension_value = :dimension_value,"
                    " #updated_at = :updated_at"
                    " ADD #requests :one,"
                    " #success_requests :success,"
                    " #error_requests :error,"
                    " #input_tokens :input_tokens,"
                    " #output_tokens :output_tokens,"
                    " #cost_usd :cost_usd,"
                    " #latency_ms_sum :latency_ms,"
                    " #unpriced_requests :unpriced"
                ),
                "ExpressionAttributeNames": {
                    "#dimension": "dimension",
                    "#dimension_value": "dimension_value",
                    "#updated_at": "updated_at",
                    "#requests": "requests",
                    "#success_requests": "success_requests",
                    "#error_requests": "error_requests",
                    "#input_tokens": "input_tokens",
                    "#output_tokens": "output_tokens",
                    "#cost_usd": "cost_usd",
                    "#latency_ms_sum": "latency_ms_sum",
                    "#unpriced_requests": "unpriced_requests",
                },
                "ExpressionAttributeValues": {
                    name: _serialize(value) for name, value in values.items()
                },
            }
        }

    @staticmethod
    def _is_duplicate_cancellation(
        exc: botocore.exceptions.ClientError,
    ) -> bool:
        """트랜잭션 취소 원인이 중복 요청인지 판별한다.

        `TransactionCanceledException` 은 조건 실패, 용량 초과, 충돌 등
        여러 이유로 발생한다. 첫 번째 항목(원본 레코드 Put)의 취소 사유가
        `ConditionalCheckFailed` 인 경우만 중복으로 간주한다. 그 외는
        예외를 그대로 올려 재시도나 알람으로 이어지게 한다.

        Args:
            exc: 발생한 ClientError.

        Returns:
            중복 요청이면 `True`.
        """
        error = exc.response.get("Error", {})
        if error.get("Code") != "TransactionCanceledException":
            return False
        reasons = exc.response.get("CancellationReasons") or []
        if not reasons:
            return False
        first_reason = reasons[0] or {}
        if first_reason.get("Code") != "ConditionalCheckFailed":
            return False
        # 다른 항목이 다른 이유로 실패했다면 중복이 아니라 실제 오류다.
        return all(
            (reason or {}).get("Code") in (None, "None", "")
            for reason in reasons[1:]
        )
