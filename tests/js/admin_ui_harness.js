/**
 * admin.js / app.js 관리 UI 동작 검증 하네스.
 *
 * 브라우저 없이 관리 화면의 상호작용을 확인하기 위한 최소 DOM 셰임이다.
 * charts_harness.js 와 같은 이유로 jsdom·npm 을 쓰지 않고, admin.js 와
 * app.js 가 실제로 쓰는 DOM API 만 직접 구현한다. 실제 클릭·제출 이벤트를
 * 발생시켜, 코드 리뷰에서 지적됐던 세 결함이 재발하지 않는지 검사한다.
 *
 *   1) 발급·재발급한 평문 키 모달이 폼 자동 닫기에 삼켜지지 않는다.
 *   2) 관리 탭 클릭이 모니터링 탭 상태(activeView, ARIA)를 깨지 않는다.
 *   3) 계정 생성·삭제 직후 상단 계정 선택 목록이 갱신된다.
 *
 * 결과는 stdout 에 JSON 으로 낸다. 호출 측은 tests/test_static_admin_ui.py 다.
 */

'use strict';

const fs = require('fs');
const path = require('path');

// -- 최소 DOM 셰임 ----------------------------------------------------------

let idCounter = 0;

class FakeClassList {
  constructor() {
    this._set = new Set();
  }
  add(...names) {
    names.forEach((n) => this._set.add(n));
  }
  remove(...names) {
    names.forEach((n) => this._set.delete(n));
  }
  contains(name) {
    return this._set.has(name);
  }
  toString() {
    return Array.from(this._set).join(' ');
  }
}

class FakeElement {
  constructor(tag) {
    this.tag = String(tag).toLowerCase();
    this.attributes = {};
    this.children = [];
    this.parent = null;
    this._textContent = '';
    this._className = '';
    this.classList = new FakeClassList();
    this.value = '';
    this.hidden = false;
    this.disabled = false;
    this.readOnly = false;
    this.type = '';
    this.step = '';
    this.placeholder = '';
    this.id = '';
    this.scope = '';
    this.colSpan = 1;
    this._listeners = {};
    this._uid = ++idCounter;
  }

  get textContent() {
    return this._textContent;
  }
  set textContent(value) {
    this._textContent = value == null ? '' : String(value);
    // 텍스트를 설정하면 기존 자식은 사라진다(브라우저 동작 근사).
    this.children = [];
  }

