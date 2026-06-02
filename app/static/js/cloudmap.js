/* 大盘云图 —— ECharts treemap，基于自家 stock_kline 数据 */
(() => {
  const $ = (id) => document.getElementById(id);
  let chart = null;

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

  // ── 渲染 ────────────────────────────────────────────────────────────────
  function render(data) {
    $('dateBadge').textContent = `📅 ${data.trade_date}`;

    // 顶部统计卡
    const s = data.summary || {};
    const cards = [
      { label: '股票数', val: data.total, cls: '' },
      { label: '上涨', val: s.up || 0, cls: 'up' },
      { label: '下跌', val: s.down || 0, cls: 'down' },
      { label: '平盘', val: s.flat || 0, cls: '' },
      { label: '平均涨跌', val: (s.avg_pct >= 0 ? '+' : '') + (s.avg_pct || 0).toFixed(2) + '%',
        cls: s.avg_pct > 0 ? 'up' : s.avg_pct < 0 ? 'down' : '' },
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

    // 按 category 聚合成 treemap 数据
    const groups = {};
    for (const item of data.items) {
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
                涨跌: <b style="color:${pctColor};">${pctSign}${d._pct.toFixed(2)}%</b>
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
        levels: [
          {
            itemStyle: {
              borderColor: '#111', borderWidth: 1, gapWidth: 2,
            },
            upperLabel: {
              show: true, height: 22, color: '#fff',
              fontSize: 12, fontWeight: 600,
            },
          },
          {
            itemStyle: {
              borderColor: '#222', borderWidth: 0.5, gapWidth: 1,
            },
            label: {
              show: true, color: '#fff', fontSize: 12,
              formatter: (params) => {
                const d = params.data;
                if (!d._code) return d.name;
                const pctSign = d._pct > 0 ? '+' : '';
                return `${d._stockName}\n${pctSign}${d._pct.toFixed(1)}%`;
              },
            },
          },
        ],
      }],
    }, true);

    // resultZone 高度可能在 init 时还没 layout，等下一帧 resize 一次
    requestAnimationFrame(() => chart.resize());
  }

  // ── 事件 ────────────────────────────────────────────────────────────────
  $('marketFilter').addEventListener('change', load);
  $('minCapFilter').addEventListener('change', load);
  $('refreshBtn').addEventListener('click', load);
  window.addEventListener('resize', () => chart && chart.resize());

  load();
})();
