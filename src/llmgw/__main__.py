"""로컬 개발용 실행 진입점.

컨테이너는 이 모듈을 쓰지 않고 uvicorn 을 직접 실행한다. Dockerfile 의
CMD 를 참고한다. 여기서는 로컬에서 `python -m llmgw` 한 줄로 띄울 수 있게만
한다.

0.0.0.0 에 바인딩하는 것은 컨테이너와 동일한 조건을 만들기 위한 것이다.
로컬에서 실행할 때는 인증 없이 접근 가능한 포트가 열리므로, 공용 네트워크
에서는 `LLMGW_BIND_HOST=127.0.0.1` 로 좁혀서 쓴다.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    """개발 서버를 실행한다."""
    uvicorn.run(
        "llmgw.app:create_app",
        factory=True,
        host=os.environ.get("LLMGW_BIND_HOST", "0.0.0.0"),  # noqa: S104
        port=int(os.environ.get("LLMGW_PORT", "8080")),
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