  get className() {
    return this._className;
  }
  set className(value) {
    this._className = value || '';
    this.classList = new FakeClassList();
    this._className
      .split(/\s+/)
      .filter(Boolean)
      .forEach((n) => this.classList.add(n));
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'role') {
      this.role = String(value);
    }
  }
  getAttribute(name) {
    return name in this.attributes ? this.attributes[name] : null;
  }
  removeAttribute(name) {
    delete this.attributes[name];
    if (name === 'role') {
      this.role = undefined;
    }
  }

  appendChild(child) {
    child.parent = this;
    this.children.push(child);
    return child;
  }
  replaceChildren(...nodes) {
    this.children.forEach((c) => {
      c.parent = null;
    });
    this.children = [];
    nodes.forEach((n) => this.appendChild(n));
  }

  addEventListener(type, handler) {
    (this._listeners[type] = this._listeners[type] || []).push(handler);
  }

  dispatchEvent(event) {
    event.target = event.target || this;
    const handlers = this._listeners[event.type] || [];
    handlers.forEach((h) => h(event));
    return true;
  }

  /** 편의: 클릭 이벤트를 발생시킨다. */
  click() {
    this.dispatchEvent({ type: 'click', target: this, preventDefault() {} });
  }

  focus() {
    /* no-op */
  }

  /** 자신과 자손을 평탄화한다. */
  _all() {
    const out = [this];
    this.children.forEach((c) => out.push(...c._all()));
    return out;
  }

  /** admin.js/app.js 가 쓰는 셀렉터만 지원하는 최소 매처. */
  _matches(selector) {
    selector = selector.trim();
    if (selector.startsWith('[') && selector.endsWith(']')) {
      const inner = selector.slice(1, -1);
      const eq = inner.indexOf('=');
      if (eq === -1) {
        return this.getAttribute(inner) !== null;
      }
      const name = inner.slice(0, eq);
      let val = inner.slice(eq + 1);
      if (
        (val.startsWith('"') && val.endsWith('"')) ||
        (val.startsWith("'") && val.endsWith("'"))
      ) {
        val = val.slice(1, -1);
      }
      return this.getAttribute(name) === val;
    }
    if (selector.startsWith('.')) {
      return this.classList.contains(selector.slice(1));
    }
    if (selector.startsWith('#')) {
      return this.id === selector.slice(1);
    }
    return this.tag === selector.toLowerCase();
  }

  querySelectorAll(selector) {
    // "A B" 형태의 자손 결합자만 지원한다(admin/app 이 쓰는 범위).
    const parts = selector.split(/\s+/).filter(Boolean);
    let scope = this._all();
    // 첫 파트로 후보를 좁히고, 이후 파트로 자손을 다시 좁힌다.
    parts.forEach((part, index) => {
      if (index === 0) {
        scope = scope.filter((el) => el !== this && el._matches(part));
        // 루트 자신도 매칭 대상에 포함(document 기준 선택).
        if (this._matches(part)) {
          scope.unshift(this);
        }
      } else {
        const next = [];
        scope.forEach((ancestor) => {
          ancestor._all().forEach((desc) => {
            if (desc !== ancestor && desc._matches(part)) {
              next.push(desc);
            }
          });
        });
        scope = next;
      }
    });
    return scope;
  }

  querySelector(selector) {
    const all = this.querySelectorAll(selector);
    return all.length ? all[0] : null;
  }
}

// -- 문서 트리 구성 ---------------------------------------------------------

const registry = {};

function el(tag, opts) {
  const node = new FakeElement(tag);
  if (opts) {
    if (opts.id) {
      node.id = opts.id;
      registry[opts.id] = node;
    }
    if (opts.role) {
      node.setAttribute('role', opts.role);
    }
    if (opts.attrs) {
      Object.keys(opts.attrs).forEach((k) => node.setAttribute(k, opts.attrs[k]));
    }
    if (opts.className) {
      node.className = opts.className;
    }
  }
  return node;
}

const body = new FakeElement('body');

// 공유 요소: 토큰·계정 선택·상태 줄.
const token = el('input', { id: 'admin-token' });
token.value = 'test-token';
const account = el('select', { id: 'account-select' });
const status = el('p', { id: 'status-line' });
body.appendChild(token);
body.appendChild(account);
body.appendChild(status);

// 뷰 전환 탭.
const viewMonitor = el('button', {
  id: 'view-monitor',
  className: 'view-tab',
  attrs: { 'data-screen': 'monitor', 'aria-pressed': 'true' },
});
const viewManage = el('button', {
  id: 'view-manage',
  className: 'view-tab',
  attrs: { 'data-screen': 'manage', 'aria-pressed': 'false' },
});
body.appendChild(viewMonitor);
body.appendChild(viewManage);

// 모니터링 화면과 그 상세 탭(app.js 소유).
const screenMonitor = el('div', { id: 'screen-monitor' });
const monitorTablist = el('div', { className: 'tabs', role: 'tablist' });
const monitorTabTeam = el('button', {
  id: 'tab-team',
  role: 'tab',
  attrs: { 'data-view': 'team', 'aria-selected': 'true' },
});
const monitorTabUser = el('button', {
  id: 'tab-user',
  role: 'tab',
  attrs: { 'data-view': 'user', 'aria-selected': 'false' },
});
monitorTablist.appendChild(monitorTabTeam);
monitorTablist.appendChild(monitorTabUser);
screenMonitor.appendChild(monitorTablist);
const panelTable = el('div', { id: 'panel-table', role: 'tabpanel' });
panelTable.setAttribute('aria-labelledby', 'tab-team');
const detailTable = el('table', { id: 'detail-table' });
const thead = el('thead');
const headRow = el('tr');
thead.appendChild(headRow);
const tbody = el('tbody');
detailTable.appendChild(thead);
detailTable.appendChild(tbody);
panelTable.appendChild(detailTable);
const tableCaption = el('caption', { id: 'table-caption' });
detailTable.appendChild(tableCaption);
screenMonitor.appendChild(panelTable);
body.appendChild(screenMonitor);

