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

// 涨跌着色(A 股习惯:涨红跌绿)。base 是元素原有的类名前缀
function signClass(base, v) {
  return base + ' ' + (v > 0 ? 'pos' : v < 0 ? 'neg' : '');
}

async function loadStats() {
  try {
    const res = await fetch('/api/ai_hotsector/stats');
    const s = await res.json();

    document.getElementById('hsInitCapital').textContent = `本金 ¥${fmtMoney(s.initial_capital)}`;
    document.getElementById('hsCapital').textContent = `¥${fmtMoney(s.capital)}`;

    const cumEl = document.getElementById('hsCumReturn');
    cumEl.textContent = fmtPct(s.cum_return);
    cumEl.className = signClass('hs-stat-val', s.cum_return);

    const feeEl = document.getElementById('hsCumReturnAfterFee');
    feeEl.textContent = fmtPct(s.cum_return_after_fee);
    feeEl.className = signClass('hs-stat-val', s.cum_return_after_fee);

    const bmEl = document.getElementById('hsBenchmarkReturn');
    bmEl.textContent = fmtPct(s.benchmark_cum_return);
    bmEl.className = signClass('hs-stat-val', s.benchmark_cum_return);

    renderExcess(s);
    renderWinRate(s);
  } catch (e) {
    console.error(e);
    document.getElementById('hsExcessSub').textContent = '统计加载失败';
  }
}

// 超额收益 = 策略累计 − 基准累计,单位 pp(百分点)。
// 这才是"这个 AI 到底行不行"的答案:原先把两个数并排摆着,要用户自己做减法。
// 用未扣费口径与基准对齐(基准也没扣费),扣费后单独列在支撑数据里,不藏。
function renderExcess(s) {
  const el = document.getElementById('hsExcess');
  const sub = document.getElementById('hsExcessSub');
  const a = s.cum_return, b = s.benchmark_cum_return;
  if (a === null || a === undefined || b === null || b === undefined) {
    el.textContent = '—';
    // 必须同时清掉涨跌色:只改文字会留下一个染成红/绿的"—",
    // 看着像"有数据且在涨",实际是没数据
    el.className = 'hs-headline-val';
    sub.textContent = '等待首批结算';
    return;
  }
  const pp = (Number(a) - Number(b)) * 100;
  el.textContent = `${pp > 0 ? '+' : ''}${pp.toFixed(1)}pp`;
  el.className = signClass('hs-headline-val', pp);
  sub.textContent = `策略 ${fmtPct(a)}　基准 ${fmtPct(b)}`;
}

function renderWinRate(s) {
  const el = document.getElementById('hsWinRate');
  const bar = document.getElementById('hsWinBar');
  const fill = bar && bar.querySelector('.hs-winrate-fill');
  if (!s.total_count) {
    el.textContent = '暂无已结算数据';
    if (bar) bar.style.display = 'none';
    if (fill) fill.style.width = '0';   // 归零,别留上一次的宽度
    return;
  }
  const pct = s.win_rate * 100;
  el.textContent = `${pct.toFixed(1)}%（${s.win_count}/${s.total_count}）`;
  if (bar) bar.style.display = '';
  if (fill) fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  if (bar) bar.setAttribute('aria-label', `胜率 ${pct.toFixed(1)}%，${s.win_count} 胜 ${s.total_count - s.win_count} 负`);
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

// 板块级聚合:该板块已结算个股的平均涨跌。
// 原先三张卡片除了名字完全一样,得逐只读完才知道哪个板块拖了后腿。
function sectorAgg(stocks) {
  const done = stocks.filter(
    st => st.settle_status === 'settled' &&
          st.pct_change !== null && st.pct_change !== undefined
  );
  if (!done.length) {
    return '<span class="sector-agg na">待结算</span>';
  }
  const avg = done.reduce((s, st) => s + Number(st.pct_change), 0) / done.length;
  const cls = avg > 0 ? 'pos' : avg < 0 ? 'neg' : '';
  return `<span class="sector-agg ${cls}">${fmtPct(avg)}` +
         `<span style="font-weight:400;opacity:.7"> · ${done.length}只</span></span>`;
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
        ${sectorAgg(sector.stocks)}
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
  const live = document.getElementById('hsLive');
  try {
    const res = await fetch('/api/ai_hotsector/intraday');
    const rows = (await res.json()).intraday || [];
    applyIntraday(rows);
    // 「盘中」标记只在真有实时价回来时点亮 —— 收盘后/无持仓时挂着一个
    // 呼吸的红点,是在骗人说数据在动
    if (live) {
      live.hidden = !rows.some(
        r => r.realtime_price !== null && r.realtime_price !== undefined
      );
    }
    if (hint) {
      const now = new Date();
      const hh = String(now.getHours()).padStart(2, '0');
      const mm = String(now.getMinutes()).padStart(2, '0');
      const ss = String(now.getSeconds()).padStart(2, '0');
      hint.textContent = `盘中数据更新于 ${hh}:${mm}:${ss}`;
    }
  } catch (e) {
    console.error(e);
    if (live) live.hidden = true;
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

// ── 近期战绩条 ───────────────────────────────────────────────────────────────
// 数据与"历史批次"表同源,只是换个读法:20 根柱子把连亏/连赢压成一眼可见的形状。
// 柱高按当日收益的绝对值归一化,颜色按正负 —— 不引入任何表里没有的数字。
const STREAK_DAYS = 20;

function renderStreak(rows) {
  const wrap = document.getElementById('hsStreak');
  const hint = document.getElementById('hsStreakHint');
  const settled = rows
    .filter(r => r.status === 'settled' &&
                 r.day_return !== null && r.day_return !== undefined)
    .slice(0, STREAK_DAYS)
    .reverse();                       // 接口按日期倒序,画图要从早到晚

  if (!settled.length) {
    wrap.innerHTML = '<div class="no-data">暂无已结算交易日</div>';
    return;
  }

  const maxAbs = Math.max(...settled.map(r => Math.abs(Number(r.day_return)))) || 1;
  wrap.innerHTML = settled.map(r => {
    const v = Number(r.day_return);
    const h = Math.max(4, Math.round(Math.abs(v) / maxAbs * 100));
    const cls = v > 0 ? 'pos' : v < 0 ? 'neg' : '';
    const win = r.total_count ? `　${r.win_count}/${r.total_count} 胜` : '';
    return `<span class="hs-bar ${cls}" title="${esc(r.pick_date)}　${fmtPct(v)}${win}">` +
           `<i style="height:${h}%"></i></span>`;
  }).join('');

  if (hint) {
    const wins = settled.filter(r => Number(r.day_return) > 0).length;
    hint.textContent =
      `近 ${settled.length} 个已结算交易日 · ${wins} 天收正 / ${settled.length - wins} 天收负`;
  }
}

async function loadHistory() {
  const wrap = document.getElementById('hsHistoryWrap');
  try {
    const res = await fetch('/api/ai_hotsector/history?limit=60');
    const rows = (await res.json()).history || [];
    renderStreak(rows);
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
    document.getElementById('hsStreak').innerHTML =
      '<div class="no-data">加载失败</div>';
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
