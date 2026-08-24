#!/usr/bin/env bash
#
# requirements.txt 의 직접 의존성으로부터 전이 의존성까지 고정한
# requirements.lock 을 재생성한다.
#
# 깨끗한 임시 가상환경에 requirements.txt 만 설치한 뒤 freeze 하기 때문에,
# 개발 도구(pytest, mypy 등)가 lock 파일에 섞여 들어가지 않는다.
# 컨테이너 이미지는 이 lock 파일만 사용한다.
#
# 사용법:
#   ./scripts/lock_requirements.sh [python 실행파일]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT
readonly PYTHON_BIN="${1:-python3.13}"
readonly LOCK_FILE="${REPO_ROOT}/requirements.lock"

tmp_venv="$(mktemp -d)"
# 성공/실패 어느 경로로 끝나도 임시 가상환경을 남기지 않는다.
trap 'rm -rf "${tmp_venv}"' EXIT

echo "==> 임시 가상환경 생성: ${tmp_venv}"
"${PYTHON_BIN}" -m venv "${tmp_venv}"
"${tmp_venv}/bin/python" -m pip install --quiet --upgrade pip

echo "==> requirements.txt 설치"
"${tmp_venv}/bin/python" -m pip install --quiet \
    -r "${REPO_ROOT}/requirements.txt"

echo "==> ${LOCK_FILE} 생성"
{
    echo "# 자동 생성 파일. 직접 편집하지 않는다."
    echo "# 재생성: ./scripts/lock_requirements.sh"
    echo "# python: $("${tmp_venv}/bin/python" --version 2>&1)"
    "${tmp_venv}/bin/python" -m pip freeze --exclude-editable |
        grep -v '^pip==' | sort -f
} >"${LOCK_FILE}"

echo "==> 완료. $(grep -vc '^#' "${LOCK_FILE}") 개 패키지 고정"
