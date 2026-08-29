/**
 * charts.js 검증 하네스.
 *
 * 브라우저 없이 차트 렌더링을 확인하기 위한 최소 DOM 셰임이다. jsdom 같은
 * 패키지를 쓰지 않은 이유는 이 프로젝트가 npm 툴체인을 들이지 않기로 했기
 * 때문이다. charts.js 가 쓰는 DOM API 는 5개뿐이라 직접 구현하는 편이 가볍다.
 *
 * stdin 으로 대시보드 응답 JSON 을 받고, 검사 결과를 stdout 에 JSON 으로
 * 출력한다. 호출 측은 tests/test_static_charts.py 다.
 */

'use strict';

const fs = require('fs');
const path = require('path');

/** 최소 DOM 요소 구현. */
class FakeElement {
  constructor(tag) {
    this.tag = tag;
    this.attributes = {};
    this.children = [];
    this.textContent = '';
    this.className = '';
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  replaceChildren(...nodes) {
    this.children = nodes;
  }

  /** 이 요소와 자손에서 특정 태그의 개수를 센다. */
  countTag(tag) {
    const self = this.tag === tag ? 1 : 0;
    return self + this.children.reduce((sum, c) => sum + c.countTag(tag), 0);
  }
}

global.document = {
  createElementNS: (ns, tag) => new FakeElement(tag),
  createElement: (tag) => new FakeElement(tag),
  documentElement: {},
};
// charts.js 는 CSS 변수로 색을 읽는다. 실제 값은 검증 대상이 아니므로 고정한다.
global.getComputedStyle = () => ({ getPropertyValue: () => '#0b5fff' });
global.window = {};
// i18n 모듈 스텁. 하네스는 렌더링 로직만 검증하므로 번역은 원문을 그대로
// 돌려주면 충분하다. 실제 번역 동작은 tests/test_ui_i18n.py 가 브라우저에서
// 검증한다.
global.window.LlmgwI18n = {
  t: function (text) {
    return text;
  },
  lang: function () {
    return 'ko';
  },
  setLang: function () {
    return false;
  },
  applyStatic: function () {},
  missing: function () {
    return [];
  },
};

const chartsPath = path.join(
  __dirname, '..', '..', 'src', 'llmgw', 'static', 'charts.js');
// eslint-disable-next-line no-eval
eval(fs.readFileSync(chartsPath, 'utf8'));
const charts = global.window.LlmgwCharts;

const formatUsd = (value) => '$' + Number(value).toFixed(6);
const formatCount = (value) => String(Math.round(value));

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const results = {};

/** 컨테이너를 만들고 자손 태그 수를 센다. */
function render(fn) {
  const container = new FakeElement('div');
  fn(container);
  return {
    container,
    count: (tag) =>
      container.children.reduce((sum, c) => sum + c.countTag(tag), 0),
    rootTag: container.children.length ? container.children[0].tag : null,
    rootClass: container.children.length
      ? container.children[0].className
      : null,
    rootAttrs: container.children.length
      ? container.children[0].attributes
      : {},
  };
}

// -- 선 그래프 ---------------------------------------------------------------
const series = input.timeseries || [];
const line = render((el) =>
  charts.lineChart(el, {
    labels: series.map((e) => e.date),
    series: [
      {
        name: 'cost',
        values: series.map((e) => e.cost_usd),
        format: formatUsd,
      },
      {
        name: 'requests',
        values: series.map((e) => e.requests),
        format: formatCount,
      },
    ],
    ariaLabel: 'daily cost and requests',
  })
);
results.line = {
  rootTag: line.rootTag,
  polylines: line.count('polyline'),
  points: line.count('circle'),
  texts: line.count('text'),
  role: line.rootAttrs.role,
  hasAriaLabel: Boolean(line.rootAttrs['aria-label']),
};

// -- 막대 그래프 -------------------------------------------------------------
const teams = (input.breakdowns || {}).team || [];
const bar = render((el) =>
  charts.barChart(el, {
    labels: teams.map((r) => r.label),
    values: teams.map((r) => r.cost_usd),
    format: formatUsd,
    ariaLabel: 'cost by team',
  })
);
results.bar = {
  rootTag: bar.rootTag,
  rects: bar.count('rect'),
  titles: bar.count('title'),
  role: bar.rootAttrs.role,
};

// -- 도넛 --------------------------------------------------------------------
const models = (input.breakdowns || {}).model || [];
const donut = render((el) =>
  charts.donutChart(el, {
    labels: models.map((r) => r.label),
    values: models.map((r) => r.requests),
    format: formatCount,
    ariaLabel: 'requests by model',
  })
);
results.donut = {
  rootTag: donut.rootTag,
  paths: donut.count('path'),
  legendRects: donut.count('rect'),
  role: donut.rootAttrs.role,
};

// -- 경계 케이스 -------------------------------------------------------------
const empty = render((el) =>
  charts.lineChart(el, { labels: [], series: [], ariaLabel: 'x' })
);
results.emptyLine = { rootTag: empty.rootTag, rootClass: empty.rootClass };

const emptyDonut = render((el) =>
  charts.donutChart(el, {
    labels: ['a'],
    values: [0],
    format: formatCount,
    ariaLabel: 'x',
  })
);
results.zeroDonut = {
  rootTag: emptyDonut.rootTag,
  rootClass: emptyDonut.rootClass,
};

const single = render((el) =>
  charts.lineChart(el, {
    labels: ['2026-08-24'],
    series: [{ name: 'x', values: [7], format: formatCount }],
    ariaLabel: 'x',
  })
);
results.singlePoint = { rootTag: single.rootTag, points: single.count('circle') };

const allZero = render((el) =>
  charts.barChart(el, {
    labels: ['a', 'b'],
    values: [0, 0],
    format: formatCount,
    ariaLabel: 'x',
  })
);
results.allZeroBar = { rootTag: allZero.rootTag, rects: allZero.count('rect') };

const fullCircle = render((el) =>
  charts.donutChart(el, {
    labels: ['only'],
    values: [5],
    format: formatCount,
    ariaLabel: 'x',
  })
);
results.fullCircleDonut = {
  rootTag: fullCircle.rootTag,
  paths: fullCircle.count('path'),
};

results.niceCeiling = {
  zero: charts.niceCeiling(0),
  small: charts.niceCeiling(0.0032),
  mid: charts.niceCeiling(137),
  negative: charts.niceCeiling(-5),
};

process.stdout.write(JSON.stringify(results));
