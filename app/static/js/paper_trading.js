/* 小市值策略 · 实盘观察 — 简洁版前端 */

const fmtPct = (v) => {
  if (v === null || v === undefined || isNaN(v)) return '—';
  const x = Number(v) * 100;
  const sign = x >= 0 ? '+' : '';
  return `${sign}${x.toFixed(2)}%`;
};
const fmtMoney = (v) => {
  if (v === null || v === undefined || isNaN(v)) return '—';
  return Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
};
const fmtPrice = (v) => {
  if (v === null || v === undefined || isNaN(v)) return '—';
  return Number(v).toFixed(3);
};

let equityChart;
const REALTIME_REFRESH_MS = 30_000;  // 持仓/账户 30s 拉一次实时价
let _realtimeTimer = null;

async function load() {
  await Promise.all([loadAccount(), loadEquity(), loadRuns()]);
  // 启动持仓表的轮询刷新；equity / runs 是历史快照，不用刷
  if (_realtimeTimer) clearInterval(_realtimeTimer);
  _realtimeTimer = setInterval(loadAccount, REALTIME_REFRESH_MS);
  // 切到后台标签时停掉，回到前台再启动 —— 不浪费请求
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (_realtimeTimer) { clearInterval(_realtimeTimer); _realtimeTimer = null; }
    } else if (!_realtimeTimer) {
      loadAccount();
      _realtimeTimer = setInterval(loadAccount, REALTIME_REFRESH_MS);
    }
  });
}

// ── 账户摘要 + 当前持仓 ─────────────────────────────────────────────────────

async function loadAccount() {
  try {
    const res = await fetch('/api/paper_trading/account');
    const data = await res.json();
    renderSummary(data);
    renderHoldings(data.holdings || []);
    _setUpdatedAt(data.realtime_count || 0);
  } catch (e) {
    console.error(e);
  }
}

function _setUpdatedAt(realtimeCount) {
  const el = document.getElementById('sumUpdatedAt');
  if (!el) return;
  const ts = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  el.textContent = realtimeCount > 0
    ? `更新于 ${ts}（实时 ${realtimeCount} 只）`
    : `更新于 ${ts}（行情未取到，显示数据库收盘价）`;
}

function renderSummary(data) {
  const acc = data.account;
  const latest = data.latest_run;
  const banner = document.getElementById('emptyBanner');

  if (!acc) {
    banner.style.display = '';
    document.getElementById('sumCapital').textContent = '—';
    document.getElementById('sumTotal').textContent = '—';
    document.getElementById('sumCumRet').textContent = '—';
    document.getElementById('sumPos').textContent = '—';
    document.getElementById('sumCash').textContent = '—';
    document.getElementById('sumLastRun').textContent = '—';
    return;
  }
  banner.style.display = 'none';

  document.getElementById('sumCapital').textContent = `¥${fmtMoney(acc.initial_capital)}`;
  document.getElementById('sumCash').textContent = `¥${fmtMoney(acc.cash)}`;

  if (latest) {
    document.getElementById('sumTotal').textContent = `¥${fmtMoney(latest.total_value)}`;
    document.getElementById('sumPos').textContent = `¥${fmtMoney(latest.position_value)}`;
    const cr = latest.cum_return;
    const el = document.getElementById('sumCumRet');
    el.textContent = fmtPct(cr);
    el.className = 'sum-val ' + (Number(cr) >= 0 ? 'pos' : 'neg');
    document.getElementById('sumLastRun').textContent = latest.run_date || '—';
  } else {
    document.getElementById('sumTotal').textContent = `¥${fmtMoney(acc.cash)}`;
    document.getElementById('sumCumRet').textContent = '0.00%';
    document.getElementById('sumPos').textContent = '¥0.00';
    document.getElementById('sumLastRun').textContent = '尚未运行';
  }
}

