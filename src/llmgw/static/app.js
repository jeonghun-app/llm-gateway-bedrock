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
    return parsed.toLocaleString('ko-KR', { hour12: false });
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
      throw new Error('관리 토큰을 입력한다.');
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
      '성공 ' + formatCount(totals.success_requests) +
      ' · 실패 ' + formatCount(totals.error_requests);

    document.getElementById('kpi-tokens').textContent = formatCount(
      totals.total_tokens
    );
    document.getElementById('kpi-tokens-note').textContent =
      '입력 ' + formatCount(totals.input_tokens) +
      ' · 출력 ' + formatCount(totals.output_tokens);

    document.getElementById('kpi-cost').textContent = formatUsd(
      totals.cost_usd
    );

    // 단가 표에 없는 모델이 섞여 있으면 비용 합계가 실제보다 작다. 그 사실을
    // 숫자 옆에 바로 알려 잘못된 값을 그대로 신뢰하지 않게 한다.
    const costNote = document.getElementById('kpi-cost-note');
    const unpriced = totals.unpriced_requests || 0;
    if (unpriced > 0) {
      costNote.textContent =
        'USD — 단가 미등록 ' + formatCount(unpriced) + '건 제외됨';
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
      totals.error_requests > 0 ? '실패 요청이 있다' : '실패 없음';
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
          name: '비용 (USD)',
          values: series.map(function (entry) {
            return entry.cost_usd;
          }),
          format: formatUsd,
        },
        {
          name: '요청 수',
          values: series.map(function (entry) {
            return entry.requests;
          }),
          format: formatCount,
        },
      ],
      ariaLabel: '일별 비용과 요청 수 추이',
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
      ariaLabel: '팀별 비용',
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
      ariaLabel: '사용자별 비용 상위 10',
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
      labels.push('기타 ' + tail.length + '개');
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
      ariaLabel: '모델별 요청 비중',
    };
  }

  /** 상세 표의 열 정의. */
  const TABLE_COLUMNS = {
    usage: [
      { key: 'label', title: '이름', numeric: false },
      { key: 'key', title: '식별자', numeric: false },
      { key: 'requests', title: '요청', numeric: true, format: formatCount },
      {
        key: 'input_tokens',
        title: '입력 토큰',
        numeric: true,
        format: formatCount,
      },
      {
        key: 'output_tokens',
        title: '출력 토큰',
        numeric: true,
        format: formatCount,
      },
      { key: 'cost_usd', title: '비용', numeric: true, format: formatUsd },
      {
        key: 'avg_latency_ms',
        title: '평균 지연',
        numeric: true,
        format: formatMs,
      },
      {
        key: 'error_rate',
        title: '에러율',
        numeric: true,
        format: formatPercent,
      },
    ],
    accounts: [
      { key: 'label', title: '계정', numeric: false },
      { key: 'account_id', title: '계정 ID', numeric: false },
      { key: 'status', title: '상태', numeric: false },
      { key: 'requests', title: '요청', numeric: true, format: formatCount },
      {
        key: 'total_tokens',
        title: '총 토큰',
        numeric: true,
        format: formatCount,
      },
      { key: 'cost_usd', title: '비용', numeric: true, format: formatUsd },
      {
        key: 'monthly_budget_usd',
        title: '월 예산',
        numeric: true,
        format: function (value) {
          return value == null ? '무제한' : formatUsd(value);
        },
      },
      {
        key: 'error_rate',
        title: '에러율',
        numeric: true,
        format: formatPercent,
      },
    ],
    requests: [
      {
        key: 'timestamp',
        title: '시각',
        numeric: false,
        format: formatTimestamp,
      },
      { key: 'user_id', title: '사용자', numeric: false },
      { key: 'team_id', title: '팀', numeric: false },
      { key: 'model_id', title: '모델', numeric: false },
      {
        key: 'input_tokens',
        title: '입력',
        numeric: true,
        format: formatCount,
      },
      {
        key: 'output_tokens',
        title: '출력',
        numeric: true,
        format: formatCount,
      },
      { key: 'cost_usd', title: '비용', numeric: true, format: formatUsd },
      {
        key: 'latency_ms',
        title: '지연',
        numeric: true,
        format: formatMs,
      },
      { key: 'status_code', title: '상태', numeric: true },
    ],
  };

  const VIEW_CAPTIONS = {
    accounts: '계정별 사용량',
    team: '팀별 사용량',
    user: '사용자별 사용량',
    model: '모델별 사용량',
    key: 'API 키별 사용량',
    requests: '최근 요청 (종료일 기준)',
  };

  /**
   * 현재 선택된 탭에 맞는 표를 그린다.
   */
  function renderTable() {
    const columns =
      activeView === 'accounts'
        ? TABLE_COLUMNS.accounts
        : activeView === 'requests'
          ? TABLE_COLUMNS.requests
          : TABLE_COLUMNS.usage;

    let rows = [];
    if (activeView === 'accounts') {
      rows = lastAccounts;
    } else if (lastDashboard) {
      rows =
        activeView === 'requests'
          ? lastDashboard.recent_requests || []
          : (lastDashboard.breakdowns || {})[activeView] || [];
    }

    dom.tableCaption.textContent = VIEW_CAPTIONS[activeView] || '상세';

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
      cell.textContent = '이 기간에 데이터가 없다.';
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
      option.textContent = '계정이 없다';
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
      setStatus('관리 토큰을 입력하고 조회를 누른다.', 'error');
      return;
    }
    sessionStorage.setItem(TOKEN_STORAGE_KEY, token);

    dom.refresh.disabled = true;
    setStatus('불러오는 중…');
    try {
      await loadAccounts();
      const accountId = dom.account.value;
      if (!accountId) {
        lastDashboard = null;
        lastAccounts = [];
        renderKpis({});
        renderTable();
        setStatus(
          '계정이 없다. scripts/seed_demo_data.py 로 데모 데이터를 넣거나 ' +
            '관리 API로 계정을 만든다.',
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
      renderTable();
      setStatus(
        '갱신 완료 · ' + dashboard.window.start + ' ~ ' +
          dashboard.window.end + ' · ' + new Date().toLocaleTimeString('ko-KR'),
        'ok'
      );
    } catch (error) {
      setStatus('조회 실패: ' + error.message, 'error');
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
    document.querySelectorAll('[role="tab"]').forEach(function (tab) {
      tab.setAttribute('aria-selected', String(tab === button));
    });
    dom.panel.setAttribute('aria-labelledby', button.id);
    activeView = button.getAttribute('data-view');
    renderTable();
  }

  /**
   * 초기화한다.
   */
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

    document.querySelectorAll('[role="tab"]').forEach(function (tab) {
      tab.addEventListener('click', function () {
        selectTab(tab);
      });
    });

    renderTable();

    if (savedToken) {
      refresh();
    } else {
      setStatus('관리 토큰을 입력하고 조회를 누른다.');
    }
  }

  init();
})();
