/**
 * 대시보드 동작.
 *
 * 관리 토큰은 `sessionStorage` 에만 둔다. `localStorage` 를 쓰면 탭을 닫은
 * 뒤에도 토큰이 디스크에 남는다. 세션 스토리지는 탭을 닫으면 사라진다.
 *
 * 데이터는 `/analytics/dashboard` 한 번의 호출로 모두 가져온다. 요약,
 * 시계열, 4개 축 분해, 최근 요청이 한 응답에 들어 있어 화면 갱신 중 축마다
 * 다른 시점의 값이 섞이는 일이 없다.
 *
 * 빌드 단계가 없으므로 TypeScript 대신 브라우저가 그대로 실행하는 JS로
 * 작성했다. 타입 의도는 JSDoc 으로 남긴다.
 */

(function () {
  'use strict';

  const t = window.LlmgwI18n.t;

  /**
   * 현재 언어에 맞는 BCP 47 로케일 태그를 반환한다.
   *
   * 시각과 숫자 표기가 언어와 어긋나면(영어 UI 에 '오후 3:26') 화면이
   * 어색해진다.
   *
   * @returns {string} 로케일 태그.
   */
  function locale() {
    return window.LlmgwI18n.lang() === 'en' ? 'en-US' : 'ko-KR';
  }


  const TOKEN_STORAGE_KEY = 'llmgw.adminToken';
  const ACCOUNT_STORAGE_KEY = 'llmgw.accountId';
  const AUTO_REFRESH_MS = 30000;
  const MAX_BAR_ROWS = 10;
  const MAX_DONUT_SLICES = 6;

  const charts = window.LlmgwCharts;

  const dom = {
    token: document.getElementById('admin-token'),
    account: document.getElementById('account-select'),
    start: document.getElementById('start-date'),
    end: document.getElementById('end-date'),
    refresh: document.getElementById('refresh-button'),
    autoRefresh: document.getElementById('auto-refresh'),
    status: document.getElementById('status-line'),
    table: document.getElementById('detail-table'),
    tableCaption: document.getElementById('table-caption'),
    panel: document.getElementById('panel-table'),
  };

  /** @type {?number} */
  let autoRefreshTimer = null;
  /** @type {string} */
  let activeView = 'team';
  /** @type {?Object} */
  let lastDashboard = null;
  /** @type {!Array<!Object>} */
  let lastAccounts = [];

  // -- 포맷터 ---------------------------------------------------------------

  const integerFormat = new Intl.NumberFormat('ko-KR');

  /**
   * 정수를 천 단위 구분자로 만든다.
   *
   * @param {number} value 값.
   * @returns {string} 표시 문자열.
   */
  function formatCount(value) {
    return integerFormat.format(Math.round(value || 0));
  }

  /**
   * 비용을 USD 문자열로 만든다.
   *
   * 소액 사용에서 `$0.00` 으로 뭉개지지 않도록 크기에 따라 소수 자리수를
   * 바꾼다. 0.01 미만은 6자리까지 보여준다.
   *
   * @param {number} value USD 값.
   * @returns {string} 표시 문자열.
   */
  function formatUsd(value) {
    const amount = value || 0;
    if (amount === 0) {
      return '$0';
    }
    if (Math.abs(amount) < 0.01) {
      return '$' + amount.toFixed(6);
    }
    if (Math.abs(amount) < 1) {
      return '$' + amount.toFixed(4);
    }
    return '$' + amount.toFixed(2);
  }

  /**
   * 밀리초를 사람이 읽는 문자열로 만든다.
   *
   * @param {number} value 밀리초.
   * @returns {string} 표시 문자열.
   */
  function formatMs(value) {
    const ms = Math.round(value || 0);
    if (ms >= 1000) {
      return (ms / 1000).toFixed(2) + ' s';
    }
    return ms + ' ms';
  }

  /**
   * 비율(0~1)을 퍼센트 문자열로 만든다.
   *
   * @param {number} value 비율.
   * @returns {string} 표시 문자열.
   */
  function formatPercent(value) {
    return ((value || 0) * 100).toFixed(2) + '%';
  }

  /**
   * ISO 시각을 로컬 시간 문자열로 만든다.
   *
   * @param {string} value ISO-8601 문자열.
   * @returns {string} 표시 문자열.
   */
  function formatTimestamp(value) {
    if (!value) {
      return '-';
    }
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) {
      return value;
    }
    return parsed.toLocaleString(locale(), { hour12: false });
  }

  /**
   * `YYYY-MM-DD` 문자열을 만든다.
   *
   * @param {!Date} date 대상 날짜.
   * @returns {string} 날짜 문자열.
   */
  function toDateInput(date) {
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return date.getFullYear() + '-' + month + '-' + day;
  }

  // -- 상태 표시 ------------------------------------------------------------

  /**
   * 상태 줄을 갱신한다.
   *
   * @param {string} message 메시지.
   * @param {string=} kind `error`, `ok`, 또는 생략.
   */
  function setStatus(message, kind) {
    dom.status.textContent = message;
    if (kind) {
      dom.status.setAttribute('data-kind', kind);
    } else {
      dom.status.removeAttribute('data-kind');
    }
  }

  // -- API ------------------------------------------------------------------

  /**
   * 관리 API 를 호출한다.
   *
   * @param {string} path 경로와 쿼리스트링.
   * @returns {!Promise<!Object>} 응답 본문.
   */
  async function apiGet(path) {
    const token = dom.token.value.trim();
    if (!token) {
      throw new Error(t('관리 토큰을 입력한다.'));
    }
    const response = await fetch(path, {
      headers: { 'X-Admin-Token': token },
      cache: 'no-store',
    });
    let body = null;
    try {
      body = await response.json();
    } catch (parseError) {
      // 본문이 JSON이 아닌 경우(프록시 오류 페이지 등)는 상태 코드로만
      // 판단한다.
      body = null;
    }
    if (!response.ok) {
      const detail =
        body && body.error && body.error.message
          ? body.error.message
          : 'HTTP ' + response.status;
      throw new Error(detail);
    }
    return body || {};
  }

  // -- 렌더링 ---------------------------------------------------------------

  /**
   * KPI 카드를 갱신한다.
   *
   * @param {!Object} totals 합계 수치.
   */
  function renderKpis(totals) {
    document.getElementById('kpi-requests').textContent = formatCount(
      totals.requests
    );
    document.getElementById('kpi-requests-note').textContent =
      t('성공 ') + formatCount(totals.success_requests) +
      t(' · 실패 ') + formatCount(totals.error_requests);

    document.getElementById('kpi-tokens').textContent = formatCount(
      totals.total_tokens
    );
    document.getElementById('kpi-tokens-note').textContent =
      t('입력 ') + formatCount(totals.input_tokens) +
      t(' · 출력 ') + formatCount(totals.output_tokens);

    document.getElementById('kpi-cost').textContent = formatUsd(
      totals.cost_usd
    );

    // 단가 표에 없는 모델이 섞여 있으면 비용 합계가 실제보다 작다. 그 사실을
    // 숫자 옆에 바로 알려 잘못된 값을 그대로 신뢰하지 않게 한다.
    const costNote = document.getElementById('kpi-cost-note');
    const unpriced = totals.unpriced_requests || 0;
    if (unpriced > 0) {
      costNote.textContent =
        t('USD — 단가 미등록 ') + formatCount(unpriced) + t('건 제외됨');
      costNote.setAttribute('data-warn', 'true');
    } else {
      costNote.textContent = 'USD';
      costNote.removeAttribute('data-warn');
    }

    document.getElementById('kpi-latency').textContent = formatMs(
      totals.avg_latency_ms
    );

    const errorRate = document.getElementById('kpi-error-rate');
    errorRate.textContent = formatPercent(totals.error_rate);
    document.getElementById('kpi-error-note').textContent =
      totals.error_requests > 0 ? t('실패 요청이 있다') : t('실패 없음');
  }


  /**
   * 이번 달 예산 소진 현황을 그린다.
   *
   * 이 제품의 핵심 가치가 비용 통제인데 화면에 예산이 없으면 소진을 사후에만
   * 알게 된다. 소진율이 높은 항목이 위에 오도록 서버가 정렬해 보낸다.
   *
   * @param {?Object} budgets 예산 블록. 없으면 비운다.
   */
  function renderBudgets(budgets) {
    const panel = document.getElementById('budget-panel');
    if (!panel) {
      return;
    }
    const entries = (budgets && budgets.entries) || [];
    if (!entries.length) {
      const empty = document.createElement('p');
      empty.className = 'chart-empty';
      empty.textContent = t(
        '예산이 설정된 항목이 없다. 계정·팀·사용자·키에 월 예산을 지정하면 소진율이 여기 표시된다.'
      );
      panel.replaceChildren(empty);
      return;
    }

    const grid = document.createElement('div');
    grid.className = 'budget-grid';
    entries.forEach(function (entry) {
      grid.appendChild(buildBudgetCard(entry));
    });
    panel.replaceChildren(grid);
  }

  /**
   * 예산 항목 카드 하나를 만든다.
   *
   * @param {!Object} entry 예산 항목.
   * @returns {!HTMLElement} 카드 요소.
   */
  function buildBudgetCard(entry) {
    const ratio = Number(entry.used_ratio) || 0;
    const percent = Math.min(ratio * 100, 100);
    // 80% 를 주의, 100% 이상을 위험으로 본다. 예산은 초과하면 요청이 거부되므로
    // 그 전에 알아야 한다.
    let level = 'ok';
    if (entry.blocked || ratio >= 1) {
      level = 'danger';
    } else if (ratio >= 0.8) {
      level = 'warn';
    }

    const card = document.createElement('article');
    card.className = 'budget-card budget-' + level;

    const head = document.createElement('div');
    head.className = 'budget-head';
    const scope = document.createElement('span');
    scope.className = 'badge';
    scope.textContent = t(scopeLabel(entry.scope));
    const label = document.createElement('span');
    label.className = 'budget-label';
    label.textContent = entry.label || entry.entity_id;
    label.title = entry.entity_id;
    head.appendChild(scope);
    head.appendChild(label);
    card.appendChild(head);

    const amount = document.createElement('p');
    amount.className = 'budget-amount';
    amount.textContent =
      formatUsd(entry.used_usd) + ' / ' + formatUsd(entry.limit_usd);
    card.appendChild(amount);

    // 진행률 바. 스크린리더에는 progressbar 로 알린다.
    const track = document.createElement('div');
    track.className = 'budget-track';
    track.setAttribute('role', 'progressbar');
    track.setAttribute('aria-valuemin', '0');
    track.setAttribute('aria-valuemax', '100');
    track.setAttribute('aria-valuenow', String(Math.round(percent)));
    track.setAttribute(
      'aria-label',
      (entry.label || entry.entity_id) + ' ' + Math.round(percent) + '%'
    );
    const fill = document.createElement('div');
    fill.className = 'budget-fill';
    fill.style.width = percent.toFixed(1) + '%';
    track.appendChild(fill);
    card.appendChild(track);

    const note = document.createElement('p');
    note.className = 'budget-note';
    if (Number(entry.limit_usd) === 0) {
      note.textContent = t('한도 0 — 즉시 차단');
    } else if (entry.blocked) {
      note.textContent = t('차단됨 — 이 축의 요청이 거부된다');
    } else {
      note.textContent = formatPercent(ratio);
    }
    card.appendChild(note);

    if (entry.unpriced_requests > 0) {
      const warning = document.createElement('p');
      warning.className = 'budget-warning';
      warning.textContent = t(
        '단가 미등록 요청이 있어 실제 소진율은 더 높을 수 있다'
      );
      card.appendChild(warning);
    }
    return card;
  }

  /**
   * 예산 축 이름을 사람이 읽는 라벨로 바꾼다.
   *
   * @param {string} scope `account`/`team`/`user`/`key`.
   * @returns {string} 한국어 라벨(번역 대상).
   */
  function scopeLabel(scope) {
    if (scope === 'team') {
      return '팀';
    }
    if (scope === 'user') {
      return '사용자';
    }
    if (scope === 'key') {
      return 'API 키';
    }
    return '계정';
  }

  /**
   * 차트 4개를 갱신한다.
   *
   * @param {!Object} dashboard 대시보드 응답.
   */
  function renderCharts(dashboard) {
    const series = dashboard.timeseries || [];
    charts.lineChart(document.getElementById('chart-timeseries'), {
      labels: series.map(function (entry) {
        return entry.date;
      }),
      series: [
        {
          name: t('비용 (USD)'),
          values: series.map(function (entry) {
            return entry.cost_usd;
          }),
          format: formatUsd,
        },
        {
          name: t('요청 수'),
          values: series.map(function (entry) {
            return entry.requests;
          }),
          format: formatCount,
        },
      ],
      ariaLabel: t('일별 비용과 요청 수 추이'),
    });

    const teams = (dashboard.breakdowns.team || []).slice(0, MAX_BAR_ROWS);
    charts.barChart(document.getElementById('chart-teams'), {
      labels: teams.map(function (row) {
        return row.label;
      }),
      values: teams.map(function (row) {
        return row.cost_usd;
      }),
      format: formatUsd,
      ariaLabel: t('팀별 비용'),
    });

    const users = (dashboard.breakdowns.user || []).slice(0, MAX_BAR_ROWS);
    charts.barChart(document.getElementById('chart-users'), {
      labels: users.map(function (row) {
        return row.label;
      }),
      values: users.map(function (row) {
        return row.cost_usd;
      }),
      format: formatUsd,
      ariaLabel: t('사용자별 비용 상위 10'),
    });

    charts.donutChart(
      document.getElementById('chart-models'),
      buildModelDonut(dashboard.breakdowns.model || [])
    );
  }

  /**
   * 모델 축을 도넛 차트 명세로 바꾼다. 하위 항목은 "기타"로 묶는다.
   *
   * @param {!Array<!Object>} rows 모델별 집계.
   * @returns {!Object} 도넛 차트 명세.
   */
  function buildModelDonut(rows) {
    const sorted = rows.slice().sort(function (left, right) {
      return right.requests - left.requests;
    });
    const head = sorted.slice(0, MAX_DONUT_SLICES);
    const tail = sorted.slice(MAX_DONUT_SLICES);
    const labels = head.map(function (row) {
      return row.label;
    });
    const values = head.map(function (row) {
      return row.requests;
    });
    if (tail.length) {
      labels.push(t('기타 ') + tail.length + t('개'));
      values.push(
        tail.reduce(function (sum, row) {
          return sum + row.requests;
        }, 0)
      );
    }
    return {
      labels: labels,
      values: values,
      format: formatCount,
      ariaLabel: t('모델별 요청 비중'),
    };
  }

  /** 상세 표의 열 정의. */
  /**
   * 상세 표의 열 정의를 만든다.
   *
   * 상수가 아니라 함수인 이유는 열 제목이 번역 대상이기 때문이다. 모듈
   * 로드 시점에 한 번 평가하면 언어를 바꿔도 제목이 그대로 남는다.
   *
   * @returns {!Object<string, !Array<!Object>>} 뷰별 열 정의.
   */
  function tableColumns() {
    return {
    usage: [
      { key: 'label', title: t('이름'), numeric: false },
      { key: 'key', title: t('식별자'), numeric: false },
      { key: 'requests', title: t('요청'), numeric: true, format: formatCount },
      {
        key: 'input_tokens',
        title: t('입력 토큰'),
        numeric: true,
        format: formatCount,
      },
      {
        key: 'output_tokens',
        title: t('출력 토큰'),
        numeric: true,
        format: formatCount,
      },
      { key: 'cost_usd', title: t('비용'), numeric: true, format: formatUsd },
      {
        key: 'avg_latency_ms',
        title: t('평균 지연'),
        numeric: true,
        format: formatMs,
      },
      {
        key: 'error_rate',
        title: t('에러율'),
        numeric: true,
        format: formatPercent,
      },
    ],
    accounts: [
      { key: 'label', title: t('계정'), numeric: false },
      { key: 'account_id', title: t('계정 ID'), numeric: false },
      { key: 'status', title: t('상태'), numeric: false },
      { key: 'requests', title: t('요청'), numeric: true, format: formatCount },
      {
        key: 'total_tokens',
        title: t('총 토큰'),
        numeric: true,
        format: formatCount,
      },
      { key: 'cost_usd', title: t('비용'), numeric: true, format: formatUsd },
      {
        key: 'monthly_budget_usd',
        title: t('월 예산'),
        numeric: true,
        format: function (value) {
          return value == null ? t('무제한') : formatUsd(value);
        },
      },
      {
        key: 'error_rate',
        title: t('에러율'),
        numeric: true,
        format: formatPercent,
      },
    ],
    requests: [
      {
        key: 'timestamp',
        title: t('시각'),
        numeric: false,
        format: formatTimestamp,
      },
      { key: 'user_id', title: t('사용자'), numeric: false },
      { key: 'team_id', title: t('팀'), numeric: false },
      { key: 'model_id', title: t('모델'), numeric: false },
      {
        key: 'input_tokens',
        title: t('입력'),
        numeric: true,
        format: formatCount,
      },
      {
        key: 'output_tokens',
        title: t('출력'),
        numeric: true,
        format: formatCount,
      },
      { key: 'cost_usd', title: t('비용'), numeric: true, format: formatUsd },
      {
        key: 'latency_ms',
        title: t('지연'),
        numeric: true,
        format: formatMs,
      },
      { key: 'status_code', title: t('상태'), numeric: true },
      ],
    };
  }

  /**
   * 뷰별 표 캡션을 만든다. 번역 대상이라 함수로 둔다.
   *
   * @returns {!Object<string, string>} 뷰별 캡션.
   */
  function viewCaptions() {
    return {
    accounts: t('계정별 사용량'),
    team: t('팀별 사용량'),
    user: t('사용자별 사용량'),
    model: t('모델별 사용량'),
    key: t('API 키별 사용량'),
      requests: t('최근 요청 (종료일 기준)'),
    };
  }

  /**
   * 현재 선택된 탭에 맞는 표를 그린다.
   */
  function renderTable() {
    const allColumns = tableColumns();
    const columns =
      activeView === 'accounts'
        ? allColumns.accounts
        : activeView === 'requests'
          ? allColumns.requests
          : allColumns.usage;

    let rows = [];
    if (activeView === 'accounts') {
      rows = lastAccounts;
    } else if (lastDashboard) {
      rows =
        activeView === 'requests'
          ? lastDashboard.recent_requests || []
          : (lastDashboard.breakdowns || {})[activeView] || [];
    }

    dom.tableCaption.textContent = viewCaptions()[activeView] || t('상세');

    const headRow = dom.table.querySelector('thead tr');
    headRow.replaceChildren();
    columns.forEach(function (column) {
      const cell = document.createElement('th');
      cell.scope = 'col';
      cell.textContent = column.title;
      if (column.numeric) {
        cell.className = 'numeric';
      }
      headRow.appendChild(cell);
    });

    const body = dom.table.querySelector('tbody');
    body.replaceChildren();

    if (!rows.length) {
      const emptyRow = document.createElement('tr');
      emptyRow.className = 'empty-row';
      const cell = document.createElement('td');
      cell.colSpan = columns.length;
      cell.textContent = t('이 기간에 데이터가 없다.');
      emptyRow.appendChild(cell);
      body.appendChild(emptyRow);
      return;
    }

    rows.forEach(function (row) {
      const tableRow = document.createElement('tr');
      columns.forEach(function (column) {
        const cell = document.createElement('td');
        if (column.numeric) {
          cell.className = 'numeric';
        }
        const raw = row[column.key];
        if (column.key === 'status_code') {
          const badge = document.createElement('span');
          const ok = raw >= 200 && raw < 300;
          badge.className = 'badge ' + (ok ? 'badge-ok' : 'badge-error');
          badge.textContent = String(raw);
          cell.appendChild(badge);
        } else if (column.format) {
          cell.textContent = column.format(raw);
        } else {
          cell.textContent =
            raw === undefined || raw === null || raw === '' ? '-' : String(raw);
        }
        tableRow.appendChild(cell);
      });
      body.appendChild(tableRow);
    });
  }

  // -- 데이터 로딩 ----------------------------------------------------------

  /**
   * 계정 목록을 불러와 선택 상자를 채운다.
   *
   * @returns {!Promise<void>}
   */
  async function loadAccounts() {
    const body = await apiGet('/admin/accounts');
    const accounts = body.data || [];
    const previous =
      dom.account.value || sessionStorage.getItem(ACCOUNT_STORAGE_KEY) || '';

    dom.account.replaceChildren();
    if (!accounts.length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = t('계정이 없다');
      dom.account.appendChild(option);
      return;
    }
    accounts.forEach(function (account) {
      const option = document.createElement('option');
      option.value = account.account_id;
      option.textContent = account.name + ' (' + account.account_id + ')';
      dom.account.appendChild(option);
    });
    const hasPrevious = accounts.some(function (account) {
      return account.account_id === previous;
    });
    dom.account.value = hasPrevious ? previous : accounts[0].account_id;
  }

  /**
   * 전체 화면을 갱신한다.
   *
   * @returns {!Promise<void>}
   */
  async function refresh() {
    const token = dom.token.value.trim();
    if (!token) {
      setStatus(t('관리 토큰을 입력하고 조회를 누른다.'), 'error');
      return;
    }
    sessionStorage.setItem(TOKEN_STORAGE_KEY, token);

    dom.refresh.disabled = true;
    setStatus(t('불러오는 중…'));
    try {
      await loadAccounts();
      const accountId = dom.account.value;
      if (!accountId) {
        lastDashboard = null;
        lastAccounts = [];
        renderKpis({});
        renderTable();
        setStatus(
          t('계정이 없다. scripts/seed_demo_data.py 로 데모 데이터를 넣거나 ') +
            t('관리 API로 계정을 만든다.'),
          'error'
        );
        return;
      }
      sessionStorage.setItem(ACCOUNT_STORAGE_KEY, accountId);

      const range =
        'start=' + encodeURIComponent(dom.start.value) +
        '&end=' + encodeURIComponent(dom.end.value);

      const [dashboard, overview] = await Promise.all([
        apiGet(
          '/analytics/dashboard?account_id=' +
            encodeURIComponent(accountId) + '&' + range
        ),
        apiGet('/analytics/accounts?' + range),
      ]);

      lastDashboard = dashboard;
      lastAccounts = overview.data || [];

      renderKpis(dashboard.totals || {});
      renderCharts(dashboard);
      renderBudgets(dashboard.budgets);
      renderTable();
      setStatus(
        t('갱신 완료 · ') + dashboard.window.start + ' ~ ' +
          dashboard.window.end + ' · ' + new Date().toLocaleTimeString(locale()),
        'ok'
      );
    } catch (error) {
      setStatus(t('조회 실패: ') + error.message, 'error');
    } finally {
      dom.refresh.disabled = false;
    }
  }

  // -- 이벤트 ---------------------------------------------------------------

  /**
   * 기간 프리셋을 적용한다.
   *
   * @param {number} days 되돌아갈 일수. 1이면 오늘 하루.
   */
  function applyQuickRange(days) {
    const end = new Date();
    const start = new Date();
    start.setDate(end.getDate() - (days - 1));
    dom.start.value = toDateInput(start);
    dom.end.value = toDateInput(end);
  }

  /**
   * 자동 새로고침 타이머를 켜거나 끈다.
   */
  function syncAutoRefresh() {
    if (autoRefreshTimer !== null) {
      window.clearInterval(autoRefreshTimer);
      autoRefreshTimer = null;
    }
    if (dom.autoRefresh.checked) {
      autoRefreshTimer = window.setInterval(function () {
        // 실패해도 타이머를 유지한다. 일시적 오류로 자동 갱신이 멈추면
        // 사용자가 원인을 알기 어렵다.
        refresh();
      }, AUTO_REFRESH_MS);
    }
  }

  /**
   * 탭 전환을 처리한다.
   *
   * @param {!Element} button 선택된 탭 버튼.
   */
  function selectTab(button) {
    document
      .querySelectorAll('#screen-monitor [role="tab"]')
      .forEach(function (tab) {
        tab.setAttribute('aria-selected', String(tab === button));
      });
    dom.panel.setAttribute('aria-labelledby', button.id);
    activeView = button.getAttribute('data-view');
    renderTable();
  }

  /**
   * 초기화한다.
   */

  /**
   * 정적 텍스트와 문서 제목, 언어 버튼 상태를 현재 언어로 맞춘다.
   */
  function applyLanguage() {
    window.LlmgwI18n.applyStatic();
    document.title = t('LLM Gateway 모니터링');
    const current = window.LlmgwI18n.lang();
    document.querySelectorAll('[data-lang]').forEach(function (button) {
      button.setAttribute(
        'aria-pressed',
        String(button.getAttribute('data-lang') === current)
      );
    });
  }

  function init() {
    applyQuickRange(30);

    const savedToken = sessionStorage.getItem(TOKEN_STORAGE_KEY);
    if (savedToken) {
      dom.token.value = savedToken;
    }

    dom.refresh.addEventListener('click', refresh);
    dom.account.addEventListener('change', refresh);
    dom.autoRefresh.addEventListener('change', syncAutoRefresh);

    dom.token.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        refresh();
      }
    });

    document.querySelectorAll('.quick-buttons button').forEach(function (btn) {
      btn.addEventListener('click', function () {
        applyQuickRange(Number(btn.getAttribute('data-days')));
        refresh();
      });
    });

    document
      .querySelectorAll('#screen-monitor [role="tab"]')
      .forEach(function (tab) {
        tab.addEventListener('click', function () {
          selectTab(tab);
        });
      });


    // 언어 전환. 정적 텍스트는 i18n 모듈이 data-i18n 으로 갱신하고, 동적으로
    // 그려지는 표·차트·관리 화면은 다시 렌더링해 반영한다.
    applyLanguage();
    document.querySelectorAll('[data-lang]').forEach(function (button) {
      button.addEventListener('click', function () {
        if (!window.LlmgwI18n.setLang(button.getAttribute('data-lang'))) {
          return;
        }
        applyLanguage();
        renderTable();
        if (lastDashboard) {
          renderCharts(lastDashboard);
          renderKpis(lastDashboard.totals || {});
          renderBudgets(lastDashboard.budgets);
        }
        if (window.LlmgwAdmin && window.LlmgwAdmin.rerender) {
          window.LlmgwAdmin.rerender();
        }
      });
    });

    renderTable();

    if (savedToken) {
      refresh();
    } else {
      setStatus(t('관리 토큰을 입력하고 조회를 누른다.'));
    }

    // 관리 화면(admin.js)이 계정을 만들고 지운 직후 상단 계정 선택 목록을
    // 다시 채울 수 있도록 최소한의 훅만 노출한다. 두 화면이 같은 select
    // 요소와 토큰을 공유하므로, 관리 작업 후 이 함수를 부르면 목록이 즉시
    // 최신 상태가 된다.
    window.LlmgwDashboard = {
      reloadAccounts: async function () {
        try {
          await loadAccounts();
        } catch (error) {
          // 계정 목록 갱신 실패가 관리 작업 자체를 되돌리지는 않는다.
          setStatus(t('계정 목록 갱신 실패: ') + error.message, 'error');
        }
      },
    };
  }

  init();
})();
