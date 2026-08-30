"""Amazon Bedrock 앞단에 놓이는 OpenAI 호환 LLM Gateway.

이 패키지는 세 가지 책임을 가진다.

1. OpenAI Chat Completions 스펙과 호환되는 HTTP 인터페이스를 제공하고,
   요청을 Bedrock Converse API로 변환해 중계한다.
2. 요청 단위로 계정(account) / 팀(team) / 사용자(user) / 모델 축의
   토큰·비용·지연·에러를 DynamoDB에 기록하고 집계한다.
3. 집계 결과를 조회하는 관리 API와 대시보드 UI를 제공한다.

레이어 구조는 아래와 같다. 위쪽이 바깥이다.

    routers/        HTTP 경계. 검증과 직렬화만 담당한다.
    auth, usage,    도메인 서비스. AWS SDK를 직접 다루지 않는다.
    analytics
    repository,     AWS 어댑터. boto3 호출이 여기에만 존재한다.
    bedrock
"""

__all__ = ["__version__"]

__version__ = "1.16.0"
