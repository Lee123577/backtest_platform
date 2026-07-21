/* 大盘云图 —— ECharts treemap + 沉浸式深色布局（数据来自本站 stock_kline，UI 视觉对齐参考站） */
(() => {
  const $ = (id) => document.getElementById(id);
  let chart = null;
  let autoTimer = null;
  let lastLoadAt = 0;   // 节流 visibilitychange 抢跑用
  let latestData = null; // 最近一次 /api/cloudmap/data 响应，切换板块按钮时纯前端重渲染
  let activeCat = 'all';
  const AUTO_INTERVAL_MS = 60_000;   // 60s (后端 60s 缓存对齐)

  const OVERVIEW_INDICES = [
    { code: '000001', name: '上证指数' },
    { code: '399006', name: '创业板指' },
    { code: '000300', name: '沪深300' },
    { code: '000905', name: '中证500' },
    { code: '000852', name: '中证1000' },
    { code: '000016', name: '上证50' },
  ];

  // ── 涨跌色阶 —— 在 -4%~+4% 之间做线性插值，超出区间钳位到端点色 ────────
  const COLOR_STOPS = [
    [-4, [48, 204, 90]],   // #30cc5a
    [-3, [47, 170, 81]],   // #2faa51
    [-2, [49, 137, 78]],   // #31894e
    [-1, [56, 105, 79]],   // #38694f
    [0, [65, 69, 84]],     // #414554
    [1, [120, 69, 81]],    // #784551
    [2, [165, 66, 74]],    // #a5424a
    [3, [206, 61, 65]],    // #ce3d41
    [4, [246, 53, 56]],    // #f63538
  ];
  function gradeColor(pct) {
    if (pct == null || isNaN(pct)) return 'rgb(65,69,84)';
    const p = Math.max(-4, Math.min(4, pct));
    for (let i = 0; i < COLOR_STOPS.length - 1; i++) {
      const [p0, c0] = COLOR_STOPS[i];
      const [p1, c1] = COLOR_STOPS[i + 1];
      if (p >= p0 && p <= p1) {
        const t = (p - p0) / (p1 - p0);
        const r = Math.round(c0[0] + (c1[0] - c0[0]) * t);
        const g = Math.round(c0[1] + (c1[1] - c0[1]) * t);
        const b = Math.round(c0[2] + (c1[2] - c0[2]) * t);
        return `rgb(${r},${g},${b})`;
      }
    }
    return 'rgb(65,69,84)';
  }

  // ── 数据请求 ────────────────────────────────────────────────────────────
  async function load() {
    lastLoadAt = Date.now();
    const market = $('marketFilter').value;
    const minCap = $('minCapFilter').value;
    $('loading').style.display = '';
    $('error').style.display = 'none';

    try {
      const res = await fetch(`/api/cloudmap/data?market=${market}&min_cap=${minCap}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      latestData = await res.json();
      render();
    } catch (e) {
      $('error').textContent = `加载失败：${e.message}`;
      $('error').style.display = '';
    } finally {
      $('loading').style.display = 'none';
    }
  }

  // ── 成交额格式化（元 → 万亿/亿） ─────────────────────────────────────
  function fmtAmount(v) {
    if (!v || isNaN(v)) return '—';
    if (v >= 1e12) return (v / 1e12).toFixed(2) + ' 万亿';
    if (v >= 1e8) return (v / 1e8).toFixed(0) + ' 亿';
    if (v >= 1e4) return (v / 1e4).toFixed(0) + ' 万';
    return Math.round(v).toString();
  }
  function pctSpan(pct) {
    if (pct == null || isNaN(pct)) return '<span class="cm-flat">—</span>';
    const cls = pct > 0 ? 'cm-up' : pct < 0 ? 'cm-down' : 'cm-flat';
    const sign = pct > 0 ? '+' : '';
    return `<span class="${cls}">${sign}${pct.toFixed(2)}%</span>`;
  }

  // ── 左侧栏板块按钮：各板块平均涨跌幅 ────────────────────────────────────
  function renderBoardPcts(items) {
    const CATS = ['沪市主板', '深市主板', '科创板', '创业板', '中小板', '北交所'];
    const sums = {}; const counts = {};
    let allSum = 0, allCount = 0;
    for (const it of items) {
      const cat = it.category;
      sums[cat] = (sums[cat] || 0) + it.pct_change;
      counts[cat] = (counts[cat] || 0) + 1;
      allSum += it.pct_change; allCount++;
    }
    const allEl = $('cmBoardPctAll');
    if (allEl) allEl.innerHTML = allCount ? pctSpan(allSum / allCount) : '—';
    CATS.forEach((cat) => {
      const el = $('cmBoardPct-' + cat);
      if (!el) return;
      el.innerHTML = counts[cat] ? pctSpan(sums[cat] / counts[cat]) : '—';
    });
  }

  // ── 渲染 ────────────────────────────────────────────────────────────────
  function render() {
    const data = latestData;
    if (!data) return;
    $('cmDateBadge').textContent = `数据日期：${data.trade_date}`;

    const s = data.summary || {};
    const totalAmt = s.total_amount || 0;
    const prevAmt = s.prev_amount || 0;
    let volText = '0';
    let volCls = '';
    if (prevAmt > 0) {
      const diff = totalAmt - prevAmt;
      volText = (diff >= 0 ? '放量 ' : '缩量 ') + fmtAmount(Math.abs(diff));
      volCls = diff >= 0 ? 'up' : 'down';
    }
    $('cmUp').textContent = s.up || 0;
    $('cmFlat').textContent = s.flat || 0;
    $('cmDown').textContent = s.down || 0;
    $('cmAmount').textContent = fmtAmount(totalAmt);
    const volEl = $('cmVolDelta');
    volEl.textContent = volText;
    volEl.className = 'cm-stats-val' + (volCls === 'up' ? ' cm-up' : volCls === 'down' ? ' cm-down' : '');

    renderBoardPcts(data.items);

    // 色阶图例（对齐参考站的 9 档 -4%~+4%）
    $('legend').innerHTML = COLOR_STOPS.map(([pct, rgb]) => {
      const label = (pct > 0 ? '+' : '') + pct + '%';
      return `<div class="cm-legend-swatch" style="background:rgb(${rgb.join(',')});">${label}</div>`;
    }).join('');

    // 板块过滤（客户端做，避免反复打 API）
    const filteredItems = (activeCat === 'all')
      ? data.items
      : data.items.filter(it => it.category === activeCat);

    // 按 category 聚合成 treemap 数据
    const groups = {};
    for (const item of filteredItems) {
      const cat = item.category || '其他';
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push({
        name: `${item.code}\n${item.name}`,
        value: item.market_cap,
        itemStyle: { color: gradeColor(item.pct_change) },
        _code: item.code,
        _stockName: item.name,
        _pct: item.pct_change,
        _close: item.close,
        _amount: item.amount,
      });
    }
    const treeData = Object.entries(groups)
      .map(([name, children]) => ({
        name,
        children,
        value: children.reduce((a, b) => a + b.value, 0),
      }))
      .sort((a, b) => b.value - a.value);

    if (!chart) chart = echarts.init($('treemap'));
    chart.setOption({
      backgroundColor: '#262931',
      tooltip: {
        confine: true,
        backgroundColor: '#1e2026',
        borderColor: '#4a4f5d',
        textStyle: { color: '#fefefe' },
        formatter: (info) => {
          const d = info.data;
          if (d._code) {
            const pctColor = d._pct > 0 ? '#f63538' : d._pct < 0 ? '#30cc5a' : '#cfd2da';
            const pctSign = d._pct > 0 ? '+' : '';
            return `
              <div style="font-size:13px;">
                <b>${d._stockName}</b> <span style="color:#8a8f9c;">${d._code}</span><br/>
                市值: <b>${d.value.toFixed(1)} 亿</b><br/>
                收盘: ${d._close != null ? d._close : '—'}<br/>
                涨跌: <b style="color:${pctColor};">${pctSign}${d._pct.toFixed(2)}%</b><br/>
                成交额: ${fmtAmount(d._amount)}
              </div>
            `;
          }
          return `<b>${d.name}</b><br/>总市值: ${d.value.toFixed(0)} 亿`;
        },
      },
      series: [{
        type: 'treemap',
        data: treeData,
        roam: false,
        nodeClick: 'zoomToNode',
        breadcrumb: {
          show: true,
          height: 24,
          left: 'center',
          top: 'top',
          itemStyle: { color: '#3f414b', borderColor: '#4a4f5d', textStyle: { color: '#fefefe' } },
          emphasis: { itemStyle: { textStyle: { color: '#f63538' } } },
        },
        visibleMin: 200,
        label: {
          show: true,
          color: '#fff',
          fontWeight: 600,
          textShadowColor: 'rgba(0,0,0,.6)',
          textShadowBlur: 2,
          formatter: (params) => {
            const d = params.data;
            if (!d._code) return '';
            const sign = d._pct > 0 ? '+' : '';
            return `${d._stockName}\n${sign}${d._pct.toFixed(2)}%`;
          },
        },
        upperLabel: {
          show: true, height: 22, color: '#fff',
          fontSize: 13, fontWeight: 700,
          backgroundColor: 'rgba(0,0,0,.35)',
        },
        levels: [
          { itemStyle: { borderColor: '#262931', borderWidth: 1, gapWidth: 2 } },
          { itemStyle: { borderColor: '#1e2026', borderWidth: 0.5, gapWidth: 1 } },
        ],
      }],
    }, true);

    requestAnimationFrame(() => chart.resize());
  }

  // ── 头部指数行情条 ──────────────────────────────────────────────────────
  function renderIndices(results) {
    $('cmIndices').innerHTML = OVERVIEW_INDICES.map((ix, i) => {
      const rows = results[i] || [];
      if (rows.length < 2) {
        return `<div class="cm-idx-tile"><div class="cm-idx-name">${ix.name}</div><div class="cm-idx-val">—</div></div>`;
      }
      const last = rows[rows.length - 1];
      const prev = rows[rows.length - 2];
      const chg = prev.close ? (last.close - prev.close) / prev.close * 100 : null;
      const cls = chg == null ? 'cm-flat' : (chg > 0 ? 'cm-up' : chg < 0 ? 'cm-down' : 'cm-flat');
      const sign = chg != null && chg > 0 ? '+' : '';
      return `<div class="cm-idx-tile">
        <div class="cm-idx-name">${ix.name}</div>
        <div class="cm-idx-val ${cls}">${Number(last.close).toFixed(2)}</div>
        <div class="cm-idx-pct ${cls}">${chg == null ? '—' : sign + chg.toFixed(2) + '%'}</div>
      </div>`;
    }).join('');
  }
  function loadIndices() {
    const end = new Date();
    const start = new Date(end.getTime() - 15 * 86400000);
    const fmt = (d) => d.toISOString().slice(0, 10);
    const qs = `?start_date=${fmt(start)}&end_date=${fmt(end)}`;
    Promise.all(OVERVIEW_INDICES.map((ix) =>
      fetch(`/api/index/${encodeURIComponent(ix.code)}/kline${qs}`)
        .then((r) => (r.ok ? r.json() : { data: [] }))
        .then((j) => j.data || [])
        .catch(() => [])
    )).then(renderIndices);
  }

  // ── 头部时钟 ────────────────────────────────────────────────────────────
  function tickClock() {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    $('cmTime').textContent =
      `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
      `${pad(now.getHours())}:${pad(now.getMinutes())}`;
  }

  // ── 全屏 ────────────────────────────────────────────────────────────────
  function toggleFullscreen() {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => {});
    } else {
      document.exitFullscreen().catch(() => {});
    }
  }

  // ── 自动刷新 ────────────────────────────────────────────────────────────
  function startAutoRefresh() {
    if (autoTimer) clearInterval(autoTimer);
    autoTimer = setInterval(() => {
      if (document.visibilityState === 'visible') load();
    }, AUTO_INTERVAL_MS);
  }
  function stopAutoRefresh() {
    if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
  }

  // ── 事件 ────────────────────────────────────────────────────────────────
  $('marketFilter').addEventListener('change', load);
  $('minCapFilter').addEventListener('change', load);
  document.querySelectorAll('.cm-board-item').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.cm-board-item').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      activeCat = btn.getAttribute('data-cat');
      render();
    });
  });
  $('refreshBtn').addEventListener('click', load);
  $('autoRefresh').addEventListener('change', (e) => {
    if (e.target.checked) {
      startAutoRefresh();
      $('autoRefreshHint').textContent = '自动刷新 60 秒';
    } else {
      stopAutoRefresh();
      $('autoRefreshHint').textContent = '自动刷新已关闭';
    }
  });
  $('cmFullscreenBtn').addEventListener('click', toggleFullscreen);
  window.addEventListener('resize', () => chart && chart.resize());

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && $('autoRefresh').checked
        && Date.now() - lastLoadAt >= AUTO_INTERVAL_MS) {
      load();
    }
  });

  load();
  loadIndices();
  tickClock();
  setInterval(tickClock, 30_000);
  setInterval(loadIndices, AUTO_INTERVAL_MS);
  startAutoRefresh();
})();
