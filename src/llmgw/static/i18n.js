/*
 * 대시보드 다국어 처리.
 *
 * 원문(한국어)을 키로 쓴다. 키를 따로 만들지 않는 이유는 두 가지다.
 *
 * 1. 번역이 빠져도 한국어가 그대로 나온다. 키 방식은 번역이 없으면 `ui.kpi.
 *    requests` 같은 내부 식별자가 화면에 노출된다.
 * 2. 코드를 읽을 때 무슨 문자열인지 바로 보인다.
 *
 * 대가는 한국어 원문을 고치면 영어 매핑이 끊긴다는 점이다. 그래서 아래
 * 사전은 원문과 나란히 두고, 누락은 `LlmgwI18n.missing()` 으로 확인할 수
 * 있게 했다.
 *
 * 외부 라이브러리를 쓰지 않는다. 사설망에서 CDN 없이 동작해야 한다.
 */

(function (global) {
  'use strict';

  const STORAGE_KEY = 'llmgw.lang';
  const SUPPORTED = ['ko', 'en'];

  /**
   * 한국어 원문 -> 영어 번역.
   *
   * @type {!Object<string, string>}
   */
  const EN = {
    // -- 셸과 내비게이션 --
    'LLM Gateway 모니터링': 'LLM Gateway Monitoring',
    '본문으로 건너뛰기': 'Skip to main content',
    모니터링: 'Monitoring',
    관리: 'Manage',
    '계정 / 팀 / 사용자 / 모델 축으로 토큰·비용·지연·에러를 집계한다.':
      'Aggregates tokens, cost, latency, and errors by account, team, user, and model.',
    '사용량 대시보드': 'Usage dashboard',
    '화면 전환': 'Switch screen',

    // -- 조회 조건 --
    '관리 토큰': 'Admin token',
    계정: 'Account',
    '토큰 입력 후 불러오기': 'Enter a token to load',
    시작일: 'Start date',
    종료일: 'End date',
    기간: 'Range',
    오늘: 'Today',
    '7일': '7 days',
    '30일': '30 days',
    '90일': '90 days',
    조회: 'Load',
    '30초 자동': 'Auto (30s)',
    '조회 조건': 'Query filters',

    // -- KPI --
    요약: 'Summary',
    '요청 수': 'Requests',
    '총 토큰': 'Total tokens',
    '총 비용': 'Total cost',
    '평균 지연': 'Avg latency',
    '게이트웨이 관점': 'Gateway view',
    에러율: 'Error rate',
    '성공 ': 'ok ',
    ' · 실패 ': ' · failed ',
    '입력 ': 'in ',
    ' · 출력 ': ' · out ',
    'USD — 단가 미등록 ': 'USD — unpriced ',
    '건 제외됨': ' excluded',
    '실패 요청이 있다': 'has failed requests',
    '실패 없음': 'no failures',

    // -- 예산 소진 --
    '예산 소진': 'Budget usage',
    '이번 달': 'This month',
    '예산이 설정된 항목이 없다. 계정·팀·사용자·키에 월 예산을 지정하면 소진율이 여기 표시된다.':
      'No budgets are set. Assign a monthly budget to an account, team, user, or key and the burn rate appears here.',
    '차단됨 — 이 축의 요청이 거부된다': 'Blocked — requests on this axis are rejected',
    '단가 미등록 요청이 있어 실제 소진율은 더 높을 수 있다':
      'Some requests use unpriced models, so the real burn may be higher',
    '한도 0 — 즉시 차단': 'Limit 0 — blocked immediately',

    // -- 차트 --
    '추이와 분포': 'Trends and distribution',
    '일별 비용과 요청 수': 'Daily cost and requests',
    '일별 비용과 요청 수 추이': 'Daily cost and request trend',
    '모델별 요청 비중': 'Requests by model',
    '팀별 비용': 'Cost by team',
    '사용자별 비용 (상위 10)': 'Cost by user (top 10)',
    '사용자별 비용 상위 10': 'Top 10 users by cost',
    '비용 (USD)': 'Cost (USD)',
    '기타 ': 'Other ',
    개: '',
    '표시할 데이터가 없다.': 'No data to display.',
    '조회하면 차트가 표시된다': 'Charts appear after you load data',
    '선 그래프': 'line chart',
    '막대 그래프': 'bar chart',
    '도넛 차트': 'donut chart',

    // -- 상세 표 --
    상세: 'Details',
    팀: 'Team',
    사용자: 'User',
    모델: 'Model',
    'API 키': 'API keys',
    '최근 요청': 'Recent requests',
    '상세 보기 전환': 'Switch detail view',
    이름: 'Name',
    식별자: 'ID',
    요청: 'Requests',
    '입력 토큰': 'Input tokens',
    '출력 토큰': 'Output tokens',
    비용: 'Cost',
    '계정 ID': 'Account ID',
    상태: 'Status',
    '월 예산': 'Monthly budget',
    무제한: 'Unlimited',
    시각: 'Time',
    입력: 'In',
    출력: 'Out',
    지연: 'Latency',
    '계정별 사용량': 'Usage by account',
    '팀별 사용량': 'Usage by team',
    '사용자별 사용량': 'Usage by user',
    '모델별 사용량': 'Usage by model',
    'API 키별 사용량': 'Usage by API key',
    '최근 요청 (종료일 기준)': 'Recent requests (as of end date)',
    '이 기간에 데이터가 없다.': 'No data for this period.',
    '계정이 없다': 'No accounts',

    // -- 상태 메시지 --
    '관리 토큰을 입력한다.': 'Enter the admin token.',
    '관리 토큰을 입력하고 조회를 누른다.':
      'Enter the admin token and press Load.',
    '불러오는 중…': 'Loading…',
    '계정이 없다. scripts/seed_demo_data.py 로 데모 데이터를 넣거나 ':
      'No accounts. Seed demo data with scripts/seed_demo_data.py or ',
    '관리 API로 계정을 만든다.': 'create one through the admin API.',
    '갱신 완료 · ': 'Updated · ',
    '조회 실패: ': 'Load failed: ',
    '계정 목록 갱신 실패: ': 'Failed to refresh accounts: ',
    '불러오기 실패: ': 'Load failed: ',

    // -- 관리 화면 공통 --
    '리소스 관리': 'Resource management',
    '선택한 계정 아래의 팀·사용자·키를 만들고 수정·삭제한다. 삭제는 하위 리소스가 없어야 하고, 키 재발급 시 옛 키는 즉시 무효가 된다.':
      'Create, edit, and delete teams, users, and keys under the selected account. Deletion requires no child resources, and rotating a key invalidates the old one immediately.',
    '관리 대상 전환': 'Switch managed resource',
    '계정을 먼저 선택한다.': 'Select an account first.',
    '예산은 0 이상의 숫자여야 한다.': 'Budget must be a number of 0 or more.',
    취소: 'Cancel',
    저장: 'Save',
    삭제: 'Delete',
    수정: 'Edit',
    작업: 'Actions',
    '항목이 없다.': 'No items.',
    활성: 'Active',
    비활성: 'Inactive',
    비활성화: 'Disable',
    활성화: 'Enable',
    항목: 'Field',
    값: 'Value',
    차단됨: 'Blocked',

    // -- 계정 관리 --
    '계정 만들기': 'Create account',
    '소문자·숫자·하이픈': 'lowercase, digits, hyphen',
    '월 예산 (USD)': 'Monthly budget (USD)',
    '비우면 무제한': 'Leave empty for unlimited',
    '계정을 만들었다.': 'Account created.',
    '계정 수정: ': 'Edit account: ',
    '계정을 수정했다.': 'Account updated.',
    '계정 상태를 변경했다.': 'Account status changed.',
    ' 계정을 삭제한다. 하위 팀·사용자·키가 있으면 거부된다.':
      ' will be deleted. Rejected if teams, users, or keys remain.',
    '계정을 삭제했다.': 'Account deleted.',

    // -- 팀 관리 --
    '팀 만들기': 'Create team',
    '팀 만들기 (': 'Create team (',
    '팀 ID': 'Team ID',
    '비우면 계정 예산만 적용': 'Leave empty to use the account budget only',
    '팀을 만들었다.': 'Team created.',
    '팀 수정: ': 'Edit team: ',
    '팀을 수정했다.': 'Team updated.',
    '팀 상태를 변경했다.': 'Team status changed.',
    ' 팀을 삭제한다. 소속 사용자·키가 있으면 거부된다.':
      ' will be deleted. Rejected if users or keys remain.',
    '팀을 삭제했다.': 'Team deleted.',

    // -- 사용자 관리 --
    '사용자 만들기': 'Create user',
    '사용자 만들기 (': 'Create user (',
    '사용자 ID': 'User ID',
    '소문자·숫자·. _ -': 'lowercase, digits, . _ -',
    이메일: 'Email',
    '비우면 팀 없음. 존재하는 팀이어야 한다.':
      'Leave empty for no team. Must be an existing team.',
    '비우면 상위 예산만 적용': 'Leave empty to use parent budgets only',
    '사용자를 만들었다.': 'User created.',
    '사용자 수정: ': 'Edit user: ',
    '존재하는 팀이어야 한다. 비우면 팀 없음.':
      'Must be an existing team. Leave empty for no team.',
    '사용자를 수정했다.': 'User updated.',
    '사용자 상태를 변경했다.': 'User status changed.',
    ' 사용자를 삭제한다. 소유 키가 있으면 거부된다.':
      ' will be deleted. Rejected if the user still owns keys.',
    '사용자를 삭제했다.': 'User deleted.',

    // -- 키 관리 --
    '발급된 API 키': 'Issued API key',
    '이 키는 지금만 볼 수 있다. 안전한 곳에 보관한다. 창을 닫으면 다시 볼 수 없다.':
      'This key is shown only once. Store it somewhere safe. You cannot view it again after closing this dialog.',
    복사: 'Copy',
    복사됨: 'Copied',
    닫기: 'Close',
    '키 발급': 'Issue key',
    '키 발급 (': 'Issue key (',
    '존재하는 사용자여야 한다. 팀은 사용자에서 상속된다.':
      'Must be an existing user. The team is inherited from the user.',
    '이름(메모)': 'Name (note)',
    '허용 모델': 'Allowed models',
    '쉼표로 구분. 비우면 서버 기본 정책.':
      'Comma separated. Leave empty to use the server default policy.',
    '키를 발급했다.': 'Key issued.',
    '키 수정: ': 'Edit key: ',
    '키를 수정했다.': 'Key updated.',
    '키 상태를 변경했다.': 'Key status changed.',
    재발급: 'Rotate',
    ' 키를 재발급한다. 옛 키는 즉시 무효가 되고 새 키가 발급된다.':
      ' will be rotated. The old key is invalidated immediately and a new one is issued.',
    '키를 재발급했다.': 'Key rotated.',
    ' 키를 삭제한다. 이 키는 즉시 무효가 된다.':
      ' will be deleted. This key is invalidated immediately.',
    '키를 삭제했다.': 'Key deleted.',
    '키 ID': 'Key ID',
    접두어: 'Prefix',

    // -- 인증 연동 --
    '인증 연동': 'Identity provider',
    '인증 서버를 연결하면 이 계정 사용자는 API 키 없이 IdP 액세스 토큰으로':
      'Once an identity provider is connected, users in this account can call the gateway with an IdP access token instead of an API key.',
    ' 호출할 수 있다. 관리자 그룹에 속한 사람은 공유 관리 토큰 없이 이':
      ' Members of the admin group can manage this account without the shared admin token.',
    ' 계정을 관리한다. 발급자는 계정 간에 겹칠 수 없다.':
      ' An issuer cannot be shared across accounts.',
    '설정 수정': 'Edit settings',
    '인증 서버 연결': 'Connect provider',
    '즉시 차단': 'Block now',
    '다시 허용': 'Allow again',
    '외부 인증을 차단했다.': 'External authentication blocked.',
    '외부 인증을 허용했다.': 'External authentication allowed.',
    '상태 변경 실패: ': 'Status change failed: ',
    '연결 해제': 'Disconnect',
    '이 계정의 외부 인증 설정을 삭제한다. 발급자 등록도 함께 사라져':
      'The external authentication settings for this account will be deleted, along with the issuer registration,',
    ' 다른 계정이 같은 발급자를 쓸 수 있게 된다. IdP 토큰으로':
      ' so another account may then use the same issuer. Users calling with IdP tokens',
    ' 호출하던 사용자는 즉시 차단된다.': ' are blocked immediately.',
    '외부 인증 설정을 삭제했다.': 'External authentication settings deleted.',
    '아직 연결된 인증 서버가 없다. 이 계정은 API 키로만 호출할 수 있다.':
      'No identity provider is connected yet. This account can only call the gateway with API keys.',
    '발급자 (iss)': 'Issuer (iss)',
    '허용 클라이언트 (aud)': 'Allowed clients (aud)',
    '(검사 안 함)': '(not checked)',
    '사용자 클레임': 'User claim',
    '팀 클레임': 'Team claim',
    '(사용 안 함)': '(unused)',
    '그룹 클레임': 'Groups claim',
    '계정 관리자 그룹': 'Account admin groups',
    '(없음 — 관리 토큰만 사용)': '(none — shared admin token only)',
    '미등록 사용자 자동 생성': 'Auto-provision unknown users',
    켜짐: 'On',
    꺼짐: 'Off',
    '자동 생성 허용 모델': 'Auto-provision allowed models',
    '(제한 없음)': '(no restriction)',
    '자동 생성 월 예산': 'Auto-provision monthly budget',
    '(미설정)': '(not set)',
    '수정 시각': 'Updated at',
    '인증 설정 수정': 'Edit identity settings',
    '발급자 URL (iss)': 'Issuer URL (iss)',
    'https://cognito-idp.<리전>.amazonaws.com/<유저풀ID>':
      'https://cognito-idp.<region>.amazonaws.com/<user-pool-id>',
    '토큰의 iss 와 정확히 일치해야 한다. 계정 간 중복 불가.':
      'Must match the token iss exactly. Cannot be reused across accounts.',
    'JWKS URL (선택)': 'JWKS URL (optional)',
    '비우면 발급자 + /.well-known/jwks.json 을 쓴다. https 만':
      'Leave empty to use issuer + /.well-known/jwks.json. Only https is',
    ' 허용하며 내부 네트워크 주소는 거부된다.':
      ' allowed, and internal network addresses are rejected.',
    '허용 클라이언트 ID (선택)': 'Allowed client IDs (optional)',
    '쉼표로 구분. 비우면 청중을 검사하지 않는다.':
      'Comma separated. Leave empty to skip audience checks.',
    '사용자 ID 로 쓸 클레임. Cognito 는 보통 email 이다.':
      'Claim to use as the user ID. Cognito usually provides email.',
    '팀 클레임 (선택)': 'Team claim (optional)',
    '예: custom:team_id. 비우면 팀 없이 동작한다.':
      'For example custom:team_id. Leave empty to run without teams.',
    '계정 관리자 그룹 (선택)': 'Account admin groups (optional)',
    '이 그룹에 속한 사람은 관리 토큰 없이 이 계정을 관리한다.':
      'Members of these groups manage this account without the admin token.',
    ' 쉼표로 구분.': ' Comma separated.',
    '미등록 사용자 자동 생성 (yes/no)':
      'Auto-provision unknown users (yes/no)',
    'yes 로 하면 IdP 에 로그인한 사람이 곧 사용자가 된다. 이때':
      'With yes, anyone who signs in to the IdP becomes a user. A monthly',
    ' 월 예산을 반드시 지정해야 한다.': ' budget is then required.',
    '자동 생성 허용 모델 (선택)': 'Auto-provision allowed models (optional)',
    '쉼표로 구분. 비우면 서버 기본값을 따른다.':
      'Comma separated. Leave empty to use the server default.',
    '자동 생성 월 예산 (USD)': 'Auto-provision monthly budget (USD)',
    '자동 생성을 켜면 필수다.': 'Required when auto-provisioning is on.',
    '외부 인증 설정을 저장했다.': 'External authentication settings saved.',

    // -- 푸터 --
    '비용은 게이트웨이가 토큰 수와 자체 단가 표로 계산한 추정치다. AWS 청구서와 다를 수 있다. 지연 백분위와 서비스 전체 메트릭은 CloudWatch':
      'Cost is an estimate the gateway computes from token counts and its own pricing table. It may differ from your AWS bill. For latency percentiles and service-wide metrics, see CloudWatch',
    '네임스페이스에서 확인한다.': 'namespace.',
  };

  /** @type {string} */
  let current = detect();
  /** @type {!Set<string>} */
  const missing = new Set();

  /**
   * 저장된 언어 또는 브라우저 설정에서 초기 언어를 고른다.
   *
   * @returns {string} `ko` 또는 `en`.
   */
  function detect() {
    let stored = null;
    try {
      stored = global.localStorage.getItem(STORAGE_KEY);
    } catch (error) {
      // 사생활 보호 모드에서 접근이 막힐 수 있다. 기본값으로 넘어간다.
      stored = null;
    }
    if (stored && SUPPORTED.indexOf(stored) !== -1) {
      return stored;
    }
    // 저장된 선택이 없으면 한국어로 시작한다. 브라우저 언어를 따르면
    // 영어권에서 열었을 때 의도치 않게 영어로 뜨고, 사용자가 왜 바뀌었는지
    // 알기 어렵다. 전환 버튼이 사이드바에 항상 보이므로 선택 비용은 낮다.
    return 'ko';
  }

  /**
   * 문자열을 현재 언어로 번역한다.
   *
   * 번역이 없으면 원문을 그대로 돌려준다. 화면에 내부 식별자가 노출되는
   * 사고를 막기 위해서다.
   *
   * @param {string} text 한국어 원문.
   * @returns {string} 번역된 문자열.
   */
  function t(text) {
    if (current === 'ko') {
      return text;
    }
    if (Object.prototype.hasOwnProperty.call(EN, text)) {
      return EN[text];
    }
    missing.add(text);
    return text;
  }

  /**
   * 현재 언어를 반환한다.
   *
   * @returns {string} `ko` 또는 `en`.
   */
  function lang() {
    return current;
  }

  /**
   * 언어를 바꾸고 저장한다.
   *
   * @param {string} next `ko` 또는 `en`.
   * @returns {boolean} 실제로 바뀌었는지 여부.
   */
  function setLang(next) {
    if (SUPPORTED.indexOf(next) === -1 || next === current) {
      return false;
    }
    current = next;
    try {
      global.localStorage.setItem(STORAGE_KEY, next);
    } catch (error) {
      // 저장에 실패해도 이번 세션에는 적용된다.
    }
    global.document.documentElement.lang = next;
    return true;
  }

  /**
   * `data-i18n` 이 붙은 정적 요소를 현재 언어로 갱신한다.
   *
   * 텍스트는 `data-i18n`, placeholder 는 `data-i18n-placeholder`,
   * aria-label 은 `data-i18n-aria` 를 본다. 값에는 한국어 원문을 그대로
   * 넣는다.
   *
   * @param {!Element=} root 갱신 범위. 생략하면 문서 전체.
   */
  function applyStatic(root) {
    const scope = root || global.document;
    scope.querySelectorAll('[data-i18n]').forEach(function (node) {
      node.textContent = t(node.getAttribute('data-i18n'));
    });
    scope
      .querySelectorAll('[data-i18n-placeholder]')
      .forEach(function (node) {
        node.setAttribute(
          'placeholder',
          t(node.getAttribute('data-i18n-placeholder'))
        );
      });
    scope.querySelectorAll('[data-i18n-aria]').forEach(function (node) {
      node.setAttribute(
        'aria-label',
        t(node.getAttribute('data-i18n-aria'))
      );
    });
  }

  /**
   * 번역이 없어 원문으로 대체된 문자열 목록을 반환한다.
   *
   * 개발 중 누락을 찾는 용도다. 콘솔에서 `LlmgwI18n.missing()` 으로 본다.
   *
   * @returns {!Array<string>} 누락된 원문 목록.
   */
  function missingStrings() {
    return Array.from(missing).sort();
  }

  global.LlmgwI18n = {
    t: t,
    lang: lang,
    setLang: setLang,
    applyStatic: applyStatic,
    missing: missingStrings,
    supported: SUPPORTED.slice(),
  };
})(window);