// 관리 화면과 그 탭(admin.js 소유).
const screenManage = el('div', { id: 'screen-manage' });
screenManage.hidden = true;
const manageTablist = el('div', { className: 'tabs', role: 'tablist' });
['accounts', 'teams', 'users', 'keys'].forEach((name, i) => {
  const tab = el('button', {
    id: 'mtab-' + name,
    role: 'tab',
    attrs: {
      'data-manage': name,
      'aria-selected': i === 0 ? 'true' : 'false',
    },
  });
  manageTablist.appendChild(tab);
});
screenManage.appendChild(manageTablist);
const managePanel = el('div', { id: 'manage-panel', role: 'tabpanel' });
screenManage.appendChild(managePanel);
body.appendChild(screenManage);

const modalRoot = el('div', { id: 'modal-root' });
modalRoot.hidden = true;
body.appendChild(modalRoot);

// 앱이 만드는 요소에도 부여할 date 입력들(app.js init 이 참조).
['start-date', 'end-date'].forEach((id) => {
  const node = el('input', { id });
  body.appendChild(node);
});
['refresh-button', 'auto-refresh'].forEach((id) => {
  const node = el('button', { id });
  body.appendChild(node);
});
// KPI 등 app.js 가 참조하는 그 밖의 id 는 조회 시 자동 생성한다(아래 참조).

// -- 전역 셰임 --------------------------------------------------------------

global.document = {
  getElementById(id) {
    if (!registry[id]) {
      // app.js 는 KPI 등 여러 id 를 참조한다. 없으면 조용히 만들어 둔다.
      const node = new FakeElement('div');
      node.id = id;
      registry[id] = node;
    }
    return registry[id];
  },
  createElement(tag) {
    return new FakeElement(tag);
  },
  createElementNS(ns, tag) {
    return new FakeElement(tag);
  },
  querySelectorAll(selector) {
    return body.querySelectorAll(selector);
  },
  querySelector(selector) {
    return body.querySelector(selector);
  },
  addEventListener() {
    /* keydown 등은 이 하네스에서 쓰지 않는다. */
  },
  documentElement: {},
};

global.getComputedStyle = () => ({ getPropertyValue: () => '#0b5fff' });

// admin.js 의 buildTable 은 `value instanceof Node` 로 DOM 노드와 문자열을
// 구분한다. 브라우저 전역 Node 가 없으면 ReferenceError 가 나므로, 셰임
// 요소 클래스를 Node 로 등록해 instanceof 가 참이 되게 한다.
global.Node = FakeElement;

global.sessionStorage = (() => {
  const store = {};
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => {
      store[k] = String(v);
    },
    removeItem: (k) => {
      delete store[k];
    },
  };
})();

// Node 22+ 는 global.navigator 를 getter 전용 내장 프로퍼티로 노출한다.
// 직접 할당하면 TypeError 가 나므로 defineProperty 로 재정의한다. 하위
// 버전(내장 navigator 가 없음)에서도 동일하게 동작한다.
Object.defineProperty(global, 'navigator', {
  value: { clipboard: { writeText: () => Promise.resolve() } },
  configurable: true,
  writable: true,
});

global.Intl = global.Intl; // 그대로 사용.
global.window = {};
global.setInterval = () => 0;
global.clearInterval = () => {};
global.setTimeout = (fn) => {
  if (typeof fn === 'function') fn();
  return 0;
};

