// AI 热门板块页面逻辑：拉 /api/ai_hotsector/* 渲染统计条 / 资金曲线 / 今日荐股 / 历史批次

let hsEquityChart;

function fmtPct(v) {
  if (v === null || v === undefined) return '—';
  return `${(Number(v) * 100).toFixed(2)}%`;
}

function fmtMoney(v) {
  if (v === null || v === undefined) return '—';
  return Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

function fmtPrice(v) {
  if (v === null || v === undefined) return '—';
  return Number(v).toFixed(3);
}

// DeepSeek 返回的板块名/股票名/理由是模型生成的自由文本，插入 innerHTML 前必须转义，
// 否则模型输出里一旦带 HTML 就是存储型 XSS
function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

document.addEventListener('DOMContentLoaded', () => {
  loadStats();
  loadEquity();
  loadToday();
  loadHistory();
});

// ── 1. 统计条 ────────────────────────────────────────────────────────────────

async function loadStats() {
  try {
    const res = await fetch('/api/ai_hotsector/stats');
    const s = await res.json();
    document.getElementById('hsInitCapital').textContent = fmtMoney(s.initial_capital);
    document.getElementById('hsCapital').textContent = fmtMoney(s.capital);

    const cumEl = document.getElementById('hsCumReturn');
    cumEl.textContent = fmtPct(s.cum_return);
    cumEl.className = 'sum-val ' + (s.cum_return > 0 ? 'pos' : s.cum_return < 0 ? 'neg' : '');

    const winEl = document.getElementById('hsWinRate');
    if (s.total_count > 0) {
      winEl.textContent = `${fmtPct(s.win_rate)}（${s.win_count}/${s.total_count}）`;
    } else {
      winEl.textContent = '暂无已结算数据';
    }
  } catch (e) {
    console.error(e);
  }
}

// ── 2. 资金曲线 ──────────────────────────────────────────────────────────────

async function loadEquity() {
  try {
    const res = await fetch('/api/ai_hotsector/equity?limit=365');
    const data = (await res.json()).equity || [];
    renderEquityChart(data);
  } catch (e) {
    console.error(e);
  }
}

function renderEquityChart(data) {
  if (!hsEquityChart) {
    hsEquityChart = echarts.init(document.getElementById('hsEquityChart'));
    window.addEventListener('resize', () => hsEquityChart && hsEquityChart.resize());
  }
  if (!data.length) {
    hsEquityChart.clear();
    hsEquityChart.setOption({
      title: { text: '暂无数据', left: 'center', top: 'center',
               textStyle: { color: '#57606a', fontSize: 13 } },
    });
    return;
  }

  const dates = data.map(d => d.pick_date);
  const cum = data.map(d => Number(d.cum_return) * 100);

  hsEquityChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' },
               valueFormatter: (v) => v == null ? '—' : `${v.toFixed(2)}%` },
    grid: { left: 60, right: 30, top: 30, bottom: 50 },
    xAxis: { type: 'category', data: dates, boundaryGap: false,
             axisLabel: { color: '#57606a', fontSize: 11 } },
    yAxis: { type: 'value', name: '累计收益(%)',
             nameTextStyle: { color: '#57606a', fontSize: 11 },
             axisLabel: { color: '#57606a', fontSize: 11,
                          formatter: (v) => `${v.toFixed(1)}%` } },
    series: [
      { name: 'AI 热门板块', type: 'line', data: cum, smooth: true,
        lineStyle: { width: 2, color: '#0969da' },
        itemStyle: { color: '#0969da' },
        areaStyle: { color: 'rgba(9,105,218,0.10)' },
        symbol: 'none' },
    ],
  });
}

// ── 3. 今日荐股 ──────────────────────────────────────────────────────────────

