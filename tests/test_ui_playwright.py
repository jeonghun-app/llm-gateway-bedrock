"""실제 Chromium에서 실행하는 관리 UI 핵심 회귀 테스트.

정적 UI는 로컬 HTTP 서버로 제공하고 관리·분석 API만 Playwright 라우팅으로
대체한다. 백엔드 구현을 다시 검증하기보다 실제 브라우저의 DOM, 이벤트,
CSS와 비동기 fetch 흐름을 확인하는 것이 목적이다.
"""

from __future__ import annotations

import dataclasses
import functools
import http.server
import json
import pathlib
import threading
import typing
import urllib.parse

from playwright import sync_api
import pytest

_STATIC_DIR = pathlib.Path(__file__).resolve().parents[1] / "src/llmgw/static"
_ADMIN_TOKEN = "test-admin-token"

pytestmark = pytest.mark.browser

_JsonDict = dict[str, typing.Any]


class _QuietStaticHandler(http.server.SimpleHTTPRequestHandler):
    """테스트 출력에 정적 파일 접근 로그를 남기지 않는다."""

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.fixture(scope="module")
def static_url() -> typing.Iterator[str]:
    """정적 UI를 임의의 localhost 포트에서 제공한다."""
    handler = functools.partial(
        _QuietStaticHandler,
        directory=str(_STATIC_DIR),
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever,
        name="llmgw-playwright-static",
        daemon=True,
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/index.html"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def chromium_browser() -> typing.Iterator[sync_api.Browser]:
    """설치된 Playwright Chromium을 한 번 띄워 모듈 전체에서 공유한다."""
    with sync_api.sync_playwright() as playwright:
        executable = pathlib.Path(playwright.chromium.executable_path)
        if not executable.is_file():
            pytest.skip(
                "Chromium이 없다. "
                "`python -m playwright install chromium`을 실행한다."
            )
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


class _ApiStub:
    """관리 UI가 호출하는 API의 인메모리 대역."""

    def __init__(self) -> None:
        self.accounts: list[_JsonDict] = [
            {
                "account_id": "acme",
                "name": "Acme",
                "status": "active",
                "monthly_budget_usd": None,
            }
        ]
        self.keys: list[_JsonDict] = [
            {
                "key_id": "key-1",
                "key_prefix": "sk-llmgw-test-abcd",
                "name": "기존 키",
                "account_id": "acme",
                "team_id": "platform",
                "user_id": "alice",
                "allowed_models": [],
                "monthly_budget_usd": None,
                "status": "active",
                "last_used_at": "",
            }
        ]
        # 계정별 외부 인증 설정. 처음에는 연결되지 않은 상태다.
        self.auth_config: _JsonDict | None = None
        self.calls: list[tuple[str, str, _JsonDict | None]] = []
        self._failure: tuple[str, str, int, str] | None = None

    def fail_once(
        self,
        method: str,
        path: str,
        *,
        status: int,
        message: str,
    ) -> None:
        """다음 일치 요청 한 건만 지정한 오류로 응답한다."""
        self._failure = (method, path, status, message)

    def handle(self, route: sync_api.Route, request: sync_api.Request) -> None:
        """Playwright route 콜백으로 요청을 처리한다."""
        method = request.method
        path = urllib.parse.urlsplit(request.url).path
        body = self._request_body(request)
        self.calls.append((method, path, body))

        if self._failure is not None:
            failed_method, failed_path, status, message = self._failure
            if (method, path) == (failed_method, failed_path):
                self._failure = None
                self._respond(
                    route,
                    status,
                    {"error": {"message": message}},
                )
                return

        if path == "/admin/accounts":
            if method == "GET":
                self._respond(route, 200, {"data": self.accounts})
                return
            if method == "POST":
                assert body is not None
                created: _JsonDict = {
                    "account_id": body["account_id"],
                    "name": body["name"],
                    "status": "active",
                    "monthly_budget_usd": body["monthly_budget_usd"],
                }
                self.accounts.append(created)
                self._respond(route, 201, created)
                return

        account_path = path.removeprefix("/admin/accounts/")
        if "/" not in account_path and account_path != path:
            account_id = urllib.parse.unquote(account_path)
            if method == "PATCH":
                assert body is not None
                account = self._account(account_id)
                account.update(body)
                self._respond(route, 200, account)
                return
            if method == "DELETE":
                self.accounts = [
                    item
                    for item in self.accounts
                    if item["account_id"] != account_id
                ]
                self._respond(route, 204, None)
                return

        if path.endswith("/teams") and method == "GET":
            self._respond(route, 200, {"data": []})
            return
        if path.endswith("/users") and method == "GET":
            self._respond(route, 200, {"data": []})
            return
        if path.endswith("/keys"):
            if method == "GET":
                self._respond(route, 200, {"data": self.keys})
                return
            if method == "POST":
                assert body is not None
                created = {
                    "key_id": "key-new",
                    "key_prefix": "sk-llmgw-test-new0",
                    "name": body["name"],
                    "account_id": "acme",
                    "team_id": "platform",
                    "user_id": body["user_id"],
                    "allowed_models": body["allowed_models"],
                    "monthly_budget_usd": body["monthly_budget_usd"],
                    "status": "active",
                    "last_used_at": "",
                }
                self.keys.append(created)
                self._respond(
                    route,
                    201,
                    {
                        **created,
                        "api_key": "sk-llmgw-test-PLAINTEXT-NEW",
                    },
                )
                return

        if path.endswith("/keys/key-1/rotate") and method == "POST":
            self._respond(
                route,
                200,
                {
                    **self.keys[0],
                    "key_prefix": "sk-llmgw-test-rot0",
                    "api_key": "sk-llmgw-test-PLAINTEXT-ROTATED",
                },
            )
            return

        if path.endswith("/auth") and method == "GET":
            if self.auth_config is None:
                self._respond(
                    route, 200, {"account_id": "acme", "configured": False}
                )
            else:
                self._respond(
                    route, 200, {**self.auth_config, "configured": True}
                )
            return

        if path.endswith("/auth") and method == "PUT":
            body = self._request_body(request) or {}
            self.auth_config = {
                "account_id": "acme",
                "issuer": body.get("issuer", ""),
                "jwks_url": body.get("jwks_url", ""),
                "effective_jwks_url": (
                    body.get("jwks_url")
                    or body.get("issuer", "") + "/.well-known/jwks.json"
                ),
                "audience": body.get("audience", ""),
                "user_claim": body.get("user_claim", "email"),
                "team_claim": body.get("team_claim", ""),
                "groups_claim": body.get("groups_claim", "cognito:groups"),
                "admin_groups": body.get("admin_groups", ""),
                "auto_provision": bool(body.get("auto_provision")),
                "provision_allowed_models": body.get(
                    "provision_allowed_models", ""
                ),
                "provision_budget_usd": body.get("provision_budget_usd"),
                "status": "active",
                "created_at": "2026-08-29T00:00:00Z",
                "updated_at": "2026-08-29T00:00:00Z",
            }
            self._respond(route, 200, self.auth_config)
            return

        if path.endswith("/auth/status") and method == "POST":
            body = self._request_body(request) or {}
            assert self.auth_config is not None
            self.auth_config["status"] = body.get("status", "active")
            self._respond(route, 200, self.auth_config)
            return

        if path.endswith("/auth") and method == "DELETE":
            self.auth_config = None
            self._respond(route, 204, None)
            return

        if path == "/analytics/dashboard" and method == "GET":
            self._respond(route, 200, self._dashboard())
            return
        if path == "/analytics/accounts" and method == "GET":
            self._respond(route, 200, {"data": []})
            return

        self._respond(
            route,
            404,
            {"error": {"message": f"테스트 대역에 없는 경로: {method} {path}"}},
        )

    def _account(self, account_id: str) -> _JsonDict:
        return next(
            item for item in self.accounts if item["account_id"] == account_id
        )

    @staticmethod
    def _request_body(request: sync_api.Request) -> _JsonDict | None:
        if not request.post_data:
            return None
        return typing.cast("_JsonDict", json.loads(request.post_data))

    @staticmethod
    def _respond(
        route: sync_api.Route,
        status: int,
        body: _JsonDict | None,
    ) -> None:
        if body is None:
            route.fulfill(status=status, body="")
            return
        route.fulfill(
            status=status,
            content_type="application/json; charset=utf-8",
            body=json.dumps(body, ensure_ascii=False),
        )

    @staticmethod
    def _dashboard() -> _JsonDict:
        totals = {
            "requests": 0,
            "success_requests": 0,
            "error_requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0,
            "avg_latency_ms": 0,
            "error_rate": 0,
            "unpriced_requests": 0,
        }
        return {
            "window": {"start": "2026-08-01", "end": "2026-08-29"},
            "totals": totals,
            "timeseries": [],
            "breakdowns": {
                "team": [],
                "user": [],
                "model": [],
                "key": [],
            },
            "recent_requests": [],
        }


@dataclasses.dataclass
class _UiSession:
    """테스트 한 건의 독립 브라우저 컨텍스트."""

    context: sync_api.BrowserContext
    page: sync_api.Page
    api: _ApiStub
    page_errors: list[str]

    def close(self) -> None:
        self.context.close()


def _open_session(
    browser: sync_api.Browser,
    static_url: str,
    *,
    viewport: dict[str, int] | None = None,
) -> _UiSession:
    context = browser.new_context(
        viewport=viewport or {"width": 1280, "height": 900},
    )
    page = context.new_page()
    api = _ApiStub()
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.route("**/admin/**", api.handle)
    page.route("**/analytics/**", api.handle)
    page.goto(static_url)
    return _UiSession(context, page, api, page_errors)


@pytest.fixture
def ui(
    chromium_browser: sync_api.Browser,
    static_url: str,
) -> typing.Iterator[_UiSession]:
    """데스크톱 크기의 독립 UI 세션을 제공한다."""
    session = _open_session(chromium_browser, static_url)
    try:
        yield session
        assert session.page_errors == []
    finally:
        session.close()


def _load_accounts(page: sync_api.Page) -> None:
    page.locator("#admin-token").fill(_ADMIN_TOKEN)
    page.locator("#refresh-button").click()
    sync_api.expect(page.locator("#account-select")).to_have_value("acme")
    sync_api.expect(page.locator("#status-line")).to_have_attribute(
        "data-kind", "ok"
    )


def _open_manage(page: sync_api.Page, view: str = "accounts") -> None:
    _load_accounts(page)
    page.locator("#view-manage").click()
    sync_api.expect(page.locator("#screen-manage")).to_be_visible()
    sync_api.expect(
        page.get_by_role("button", name="계정 만들기", exact=True)
    ).to_be_visible()
    if view != "accounts":
        page.locator(f"#mtab-{view}").click()


def test_모니터링과관리탭상태가서로독립적이다(ui: _UiSession) -> None:
    page = ui.page
    _load_accounts(page)

    page.locator("#tab-user").click()
    sync_api.expect(page.locator("#tab-user")).to_have_attribute(
        "aria-selected", "true"
    )

    page.locator("#view-manage").click()
    sync_api.expect(page.locator("#screen-manage")).to_be_visible()
    page.locator("#mtab-teams").click()
    sync_api.expect(page.locator("#mtab-teams")).to_have_attribute(
        "aria-selected", "true"
    )
    sync_api.expect(page.locator("#tab-user")).to_have_attribute(
        "aria-selected", "true"
    )

    page.locator("#view-monitor").click()
    sync_api.expect(page.locator("#screen-monitor")).to_be_visible()
    sync_api.expect(page.locator("#screen-manage")).to_be_hidden()
    sync_api.expect(page.locator("#view-monitor")).to_have_attribute(
        "aria-pressed", "true"
    )


def test_계정CRUD후상단선택목록이동기화된다(ui: _UiSession) -> None:
    page = ui.page
    _open_manage(page)

    page.get_by_role("button", name="계정 만들기", exact=True).click()
    dialog = page.get_by_role("dialog", name="계정 만들기")
    dialog.get_by_label("계정 ID").fill("beta")
    dialog.get_by_label("이름").fill("Beta")
    dialog.get_by_role("button", name="저장").click()

    beta_row = page.locator("#manage-panel tbody tr", has_text="beta")
    sync_api.expect(beta_row).to_contain_text("Beta")
    sync_api.expect(
        page.locator('#account-select option[value="beta"]')
    ).to_have_count(1)

    beta_row.get_by_role("button", name="수정").click()
    edit = page.get_by_role("dialog", name="계정 수정: beta")
    edit.get_by_label("이름").fill("Beta Updated")
    edit.get_by_role("button", name="저장").click()
    sync_api.expect(beta_row).to_contain_text("Beta Updated")
    sync_api.expect(
        page.locator('#account-select option[value="beta"]')
    ).to_have_text("Beta Updated (beta)")

    page.locator("#account-select").select_option("beta")
    beta_row.get_by_role("button", name="삭제").click()
    confirm = page.get_by_role("alertdialog")
    confirm.get_by_role("button", name="삭제").click()

    sync_api.expect(beta_row).to_have_count(0)
    sync_api.expect(
        page.locator('#account-select option[value="beta"]')
    ).to_have_count(0)
    sync_api.expect(page.locator("#account-select")).to_have_value("acme")


def test_키발급후평문모달이유지된다(ui: _UiSession) -> None:
    page = ui.page
    _open_manage(page, "keys")
    sync_api.expect(
        page.get_by_role("button", name="키 발급", exact=True)
    ).to_be_visible()

    page.get_by_role("button", name="키 발급", exact=True).click()
    issue = page.get_by_role("dialog", name="키 발급 (acme)")
    issue.get_by_label("사용자 ID").fill("alice")
    issue.get_by_label("이름(메모)").fill("브라우저 테스트")
    issue.get_by_role("button", name="저장").click()

    plaintext = page.get_by_role("dialog", name="발급된 API 키")
    sync_api.expect(plaintext).to_be_visible()
    sync_api.expect(plaintext.locator(".key-plaintext")).to_have_text(
        "sk-llmgw-test-PLAINTEXT-NEW"
    )
    sync_api.expect(page.locator("#modal-root")).not_to_have_attribute(
        "hidden", ""
    )


def test_키재발급확인후새평문을표시한다(ui: _UiSession) -> None:
    page = ui.page
    _open_manage(page, "keys")
    key_row = page.locator("#manage-panel tbody tr", has_text="key-1")
    sync_api.expect(key_row).to_be_visible()

    key_row.get_by_role("button", name="재발급").click()
    confirm = page.get_by_role("alertdialog")
    sync_api.expect(confirm).to_contain_text("옛 키는 즉시 무효")
    confirm.get_by_role("button", name="재발급").click()

    plaintext = page.get_by_role("dialog", name="발급된 API 키")
    sync_api.expect(plaintext.locator(".key-plaintext")).to_have_text(
        "sk-llmgw-test-PLAINTEXT-ROTATED"
    )
    assert (
        "POST",
        "/admin/accounts/acme/keys/key-1/rotate",
        None,
    ) in ui.api.calls


def test_오류응답이면폼모달을유지하고재시도할수있다(
    ui: _UiSession,
) -> None:
    page = ui.page
    _open_manage(page)
    ui.api.fail_once(
        "POST",
        "/admin/accounts",
        status=409,
        message="이미 존재하는 계정이다.",
    )

    page.get_by_role("button", name="계정 만들기", exact=True).click()
    dialog = page.get_by_role("dialog", name="계정 만들기")
    dialog.get_by_label("계정 ID").fill("beta")
    dialog.get_by_label("이름").fill("Beta")
    save = dialog.get_by_role("button", name="저장")
    save.click()

    sync_api.expect(dialog).to_be_visible()
    sync_api.expect(dialog.get_by_role("alert")).to_have_text(
        "이미 존재하는 계정이다."
    )
    sync_api.expect(save).to_be_enabled()

    save.click()
    sync_api.expect(dialog).to_be_hidden()
    sync_api.expect(
        page.locator("#manage-panel tbody tr", has_text="beta")
    ).to_be_visible()


@pytest.mark.parametrize(
    "viewport",
    [
        {"width": 1280, "height": 900},
        {"width": 390, "height": 844},
    ],
    ids=["desktop", "mobile"],
)
def test_데스크톱과모바일기본레이아웃이뷰포트를넘지않는다(
    chromium_browser: sync_api.Browser,
    static_url: str,
    viewport: dict[str, int],
) -> None:
    session = _open_session(
        chromium_browser,
        static_url,
        viewport=viewport,
    )
    try:
        page = session.page
        sync_api.expect(page.locator(".topbar")).to_be_visible()
        sync_api.expect(page.locator(".controls")).to_be_visible()
        sync_api.expect(page.locator("#screen-monitor")).to_be_visible()

        metrics = typing.cast(
            "_JsonDict",
            page.evaluate("""() => {
                  const controls = document.querySelector('.controls');
                  const first = controls.querySelector('.field');
                  const actions = controls.querySelector('.field-actions');
                  const rect = controls.getBoundingClientRect();
                  return {
                    viewportWidth: window.innerWidth,
                    documentWidth: document.documentElement.scrollWidth,
                    controlsLeft: rect.left,
                    controlsRight: rect.right,
                    firstTop: first.getBoundingClientRect().top,
                    actionsTop: actions.getBoundingClientRect().top,
                  };
                }"""),
        )
        assert metrics["documentWidth"] <= metrics["viewportWidth"]
        assert metrics["controlsLeft"] >= 0
        assert metrics["controlsRight"] <= metrics["viewportWidth"]
        if viewport["width"] < 600:
            assert metrics["actionsTop"] > metrics["firstTop"]

        page.locator("#admin-token").fill(_ADMIN_TOKEN)
        page.locator("#view-manage").click()
        sync_api.expect(
            page.get_by_role("button", name="계정 만들기", exact=True)
        ).to_be_visible()
        page.get_by_role("button", name="계정 만들기", exact=True).click()
        modal = page.get_by_role("dialog", name="계정 만들기")
        sync_api.expect(modal).to_be_visible()
        modal_bounds = modal.bounding_box()
        assert modal_bounds is not None
        assert modal_bounds["x"] >= 0
        assert modal_bounds["x"] + modal_bounds["width"] <= viewport["width"]
        assert modal_bounds["y"] >= 0
        assert modal_bounds["y"] + modal_bounds["height"] <= viewport["height"]
        assert session.page_errors == []
    finally:
        session.close()


def test_인증연동탭에서OIDC설정을저장하고차단한다(ui: _UiSession) -> None:
    """고객이 자기 IdP 를 UI 에서 붙이고 즉시 차단할 수 있어야 한다.

    관리 토큰 하나로만 관리하던 구조에서, 계정별 인증 연동으로 넘어가는
    흐름이 화면에서 실제로 동작하는지 확인한다.
    """
    page = ui.page
    page.locator("#admin-token").fill(_ADMIN_TOKEN)
    # 인증 설정은 계정 단위라 계정을 먼저 골라야 한다.
    page.locator("#refresh-button").click()
    sync_api.expect(page.locator("#account-select")).to_have_value("acme")
    page.locator("#view-manage").click()
    page.locator("#mtab-auth").click()

    # 아직 연결되지 않은 상태를 안내해야 한다.
    sync_api.expect(
        page.get_by_role("button", name="인증 서버 연결", exact=True)
    ).to_be_visible()

    page.get_by_role("button", name="인증 서버 연결", exact=True).click()
    modal = page.get_by_role("dialog")
    sync_api.expect(modal).to_be_visible()
    page.locator("#modal-issuer").fill(
        "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_UI"
    )
    page.locator("#modal-audience").fill("ui-client")
    page.locator("#modal-admin_groups").fill("acme-admins")
    page.get_by_role("button", name="저장", exact=True).click()

    # 저장 후 표에 발급자와 활성 배지가 보여야 한다.
    sync_api.expect(
        page.get_by_text(
            "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_UI",
            exact=True,
        )
    ).to_be_visible()
    sync_api.expect(page.get_by_text("활성", exact=True)).to_be_visible()
    sync_api.expect(page.get_by_text("acme-admins")).to_be_visible()

    # 즉시 차단이 동작해야 한다.
    page.get_by_role("button", name="즉시 차단", exact=True).click()
    sync_api.expect(page.get_by_text("차단됨", exact=True)).to_be_visible()

    assert ui.page_errors == []


# ---------------------------------------------------------------------------
# 표 정렬
#
# 마우스 클릭만 검증하면 키보드 사용자가 정렬을 못 쓰는 것을 놓친다. 네이티브
# button 을 쓴 이유가 키보드 지원이므로 Tab·Enter 경로도 함께 고정한다.
# ---------------------------------------------------------------------------


def _cost_column_values(page: sync_api.Page) -> list[str]:
    """상세 표의 비용 열 값을 위에서부터 읽는다."""
    headers = page.locator("#detail-table thead th")
    index = -1
    for position in range(headers.count()):
        if "비용" in (headers.nth(position).inner_text() or ""):
            index = position
            break
    assert index >= 0, "비용 열을 찾지 못했다"
    cells = page.locator(f"#detail-table tbody tr td:nth-child({index + 1})")
    return [cells.nth(row).inner_text().strip() for row in range(cells.count())]


def test_표머리를누르면정렬되고aria_sort가붙는다(ui: _UiSession) -> None:
    page = ui.page
    _load_accounts(page)
    page.locator("#tab-user").click()

    cost_header = page.locator("#detail-table thead th").filter(has_text="비용")
    # 정렬 전에는 어떤 열에도 aria-sort 가 없어야 한다. 모든 열에 none 을
    # 두면 스크린리더가 불필요하게 읽는다.
    assert page.locator("#detail-table thead th[aria-sort]").count() == 0

    cost_header.locator("button").click()
    sync_api.expect(cost_header).to_have_attribute("aria-sort", "descending")
    descending = _cost_column_values(page)

    # 다시 누르면 방향이 바뀐다.
    cost_header.locator("button").click()
    sync_api.expect(cost_header).to_have_attribute("aria-sort", "ascending")
    ascending = _cost_column_values(page)

    assert descending == list(reversed(ascending))
    # 정렬 중인 열은 하나뿐이어야 한다.
    assert page.locator("#detail-table thead th[aria-sort]").count() == 1


def test_키보드로도정렬할수있다(ui: _UiSession) -> None:
    page = ui.page
    _load_accounts(page)
    page.locator("#tab-user").click()

    button = (
        page.locator("#detail-table thead th")
        .filter(has_text="비용")
        .locator("button")
    )
    button.focus()
    page.keyboard.press("Enter")
    sync_api.expect(
        page.locator("#detail-table thead th").filter(has_text="비용")
    ).to_have_attribute("aria-sort", "descending")


def test_탭을옮기면각자의정렬이유지된다(ui: _UiSession) -> None:
    page = ui.page
    _load_accounts(page)

    page.locator("#tab-user").click()
    page.locator("#detail-table thead th").filter(has_text="비용").locator(
        "button"
    ).click()

    # 다른 탭은 정렬되지 않은 상태여야 한다.
    page.locator("#tab-model").click()
    assert page.locator("#detail-table thead th[aria-sort]").count() == 0

    # 돌아오면 정렬이 유지된다.
    page.locator("#tab-user").click()
    sync_api.expect(
        page.locator("#detail-table thead th").filter(has_text="비용")
    ).to_have_attribute("aria-sort", "descending")


def test_정렬버튼라벨이언어를따른다(ui: _UiSession) -> None:
    page = ui.page
    _load_accounts(page)
    page.locator("#tab-user").click()

    button = (
        page.locator("#detail-table thead th")
        .filter(has_text="비용")
        .locator("button")
    )
    label = button.get_attribute("aria-label") or ""
    assert "내림차순 정렬" in label

    page.locator('.lang-switch button[data-lang="en"]').click()
    english = (
        page.locator("#detail-table thead th")
        .filter(has_text="Cost")
        .locator("button")
        .get_attribute("aria-label")
        or ""
    )
    assert "sort descending" in english
