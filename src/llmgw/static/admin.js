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
      throw new Error('관리 토큰을 입력한다.');
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
    return value == null ? '무제한' : '$' + Number(value).toFixed(2);
  }

  /**
   * 숫자 입력값을 예산 필드로 정규화한다. 빈 값은 null(무제한)로 본다.
   *
   * @param {string} raw 입력 문자열.
   * @returns {?number} 숫자 또는 null.
   */
  function parseBudget(raw) {
    const trimmed = String(raw == null ? '' : raw).trim();
    if (trimmed === '') {
      return null;
    }
    const value = Number(trimmed);
    if (Number.isNaN(value) || value < 0) {
      throw new Error('예산은 0 이상의 숫자여야 한다.');
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
    cancel.textContent = '취소';
    cancel.addEventListener('click', closeModal);

    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.className = 'primary';
    submit.textContent = '저장';

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
    cancel.textContent = '취소';
    cancel.addEventListener('click', closeModal);

    const confirm = document.createElement('button');
    confirm.type = 'button';
    confirm.className = opts.confirmKind || 'danger';
    confirm.textContent = opts.confirmLabel || '삭제';
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
      cell.textContent = '항목이 없다.';
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
    badge.textContent = ok ? '활성' : '비활성';
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
      buildToolbar('계정 만들기', function () {
        openFormModal(
          '계정 만들기',
          [
            {
              name: 'account_id',
              label: '계정 ID',
              placeholder: '소문자·숫자·하이픈',
            },
            { name: 'name', label: '이름' },
            {
              name: 'monthly_budget_usd',
              label: '월 예산 (USD)',
              type: 'number',
              step: '0.01',
              hint: '비우면 무제한',
            },
          ],
          async function (values) {
            await api('POST', '/admin/accounts', {
              account_id: values.account_id.trim(),
              name: values.name.trim(),
              monthly_budget_usd: parseBudget(values.monthly_budget_usd),
            });
            setStatus('계정을 만들었다.', 'ok');
            await renderManage();
          }
        );
      })
    );

    const rows = accounts.map(function (account) {
      const actions = buildActions([
        {
          label: '수정',
          onClick: function () {
            openFormModal(
              '계정 수정: ' + account.account_id,
              [
                { name: 'name', label: '이름', value: account.name },
                {
                  name: 'monthly_budget_usd',
                  label: '월 예산 (USD)',
                  type: 'number',
                  step: '0.01',
                  value: account.monthly_budget_usd,
                  hint: '비우면 무제한',
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
                setStatus('계정을 수정했다.', 'ok');
                await renderManage();
              }
            );
          },
        },
        {
          label: account.status === 'active' ? '비활성화' : '활성화',
          onClick: async function () {
            const next =
              account.status === 'active' ? 'disabled' : 'active';
            await api(
              'POST',
              '/admin/accounts/' +
                encodeURIComponent(account.account_id) + '/status',
              { status: next }
            );
            setStatus('계정 상태를 변경했다.', 'ok');
            await renderManage();
          },
        },
        {
          label: '삭제',
          kind: 'danger',
          onClick: function () {
            openConfirm(
              account.account_id +
                ' 계정을 삭제한다. 하위 팀·사용자·키가 있으면 거부된다.',
              async function () {
                await api(
                  'DELETE',
                  '/admin/accounts/' + encodeURIComponent(account.account_id)
                );
                setStatus('계정을 삭제했다.', 'ok');
                await renderManage();
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
      buildTable(['계정 ID', '이름', '상태', '월 예산', '작업'], rows)
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
      throw new Error('계정을 먼저 선택한다.');
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
      buildToolbar('팀 만들기', function () {
        openFormModal(
          '팀 만들기 (' + accountId + ')',
          [
            {
              name: 'team_id',
              label: '팀 ID',
              placeholder: '소문자·숫자·하이픈',
            },
            { name: 'name', label: '이름' },
            {
              name: 'monthly_budget_usd',
              label: '월 예산 (USD)',
              type: 'number',
              step: '0.01',
              hint: '비우면 계정 예산만 적용',
            },
          ],
          async function (values) {
            await api('POST', base + '/teams', {
              team_id: values.team_id.trim(),
              name: values.name.trim(),
              monthly_budget_usd: parseBudget(values.monthly_budget_usd),
            });
            setStatus('팀을 만들었다.', 'ok');
            await renderManage();
          }
        );
      })
    );

    const rows = teams.map(function (team) {
      const actions = buildActions([
        {
          label: '수정',
          onClick: function () {
            openFormModal(
              '팀 수정: ' + team.team_id,
              [
                { name: 'name', label: '이름', value: team.name },
                {
                  name: 'monthly_budget_usd',
                  label: '월 예산 (USD)',
                  type: 'number',
                  step: '0.01',
                  value: team.monthly_budget_usd,
                  hint: '비우면 계정 예산만 적용',
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
                setStatus('팀을 수정했다.', 'ok');
                await renderManage();
              }
            );
          },
        },
        {
          label: team.status === 'active' ? '비활성화' : '활성화',
          onClick: async function () {
            const next = team.status === 'active' ? 'disabled' : 'active';
            await api(
              'POST',
              base + '/teams/' + encodeURIComponent(team.team_id) + '/status',
              { status: next }
            );
            setStatus('팀 상태를 변경했다.', 'ok');
            await renderManage();
          },
        },
        {
          label: '삭제',
          kind: 'danger',
          onClick: function () {
            openConfirm(
              team.team_id + ' 팀을 삭제한다. 소속 사용자·키가 있으면 거부된다.',
              async function () {
                await api(
                  'DELETE',
                  base + '/teams/' + encodeURIComponent(team.team_id)
                );
                setStatus('팀을 삭제했다.', 'ok');
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
      buildTable(['팀 ID', '이름', '상태', '월 예산', '작업'], rows)
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
      buildToolbar('사용자 만들기', function () {
        openFormModal(
          '사용자 만들기 (' + accountId + ')',
          [
            {
              name: 'user_id',
              label: '사용자 ID',
              placeholder: '소문자·숫자·. _ -',
            },
            { name: 'name', label: '이름' },
            { name: 'email', label: '이메일', type: 'email' },
            {
              name: 'team_id',
              label: '팀 ID',
              hint: '비우면 팀 없음. 존재하는 팀이어야 한다.',
            },
            {
              name: 'monthly_budget_usd',
              label: '월 예산 (USD)',
              type: 'number',
              step: '0.01',
              hint: '비우면 상위 예산만 적용',
            },
          ],
          async function (values) {
            const payload = {
              user_id: values.user_id.trim(),
              name: values.name.trim(),
              monthly_budget_usd: parseBudget(values.monthly_budget_usd),
            };
            if (values.email.trim()) {
              payload.email = values.email.trim();
            }
            if (values.team_id.trim()) {
              payload.team_id = values.team_id.trim();
            }
            await api('POST', base + '/users', payload);
            setStatus('사용자를 만들었다.', 'ok');
            await renderManage();
          }
        );
      })
    );

    const rows = users.map(function (user) {
      const actions = buildActions([
        {
          label: '수정',
          onClick: function () {
            openFormModal(
              '사용자 수정: ' + user.user_id,
              [
                { name: 'name', label: '이름', value: user.name },
                {
                  name: 'email',
                  label: '이메일',
                  type: 'email',
                  value: user.email,
                },
                {
                  name: 'team_id',
                  label: '팀 ID',
                  value: user.team_id,
                  hint: '존재하는 팀이어야 한다. 비우면 팀 없음.',
                },
                {
                  name: 'monthly_budget_usd',
                  label: '월 예산 (USD)',
                  type: 'number',
                  step: '0.01',
                  value: user.monthly_budget_usd,
                  hint: '비우면 상위 예산만 적용',
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
                  }
                );
                setStatus('사용자를 수정했다.', 'ok');
                await renderManage();
              }
            );
          },
        },
        {
          label: user.status === 'active' ? '비활성화' : '활성화',
          onClick: async function () {
            const next = user.status === 'active' ? 'disabled' : 'active';
            await api(
              'POST',
              base + '/users/' + encodeURIComponent(user.user_id) + '/status',
              { status: next }
            );
            setStatus('사용자 상태를 변경했다.', 'ok');
            await renderManage();
          },
        },
        {
          label: '삭제',
          kind: 'danger',
          onClick: function () {
            openConfirm(
              user.user_id + ' 사용자를 삭제한다. 소유 키가 있으면 거부된다.',
              async function () {
                await api(
                  'DELETE',
                  base + '/users/' + encodeURIComponent(user.user_id)
                );
                setStatus('사용자를 삭제했다.', 'ok');
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
        ['사용자 ID', '이름', '팀', '상태', '월 예산', '작업'],
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
    dialog.setAttribute('aria-label', '발급된 API 키');

    const heading = document.createElement('h3');
    heading.textContent = '발급된 API 키';
    dialog.appendChild(heading);

    const note = document.createElement('p');
    note.className = 'modal-hint';
    note.textContent =
      '이 키는 지금만 볼 수 있다. 안전한 곳에 보관한다. 창을 닫으면 다시 볼 수 없다.';
    dialog.appendChild(note);

    const code = document.createElement('pre');
    code.className = 'key-plaintext';
    code.textContent = plaintext;
    dialog.appendChild(code);

    const actions = document.createElement('div');
    actions.className = 'modal-actions';

    const copy = document.createElement('button');
    copy.type = 'button';
    copy.textContent = '복사';
    copy.addEventListener('click', function () {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(plaintext).then(function () {
          copy.textContent = '복사됨';
        });
      }
    });

    const done = document.createElement('button');
    done.type = 'button';
    done.className = 'primary';
    done.textContent = '닫기';
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
      buildToolbar('키 발급', function () {
        openFormModal(
          '키 발급 (' + accountId + ')',
          [
            {
              name: 'user_id',
              label: '사용자 ID',
              hint: '존재하는 사용자여야 한다. 팀은 사용자에서 상속된다.',
            },
            { name: 'name', label: '이름(메모)' },
            {
              name: 'allowed_models',
              label: '허용 모델',
              hint: '쉼표로 구분. 비우면 서버 기본 정책.',
            },
            {
              name: 'monthly_budget_usd',
              label: '월 예산 (USD)',
              type: 'number',
              step: '0.01',
              hint: '비우면 상위 예산만 적용',
            },
          ],
          async function (values) {
            const created = await api('POST', base + '/keys', {
              user_id: values.user_id.trim(),
              name: values.name.trim(),
              allowed_models: splitModels(values.allowed_models),
              monthly_budget_usd: parseBudget(values.monthly_budget_usd),
            });
            setStatus('키를 발급했다.', 'ok');
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
          label: '수정',
          onClick: function () {
            openFormModal(
              '키 수정: ' + key.key_id,
              [
                { name: 'name', label: '이름(메모)', value: key.name },
                {
                  name: 'allowed_models',
                  label: '허용 모델',
                  value: (key.allowed_models || []).join(', '),
                  hint: '쉼표로 구분. 비우면 서버 기본 정책.',
                },
                {
                  name: 'monthly_budget_usd',
                  label: '월 예산 (USD)',
                  type: 'number',
                  step: '0.01',
                  value: key.monthly_budget_usd,
                  hint: '비우면 상위 예산만 적용',
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
                  }
                );
                setStatus('키를 수정했다.', 'ok');
                await renderManage();
              }
            );
          },
        },
        {
          label: key.status === 'active' ? '비활성화' : '활성화',
          onClick: async function () {
            const next = key.status === 'active' ? 'disabled' : 'active';
            await api(
              'POST',
              base + '/keys/' + encodeURIComponent(key.key_id) + '/status',
              { status: next }
            );
            setStatus('키 상태를 변경했다.', 'ok');
            await renderManage();
          },
        },
        {
          label: '재발급',
          onClick: function () {
            openConfirm(
              key.key_id +
                ' 키를 재발급한다. 옛 키는 즉시 무효가 되고 새 키가 발급된다.',
              async function () {
                const rotated = await api(
                  'POST',
                  base + '/keys/' + encodeURIComponent(key.key_id) + '/rotate'
                );
                setStatus('키를 재발급했다.', 'ok');
                await renderManage();
                if (rotated && rotated.api_key) {
                  showPlaintextKey(rotated.api_key);
                  // 평문 키 모달을 열었으므로 확인 대화상자의 자동 닫기를 막는다.
                  return true;
                }
                return false;
              },
              { confirmLabel: '재발급', confirmKind: 'primary' }
            );
          },
        },
        {
          label: '삭제',
          kind: 'danger',
          onClick: function () {
            openConfirm(
              key.key_id + ' 키를 삭제한다. 이 키는 즉시 무효가 된다.',
              async function () {
                await api(
                  'DELETE',
                  base + '/keys/' + encodeURIComponent(key.key_id)
                );
                setStatus('키를 삭제했다.', 'ok');
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
        ['키 ID', '접두어', '이름', '사용자', '팀', '상태', '월 예산', '작업'],
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
   * 현재 선택된 관리 뷰를 그린다.
   *
   * @returns {!Promise<void>}
   */
  async function renderManage() {
    const token = dom.token.value.trim();
    if (!token) {
      dom.managePanel.replaceChildren();
      setStatus('관리 토큰을 입력하고 조회를 누른다.', 'error');
      return;
    }
    try {
      if (activeManageView === 'accounts') {
        await renderAccounts();
      } else if (activeManageView === 'teams') {
        await renderTeams();
      } else if (activeManageView === 'users') {
        await renderUsers();
      } else {
        await renderKeys();
      }
    } catch (error) {
      const message = document.createElement('p');
      message.className = 'manage-error';
      message.textContent = '불러오기 실패: ' + error.message;
      dom.managePanel.replaceChildren(message);
      setStatus('불러오기 실패: ' + error.message, 'error');
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

  init();
})();
