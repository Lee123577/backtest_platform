/* 大盘云图 —— ECharts treemap，基于自家 stock_kline 数据 */
(() => {
  const $ = (id) => document.getElementById(id);
  let chart = null;
  let autoTimer = null;
  const AUTO_INTERVAL_MS = 60_000;   // 60s (后端 60s 缓存对齐)

  // ── 涨跌色阶（A 股红涨绿跌）──────────────────────────────────────────────
  // 8 级渐变，0 居中灰
  function gradeColor(pct) {
    if (pct >= 9.8) return '#cf0000';   // 涨停级
    if (pct >= 5) return '#cf222e';
    if (pct >= 3) return '#e84343';
    if (pct >= 1) return '#f08585';
    if (pct >= 0.2) return '#f4b9b9';
    if (pct > -0.2) return '#888';
    if (pct > -1) return '#9bc89a';
    if (pct > -3) return '#6cb069';
    if (pct > -5) return '#3d9038';
    if (pct > -9.8) return '#1a7f37';
    return '#005c0c';                   // 跌停级
  }

  // ── 数据请求 ────────────────────────────────────────────────────────────
  async function load() {
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
      const data = await res.json();
      render(data);
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

  // ── 渲染 ────────────────────────────────────────────────────────────────
  function render(data) {
    $('dateBadge').textContent = `📅 ${data.trade_date}`;

    // 顶部统计卡（对齐 52etf：上涨/平盘/下跌 + 全市场成交额 + 缩放量）
    const s = data.summary || {};
    const totalAmt = s.total_amount || 0;
    const prevAmt = s.prev_amount || 0;
    let volDelta = '—';
    let volCls = '';
    if (prevAmt > 0) {
      const diff = totalAmt - prevAmt;
      volDelta = (diff >= 0 ? '放量 ' : '缩量 ') + fmtAmount(Math.abs(diff));
      volCls = diff >= 0 ? 'up' : 'down';
    }

    const cards = [
      { label: '股票总数', val: data.total, cls: '' },
      { label: '上涨', val: s.up || 0, cls: 'up' },
      { label: '下跌', val: s.down || 0, cls: 'down' },
      { label: '平盘', val: s.flat || 0, cls: '' },
      { label: '平均涨跌',
        val: (s.avg_pct >= 0 ? '+' : '') + (s.avg_pct || 0).toFixed(2) + '%',
        cls: s.avg_pct > 0 ? 'up' : s.avg_pct < 0 ? 'down' : '' },
      { label: '全市场成交额', val: fmtAmount(totalAmt), cls: '' },
      { label: '比昨日', val: volDelta, cls: volCls },
    ];
    $('summaryRow').innerHTML = cards.map(c => `
      <div class="cm-stat ${c.cls}">
        <div class="label">${c.label}</div>
        <div class="val">${c.val}</div>
      </div>
    `).join('');

    // 色阶图例
    $('legend').innerHTML = [
      ['#005c0c', '≤ -9.8%'], ['#1a7f37', '-5 ~ -9.8%'], ['#3d9038', '-3 ~ -5%'],
      ['#6cb069', '-1 ~ -3%'], ['#9bc89a', '0 ~ -1%'], ['#888', '0%'],
      ['#f4b9b9', '0 ~ 1%'], ['#f08585', '1 ~ 3%'], ['#e84343', '3 ~ 5%'],
      ['#cf222e', '5 ~ 9.8%'], ['#cf0000', '≥ 9.8%'],
    ].map(([c, t]) => `<span class="swatch" style="background:${c};"></span><span>${t}</span>`)
     .join('  ');

    // 板块过滤（客户端做，避免反复打 API）
    const catFilter = $('categoryFilter').value;
    const filteredItems = (catFilter === 'all')
      ? data.items
      : data.items.filter(it => it.category === catFilter);

    // 按 category 聚合成 treemap 数据
    const groups = {};
    for (const item of filteredItems) {
      const cat = item.category || '其他';
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push({
        name: `${item.code}\n${item.name}`,
        value: item.market_cap,
        itemStyle: { color: gradeColor(item.pct_change) },
        // 自定义字段供 tooltip 用
        _code: item.code,
        _stockName: item.name,
        _pct: item.pct_change,
        _close: item.close,
        _amount: item.amount,
      });
    }
    // 大分类按总市值排序
    const treeData = Object.entries(groups)
      .map(([name, children]) => ({
        name,
        children,
        value: children.reduce((a, b) => a + b.value, 0),
      }))
      .sort((a, b) => b.value - a.value);

    if (!chart) chart = echarts.init($('treemap'));
    chart.setOption({
      tooltip: {
        confine: true,
        formatter: (info) => {
          const d = info.data;
          if (d._code) {
            const pctColor = d._pct > 0 ? '#cf222e' : d._pct < 0 ? '#1a7f37' : '#888';
            const pctSign = d._pct > 0 ? '+' : '';
            return `
              <div style="font-size:13px;">
                <b>${d._stockName}</b> <span style="color:#888;">${d._code}</span><br/>
                市值: <b>${d.value.toFixed(1)} 亿</b><br/>
                收盘: ${d._close != null ? d._close : '—'}<br/>
                涨跌: <b style="color:${pctColor};">${pctSign}${d._pct.toFixed(2)}%</b><br/>
                成交额: ${fmtAmount(d._amount)}
              </div>
            `;
          }
          // 大分类节点
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
          itemStyle: { color: '#444', borderColor: '#666', textStyle: { color: '#fff' } },
          emphasis: { itemStyle: { textStyle: { color: '#ffd700' } } },
        },
        visibleMin: 200,
        // 全局 label：叶节点显示「名字\n涨跌幅」（不显示代码）
        // 用矩形面积自适应字号：大盘股大字、小票小字、太小不显示
        label: {
          show: true,
          color: '#fff',
          fontWeight: 600,
          textShadowColor: 'rgba(0,0,0,.6)',
          textShadowBlur: 2,
          formatter: (params) => {
            const d = params.data;
            // 大分类节点（无 _code）由 upperLabel 渲染，这里返回空
            if (!d._code) return '';
            const sign = d._pct > 0 ? '+' : '';
            return `${d._stockName}\n${sign}${d._pct.toFixed(2)}%`;
          },
        },
        // 大分类块顶部条（显示"沪市主板"等）
        upperLabel: {
          show: true, height: 22, color: '#fff',
          fontSize: 13, fontWeight: 700,
          backgroundColor: 'rgba(0,0,0,.25)',
        },
        levels: [
          {
            // 大分类层
            itemStyle: { borderColor: '#111', borderWidth: 1, gapWidth: 2 },
          },
          {
            // 叶节点层（股票）
            itemStyle: { borderColor: '#222', borderWidth: 0.5, gapWidth: 1 },
          },
        ],
      }],
    }, true);

    // resultZone 高度可能在 init 时还没 layout，等下一帧 resize 一次
    requestAnimationFrame(() => chart.resize());
  }

  // ── 自动刷新 ────────────────────────────────────────────────────────────
  function startAutoRefresh() {
    if (autoTimer) clearInterval(autoTimer);
    autoTimer = setInterval(() => {
      // 后台 tab 不刷新
      if (document.visibilityState === 'visible') load();
    }, AUTO_INTERVAL_MS);
  }
  function stopAutoRefresh() {
    if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
  }

  // ── 事件 ────────────────────────────────────────────────────────────────
  $('marketFilter').addEventListener('change', load);
  $('minCapFilter').addEventListener('change', load);
  // category 切换是纯客户端过滤，不打 API
  $('categoryFilter').addEventListener('change', () => {
    // 取出缓存的 data 重新 render（简单做：直接 load 一次，命中后端缓存秒回）
    load();
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
  window.addEventListener('resize', () => chart && chart.resize());

  // 页面隐藏停轮询，回前台首次立刻刷新
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && $('autoRefresh').checked) {
      load();
    }
  });

  load();
  startAutoRefresh();
})();
