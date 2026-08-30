/**
 * 관리 화면 동작.
 *
 * 읽기 전용 대시보드(app.js) 옆에 계정·팀·사용자·API 키를 만들고
 * 수정·삭제하는 쓰기 화면을 얹는다. 관리 토큰과 계정 선택은 app.js 가 이미
 * 다루는 DOM 요소(`#admin-token`, `#account-select`)를 그대로 공유한다.
 * 별도 상태 저장소를 두지 않아 두 화면이 항상 같은 토큰·계정을 본다.
 *
 * app.js 와 마찬가지로 빌드 단계 없이 브라우저가 그대로 실행하는 바닐라
 * JS 다. 타입 의도는 JSDoc 으로 남긴다.
 */

(function () {
  'use strict';

  const t = window.LlmgwI18n.t;

  const dom = {
    token: document.getElementById('admin-token'),
    account: document.getElementById('account-select'),
    status: document.getElementById('status-line'),
    screenMonitor: document.getElementById('screen-monitor'),
    screenManage: document.getElementById('screen-manage'),
    managePanel: document.getElementById('manage-panel'),
    modalRoot: document.getElementById('modal-root'),
  };

  /** @type {string} */
  let activeManageView = 'accounts';

  // -- 상태 표시 ------------------------------------------------------------

  /**
   * 상태 줄을 갱신한다. app.js 와 같은 요소를 쓴다.
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

  /**
   * 상단 계정 선택 목록을 다시 채운다.
   *
   * 계정 정보나 상태가 바뀐 직후 호출한다. 모니터링 화면(app.js)이 노출한
   * 훅을 쓴다. 훅이 아직 준비되지 않았으면(로드 순서 등) 조용히 넘어간다.
   *
   * @returns {!Promise<void>}
   */
  async function syncAccountSelect() {
    const dashboard = window.LlmgwDashboard;
    if (dashboard && typeof dashboard.reloadAccounts === 'function') {
      await dashboard.reloadAccounts();
    }
  }

  // -- API ------------------------------------------------------------------

  /**
   * 관리 API 를 호출한다. GET 은 물론 쓰기 메서드도 처리한다.
   *
   * @param {string} method HTTP 메서드.
   * @param {string} path 경로.
   * @param {?Object=} body 요청 본문. 없으면 생략한다.
   * @returns {!Promise<?Object>} 응답 본문. 204 면 null.
   */
  async function api(method, path, body) {
    const token = dom.token.value.trim();
    if (!token) {
      throw new Error(t('관리 토큰을 입력한다.'));
    }
    const options = {
      method: method,
      headers: { 'X-Admin-Token': token },
      cache: 'no-store',
    };
    if (body !== undefined && body !== null) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    if (response.status === 204) {
      return null;
    }
    let payload = null;
    try {
      payload = await response.json();
    } catch (parseError) {
      payload = null;
    }
    if (!response.ok) {
      const detail =
        payload && payload.error && payload.error.message
          ? payload.error.message
          : 'HTTP ' + response.status;
      throw new Error(detail);
    }
    return payload || {};
  }

  // -- 포맷 -----------------------------------------------------------------

  /**
   * 월 예산을 표시 문자열로 만든다.
   *
   * @param {?number} value USD 값 또는 null.
   * @returns {string} 표시 문자열.
   */
  function budgetLabel(value) {
    return value == null ? t('무제한') : '$' + Number(value).toFixed(2);
  }

  /**
   * 숫자 입력값을 예산 필드로 정규화한다. 빈 값은 null(무제한)로 본다.
   *
   * @param {string} raw 입력 문자열.
   * @returns {?number} 숫자 또는 null.
   */
  /**
   * 정수 입력을 숫자 또는 null 로 바꾼다.
   *
   * 빈 값은 "제한 없음" 이지 0 이 아니다. 0 으로 보내면 모든 요청이 막힌다.
   *
   * @param {string} raw 입력 문자열.
   * @returns {number|null} 정수 또는 `null`.
   */
  function parseCount(raw) {
    const trimmed = (raw || '').trim();
    if (!trimmed) {
      return null;
    }
    const value = Number(trimmed);
    return Number.isFinite(value) && value > 0 ? Math.floor(value) : null;
  }

  function parseBudget(raw) {
    const trimmed = String(raw == null ? '' : raw).trim();
    if (trimmed === '') {
      return null;
    }
    const value = Number(trimmed);
    if (Number.isNaN(value) || value < 0) {
      throw new Error(t('예산은 0 이상의 숫자여야 한다.'));
    }
    return value;
  }

  // -- 모달 -----------------------------------------------------------------

  /**
   * 모달을 닫는다.
   */
  function closeModal() {
    dom.modalRoot.hidden = true;
    dom.modalRoot.replaceChildren();
  }

  /**
   * 폼 모달을 연다.
   *
   * @param {string} title 제목.
   * @param {!Array<!Object>} fields 필드 정의 목록.
   * @param {function(!Object):!Promise<void>} onSubmit 제출 처리기.
   */
  function openFormModal(title, fields, onSubmit) {
    dom.modalRoot.replaceChildren();

    const dialog = document.createElement('div');
    dialog.className = 'modal';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-label', title);

    const heading = document.createElement('h3');
    heading.textContent = title;
    dialog.appendChild(heading);

    const form = document.createElement('form');
    /** @type {!Object<string, !HTMLElement>} */
    const inputs = {};

    fields.forEach(function (field) {
      const wrap = document.createElement('div');
      wrap.className = 'modal-field';

      const label = document.createElement('label');
      label.textContent = field.label;
      const inputId = 'modal-' + field.name;
      label.setAttribute('for', inputId);
      wrap.appendChild(label);

      const input = document.createElement('input');
      input.id = inputId;
      // name 을 함께 둔다. 폼 의미가 명확해지고 브라우저 자동완성이
      // 필드를 구분할 수 있다.
      input.name = field.name;
      input.type = field.type || 'text';
      if (field.placeholder) {
        input.placeholder = field.placeholder;
      }
      if (field.value !== undefined && field.value !== null) {
        input.value = String(field.value);
      }
      if (field.readonly) {
        input.readOnly = true;
        input.classList.add('readonly');
      }
      if (field.step) {
        input.step = field.step;
      }
      inputs[field.name] = input;
      wrap.appendChild(input);

      if (field.hint) {
        const hint = document.createElement('p');
        hint.className = 'modal-hint';
        hint.textContent = field.hint;
        wrap.appendChild(hint);
      }
      form.appendChild(wrap);
    });

    const error = document.createElement('p');
    error.className = 'modal-error';
    error.setAttribute('role', 'alert');
    form.appendChild(error);

    const actions = document.createElement('div');
    actions.className = 'modal-actions';

    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.textContent = t('취소');
    cancel.addEventListener('click', closeModal);

    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.className = 'primary';
    submit.textContent = t('저장');

    actions.appendChild(cancel);
    actions.appendChild(submit);
    form.appendChild(actions);

    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      error.textContent = '';
      const values = {};
      fields.forEach(function (field) {
        values[field.name] = inputs[field.name].value;
      });
      submit.disabled = true;
      try {
        // onSubmit 이 truthy 를 반환하면 이 폼 모달을 닫지 않는다. 발급·
        // 재발급처럼 이어서 다른 모달(평문 키 표시)을 여는 흐름에서, 여기서
        // closeModal 을 부르면 방금 연 키 모달까지 닫혀 키를 볼 수 없다.
        const keepOpen = await onSubmit(values);
        if (!keepOpen) {
          closeModal();
        }
      } catch (submitError) {
        error.textContent = submitError.message;
      } finally {
        submit.disabled = false;
      }
    });

    dialog.appendChild(form);
    dom.modalRoot.appendChild(dialog);
    dom.modalRoot.hidden = false;

    const firstInput = fields.find(function (field) {
      return !field.readonly;
    });
    if (firstInput && inputs[firstInput.name].focus) {
      inputs[firstInput.name].focus();
    }
  }

  /**
   * 확인 대화상자를 연다.
   *
   * @param {string} message 안내 문구.
   * @param {function():!Promise<(boolean|void)>} onConfirm 확인 처리기.
   *     truthy 를 반환하면 이 대화상자를 닫지 않는다(예: 이어서 평문 키
   *     모달을 여는 재발급 흐름).
   * @param {{confirmLabel?: string, confirmKind?: string}=} options 표시 옵션.
   */
  function openConfirm(message, onConfirm, options) {
    const opts = options || {};
    dom.modalRoot.replaceChildren();

    const dialog = document.createElement('div');
    dialog.className = 'modal';
    dialog.setAttribute('role', 'alertdialog');
    dialog.setAttribute('aria-modal', 'true');

    const text = document.createElement('p');
    text.textContent = message;
    dialog.appendChild(text);

    const error = document.createElement('p');
    error.className = 'modal-error';
    error.setAttribute('role', 'alert');
    dialog.appendChild(error);

    const actions = document.createElement('div');
    actions.className = 'modal-actions';

    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.textContent = t('취소');
    cancel.addEventListener('click', closeModal);

    const confirm = document.createElement('button');
    confirm.type = 'button';
    confirm.className = opts.confirmKind || 'danger';
    confirm.textContent = opts.confirmLabel || t('삭제');
    confirm.addEventListener('click', async function () {
      error.textContent = '';
      confirm.disabled = true;
      try {
        const keepOpen = await onConfirm();
        if (!keepOpen) {
          closeModal();
        }
      } catch (confirmError) {
        error.textContent = confirmError.message;
        confirm.disabled = false;
      }
    });

    actions.appendChild(cancel);
    actions.appendChild(confirm);
    dialog.appendChild(actions);

    dom.modalRoot.appendChild(dialog);
    dom.modalRoot.hidden = false;
  }

  // -- 표 만들기 ------------------------------------------------------------

  /**
   * 관리 표를 만든다.
   *
   * @param {!Array<string>} headers 헤더 라벨.
   * @param {!Array<!Array<(string|!Node)>>} rows 셀 값.
   * @returns {!HTMLElement} 표를 감싼 요소.
   */
  function buildTable(headers, rows) {
    const wrap = document.createElement('div');
    wrap.className = 'table-wrap';

    const table = document.createElement('table');
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    headers.forEach(function (title) {
      const th = document.createElement('th');
      th.scope = 'col';
      th.textContent = title;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    if (!rows.length) {
      const emptyRow = document.createElement('tr');
      emptyRow.className = 'empty-row';
      const cell = document.createElement('td');
      cell.colSpan = headers.length;
      cell.textContent = t('항목이 없다.');
      emptyRow.appendChild(cell);
      tbody.appendChild(emptyRow);
    } else {
      rows.forEach(function (cells) {
        const tableRow = document.createElement('tr');
        cells.forEach(function (value) {
          const cell = document.createElement('td');
          if (value instanceof Node) {
            cell.appendChild(value);
          } else {
            cell.textContent =
              value === undefined || value === null || value === ''
                ? '-'
                : String(value);
          }
          tableRow.appendChild(cell);
        });
        tbody.appendChild(tableRow);
      });
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  /**
   * 액션 버튼 묶음을 만든다.
   *
   * @param {!Array<!Object>} specs {label, kind?, onClick} 목록.
   * @returns {!HTMLElement} 버튼을 감싼 요소.
   */
  function buildActions(specs) {
    const group = document.createElement('div');
    group.className = 'row-actions';
    specs.forEach(function (spec) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = spec.label;
      if (spec.kind) {
        button.className = spec.kind;
      }
      button.addEventListener('click', spec.onClick);
      group.appendChild(button);
    });
    return group;
  }

  /**
   * 상태 배지를 만든다.
   *
   * @param {string} status `active` 또는 `disabled`.
   * @returns {!HTMLElement} 배지 요소.
   */
  function statusBadge(status) {
    const badge = document.createElement('span');
    const ok = status === 'active';
    badge.className = 'badge ' + (ok ? 'badge-ok' : 'badge-error');
    badge.textContent = ok ? t('활성') : t('비활성');
    return badge;
  }

  /**
   * "새로 만들기" 버튼이 달린 툴바를 만든다.
   *
   * @param {string} label 버튼 라벨.
   * @param {function():void} onClick 처리기.
   * @returns {!HTMLElement} 툴바 요소.
   */
  function buildToolbar(label, onClick) {
    const bar = document.createElement('div');
    bar.className = 'manage-toolbar';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'primary';
    button.textContent = label;
    button.addEventListener('click', onClick);
    bar.appendChild(button);
    return bar;
  }

  // -- 렌더링: 계정 ---------------------------------------------------------

  /**
   * 계정 관리 화면을 그린다.
   *
   * @returns {!Promise<void>}
   */
  async function renderAccounts() {
    const body = await api('GET', '/admin/accounts');
    const accounts = body.data || [];

    const container = document.createElement('div');
    container.appendChild(
      buildToolbar(t('계정 만들기'), function () {
        openFormModal(
          t('계정 만들기'),
          [
            {
              name: 'account_id',
              label: t('계정 ID'),
              placeholder: t('소문자·숫자·하이픈'),
            },
            { name: 'name', label: t('이름') },
            {
              name: 'monthly_budget_usd',
              label: t('월 예산 (USD)'),
              type: 'number',
              step: '0.01',
              hint: t('비우면 무제한'),
            },
          ],
          async function (values) {
            await api('POST', '/admin/accounts', {
              account_id: values.account_id.trim(),
              name: values.name.trim(),
              monthly_budget_usd: parseBudget(values.monthly_budget_usd),
            });
            setStatus(t('계정을 만들었다.'), 'ok');
            await renderManage();
            await syncAccountSelect();
          }
        );
      })
    );

    const rows = accounts.map(function (account) {
      const actions = buildActions([
        {
          label: t('수정'),
          onClick: function () {
            openFormModal(
              t('계정 수정: ') + account.account_id,
              [
                { name: 'name', label: t('이름'), value: account.name },
                {
                  name: 'monthly_budget_usd',
                  label: t('월 예산 (USD)'),
                  type: 'number',
                  step: '0.01',
                  value: account.monthly_budget_usd,
                  hint: t('비우면 무제한'),
                },
              ],
              async function (values) {
                await api(
                  'PATCH',
                  '/admin/accounts/' + encodeURIComponent(account.account_id),
                  {
                    name: values.name.trim(),
                    monthly_budget_usd: parseBudget(values.monthly_budget_usd),
                  }
                );
                setStatus(t('계정을 수정했다.'), 'ok');
                await renderManage();
                await syncAccountSelect();
              }
            );
          },
        },
        {
          label: account.status === 'active' ? t('비활성화') : t('활성화'),
          onClick: async function () {
            const next =
              account.status === 'active' ? 'disabled' : 'active';
            await api(
              'POST',
              '/admin/accounts/' +
                encodeURIComponent(account.account_id) + '/status',
              { status: next }
            );
            setStatus(t('계정 상태를 변경했다.'), 'ok');
            await renderManage();
            await syncAccountSelect();
          },
        },
        {
          label: t('삭제'),
          kind: 'danger',
          onClick: function () {
            openConfirm(
              account.account_id +
                t(' 계정을 삭제한다. 하위 팀·사용자·키가 있으면 거부된다.'),
              async function () {
                await api(
                  'DELETE',
                  '/admin/accounts/' + encodeURIComponent(account.account_id)
                );
                setStatus(t('계정을 삭제했다.'), 'ok');
                await renderManage();
                await syncAccountSelect();
              }
            );
          },
        },
      ]);
      return [
        account.account_id,
        account.name,
        statusBadge(account.status),
        budgetLabel(account.monthly_budget_usd),
        actions,
      ];
    });

    container.appendChild(
      buildTable([t('계정 ID'), t('이름'), t('상태'), t('월 예산'), t('작업')], rows)
    );
    dom.managePanel.replaceChildren(container);
  }

  // -- 렌더링: 팀 -----------------------------------------------------------

  /**
   * 현재 선택된 계정 ID 를 반환한다. 없으면 예외를 던진다.
   *
   * @returns {string} 계정 ID.
   */
  function requireAccountId() {
    const accountId = dom.account.value;
    if (!accountId) {
      throw new Error(t('계정을 먼저 선택한다.'));
    }
    return accountId;
  }

  /**
   * 팀 관리 화면을 그린다.
   *
   * @returns {!Promise<void>}
   */
  async function renderTeams() {
    const accountId = requireAccountId();
    const base = '/admin/accounts/' + encodeURIComponent(accountId);
    const body = await api('GET', base + '/teams');
    const teams = body.data || [];

    const container = document.createElement('div');
    container.appendChild(
      buildToolbar(t('팀 만들기'), function () {
        openFormModal(
          t('팀 만들기 (') + accountId + ')',
          [
            {
              name: 'team_id',
              label: t('팀 ID'),
              placeholder: t('소문자·숫자·하이픈'),
            },
            { name: 'name', label: t('이름') },
            {
              name: 'monthly_budget_usd',
              label: t('월 예산 (USD)'),
              type: 'number',
              step: '0.01',
              hint: t('비우면 계정 예산만 적용'),
            },
          ],
          async function (values) {
            await api('POST', base + '/teams', {
              team_id: values.team_id.trim(),
              name: values.name.trim(),
              monthly_budget_usd: parseBudget(values.monthly_budget_usd),
            });
            setStatus(t('팀을 만들었다.'), 'ok');
            await renderManage();
          }
        );
      })
    );

    const rows = teams.map(function (team) {
      const actions = buildActions([
        {
          label: t('수정'),
          onClick: function () {
            openFormModal(
              t('팀 수정: ') + team.team_id,
              [
                { name: 'name', label: t('이름'), value: team.name },
                {
                  name: 'monthly_budget_usd',
                  label: t('월 예산 (USD)'),
                  type: 'number',
                  step: '0.01',
                  value: team.monthly_budget_usd,
                  hint: t('비우면 계정 예산만 적용'),
                },
              ],
              async function (values) {
                await api(
                  'PATCH',
                  base + '/teams/' + encodeURIComponent(team.team_id),
                  {
                    name: values.name.trim(),
                    monthly_budget_usd: parseBudget(values.monthly_budget_usd),
                  }
                );
                setStatus(t('팀을 수정했다.'), 'ok');
                await renderManage();
              }
            );
          },
        },
        {
          label: team.status === 'active' ? t('비활성화') : t('활성화'),
          onClick: async function () {
            const next = team.status === 'active' ? 'disabled' : 'active';
            await api(
              'POST',
              base + '/teams/' + encodeURIComponent(team.team_id) + '/status',
              { status: next }
            );
            setStatus(t('팀 상태를 변경했다.'), 'ok');
            await renderManage();
          },
        },
        {
          label: t('삭제'),
          kind: 'danger',
          onClick: function () {
            openConfirm(
              team.team_id + t(' 팀을 삭제한다. 소속 사용자·키가 있으면 거부된다.'),
              async function () {
                await api(
                  'DELETE',
                  base + '/teams/' + encodeURIComponent(team.team_id)
                );
                setStatus(t('팀을 삭제했다.'), 'ok');
                await renderManage();
              }
            );
          },
        },
      ]);
      return [
        team.team_id,
        team.name,
        statusBadge(team.status),
        budgetLabel(team.monthly_budget_usd),
        actions,
      ];
    });

    container.appendChild(
      buildTable([t('팀 ID'), t('이름'), t('상태'), t('월 예산'), t('작업')], rows)
    );
    dom.managePanel.replaceChildren(container);
  }

  // -- 렌더링: 사용자 -------------------------------------------------------

  /**
   * 사용자 관리 화면을 그린다.
   *
   * @returns {!Promise<void>}
   */
  async function renderUsers() {
    const accountId = requireAccountId();
    const base = '/admin/accounts/' + encodeURIComponent(accountId);
    const body = await api('GET', base + '/users');
    const users = body.data || [];

    const container = document.createElement('div');
    container.appendChild(
      buildToolbar(t('사용자 만들기'), function () {
        openFormModal(
          t('사용자 만들기 (') + accountId + ')',
          [
            {
              name: 'user_id',
              label: t('사용자 ID'),
              placeholder: t('소문자·숫자·. _ -'),
            },
            { name: 'name', label: t('이름') },
            { name: 'email', label: t('이메일'), type: 'email' },
            {
              name: 'team_id',
              label: t('팀 ID'),
              hint: t('비우면 팀 없음. 존재하는 팀이어야 한다.'),
            },
            {
              name: 'monthly_budget_usd',
              label: t('월 예산 (USD)'),
              type: 'number',
              step: '0.01',
              hint: t('비우면 상위 예산만 적용'),
            },
            {
              name: 'rpm_limit',
              label: t('분당 요청 한도'),
              type: 'number',
              step: '1',
              hint: t('비우면 제한 없음. 예산 초과폭을 줄이는 데 함께 쓴다.'),
            },
          ],
          async function (values) {
            const payload = {
              user_id: values.user_id.trim(),
              name: values.name.trim(),
              monthly_budget_usd: parseBudget(values.monthly_budget_usd),
              rpm_limit: parseCount(values.rpm_limit),
            };
            if (values.email.trim()) {
              payload.email = values.email.trim();
            }
            if (values.team_id.trim()) {
              payload.team_id = values.team_id.trim();
            }
            await api('POST', base + '/users', payload);
            setStatus(t('사용자를 만들었다.'), 'ok');
            await renderManage();
          }
        );
      })
    );

    const rows = users.map(function (user) {
      const actions = buildActions([
        {
          label: t('수정'),
          onClick: function () {
            openFormModal(
              t('사용자 수정: ') + user.user_id,
              [
                { name: 'name', label: t('이름'), value: user.name },
                {
                  name: 'email',
                  label: t('이메일'),
                  type: 'email',
                  value: user.email,
                },
                {
                  name: 'team_id',
                  label: t('팀 ID'),
                  value: user.team_id,
                  hint: t('존재하는 팀이어야 한다. 비우면 팀 없음.'),
                },
                {
                  name: 'monthly_budget_usd',
                  label: t('월 예산 (USD)'),
                  type: 'number',
                  step: '0.01',
                  value: user.monthly_budget_usd,
                  hint: t('비우면 상위 예산만 적용'),
                },
                {
                  name: 'rpm_limit',
                  label: t('분당 요청 한도'),
                  type: 'number',
                  step: '1',
                  value: user.rpm_limit,
                  hint: t('비우면 제한 없음. 예산 초과폭을 줄이는 데 함께 쓴다.'),
                },
              ],
              async function (values) {
                await api(
                  'PATCH',
                  base + '/users/' + encodeURIComponent(user.user_id),
                  {
                    name: values.name.trim(),
                    email: values.email.trim(),
                    team_id: values.team_id.trim(),
                    monthly_budget_usd: parseBudget(values.monthly_budget_usd),
                    rpm_limit: parseCount(values.rpm_limit),
                  }
                );
                setStatus(t('사용자를 수정했다.'), 'ok');
                await renderManage();
              }
            );
          },
        },
        {
          label: user.status === 'active' ? t('비활성화') : t('활성화'),
          onClick: async function () {
            const next = user.status === 'active' ? 'disabled' : 'active';
            await api(
              'POST',
              base + '/users/' + encodeURIComponent(user.user_id) + '/status',
              { status: next }
            );
            setStatus(t('사용자 상태를 변경했다.'), 'ok');
            await renderManage();
          },
        },
        {
          label: t('삭제'),
          kind: 'danger',
          onClick: function () {
            openConfirm(
              user.user_id + t(' 사용자를 삭제한다. 소유 키가 있으면 거부된다.'),
              async function () {
                await api(
                  'DELETE',
                  base + '/users/' + encodeURIComponent(user.user_id)
                );
                setStatus(t('사용자를 삭제했다.'), 'ok');
                await renderManage();
              }
            );
          },
        },
      ]);
      return [
        user.user_id,
        user.name,
        user.team_id || '-',
        statusBadge(user.status),
        budgetLabel(user.monthly_budget_usd),
        actions,
      ];
    });

    container.appendChild(
      buildTable(
        [t('사용자 ID'), t('이름'), t('팀'), t('상태'), t('월 예산'), t('작업')],
        rows
      )
    );
    dom.managePanel.replaceChildren(container);
  }

  // -- 렌더링: API 키 -------------------------------------------------------

  /**
   * 발급·재발급된 평문 키를 보여주는 모달을 연다.
   *
   * @param {string} plaintext 평문 키.
   */
  function showPlaintextKey(plaintext) {
    dom.modalRoot.replaceChildren();

    const dialog = document.createElement('div');
    dialog.className = 'modal';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-label', t('발급된 API 키'));

    const heading = document.createElement('h3');
    heading.textContent = t('발급된 API 키');
    dialog.appendChild(heading);

    const note = document.createElement('p');
    note.className = 'modal-hint';
    note.textContent =
      t('이 키는 지금만 볼 수 있다. 안전한 곳에 보관한다. 창을 닫으면 다시 볼 수 없다.');
    dialog.appendChild(note);

    const code = document.createElement('pre');
    code.className = 'key-plaintext';
    code.textContent = plaintext;
    dialog.appendChild(code);

    const actions = document.createElement('div');
    actions.className = 'modal-actions';

    const copy = document.createElement('button');
    copy.type = 'button';
    copy.textContent = t('복사');
    copy.addEventListener('click', function () {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(plaintext).then(function () {
          copy.textContent = t('복사됨');
        });
      }
    });

    const done = document.createElement('button');
    done.type = 'button';
    done.className = 'primary';
    done.textContent = t('닫기');
    done.addEventListener('click', closeModal);

    actions.appendChild(copy);
    actions.appendChild(done);
    dialog.appendChild(actions);

    dom.modalRoot.appendChild(dialog);
    dom.modalRoot.hidden = false;
  }

  /**
   * API 키 관리 화면을 그린다.
   *
   * @returns {!Promise<void>}
   */
  async function renderKeys() {
    const accountId = requireAccountId();
    const base = '/admin/accounts/' + encodeURIComponent(accountId);
    const body = await api('GET', base + '/keys');
    const keys = body.data || [];

    const container = document.createElement('div');
    container.appendChild(
      buildToolbar(t('키 발급'), function () {
        openFormModal(
          t('키 발급 (') + accountId + ')',
          [
            {
              name: 'user_id',
              label: t('사용자 ID'),
              hint: t('존재하는 사용자여야 한다. 팀은 사용자에서 상속된다.'),
            },
            { name: 'name', label: t('이름(메모)') },
            {
              name: 'allowed_models',
              label: t('허용 모델'),
              hint: t('쉼표로 구분. 비우면 서버 기본 정책.'),
            },
            {
              name: 'monthly_budget_usd',
              label: t('월 예산 (USD)'),
              type: 'number',
              step: '0.01',
              hint: t('비우면 상위 예산만 적용'),
            },
            {
              name: 'rpm_limit',
              label: t('분당 요청 한도'),
              type: 'number',
              step: '1',
              hint: t('비우면 사용자 설정을 따른다.'),
            },
            {
              name: 'expires_at',
              label: t('만료 시각'),
              placeholder: '2027-01-01T00:00:00Z',
              hint: t('비우면 만료 없음. ISO-8601 UTC. 만료된 키는 401 이 되고 키는 남는다.'),
            },
          ],
          async function (values) {
            const created = await api('POST', base + '/keys', {
              user_id: values.user_id.trim(),
              name: values.name.trim(),
              allowed_models: splitModels(values.allowed_models),
              monthly_budget_usd: parseBudget(values.monthly_budget_usd),
              rpm_limit: parseCount(values.rpm_limit),
              expires_at: values.expires_at.trim() || null,
            });
            setStatus(t('키를 발급했다.'), 'ok');
            await renderManage();
            if (created && created.api_key) {
              showPlaintextKey(created.api_key);
              // 평문 키 모달을 열었으므로 폼 모달의 자동 닫기를 막는다.
              return true;
            }
            return false;
          }
        );
      })
    );

    const rows = keys.map(function (key) {
      const actions = buildActions([
        {
          label: t('수정'),
          onClick: function () {
            openFormModal(
              t('키 수정: ') + key.key_id,
              [
                { name: 'name', label: t('이름(메모)'), value: key.name },
                {
                  name: 'allowed_models',
                  label: t('허용 모델'),
                  value: (key.allowed_models || []).join(', '),
                  hint: t('쉼표로 구분. 비우면 서버 기본 정책.'),
                },
                {
                  name: 'monthly_budget_usd',
                  label: t('월 예산 (USD)'),
                  type: 'number',
                  step: '0.01',
                  value: key.monthly_budget_usd,
                  hint: t('비우면 상위 예산만 적용'),
                },
                {
                  name: 'rpm_limit',
                  label: t('분당 요청 한도'),
                  type: 'number',
                  step: '1',
                  value: key.rpm_limit,
                  hint: t('비우면 사용자 설정을 따른다.'),
                },
                {
                  name: 'expires_at',
                  label: t('만료 시각'),
                  value: key.expires_at,
                  placeholder: '2027-01-01T00:00:00Z',
                  hint: t('비우면 만료 없음. ISO-8601 UTC. 만료된 키는 401 이 되고 키는 남는다.'),
                },
              ],
              async function (values) {
                await api(
                  'PATCH',
                  base + '/keys/' + encodeURIComponent(key.key_id),
                  {
                    name: values.name.trim(),
                    allowed_models: splitModels(values.allowed_models),
                    monthly_budget_usd: parseBudget(values.monthly_budget_usd),
                    rpm_limit: parseCount(values.rpm_limit),
                    expires_at: values.expires_at.trim() || null,
                  }
                );
                setStatus(t('키를 수정했다.'), 'ok');
                await renderManage();
              }
            );
          },
        },
        {
          label: key.status === 'active' ? t('비활성화') : t('활성화'),
          onClick: async function () {
            const next = key.status === 'active' ? 'disabled' : 'active';
            await api(
              'POST',
              base + '/keys/' + encodeURIComponent(key.key_id) + '/status',
              { status: next }
            );
            setStatus(t('키 상태를 변경했다.'), 'ok');
            await renderManage();
          },
        },
        {
          label: t('재발급'),
          onClick: function () {
            openConfirm(
              key.key_id +
                t(' 키를 재발급한다. 옛 키는 즉시 무효가 되고 새 키가 발급된다.'),
              async function () {
                const rotated = await api(
                  'POST',
                  base + '/keys/' + encodeURIComponent(key.key_id) + '/rotate'
                );
                setStatus(t('키를 재발급했다.'), 'ok');
                await renderManage();
                if (rotated && rotated.api_key) {
                  showPlaintextKey(rotated.api_key);
                  // 평문 키 모달을 열었으므로 확인 대화상자의 자동 닫기를 막는다.
                  return true;
                }
                return false;
              },
              { confirmLabel: t('재발급'), confirmKind: 'primary' }
            );
          },
        },
        {
          label: t('삭제'),
          kind: 'danger',
          onClick: function () {
            openConfirm(
              key.key_id + t(' 키를 삭제한다. 이 키는 즉시 무효가 된다.'),
              async function () {
                await api(
                  'DELETE',
                  base + '/keys/' + encodeURIComponent(key.key_id)
                );
                setStatus(t('키를 삭제했다.'), 'ok');
                await renderManage();
              }
            );
          },
        },
      ]);
      return [
        key.key_id,
        key.key_prefix + '…',
        key.name || '-',
        key.user_id,
        key.team_id || '-',
        statusBadge(key.status),
        budgetLabel(key.monthly_budget_usd),
        actions,
      ];
    });

    container.appendChild(
      buildTable(
        [t('키 ID'), t('접두어'), t('이름'), t('사용자'), t('팀'), t('상태'), t('월 예산'), t('작업')],
        rows
      )
    );
    dom.managePanel.replaceChildren(container);
  }

  /**
   * 쉼표 구분 모델 문자열을 배열로 만든다.
   *
   * @param {string} raw 입력 문자열.
   * @returns {!Array<string>} 모델 ID 목록.
   */
  function splitModels(raw) {
    return String(raw || '')
      .split(',')
      .map(function (item) {
        return item.trim();
      })
      .filter(function (item) {
        return item.length > 0;
      });
  }

  // -- 라우팅 ---------------------------------------------------------------


  /**
   * 계정별 외부 인증(OIDC) 연동 화면을 그린다.
   *
   * 고객이 이미 쓰는 인증 서버(Amazon Cognito, Okta, Azure AD, Google)를
   * 계정에 붙인다. 붙이면 그 계정 사용자는 API 키 없이 IdP 토큰으로 호출할
   * 수 있고, 관리자 그룹에 속한 사람은 공유 관리 토큰 없이 자기 계정을
   * 관리할 수 있다.
   *
   * @returns {!Promise<void>}
   */
  async function renderAuthConfig() {
    const accountId = requireAccountId();
    const base = '/admin/accounts/' + encodeURIComponent(accountId);
    const config = await api('GET', base + '/auth');
    const configured = config.configured === true;

    const container = document.createElement('div');

    const hint = document.createElement('p');
    hint.className = 'manage-hint';
    hint.textContent =
      t('인증 서버를 연결하면 이 계정 사용자는 API 키 없이 IdP 액세스 토큰으로') +
      t(' 호출할 수 있다. 관리자 그룹에 속한 사람은 공유 관리 토큰 없이 이') +
      t(' 계정을 관리한다. 발급자는 계정 간에 겹칠 수 없다.');
    container.appendChild(hint);

    const toolbar = document.createElement('div');
    toolbar.className = 'manage-toolbar';

    const editButton = document.createElement('button');
    editButton.type = 'button';
    editButton.className = 'primary';
    editButton.textContent = configured ? t('설정 수정') : t('인증 서버 연결');
    editButton.addEventListener('click', function () {
      openAuthConfigForm(accountId, base, configured ? config : null);
    });
    toolbar.appendChild(editButton);

    if (configured) {
      const toggle = document.createElement('button');
      toggle.type = 'button';
      const disabling = config.status === 'active';
      toggle.textContent = disabling ? t('즉시 차단') : t('다시 허용');
      toggle.addEventListener('click', async function () {
        try {
          await api('POST', base + '/auth/status', {
            status: disabling ? 'disabled' : 'active',
          });
          setStatus(
            disabling ? t('외부 인증을 차단했다.') : t('외부 인증을 허용했다.'),
            'ok'
          );
          renderManage();
        } catch (error) {
          setStatus(t('상태 변경 실패: ') + error.message, 'error');
        }
      });
      toolbar.appendChild(toggle);

      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = t('연결 해제');
      remove.addEventListener('click', function () {
        openConfirm(
          t('이 계정의 외부 인증 설정을 삭제한다. 발급자 등록도 함께 사라져') +
            t(' 다른 계정이 같은 발급자를 쓸 수 있게 된다. IdP 토큰으로') +
            t(' 호출하던 사용자는 즉시 차단된다.'),
          async function () {
            await api('DELETE', base + '/auth');
            setStatus(t('외부 인증 설정을 삭제했다.'), 'ok');
            renderManage();
          },
          { confirmLabel: t('연결 해제'), confirmKind: 'danger' }
        );
      });
      toolbar.appendChild(remove);
    }
    container.appendChild(toolbar);

    if (!configured) {
      const empty = document.createElement('p');
      empty.className = 'chart-empty';
      empty.textContent =
        t('아직 연결된 인증 서버가 없다. 이 계정은 API 키로만 호출할 수 있다.');
      container.appendChild(empty);
      dom.managePanel.replaceChildren(container);
      return;
    }

    const rows = [
      [t('상태'), statusBadge(config.status)],
      [t('발급자 (iss)'), codeCell(config.issuer)],
      ['JWKS URL', codeCell(config.effective_jwks_url)],
      [t('허용 클라이언트 (aud)'), textCell(config.audience || t('(검사 안 함)'))],
      [t('사용자 클레임'), codeCell(config.user_claim)],
      [t('팀 클레임'), codeCell(config.team_claim || t('(사용 안 함)'))],
      [t('그룹 클레임'), codeCell(config.groups_claim)],
      [
        t('계정 관리자 그룹'),
        textCell(config.admin_groups || t('(없음 — 관리 토큰만 사용)')),
      ],
      [
        t('미등록 사용자 자동 생성'),
        textCell(config.auto_provision ? t('켜짐') : t('꺼짐')),
      ],
      [
        t('자동 생성 허용 모델'),
        textCell(config.provision_allowed_models || t('(제한 없음)')),
      ],
      [
        t('자동 생성 월 예산'),
        textCell(
          config.provision_budget_usd === null
            ? t('(미설정)')
            : '$' + config.provision_budget_usd
        ),
      ],
      [t('수정 시각'), textCell(config.updated_at || '-')],
    ];
    container.appendChild(buildTable([t('항목'), t('값')], rows));
    dom.managePanel.replaceChildren(container);
  }

  /**
   * 값을 담은 표 셀을 만든다.
   *
   * @param {string} value 표시할 값.
   * @returns {!HTMLElement} 셀 내용.
   */
  function textCell(value) {
    const span = document.createElement('span');
    span.textContent = value;
    return span;
  }

  /**
   * 식별자를 코드 스타일로 표시하는 셀을 만든다.
   *
   * @param {string} value 표시할 값.
   * @returns {!HTMLElement} 셀 내용.
   */
  function codeCell(value) {
    const code = document.createElement('code');
    code.textContent = value || '-';
    return code;
  }

  /**
   * 상태 배지를 만든다.
   *
   * @param {string} status `active` 또는 `disabled`.
   * @returns {!HTMLElement} 배지 요소.
   */
  function statusBadge(status) {
    const badge = document.createElement('span');
    badge.className =
      'badge ' + (status === 'active' ? 'badge-ok' : 'badge-error');
    badge.textContent = status === 'active' ? t('활성') : t('차단됨');
    return badge;
  }

  /**
   * 인증 설정 입력 폼을 연다.
   *
   * @param {string} accountId 계정 ID.
   * @param {string} base 계정 API 기준 경로.
   * @param {?Object} current 기존 설정. 신규면 `null`.
   */
  function openAuthConfigForm(accountId, base, current) {
    const existing = current || {};
    openFormModal(
      (current ? t('인증 설정 수정') : t('인증 서버 연결')) + ' (' + accountId + ')',
      [
        {
          name: 'issuer',
          label: t('발급자 URL (iss)'),
          value: existing.issuer || '',
          placeholder:
            t('https://cognito-idp.<리전>.amazonaws.com/<유저풀ID>'),
          hint: t('토큰의 iss 와 정확히 일치해야 한다. 계정 간 중복 불가.'),
        },
        {
          name: 'jwks_url',
          label: t('JWKS URL (선택)'),
          value: existing.jwks_url || '',
          hint:
            t('비우면 발급자 + /.well-known/jwks.json 을 쓴다. https 만') +
            t(' 허용하며 내부 네트워크 주소는 거부된다.'),
        },
        {
          name: 'audience',
          label: t('허용 클라이언트 ID (선택)'),
          value: existing.audience || '',
          hint: t('쉼표로 구분. 비우면 청중을 검사하지 않는다.'),
        },
        {
          name: 'user_claim',
          label: t('사용자 클레임'),
          value: existing.user_claim || 'email',
          hint: t('사용자 ID 로 쓸 클레임. Cognito 는 보통 email 이다.'),
        },
        {
          name: 'team_claim',
          label: t('팀 클레임 (선택)'),
          value: existing.team_claim || '',
          hint: t('예: custom:team_id. 비우면 팀 없이 동작한다.'),
        },
        {
          name: 'groups_claim',
          label: t('그룹 클레임'),
          value: existing.groups_claim || 'cognito:groups',
        },
        {
          name: 'admin_groups',
          label: t('계정 관리자 그룹 (선택)'),
          value: existing.admin_groups || '',
          hint:
            t('이 그룹에 속한 사람은 관리 토큰 없이 이 계정을 관리한다.') +
            t(' 쉼표로 구분.'),
        },
        {
          name: 'auto_provision',
          label: t('미등록 사용자 자동 생성 (yes/no)'),
          value: existing.auto_provision ? 'yes' : 'no',
          hint:
            t('yes 로 하면 IdP 에 로그인한 사람이 곧 사용자가 된다. 이때') +
            t(' 월 예산을 반드시 지정해야 한다.'),
        },
        {
          name: 'provision_allowed_models',
          label: t('자동 생성 허용 모델 (선택)'),
          value: existing.provision_allowed_models || '',
          hint: t('쉼표로 구분. 비우면 서버 기본값을 따른다.'),
        },
        {
          name: 'provision_budget_usd',
          label: t('자동 생성 월 예산 (USD)'),
          type: 'number',
          step: '0.01',
          value:
            existing.provision_budget_usd === null ||
            existing.provision_budget_usd === undefined
              ? ''
              : existing.provision_budget_usd,
          hint: t('자동 생성을 켜면 필수다.'),
        },
      ],
      async function (values) {
        const autoProvision =
          values.auto_provision.trim().toLowerCase() === 'yes';
        await api('PUT', base + '/auth', {
          issuer: values.issuer.trim(),
          jwks_url: values.jwks_url.trim(),
          audience: values.audience.trim(),
          user_claim: values.user_claim.trim() || 'email',
          team_claim: values.team_claim.trim(),
          groups_claim: values.groups_claim.trim() || 'cognito:groups',
          admin_groups: values.admin_groups.trim(),
          auto_provision: autoProvision,
          provision_allowed_models: values.provision_allowed_models.trim(),
          provision_budget_usd: parseBudget(values.provision_budget_usd),
        });
        setStatus(t('외부 인증 설정을 저장했다.'), 'ok');
        renderManage();
      }
    );
  }

  /**
   * 현재 선택된 관리 뷰를 그린다.
   *
   * @returns {!Promise<void>}
   */
  /**
   * 가드레일 탭을 그린다.
   *
   * 안전 통제를 다루는 화면이라 두 가지를 화면에 드러낸다.
   *
   * 1. 기준선이 설정돼 있는지. 없으면 이 계정은 가드레일 없이 동작한다.
   * 2. 누가 면제됐는지와 그 사유. 면제에 만료가 없으므로 화면에서 검토할 수
   *    있어야 임시 예외가 영구화되는 것을 사람이 막을 수 있다.
   */
  async function renderGuardrail() {
    const accountId = requireAccountId();
    const base = '/admin/accounts/' + encodeURIComponent(accountId);
    const [config, teams, users] = await Promise.all([
      api('GET', base + '/guardrail'),
      api('GET', base + '/teams'),
      api('GET', base + '/users'),
    ]);
    const configured = config.configured === true;

    const container = document.createElement('div');

    const hint = document.createElement('p');
    hint.className = 'manage-hint';
    hint.textContent =
      t('Amazon Bedrock Guardrails 를 게이트웨이가 모든 요청에 붙인다.') +
      t(' 콘솔에서 가드레일을 만들어도 호출마다 식별자를 실어야 적용되므로,') +
      t(' 게이트웨이가 붙이면 클라이언트가 빼거나 바꿀 수 없다.');
    container.appendChild(hint);

    // 기준선 상태를 눈에 띄게 보여준다. 없으면 통제가 없는 상태다.
    const state = document.createElement('div');
    state.className = configured
      ? 'guardrail-state guardrail-on'
      : 'guardrail-state guardrail-off';
    const stateText = document.createElement('strong');
    stateText.textContent = configured
      ? t('기준선 적용 중')
      : t('기준선이 없다. 이 계정은 가드레일 없이 동작한다.');
    state.appendChild(stateText);
    if (configured) {
      const detail = document.createElement('span');
      detail.textContent =
        ' · ' + config.guardrail_id + ' v' + config.guardrail_version +
        (config.enabled ? '' : ' · ' + t('꺼짐'));
      state.appendChild(detail);
    }
    container.appendChild(state);

    const toolbar = document.createElement('div');
    toolbar.className = 'manage-toolbar';

    const editButton = document.createElement('button');
    editButton.type = 'button';
    editButton.className = 'primary';
    editButton.textContent = configured
      ? t('기준선 수정')
      : t('기준선 설정');
    editButton.addEventListener('click', function () {
      openGuardrailForm(accountId, base, configured ? config : null);
    });
    toolbar.appendChild(editButton);

    if (configured) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = t('기준선 삭제');
      remove.addEventListener('click', function () {
        openConfirm(
          t('이 계정 전체가 가드레일 없이 동작하게 된다. 플랫폼 관리자만') +
            t(' 할 수 있다.'),
          async function () {
            await api('DELETE', base + '/guardrail');
            setStatus(t('기준선을 삭제했다.'), 'ok');
            renderManage();
          },
          { confirmLabel: t('삭제'), danger: true }
        );
      });
      toolbar.appendChild(remove);
    }
    container.appendChild(toolbar);

    // 면제 목록. 면제된 것만이 아니라 전체를 보여주고 상태를 표시한다.
    // 그래야 "면제가 없다" 는 것도 확인할 수 있다.
    container.appendChild(
      buildExemptionSection(
        t('팀 면제'),
        base + '/teams',
        (teams.data || []).map(function (item) {
          return {
            id: item.team_id,
            name: item.name,
            exempt: item.guardrail_exempt === true,
            reason: item.guardrail_exempt_reason || '',
          };
        }),
        'team'
      )
    );
    container.appendChild(
      buildExemptionSection(
        t('사용자 면제'),
        base + '/users',
        (users.data || []).map(function (item) {
          return {
            id: item.user_id,
            name: item.name,
            exempt: item.guardrail_exempt === true,
            reason: item.guardrail_exempt_reason || '',
          };
        }),
        'user'
      )
    );

    dom.managePanel.replaceChildren(container);
  }

  /**
   * 면제 목록 한 절을 만든다.
   *
   * @param {string} title 절 제목.
   * @param {string} listBase 대상 목록의 기준 경로.
   * @param {Array<Object>} rows 대상 목록.
   * @param {string} kind `team` 또는 `user`.
   * @returns {HTMLElement} 완성된 절.
   */
  function buildExemptionSection(title, listBase, rows, kind) {
    const section = document.createElement('section');
    section.className = 'manage-subsection';

    const heading = document.createElement('h3');
    heading.textContent = title;
    section.appendChild(heading);

    const exemptCount = rows.filter(function (row) {
      return row.exempt;
    }).length;
    const summary = document.createElement('p');
    summary.className = 'manage-hint';
    summary.textContent =
      exemptCount === 0
        ? t('면제된 대상이 없다.')
        : t('면제 ') + exemptCount + t('건. 면제에는 만료가 없으므로 정기적으로 검토한다.');
    section.appendChild(summary);

    section.appendChild(
      buildTable(
        [t('ID'), t('이름'), t('가드레일'), t('사유'), t('작업')],
        rows.map(function (row) {
          return [
            codeCell(row.id),
            textCell(row.name),
            exemptBadge(row.exempt),
            textCell(row.reason || '—'),
            buildActions([
              {
                label: row.exempt ? t('면제 해제') : t('면제'),
                onClick: function () {
                  openExemptionForm(listBase, row, kind);
                },
              },
            ]),
          ];
        })
      )
    );
    return section;
  }

  /**
   * 면제 상태 배지를 만든다.
   *
   * @param {boolean} exempt 면제 여부.
   * @returns {HTMLElement} 배지 요소.
   */
  function exemptBadge(exempt) {
    const cell = document.createElement('td');
    const badge = document.createElement('span');
    // 면제는 통제가 꺼진 상태다. 정상(적용)보다 눈에 띄어야 한다.
    badge.className = exempt ? 'badge badge-error' : 'badge badge-ok';
    badge.textContent = exempt ? t('면제') : t('적용');
    cell.appendChild(badge);
    return cell;
  }

  /**
   * 면제 설정 폼을 연다.
   *
   * @param {string} listBase 대상 목록의 기준 경로.
   * @param {Object} row 대상.
   * @param {string} kind `team` 또는 `user`.
   */
  function openExemptionForm(listBase, row, kind) {
    const path =
      listBase + '/' + encodeURIComponent(row.id) + '/guardrail-exemption';
    if (row.exempt) {
      openConfirm(
        t('면제를 해제한다. 이 대상은 다시 가드레일을 거친다.'),
        async function () {
          await api('PUT', path, { exempt: false });
          setStatus(t('면제를 해제했다.'), 'ok');
          renderManage();
        },
        { confirmLabel: t('해제') }
      );
      return;
    }
    openFormModal(
      t('면제: ') + row.id,
      [
        {
          name: 'reason',
          label: t('사유'),
          placeholder: t('예: 레드팀 평가 (TICKET-1234)'),
          hint: t('왜 통제를 껐는지 남지 않으면 나중에 검토할 수 없다. 필수다.'),
        },
      ],
      async function (values) {
        await api('PUT', path, {
          exempt: true,
          reason: values.reason,
        });
        setStatus(t('면제했다.'), 'ok');
        renderManage();
      }
    );
    void kind;
  }

  /**
   * 가드레일 기준선 폼을 연다.
   *
   * @param {string} accountId 계정 ID.
   * @param {string} base 계정 기준 경로.
   * @param {Object|null} current 현재 설정. 없으면 신규.
   */
  function openGuardrailForm(accountId, base, current) {
    openFormModal(
      current ? t('기준선 수정: ') + accountId : t('기준선 설정: ') + accountId,
      [
        {
          name: 'guardrail_id',
          label: t('가드레일 ID 또는 ARN'),
          value: current ? current.guardrail_id : '',
          placeholder: t('AWS 콘솔의 Guardrail ID'),
        },
        {
          name: 'guardrail_version',
          label: t('버전'),
          value: current ? current.guardrail_version : '',
          placeholder: '1',
          hint: t('숫자만. DRAFT 는 내용이 예고 없이 바뀌어 거부된다.'),
        },
      ],
      async function (values) {
        await api('PUT', base + '/guardrail', {
          guardrail_id: values.guardrail_id,
          guardrail_version: values.guardrail_version,
          enabled: true,
        });
        setStatus(t('기준선을 저장했다.'), 'ok');
        renderManage();
      }
    );
  }

  async function renderManage() {
    const token = dom.token.value.trim();
    if (!token) {
      dom.managePanel.replaceChildren();
      setStatus(t('관리 토큰을 입력하고 조회를 누른다.'), 'error');
      return;
    }
    try {
      if (activeManageView === 'accounts') {
        await renderAccounts();
      } else if (activeManageView === 'teams') {
        await renderTeams();
      } else if (activeManageView === 'users') {
        await renderUsers();
      } else if (activeManageView === 'auth') {
        await renderAuthConfig();
      } else if (activeManageView === 'guardrail') {
        await renderGuardrail();
      } else {
        await renderKeys();
      }
    } catch (error) {
      const message = document.createElement('p');
      message.className = 'manage-error';
      message.textContent = t('불러오기 실패: ') + error.message;
      dom.managePanel.replaceChildren(message);
      setStatus(t('불러오기 실패: ') + error.message, 'error');
    }
  }

  /**
   * 관리 대상 탭을 전환한다.
   *
   * @param {!Element} button 선택된 탭 버튼.
   */
  function selectManageTab(button) {
    document
      .querySelectorAll('[data-manage]')
      .forEach(function (tab) {
        tab.setAttribute('aria-selected', String(tab === button));
      });
    activeManageView = button.getAttribute('data-manage');
    renderManage();
  }

  /**
   * 모니터링/관리 화면을 전환한다.
   *
   * @param {string} screen `monitor` 또는 `manage`.
   */
  function selectScreen(screen) {
    const manage = screen === 'manage';
    dom.screenMonitor.hidden = manage;
    dom.screenManage.hidden = !manage;
    document.querySelectorAll('.view-tab').forEach(function (tab) {
      tab.setAttribute(
        'aria-pressed',
        String(tab.getAttribute('data-screen') === screen)
      );
    });
    if (manage) {
      renderManage();
    }
  }

  /**
   * 초기화한다.
   */
  function init() {
    document.querySelectorAll('.view-tab').forEach(function (tab) {
      tab.addEventListener('click', function () {
        selectScreen(tab.getAttribute('data-screen'));
      });
    });

    document.querySelectorAll('[data-manage]').forEach(function (tab) {
      tab.addEventListener('click', function () {
        selectManageTab(tab);
      });
    });

    // 계정 선택이 바뀌면 관리 화면도 그 계정으로 다시 그린다.
    dom.account.addEventListener('change', function () {
      if (!dom.screenManage.hidden) {
        renderManage();
      }
    });

    // 모달 배경 클릭이나 Esc 로 닫는다.
    dom.modalRoot.addEventListener('click', function (event) {
      if (event.target === dom.modalRoot) {
        closeModal();
      }
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !dom.modalRoot.hidden) {
        closeModal();
      }
    });
  }

  // 언어 전환 시 관리 화면을 다시 그린다. 관리 화면은 동적으로 만들어지므로
  // data-i18n 갱신만으로는 반영되지 않는다.
  window.LlmgwAdmin = {
    rerender: function () {
      if (!dom.screenManage.hidden) {
        renderManage();
      }
    },
  };

  init();
})();