async function loadToday() {
  const wrap = document.getElementById('hsTodayWrap');
  try {
    const res = await fetch('/api/ai_hotsector/today');
    const { pick } = await res.json();
    if (!pick) {
      wrap.innerHTML = '<div class="no-data">尚未运行过预测，请等待 15:05 定时任务，或在 /tasks 页面手动触发 ai_hotsector_predict</div>';
      return;
    }

    document.getElementById('hsTodayDate').textContent = `（${pick.pick_date}）`;

    if (pick.status === 'failed') {
      wrap.innerHTML = `<div class="no-data">最近一次预测失败：${esc(pick.error_msg)}</div>`;
      return;
    }

    const stocks = pick.stocks || [];
    if (!stocks.length) {
      wrap.innerHTML = '<div class="no-data">暂无选股明细</div>';
      return;
    }

    // 按 sector_rank 分组
    const sectors = new Map();
    for (const st of stocks) {
      if (!sectors.has(st.sector_rank)) {
        sectors.set(st.sector_rank, {
          name: st.sector_name, reason: st.sector_reason, stocks: [],
        });
      }
      sectors.get(st.sector_rank).stocks.push(st);
    }

    const cards = [...sectors.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([rank, sec]) => renderSectorCard(rank, sec))
      .join('');
    wrap.innerHTML = `<div class="sector-grid">${cards}</div>`;
  } catch (e) {
    console.error(e);
    wrap.innerHTML = '<div class="no-data">加载失败</div>';
  }
}

function renderSectorCard(rank, sector) {
  const stockRows = sector.stocks
    .sort((a, b) => a.stock_rank - b.stock_rank)
    .map(renderStockRow)
    .join('');
  return `
    <div class="sector-card">
      <div class="sector-card-head">
        <div><span class="sector-rank">${rank}</span><span class="sector-name">${esc(sector.name)}</span></div>
      </div>
      <div class="sector-reason">${esc(sector.reason)}</div>
      ${stockRows}
    </div>
  `;
}

function renderStockRow(st) {
  let badge = '<span class="settle-badge pending">待结算</span>';
  let pctHtml = '<span class="stock-pct na">—</span>';
  let priceLine = '';

  if (st.settle_status === 'code_not_found') {
    badge = '<span class="settle-badge na">代码无效</span>';
  } else if (st.settle_status === 'settled') {
    const isWin = st.is_win === 1;
    badge = isWin
      ? '<span class="settle-badge win">胜</span>'
      : '<span class="settle-badge lose">负</span>';
    pctHtml = `<span class="stock-pct ${isWin ? 'pos' : 'neg'}">${fmtPct(st.pct_change)}</span>`;
    priceLine = `买 ${fmtPrice(st.buy_price)} → 卖 ${fmtPrice(st.sell_price)}`;
  } else if (st.settle_status === 'priced') {
    priceLine = `买 ${fmtPrice(st.buy_price)}，等待次日收盘`;
  }

  return `
    <div class="stock-row">
      <div class="stock-main">
        <div class="stock-code-name">${esc(st.name)}<span class="code">${esc(st.code)}</span>${badge}</div>
        <div class="stock-reason">${esc(st.stock_reason)}</div>
      </div>
      <div class="stock-perf">
        ${pctHtml}
        <div class="stock-price-line">${priceLine}</div>
      </div>
    </div>
  `;
}

// ── 4. 历史批次 ──────────────────────────────────────────────────────────────

async function loadHistory() {
  const wrap = document.getElementById('hsHistoryWrap');
  try {
    const res = await fetch('/api/ai_hotsector/history?limit=60');
    const rows = (await res.json()).history || [];
    if (!rows.length) {
      wrap.innerHTML = '<div class="no-data">暂无记录</div>';
      return;
    }
    const trs = rows.map(r => {
      const statusText = { predicted: '已预测(待回填)', priced: '已回填买入价',
                            settled: '已结算', failed: '预测失败' }[r.status] || r.status;
      const winRate = r.total_count ? `${r.win_count}/${r.total_count}` : '—';
      const cls = r.status === 'failed' ? 'status-failed' : '';
      return `
        <tr class="${cls}">
          <td>${r.pick_date}</td>
          <td>${statusText}</td>
          <td>${winRate}</td>
          <td>${r.day_return !== null && r.day_return !== undefined ? fmtPct(r.day_return) : '—'}</td>
          <td>${r.cum_return !== null && r.cum_return !== undefined ? fmtPct(r.cum_return) : '—'}</td>
        </tr>
      `;
    }).join('');
    wrap.innerHTML = `
      <div class="holdings-table-wrap">
        <table class="hs-tbl">
          <thead><tr><th>日期</th><th>状态</th><th>当批胜率</th><th>当批收益</th><th>累计收益</th></tr></thead>
          <tbody>${trs}</tbody>
        </table>
      </div>
    `;
  } catch (e) {
    console.error(e);
    wrap.innerHTML = '<div class="no-data">加载失败</div>';
  }
}