// -- fetch 목: 인메모리 백엔드 ----------------------------------------------

const state = {
  accounts: [
    { account_id: 'acme', name: 'Acme', status: 'active', monthly_budget_usd: null },
  ],
  keys: [
    {
      key_id: 'key-1',
      key_prefix: 'sk-llmgw-test-abcd',
      name: '기존 키',
      account_id: 'acme',
      team_id: 'platform',
      user_id: 'alice',
      allowed_models: [],
      monthly_budget_usd: null,
      status: 'active',
    },
  ],
};

const calls = [];

function jsonResponse(status, body) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  });
}

global.fetch = function (url, options) {
  const method = (options && options.method) || 'GET';
  calls.push({ method, url });
  const body =
    options && options.body ? JSON.parse(options.body) : null;

  // 계정 목록/생성/삭제.
  if (url === '/admin/accounts' && method === 'GET') {
    return jsonResponse(200, { data: state.accounts });
  }
  if (url === '/admin/accounts' && method === 'POST') {
    state.accounts.push({
      account_id: body.account_id,
      name: body.name,
      status: 'active',
      monthly_budget_usd: body.monthly_budget_usd,
    });
    return jsonResponse(201, state.accounts[state.accounts.length - 1]);
  }
  if (/^\/admin\/accounts\/[^/]+$/.test(url) && method === 'DELETE') {
    const id = decodeURIComponent(url.split('/').pop());
    state.accounts = state.accounts.filter((a) => a.account_id !== id);
    return jsonResponse(204, null);
  }

  // 키 목록/발급/재발급.
  if (/\/keys$/.test(url) && method === 'GET') {
    return jsonResponse(200, { data: state.keys });
  }
  if (/\/keys$/.test(url) && method === 'POST') {
    const created = {
      key_id: 'key-new',
      key_prefix: 'sk-llmgw-test-wxyz',
      name: body.name || '',
      account_id: 'acme',
      team_id: 'platform',
      user_id: body.user_id,
      allowed_models: body.allowed_models || [],
      monthly_budget_usd: body.monthly_budget_usd,
      status: 'active',
      api_key: 'sk-llmgw-test-PLAINTEXT-NEW',
    };
    state.keys.push({ ...created });
    delete state.keys[state.keys.length - 1].api_key;
    return jsonResponse(201, created);
  }
  if (/\/keys\/[^/]+\/rotate$/.test(url) && method === 'POST') {
    return jsonResponse(200, {
      key_id: 'key-1',
      key_prefix: 'sk-llmgw-test-rot0',
      name: '기존 키',
      account_id: 'acme',
      team_id: 'platform',
      user_id: 'alice',
      allowed_models: [],
      monthly_budget_usd: null,
      status: 'active',
      api_key: 'sk-llmgw-test-PLAINTEXT-ROTATED',
    });
  }

  return jsonResponse(404, { error: { message: 'not found' } });
};

// -- 스크립트 로드 (app.js 먼저, admin.js 다음) -----------------------------