function renderHoldings(holdings) {
  const wrap = document.getElementById('holdingsWrap');
  if (!holdings.length) {
    wrap.innerHTML = '<div class="no-data">当前无持仓</div>';
    return;
  }
  const rows = holdings.map(h => {
    const pnlCls = h.pnl >= 0 ? 'val-pos' : 'val-neg';
    const liveBadge = h.is_realtime
      ? ' <span style="font-size:10px;color:#0a7d33;background:#e6f4ea;padding:1px 5px;border-radius:3px;">实时</span>'
      : ' <span style="font-size:10px;color:#9a6700;background:#fff8c5;padding:1px 5px;border-radius:3px;">收盘</span>';
    return `
      <tr>
        <td class="code-cell">${h.code}</td>
        <td>${h.name || ''}</td>
        <td>${h.shares}</td>
        <td>${fmtPrice(h.buy_price)}</td>
        <td>${fmtPrice(h.last_close)}${liveBadge}</td>
        <td>¥${fmtMoney(h.market_value)}</td>
        <td>¥${fmtMoney(h.cost)}</td>
        <td class="${pnlCls}">¥${fmtMoney(h.pnl)}</td>
        <td class="${pnlCls}">${fmtPct(h.pnl_pct / 100)}</td>
        <td>${h.buy_date || ''}</td>
      </tr>`;
  }).join('');
  wrap.innerHTML = `
    <table class="pt-tbl">
      <thead><tr>
        <th>代码</th><th>名称</th><th>持股</th><th>买入价</th><th>最新价</th>
        <th>市值</th><th>成本</th><th>浮盈</th><th>浮盈率</th><th>买入日</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ── 收益曲线 ─────────────────────────────────────────────────────────────────

async function loadEquity() {
  try {
    const res = await fetch('/api/paper_trading/equity');
    const { data } = await res.json();
    renderEquityChart(data || []);
  } catch (e) {
    console.error(e);
  }
}

function renderEquityChart(data) {
  if (!equityChart) {
    equityChart = echarts.init(document.getElementById('equityChart'));
    window.addEventListener('resize', () => equityChart && equityChart.resize());
  }
  if (!data.length) {
    equityChart.clear();
    equityChart.setOption({
      title: { text: '暂无数据', left: 'center', top: 'center',
               textStyle: { color: '#57606a', fontSize: 13 } },
    });
    return;
  }

  const dates = data.map(d => d.trade_date);
  const strategyCum = data.map(d => d.cum_return !== null ? Number(d.cum_return) * 100 : null);
  const bmCum = data.map(d => d.benchmark_cum_return !== null ? Number(d.benchmark_cum_return) * 100 : null);

  equityChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' },
               valueFormatter: (v) => v == null ? '—' : `${v.toFixed(2)}%` },
    legend: { data: ['小市值策略', '上证综指'], top: 8 },
    grid: { left: 60, right: 30, top: 50, bottom: 50 },
    xAxis: { type: 'category', data: dates, boundaryGap: false,
             axisLabel: { color: '#57606a', fontSize: 11 } },
    yAxis: { type: 'value', name: '累计收益(%)',
             nameTextStyle: { color: '#57606a', fontSize: 11 },
             axisLabel: { color: '#57606a', fontSize: 11,
                          formatter: (v) => `${v.toFixed(1)}%` } },
    series: [
      { name: '小市值策略', type: 'line', data: strategyCum, smooth: true,
        lineStyle: { width: 2, color: '#0969da' },
        itemStyle: { color: '#0969da' },
        areaStyle: { color: 'rgba(9,105,218,0.10)' },
        symbol: 'none' },
      { name: '上证综指', type: 'line', data: bmCum, smooth: true,
        lineStyle: { width: 2, color: '#9a6700', type: 'dashed' },
        itemStyle: { color: '#9a6700' },
        symbol: 'none' },
    ],
  });
}

// ── 历史运行记录 ─────────────────────────────────────────────────────────────

async function loadRuns() {
  try {
    const res = await fetch('/api/paper_trading/runs?limit=60');
    const runs = await res.json();
    renderRuns(runs || []);
  } catch (e) {
    console.error(e);
  }
}

function renderRuns(runs) {
  const wrap = document.getElementById('runsWrap');
  document.getElementById('runsHint').textContent =
    runs.length ? `（共 ${runs.length} 条，点击查看详情）` : '';

  if (!runs.length) {
    wrap.innerHTML = '<div class="no-data">暂无记录</div>';
    return;
  }
  const rows = runs.map(r => {
    const tag = r.status === 'error'
      ? '<span class="tag-error">错误</span>'
      : (r.is_rebalance ? '<span class="tag-rebal">调仓</span>'
                        : '<span class="tag-hold">持有</span>');
    const stopTag = r.stop_loss_count > 0
      ? ` <span class="tag-stop">止损×${r.stop_loss_count}</span>`
      : '';
    const cr = r.cum_return;
    const crCls = cr === null || cr === undefined ? '' :
                  (Number(cr) >= 0 ? 'val-pos' : 'val-neg');
    return `
      <tr class="row-link" onclick="showRunDetail(${r.run_id})">
        <td>${r.run_date}</td>
        <td>${tag}${stopTag}</td>
        <td>${r.universe_size ?? '—'}</td>
        <td>${r.selected_count ?? '—'}</td>
        <td>¥${fmtMoney(r.total_value)}</td>
        <td>¥${fmtMoney(r.cash)}</td>
        <td class="${crCls}">${fmtPct(cr)}</td>
        <td style="color:#57606a;font-size:12px;max-width:280px;overflow:hidden;text-overflow:ellipsis;">
          ${r.notes || r.error_msg || ''}
        </td>
      </tr>`;
  }).join('');
  wrap.innerHTML = `
    <table class="pt-tbl">
      <thead><tr>
        <th>日期</th><th>状态</th><th>候选数</th><th>持仓数</th>
        <th>总值</th><th>现金</th><th>累计收益</th><th>备注</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ── 运行详情弹窗 ─────────────────────────────────────────────────────────────

async function showRunDetail(runId) {
  document.getElementById('runDetailModal').style.display = '';
  document.getElementById('modalContent').innerHTML = '加载中…';
  try {
    const res = await fetch(`/api/paper_trading/run/${runId}`);
    if (!res.ok) throw new Error(await res.text());
    const r = await res.json();
    renderRunDetail(r);
  } catch (e) {
    document.getElementById('modalContent').innerHTML =
      `<div class="error-box">加载失败：${e.message || e}</div>`;
  }
}

function closeRunDetail() {
  document.getElementById('runDetailModal').style.display = 'none';
}

function renderRunDetail(r) {
  document.getElementById('modalTitle').textContent =
    `${r.run_date} 运行详情`;

  const kv = [
    ['状态', r.status === 'error' ? '错误' : (r.is_rebalance ? '调仓日' : '持有日')],
    ['候选池', r.universe_size ?? '—'],
    ['持仓数', r.selected_count ?? '—'],
    ['止损数', r.stop_loss_count ?? 0],
    ['总值', `¥${fmtMoney(r.total_value)}`],
    ['现金', `¥${fmtMoney(r.cash)}`],
    ['持仓市值', `¥${fmtMoney(r.position_value)}`],
    ['累计收益', fmtPct(r.cum_return)],
  ];
  const kvHtml = kv.map(([k, v]) => `
    <div class="kv-item">
      <div class="kv-label">${k}</div><div class="kv-val">${v}</div>
    </div>`).join('');

  const notes = r.notes
    ? `<div class="notes-line">📝 ${r.notes}</div>` : '';
  const err = r.error_msg
    ? `<div class="error-box" style="margin-bottom:12px">${r.error_msg}</div>` : '';

  let positionsHtml = '';
  if (r.positions && r.positions.length) {
    const trs = r.positions.map(p => {
      const tag = p.action === '买入' ? '<span class="badge-buy">买入</span>'
                : p.action === '卖出' ? '<span class="badge-sell">卖出</span>'
                : p.action === '止损卖出' ? '<span class="tag-stop">止损</span>'
                : `<span class="tag-hold">${p.action || ''}</span>`;
      return `
        <tr>
          <td>${tag}</td>
          <td class="code-cell">${p.code}</td>
          <td>${p.name || ''}</td>
          <td>${p.market_cap ? Number(p.market_cap).toFixed(2) + '亿' : '—'}</td>
          <td>${fmtPrice(p.price)}</td>
          <td>${p.shares || '—'}</td>
          <td>¥${fmtMoney(p.amount)}</td>
        </tr>`;
    }).join('');
    positionsHtml = `
      <table class="pt-tbl" style="font-size:12px">
        <thead><tr>
          <th>动作</th><th>代码</th><th>名称</th><th>市值</th>
          <th>价格</th><th>股数</th><th>金额</th>
        </tr></thead>
        <tbody>${trs}</tbody>
      </table>`;
  } else {
    positionsHtml = '<div class="no-data">本次运行无交易/持仓记录</div>';
  }

  document.getElementById('modalContent').innerHTML = `
    <div class="kv-grid">${kvHtml}</div>
    ${notes}${err}
    ${positionsHtml}
  `;
}

window.showRunDetail = showRunDetail;
window.closeRunDetail = closeRunDetail;

load();
