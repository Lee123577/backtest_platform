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

// esc() 现在是 util.js 里的全站共用实现(必须比本文件先加载)。
// DeepSeek 返回的板块名/股票名/理由是模型生成的自由文本，插入 innerHTML 前必须转义，
// 否则模型输出里一旦带 HTML 就是存储型 XSS

document.addEventListener('DOMContentLoaded', () => {
  loadStats();
  loadEquity();
  // loadIntraday 依赖 loadToday 先把 .stock-row 渲染到 DOM 里，
  // 并发跑的话会因为元素还不存在而"更新"了个寂寞
  loadToday().then(loadIntraday);
  loadHistory();
  loadSectorStats();
  loadPromptStats();
  // 盘中浮动盈亏会变，定时刷新；用 setInterval 而不是整页重载，避免打断阅读
  setInterval(loadIntraday, 30_000);
});

// ── 1. 统计条 ────────────────────────────────────────────────────────────────

async function loadStats() {
  try {
    const res = await fetch('/api/ai_hotsector/stats');
    const s = await res.json();
    document.getElementById('hsInitCapital').textContent = `¥${fmtMoney(s.initial_capital)}`;
    document.getElementById('hsCapital').textContent = `¥${fmtMoney(s.capital)}`;

    const cumEl = document.getElementById('hsCumReturn');
    cumEl.textContent = fmtPct(s.cum_return);
    cumEl.className = 'sum-val ' + (s.cum_return > 0 ? 'pos' : s.cum_return < 0 ? 'neg' : '');

    const feeEl = document.getElementById('hsCumReturnAfterFee');
    feeEl.textContent = fmtPct(s.cum_return_after_fee);
    feeEl.className = 'sum-val ' + (s.cum_return_after_fee > 0 ? 'pos' : s.cum_return_after_fee < 0 ? 'neg' : '');

    const bmEl = document.getElementById('hsBenchmarkReturn');
    bmEl.textContent = fmtPct(s.benchmark_cum_return);
    bmEl.className = 'sum-val ' + (s.benchmark_cum_return > 0 ? 'pos' : s.benchmark_cum_return < 0 ? 'neg' : '');

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
  // 单点画不出曲线,空图很难看 —— 少于 2 个结算日时用文字占位
  if (data.length < 2) {
    const msg = data.length === 0
      ? '暂无已结算数据，资金曲线将在首批预测结算后出现'
      : `已结算 1 个交易日（${data[0].pick_date}，${(Number(data[0].day_return) * 100).toFixed(2)}%），再结算 1 天即可画出曲线`;
    hsEquityChart.clear();
    hsEquityChart.setOption({
      title: { text: msg, left: 'center', top: 'center',
               textStyle: { color: '#57606a', fontSize: 13, fontWeight: 'normal' } },
    });
    return;
  }

  const dates = data.map(d => d.pick_date);
  const cum = data.map(d => Number(d.cum_return) * 100);
  const bmCum = data.map(d => d.benchmark_cum_return !== null && d.benchmark_cum_return !== undefined
    ? Number(d.benchmark_cum_return) * 100 : null);
  const feeCum = data.map(d => d.cum_return_after_fee !== null && d.cum_return_after_fee !== undefined
    ? Number(d.cum_return_after_fee) * 100 : null);

  hsEquityChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' },
               valueFormatter: (v) => v == null ? '—' : `${v.toFixed(2)}%` },
    legend: { data: ['AI 热门板块', '扣费后', '中证1000(基准)'], top: 0,
              textStyle: { fontSize: 11 } },
    grid: { left: 60, right: 30, top: 40, bottom: 50 },
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
      { name: '扣费后', type: 'line', data: feeCum, smooth: true,
        lineStyle: { width: 2, color: '#9a6700', type: 'dashed' },
        itemStyle: { color: '#9a6700' },
        symbol: 'none' },
      { name: '中证1000(基准)', type: 'line', data: bmCum, smooth: true,
        lineStyle: { width: 2, color: '#57606a' },
        itemStyle: { color: '#57606a' },
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
  let pctHtml = '';   // 未结算不显示悬空的"—"，右侧只留价格行
  let priceLine = '等待买入价回填';

  if (st.settle_status === 'code_not_found') {
    badge = '<span class="settle-badge na">代码无效</span>';
    priceLine = '';
  } else if (st.settle_status === 'suspended') {
    badge = '<span class="settle-badge na">停牌排除</span>';
    priceLine = '长期无行情，已排除不计入胜率';
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
    <div class="stock-row" data-code="${esc(st.code)}" data-settle-status="${esc(st.settle_status)}">
      <div class="stock-main">
        <div class="stock-code-name">${esc(st.name)}<span class="code">${esc(st.code)}</span>${badge}</div>
        <div class="stock-reason">${esc(st.stock_reason)}</div>
      </div>
      <div class="stock-perf">
        <span class="stock-pct-slot">${pctHtml}</span>
        <div class="stock-price-line">${priceLine}</div>
      </div>
    </div>
  `;
}

// ── 盘中浮动盈亏(priced 状态:已买入、还没到次日收盘)──────────────────────────

async function loadIntraday() {
  const hint = document.getElementById('hsIntradayUpdated');
  try {
    const res = await fetch('/api/ai_hotsector/intraday');
    const rows = (await res.json()).intraday || [];
    applyIntraday(rows);
    if (hint) {
      const now = new Date();
      const hh = String(now.getHours()).padStart(2, '0');
      const mm = String(now.getMinutes()).padStart(2, '0');
      const ss = String(now.getSeconds()).padStart(2, '0');
      hint.textContent = `盘中数据更新于 ${hh}:${mm}:${ss}`;
    }
  } catch (e) {
    console.error(e);
    if (hint) hint.textContent = '盘中数据更新失败，将于 30 秒后重试';
  }
}

function applyIntraday(rows) {
  if (!rows.length) return;
  const byCode = new Map(rows.map(r => [r.code, r]));
  document.querySelectorAll('.stock-row[data-settle-status="priced"]').forEach(el => {
    const info = byCode.get(el.dataset.code);
    if (!info || info.realtime_price === null || info.realtime_price === undefined) return;
    const isUp = info.floating_pct !== null && info.floating_pct > 0;
    const isDown = info.floating_pct !== null && info.floating_pct < 0;
    const pctSlot = el.querySelector('.stock-pct-slot');
    if (pctSlot) {
      pctSlot.innerHTML = `<span class="stock-pct ${isUp ? 'pos' : isDown ? 'neg' : ''}">${fmtPct(info.floating_pct)}</span>`;
    }
    const priceLine = el.querySelector('.stock-price-line');
    if (priceLine) {
      priceLine.textContent = `买 ${fmtPrice(info.buy_price)} → 现价 ${fmtPrice(info.realtime_price)}(盘中)`;
    }
  });
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
      const excludedNote = r.excluded_count ? `（排除${r.excluded_count}只）` : '';
      const winRate = r.total_count
        ? `${r.win_count}/${r.total_count}${excludedNote}`
        : (excludedNote ? `—${excludedNote}` : '—');
      const cls = r.status === 'failed' ? 'status-failed' : '';
      return `
        <tr class="${cls}">
          <td>${esc(r.pick_date)}</td>
          <td>${esc(statusText)}</td>
          <td>${esc(winRate)}</td>
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

// ── 5. 板块表现 ──────────────────────────────────────────────────────────────

async function loadSectorStats() {
  const wrap = document.getElementById('hsSectorStatsWrap');
  try {
    const res = await fetch('/api/ai_hotsector/sector_stats');
    const rows = (await res.json()).sectors || [];
    if (!rows.length) {
      wrap.innerHTML = '<div class="no-data">暂无数据</div>';
      return;
    }
    const trs = rows.map(r => {
      const winRate = r.settled_count > 0
        ? fmtPct(r.win_count / r.settled_count)
        : '—';
      const avgPct = r.avg_pct_change !== null && r.avg_pct_change !== undefined
        ? fmtPct(r.avg_pct_change) : '—';
      // 出现天数 >= 3 视为"AI 反复推荐同一板块"，高亮提示同质化
      const rowClass = r.days_picked >= 3 ? ' class="hs-repeat-sector"' : '';
      return `
        <tr${rowClass}>
          <td>${esc(r.sector_name)}</td>
          <td>${r.days_picked}</td>
          <td>${r.settled_count}/${r.stock_count}</td>
          <td>${r.settled_count > 0 ? `${r.win_count}/${r.settled_count}（${winRate}）` : '—'}</td>
          <td>${avgPct}</td>
        </tr>
      `;
    }).join('');
    wrap.innerHTML = `
      <div class="holdings-table-wrap">
        <table class="hs-tbl">
          <thead><tr><th>板块</th><th>出现天数</th><th>已结算/累计出现</th><th>胜率</th><th>平均涨跌幅</th></tr></thead>
          <tbody>${trs}</tbody>
        </table>
      </div>
    `;
  } catch (e) {
    console.error(e);
    wrap.innerHTML = '<div class="no-data">加载失败</div>';
  }
}

// ── 6. 提示词版本对比 ────────────────────────────────────────────────────────

async function loadPromptStats() {
  const wrap = document.getElementById('hsPromptStatsWrap');
  try {
    const res = await fetch('/api/ai_hotsector/prompt_stats');
    const rows = (await res.json()).prompts || [];
    if (!rows.length) {
      wrap.innerHTML = '<div class="no-data">暂无数据</div>';
      return;
    }
    const trs = rows.map(r => {
      const winRate = r.settled_count > 0
        ? fmtPct(r.win_count / r.settled_count)
        : '—';
      const avgPct = r.avg_pct_change !== null && r.avg_pct_change !== undefined
        ? fmtPct(r.avg_pct_change) : '—';
      return `
        <tr>
          <td>${esc(r.stock_prompt_version)}</td>
          <td>${r.days_used}</td>
          <td>${r.settled_count}/${r.stock_count}</td>
          <td>${r.settled_count > 0 ? `${r.win_count}/${r.settled_count}（${winRate}）` : '—'}</td>
          <td>${avgPct}</td>
        </tr>
      `;
    }).join('');
    wrap.innerHTML = `
      <div class="holdings-table-wrap">
        <table class="hs-tbl">
          <thead><tr><th>提示词版本</th><th>使用天数</th><th>已结算/累计出现</th><th>胜率</th><th>平均涨跌幅</th></tr></thead>
          <tbody>${trs}</tbody>
        </table>
      </div>
    `;
  } catch (e) {
    console.error(e);
    wrap.innerHTML = '<div class="no-data">加载失败</div>';
  }
}