const staticDir = path.join(__dirname, '..', '..', 'src', 'llmgw', 'static');
// eslint-disable-next-line no-eval
eval(fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8'));
// eslint-disable-next-line no-eval
eval(fs.readFileSync(path.join(staticDir, 'admin.js'), 'utf8'));

// -- 검사 유틸 --------------------------------------------------------------

const results = {};

function findByText(root, tag, text) {
  return root._all().find((n) => n.tag === tag && n.textContent === text) || null;
}

function firstModal() {
  return modalRoot.children.length ? modalRoot.children[0] : null;
}

/** 관리 화면을 켜고 특정 관리 탭을 선택한다. */
function openManage(tabName) {
  viewManage.click();
  registry['mtab-' + tabName].click();
}

// 마이크로태스크(fetch 목의 Promise 체인)를 여러 번 비운다. admin.js 의
// 렌더링은 async 라, 클릭 후 이만큼 양보해야 DOM 이 갱신된다.
async function flush() {
  for (let i = 0; i < 20; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await Promise.resolve();
  }
}

async function run() {
  // -- 시나리오 1: 관리 탭이 모니터링 탭 상태를 깨지 않는다 -----------------
  monitorTabUser.click();
  const userSelectedBefore =
    monitorTabUser.getAttribute('aria-selected') === 'true';

  viewManage.click();
  await flush();
  registry['mtab-teams'].click();
  await flush();

  results.tabConflict = {
    userSelectedBefore,
    monitorStillIntact:
      monitorTabUser.getAttribute('aria-selected') === 'true' &&
      monitorTabTeam.getAttribute('aria-selected') === 'false',
    manageTabState:
      registry['mtab-teams'].getAttribute('aria-selected') === 'true' &&
      registry['mtab-accounts'].getAttribute('aria-selected') === 'false',
  };

  // -- 시나리오 2: 계정 생성 후 상단 계정 선택 목록 갱신 -------------------
  openManage('accounts');
  await flush();
  const getCallsBefore = calls.filter(
    (c) => c.url === '/admin/accounts' && c.method === 'GET'
  ).length;

  const createBtn = findByText(managePanel, 'button', '계정 만들기');
  createBtn.click();
  await flush();
  let modal = firstModal();
  const cInputs = modal._all().filter((n) => n.tag === 'input');
  cInputs.find((n) => n.id === 'modal-account_id').value = 'beta';
  cInputs.find((n) => n.id === 'modal-name').value = 'Beta';
  let form = modal._all().find((n) => n.tag === 'form');
  form.dispatchEvent({ type: 'submit', preventDefault() {} });
  await flush();

  const getCallsAfter = calls.filter(
    (c) => c.url === '/admin/accounts' && c.method === 'GET'
  ).length;
  results.accountSync = {
    createPosted: calls.some(
      (c) => c.url === '/admin/accounts' && c.method === 'POST'
    ),
    // 계정 선택 목록 갱신 훅이 GET 을 다시 호출했는지(생성 렌더 + reload).
    reloadedAfterCreate: getCallsAfter > getCallsBefore + 1,
    // 상단 select 에 새 계정이 실제로 반영됐는지.
    selectHasBeta: account.children.some((o) => o.value === 'beta'),
  };

  // -- 시나리오 3: 키 발급 후 평문 키 모달이 유지된다 ---------------------
  openManage('keys');
  await flush();
  const issueBtn = findByText(managePanel, 'button', '키 발급');
  issueBtn.click();
  await flush();
  modal = firstModal();
  modal
    ._all()
    .filter((n) => n.tag === 'input')
    .find((n) => n.id === 'modal-user_id').value = 'alice';
  form = modal._all().find((n) => n.tag === 'form');
  form.dispatchEvent({ type: 'submit', preventDefault() {} });
  await flush();

  let modalNow = firstModal();
  let text = modalNow
    ? modalNow._all().map((n) => n.textContent).join(' ')
    : '';
  results.keyIssueModal = {
    modalOpen: modalNow !== null,
    showsPlaintext: text.indexOf('sk-llmgw-test-PLAINTEXT-NEW') !== -1,
  };

  // -- 시나리오 4: 재발급 후 평문 키 모달이 유지된다 ---------------------
  openManage('keys');
  await flush();
  const rotateBtn = findByText(managePanel, 'button', '재발급');
  rotateBtn.click();
  await flush();
  const confirmModal = firstModal();
  const confirmBtn = confirmModal
    ._all()
    .filter((n) => n.tag === 'button')
    .find((n) => n.textContent === '재발급');
  confirmBtn.click();
  await flush();

  modalNow = firstModal();
  text = modalNow
    ? modalNow._all().map((n) => n.textContent).join(' ')
    : '';
  results.keyRotateModal = {
    modalOpen: modalNow !== null,
    showsPlaintext:
      text.indexOf('sk-llmgw-test-PLAINTEXT-ROTATED') !== -1,
  };

  process.stdout.write(JSON.stringify(results));
}

run().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
