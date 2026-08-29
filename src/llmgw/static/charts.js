/**
 * 의존성 없는 SVG 차트 렌더러.
 *
 * Chart.js 같은 라이브러리를 쓰지 않은 이유는 두 가지다. 첫째, 이 UI는
 * 인터넷 접근이 제한된 환경에서도 그대로 동작해야 한다. CDN 링크를 두면
 * 사설 네트워크에서 차트가 사라진다. 둘째, 컨테이너 빌드에 npm 툴체인을
 * 끌어들이지 않기 위해서다.
 *
 * 필요한 차트는 선/막대/도넛 세 종류뿐이라 직접 그리는 편이 총량이 적다.
 *
 * 접근성: 각 차트는 role="img" 와 aria-label 을 갖고, 같은 데이터가 아래
 * 상세 표에 텍스트로 존재한다. 색만으로 정보를 전달하지 않는다.
 */

(function (global) {
  'use strict';

  const t = window.LlmgwI18n.t;

  const SVG_NS = 'http://www.w3.org/2000/svg';

  const SERIES_VARS = [
    '--series-1',
    '--series-2',
    '--series-3',
    '--series-4',
    '--series-5',
    '--series-6',
  ];

  /**
   * CSS 변수 값을 읽는다.
   *
   * @param {string} name CSS 변수 이름.
   * @returns {string} 해석된 색상 값.
   */
  function cssVar(name) {
    const value = getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
    return value || '#0b5fff';
  }

  /**
   * 시리즈 인덱스에 대응하는 색을 돌려준다.
   *
   * @param {number} index 0부터 시작하는 시리즈 번호.
   * @returns {string} 색상 값.
   */
  function seriesColor(index) {
    return cssVar(SERIES_VARS[index % SERIES_VARS.length]);
  }

  /**
   * SVG 요소를 만든다.
   *
   * @param {string} tag 태그 이름.
   * @param {!Object<string, (string|number)>} attributes 속성 맵.
   * @returns {!Element} 생성된 요소.
   */
  function el(tag, attributes) {
    const node = document.createElementNS(SVG_NS, tag);
    Object.keys(attributes || {}).forEach(function (key) {
      node.setAttribute(key, String(attributes[key]));
    });
    return node;
  }

  /**
   * 텍스트 노드를 가진 SVG 요소를 만든다.
   *
   * @param {string} tag 태그 이름.
   * @param {!Object<string, (string|number)>} attributes 속성 맵.
   * @param {string} content 텍스트 내용.
   * @returns {!Element} 생성된 요소.
   */
  function textEl(tag, attributes, content) {
    const node = el(tag, attributes);
    node.textContent = content;
    return node;
  }

  /**
   * 컨테이너를 비우고 "데이터 없음" 문구를 넣는다.
   *
   * @param {!Element} container 대상 컨테이너.
   * @param {string} message 표시할 문구.
   */
  function renderEmpty(container, message) {
    container.replaceChildren();
    const note = document.createElement('p');
    note.className = 'chart-empty';
    note.textContent = message;
    container.appendChild(note);
  }

  /**
   * 눈금에 쓸 "보기 좋은" 최대값을 만든다.
   *
   * 1, 2, 2.5, 5, 10 배수 중 데이터 최대값을 덮는 가장 작은 값을 고른다.
   * 예: 최대 0.0032 → 0.004, 최대 137 → 200.
   *
   * @param {number} maxValue 데이터 최대값.
   * @returns {number} 축 상단값. 데이터가 모두 0이면 1.
   */
  function niceCeiling(maxValue) {
    if (!(maxValue > 0)) {
      return 1;
    }
    const exponent = Math.floor(Math.log10(maxValue));
    const magnitude = Math.pow(10, exponent);
    const normalized = maxValue / magnitude;
    const steps = [1, 2, 2.5, 5, 10];
    for (let i = 0; i < steps.length; i += 1) {
      if (normalized <= steps[i]) {
        return steps[i] * magnitude;
      }
    }
    return 10 * magnitude;
  }

  /**
   * 축 레이블을 과밀하지 않게 솎아낸다.
   *
   * @param {number} count 전체 항목 수.
   * @param {number} maxLabels 표시할 최대 개수.
   * @returns {number} 몇 개마다 하나를 표시할지.
   */
  function labelStride(count, maxLabels) {
    return Math.max(1, Math.ceil(count / maxLabels));
  }

  /**
   * 선 그래프를 그린다. 두 번째 시리즈는 오른쪽 축을 쓴다.
   *
   * @param {!Element} container 대상 컨테이너.
   * @param {{
   *   labels: !Array<string>,
   *   series: !Array<{name: string, values: !Array<number>,
   *                   format: function(number): string}>,
   *   ariaLabel: string
   * }} spec 차트 명세.
   */
  function lineChart(container, spec) {
    const labels = spec.labels || [];
    const series = (spec.series || []).filter(function (item) {
      return item.values && item.values.length === labels.length;
    });
    if (!labels.length || !series.length) {
      renderEmpty(container, t('표시할 데이터가 없다.'));
      return;
    }

    const width = 900;
    const height = 300;
    const pad = { top: 20, right: 62, bottom: 40, left: 68 };
    const plotWidth = width - pad.left - pad.right;
    const plotHeight = height - pad.top - pad.bottom;

    const svg = el('svg', {
      viewBox: '0 0 ' + width + ' ' + height,
      role: 'img',
      'aria-label': spec.ariaLabel || t('선 그래프'),
    });

    const gridColor = cssVar('--border');
    const mutedColor = cssVar('--text-muted');

    const scales = series.map(function (item) {
      return niceCeiling(Math.max.apply(null, item.values));
    });

    // 가로 격자선과 좌우 축 눈금.
    const ticks = 4;
    for (let t = 0; t <= ticks; t += 1) {
      const y = pad.top + (plotHeight * t) / ticks;
      svg.appendChild(
        el('line', {
          x1: pad.left,
          y1: y,
          x2: pad.left + plotWidth,
          y2: y,
          stroke: gridColor,
          'stroke-width': 1,
        })
      );
      const ratio = 1 - t / ticks;
      svg.appendChild(
        textEl(
          'text',
          {
            x: pad.left - 8,
            y: y + 4,
            'text-anchor': 'end',
            'font-size': 11,
            fill: mutedColor,
          },
          series[0].format(scales[0] * ratio)
        )
      );
      if (series.length > 1) {
        svg.appendChild(
          textEl(
            'text',
            {
              x: pad.left + plotWidth + 8,
              y: y + 4,
              'text-anchor': 'start',
              'font-size': 11,
              fill: mutedColor,
            },
            series[1].format(scales[1] * ratio)
          )
        );
      }
    }

    const xAt = function (index) {
      if (labels.length === 1) {
        return pad.left + plotWidth / 2;
      }
      return pad.left + (plotWidth * index) / (labels.length - 1);
    };

    series.forEach(function (item, seriesIndex) {
      const scale = scales[seriesIndex] || 1;
      const color = seriesColor(seriesIndex);
      const points = item.values.map(function (value, index) {
        const y =
          pad.top + plotHeight - (plotHeight * (value || 0)) / scale;
        return xAt(index) + ',' + y;
      });
      svg.appendChild(
        el('polyline', {
          points: points.join(' '),
          fill: 'none',
          stroke: color,
          'stroke-width': 2.5,
          'stroke-linejoin': 'round',
          'stroke-linecap': 'round',
        })
      );
      item.values.forEach(function (value, index) {
        const y =
          pad.top + plotHeight - (plotHeight * (value || 0)) / scale;
        const dot = el('circle', {
          cx: xAt(index),
          cy: y,
          r: labels.length > 45 ? 1.6 : 3,
          fill: color,
        });
        dot.appendChild(
          textEl('title', {}, labels[index] + ' · ' + item.name + ' ' +
            item.format(value || 0))
        );
        svg.appendChild(dot);
      });
    });

    const stride = labelStride(labels.length, 10);
    labels.forEach(function (label, index) {
      if (index % stride !== 0 && index !== labels.length - 1) {
        return;
      }
      svg.appendChild(
        textEl(
          'text',
          {
            x: xAt(index),
            y: height - 16,
            'text-anchor': 'middle',
            'font-size': 11,
            fill: mutedColor,
          },
          label.length > 5 ? label.slice(5) : label
        )
      );
    });

    // 범례. 색과 함께 이름을 텍스트로 적어 색약 사용자도 구분할 수 있다.
    series.forEach(function (item, seriesIndex) {
      const legendX = pad.left + seriesIndex * 190;
      svg.appendChild(
        el('rect', {
          x: legendX,
          y: 4,
          width: 12,
          height: 12,
          rx: 3,
          fill: seriesColor(seriesIndex),
        })
      );
      svg.appendChild(
        textEl(
          'text',
          {
            x: legendX + 18,
            y: 14,
            'font-size': 12,
            fill: mutedColor,
          },
          item.name
        )
      );
    });

    container.replaceChildren(svg);
  }

  /**
   * 가로 막대 그래프를 그린다.
   *
   * @param {!Element} container 대상 컨테이너.
   * @param {{
   *   labels: !Array<string>,
   *   values: !Array<number>,
   *   format: function(number): string,
   *   ariaLabel: string
   * }} spec 차트 명세.
   */
  function barChart(container, spec) {
    const labels = spec.labels || [];
    const values = spec.values || [];
    if (!labels.length || !values.length) {
      renderEmpty(container, t('표시할 데이터가 없다.'));
      return;
    }

    const rowHeight = 30;
    const width = 520;
    const pad = { top: 10, right: 96, bottom: 10, left: 132 };
    const height = pad.top + pad.bottom + labels.length * rowHeight;
    const plotWidth = width - pad.left - pad.right;
    const scale = niceCeiling(Math.max.apply(null, values));
    const mutedColor = cssVar('--text-muted');

    const svg = el('svg', {
      viewBox: '0 0 ' + width + ' ' + height,
      role: 'img',
      'aria-label': spec.ariaLabel || t('막대 그래프'),
    });

    labels.forEach(function (label, index) {
      const y = pad.top + index * rowHeight;
      const value = values[index] || 0;
      const barWidth = Math.max((plotWidth * value) / scale, value > 0 ? 2 : 0);

      svg.appendChild(
        textEl(
          'text',
          {
            x: pad.left - 10,
            y: y + rowHeight / 2 + 4,
            'text-anchor': 'end',
            'font-size': 12,
            fill: cssVar('--text'),
          },
          label.length > 18 ? label.slice(0, 17) + '…' : label
        )
      );

      const bar = el('rect', {
        x: pad.left,
        y: y + 6,
        width: barWidth,
        height: rowHeight - 12,
        rx: 4,
        fill: seriesColor(index),
      });
      bar.appendChild(textEl('title', {}, label + ' · ' + spec.format(value)));
      svg.appendChild(bar);

      svg.appendChild(
        textEl(
          'text',
          {
            x: pad.left + plotWidth + 8,
            y: y + rowHeight / 2 + 4,
            'font-size': 12,
            fill: mutedColor,
          },
          spec.format(value)
        )
      );
    });

    container.replaceChildren(svg);
  }

  /**
   * 도넛 차트를 그린다.
   *
   * @param {!Element} container 대상 컨테이너.
   * @param {{
   *   labels: !Array<string>,
   *   values: !Array<number>,
   *   format: function(number): string,
   *   ariaLabel: string
   * }} spec 차트 명세.
   */
  function donutChart(container, spec) {
    const labels = spec.labels || [];
    const values = spec.values || [];
    const total = values.reduce(function (sum, value) {
      return sum + (value || 0);
    }, 0);
    if (!labels.length || total <= 0) {
      renderEmpty(container, t('표시할 데이터가 없다.'));
      return;
    }

    const width = 520;
    const height = Math.max(220, 40 + labels.length * 22);
    const cx = 110;
    const cy = height / 2;
    const outer = 84;
    const inner = 50;

    const svg = el('svg', {
      viewBox: '0 0 ' + width + ' ' + height,
      role: 'img',
      'aria-label': spec.ariaLabel || t('도넛 차트'),
    });

    let startAngle = -Math.PI / 2;
    labels.forEach(function (label, index) {
      const value = values[index] || 0;
      if (value <= 0) {
        return;
      }
      const sweep = (value / total) * Math.PI * 2;
      const endAngle = startAngle + sweep;
      // 한 조각이 원 전체일 때 시작점과 끝점이 같아져 경로가 사라진다.
      // 그 경우 미세하게 줄여 링이 보이게 한다.
      const drawEnd =
        sweep >= Math.PI * 2 - 1e-6 ? endAngle - 1e-4 : endAngle;

      const path = el('path', {
        d: arcPath(cx, cy, outer, inner, startAngle, drawEnd),
        fill: seriesColor(index),
      });
      const share = ((value / total) * 100).toFixed(1);
      path.appendChild(
        textEl('title', {}, label + ' · ' + spec.format(value) +
          ' (' + share + '%)')
      );
      svg.appendChild(path);
      startAngle = endAngle;
    });

    labels.forEach(function (label, index) {
      const value = values[index] || 0;
      const legendY = 24 + index * 22;
      svg.appendChild(
        el('rect', {
          x: 220,
          y: legendY - 10,
          width: 12,
          height: 12,
          rx: 3,
          fill: seriesColor(index),
        })
      );
      const share = total > 0 ? ((value / total) * 100).toFixed(1) : '0.0';
      svg.appendChild(
        textEl(
          'text',
          { x: 240, y: legendY, 'font-size': 12, fill: cssVar('--text') },
          (label.length > 30 ? label.slice(0, 29) + '…' : label) +
            ' — ' +
            spec.format(value) +
            ' (' +
            share +
            '%)'
        )
      );
    });

    container.replaceChildren(svg);
  }

  /**
   * 도넛 조각의 SVG path 문자열을 만든다.
   *
   * @param {number} cx 중심 x.
   * @param {number} cy 중심 y.
   * @param {number} outer 외반경.
   * @param {number} inner 내반경.
   * @param {number} start 시작 각(라디안).
   * @param {number} end 끝 각(라디안).
   * @returns {string} path 의 d 속성 값.
   */
  function arcPath(cx, cy, outer, inner, start, end) {
    const largeArc = end - start > Math.PI ? 1 : 0;
    const ox1 = cx + outer * Math.cos(start);
    const oy1 = cy + outer * Math.sin(start);
    const ox2 = cx + outer * Math.cos(end);
    const oy2 = cy + outer * Math.sin(end);
    const ix2 = cx + inner * Math.cos(end);
    const iy2 = cy + inner * Math.sin(end);
    const ix1 = cx + inner * Math.cos(start);
    const iy1 = cy + inner * Math.sin(start);
    return [
      'M', ox1, oy1,
      'A', outer, outer, 0, largeArc, 1, ox2, oy2,
      'L', ix2, iy2,
      'A', inner, inner, 0, largeArc, 0, ix1, iy1,
      'Z',
    ].join(' ');
  }

  global.LlmgwCharts = {
    lineChart: lineChart,
    barChart: barChart,
    donutChart: donutChart,
    renderEmpty: renderEmpty,
    niceCeiling: niceCeiling,
  };
})(window);
