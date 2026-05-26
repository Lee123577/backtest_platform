/* 小市值策略 · 实盘观察 — 简洁版前端 */

function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

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
const SCAN_REFRESH_MS = 60_000;       // 今日扫描状态 60s 拉一次
let _realtimeTimer = null;
let _scanTimer = null;
let _allRuns = [];  // 缓存全量历史，供前端过滤

// 全局权限状态：load() 时拉一次；admin=true 解锁所有写按钮
let _adminInfo = { ip: '—', is_admin: false, whitelist_empty: false };

async function load() {
  // 先拉权限，再并行加载其它数据 —— 让 UI 一开始就能正确禁用按钮
  await loadAdminStatus();
  await Promise.all([
    loadAccount(), loadEquity(), loadRuns(), loadTrades(),
    loadScanStatus(), loadParams(),
  ]);
  applyAdminGuards();
  // 启动持仓表的轮询刷新；equity / runs / trades 是历史快照，不用频繁刷
  if (_realtimeTimer) clearInterval(_realtimeTimer);
  _realtimeTimer = setInterval(loadAccount, REALTIME_REFRESH_MS);
  if (_scanTimer) clearInterval(_scanTimer);
  _scanTimer = setInterval(loadScanStatus, SCAN_REFRESH_MS);
  // 切到后台标签时停掉，回到前台再启动 —— 不浪费请求
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (_realtimeTimer) { clearInterval(_realtimeTimer); _realtimeTimer = null; }
      if (_scanTimer) { clearInterval(_scanTimer); _scanTimer = null; }
    } else {
      if (!_realtimeTimer) {
        loadAccount();
        _realtimeTimer = setInterval(loadAccount, REALTIME_REFRESH_MS);
      }
      if (!_scanTimer) {
        loadScanStatus();
        _scanTimer = setInterval(loadScanStatus, SCAN_REFRESH_MS);
      }
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
    document.getElementById('sumNextRebal').textContent = '—';
    document.getElementById('sumRebalSub').textContent = '';
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

  // ── 下次调仓日 ────────────────────────────────────────────────────────────
  const nrd = data.next_rebalance_date;
  const daysLeft = data.days_until_rebalance;
  const overdue = Number(data.rebalance_overdue_days || 0);
  const rebalCard = document.getElementById('sumRebalCard');
  const rebalVal  = document.getElementById('sumNextRebal');
  const rebalSub  = document.getElementById('sumRebalSub');
  rebalCard.classList.remove('rebal-today', 'rebal-soon', 'rebal-overdue');
  if (nrd) {
    rebalVal.textContent = nrd;
    if (overdue > 0) {
      // daily_signal 几天没跑，或者刚好碰上长假。给出逾期提示
      rebalSub.textContent = `已逾期 ${overdue} 个交易日，下次扫描会补仓`;
      rebalCard.classList.add('rebal-overdue');
    } else if (daysLeft === 0) {
      rebalSub.textContent = '今天调仓 🎯';
      rebalCard.classList.add('rebal-today');
    } else if (daysLeft === 1) {
      rebalSub.textContent = '明天调仓';
      rebalCard.classList.add('rebal-soon');
    } else {
      rebalSub.textContent = `还有 ${daysLeft} 个交易日`;
    }
  } else {
    rebalVal.textContent = '—';
    rebalSub.textContent = '';
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
    const res = await fetch('/api/paper_trading/runs?limit=120');
    const runs = await res.json();
    _allRuns = runs || [];
    renderRunsFiltered();
  } catch (e) {
    console.error(e);
  }
}

// 复选框切换：默认只看有交易的（调仓 / 止损 / 错误）
function renderRunsFiltered() {
  const hideHold = document.getElementById('hideHoldRuns')?.checked;
  const list = hideHold
    ? _allRuns.filter(r =>
        r.is_rebalance || r.stop_loss_count > 0 || r.status === 'error')
    : _allRuns;
  renderRuns(list);
}
window.renderRunsFiltered = renderRunsFiltered;

function renderRuns(runs) {
  const wrap = document.getElementById('runsWrap');
  const total = _allRuns.length;
  const shown = runs.length;
  document.getElementById('runsHint').textContent =
    total ? `（显示 ${shown}/${total} 条，点击查看详情）` : '';

  if (!runs.length) {
    wrap.innerHTML = '<div class="no-data">无符合条件的记录</div>';
    return;
  }
  // 把 notes_struct（结构化）或 notes 字符串渲染为带 badge 的 HTML
  function renderNotes(r) {
    if (r.status === 'error') {
      return `<span style="color:#cf222e">${escapeHtml(r.error_msg || '')}</span>`;
    }
    // 优先用结构化字段（新版 runner 写入）
    let ns = r.notes_struct;
    if (typeof ns === 'string') {
      try { ns = JSON.parse(ns); } catch { ns = null; }
    }
    if (ns && typeof ns === 'object') {
      const parts = [];
      if (ns.sell && ns.sell.length)
        parts.push(`<span class="note-sell">▼卖出</span> <span class="note-codes">${ns.sell.join(',')}</span>`);
      if (ns.buy && ns.buy.length)
        parts.push(`<span class="note-buy">▲买入</span> <span class="note-codes">${ns.buy.join(',')}</span>`);
      if (ns.stop_loss && ns.stop_loss.length)
        parts.push(`<span class="tag-stop" style="font-size:11px">⚠止损</span> <span class="note-codes">${ns.stop_loss.join(',')}</span>`);
      if (ns.reason) parts.push(`<span style="color:#57606a">${escapeHtml(ns.reason)}</span>`);
      return parts.join('　') || '<span style="color:#57606a">持有日，无交易</span>';
    }
    // 兜底：旧字符串 + 正则上色（兼容历史数据）
    const raw = r.notes || '';
    return raw
      .replace(/买入\s+([^；]+)/g, (_, c) =>
        `<span class="note-buy">▲买入</span> <span class="note-codes">${c.trim()}</span>`)
      .replace(/卖出\s+([^；]+)/g, (_, c) =>
        `<span class="note-sell">▼卖出</span> <span class="note-codes">${c.trim()}</span>`)
      .replace(/止损\s+([^；]+)/g, (_, c) =>
        `<span class="tag-stop" style="font-size:11px">⚠止损</span> <span class="note-codes">${c.trim()}</span>`)
      .replace(/；/g, '　');
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
        <td style="font-size:12px;max-width:300px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
          ${renderNotes(r)}
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

// ── 成交流水（仅真实买卖，配有实现盈亏）────────────────────────────────────

async function loadTrades() {
  try {
    const res = await fetch('/api/paper_trading/trades?limit=300');
    const { trades } = await res.json();
    renderTrades(trades || []);
  } catch (e) {
    console.error(e);
    document.getElementById('tradesWrap').innerHTML =
      `<div class="no-data">加载失败：${e.message || e}</div>`;
  }
}

function renderTrades(trades) {
  const wrap = document.getElementById('tradesWrap');
  const hint = document.getElementById('tradesHint');

  if (!trades.length) {
    wrap.innerHTML = '<div class="no-data">暂无成交记录</div>';
    if (hint) hint.textContent = '每一笔买入 / 卖出 / 止损卖出';
    return;
  }

  // 统计：胜负次数 / 累计已实现盈亏
  let wins = 0, losses = 0, realized = 0;
  trades.forEach(t => {
    if (t.pnl !== null && t.pnl !== undefined) {
      realized += Number(t.pnl);
      if (t.pnl > 0) wins++;
      else if (t.pnl < 0) losses++;
    }
  });
  const winRate = (wins + losses) > 0
    ? ((wins / (wins + losses)) * 100).toFixed(1) + '%'
    : '—';
  const realizedCls = realized >= 0 ? 'val-pos' : 'val-neg';
  if (hint) {
    hint.innerHTML = `共 ${trades.length} 笔　|　胜/负 ${wins}/${losses}　|　胜率 ${winRate}　|　已实现盈亏 <span class="${realizedCls}">¥${fmtMoney(realized)}</span>`;
  }

  const rows = trades.map(t => {
    let tag;
    if (t.action === '买入') tag = '<span class="badge-buy">买入</span>';
    else if (t.action === '止损卖出') tag = '<span class="tag-stop">止损卖出</span>';
    else tag = '<span class="badge-sell">卖出</span>';

    const isSell = t.action !== '买入';

    // 买入价 + 持有天数（仅卖出行）
    let buyInfoCell = '<span class="trade-pnl-empty">—</span>';
    if (isSell && t.buy_price != null) {
      const holdStr = t.hold_days != null ? `<span style="color:#57606a;font-size:11px">（持${t.hold_days}天）</span>` : '';
      buyInfoCell = `${fmtPrice(t.buy_price)}${holdStr}`;
    }

    // 手续费
    let commCell = '<span class="trade-pnl-empty">—</span>';
    if (t.commission != null) {
      commCell = `<span style="color:#57606a">¥${fmtMoney(t.commission)}</span>`;
    }

    // 实现盈亏 & 收益率（仅卖出行）
    let pnlCell = '<span class="trade-pnl-empty">—</span>';
    let pnlPctCell = '<span class="trade-pnl-empty">—</span>';
    if (isSell && t.pnl != null) {
      const cls = t.pnl >= 0 ? 'trade-pnl-pos' : 'trade-pnl-neg';
      const sign = t.pnl >= 0 ? '+' : '';
      pnlCell = `<span class="${cls}">${sign}¥${fmtMoney(t.pnl)}</span>`;
      if (t.pnl_pct != null) {
        pnlPctCell = `<span class="${cls}">${fmtPct(t.pnl_pct)}</span>`;
      }
    }

    return `
      <tr class="row-link" onclick="showRunDetail(${t.run_id})">
        <td>${t.run_date}</td>
        <td>${tag}</td>
        <td class="code-cell">${t.code}</td>
        <td>${t.name || ''}</td>
        <td>${fmtPrice(t.price)}</td>
        <td>${t.shares || '—'}</td>
        <td>¥${fmtMoney(t.amount)}</td>
        <td>${buyInfoCell}</td>
        <td>${commCell}</td>
        <td>${pnlCell}</td>
        <td>${pnlPctCell}</td>
      </tr>`;
  }).join('');

  wrap.innerHTML = `
    <table class="pt-tbl">
      <thead><tr>
        <th>日期</th><th>动作</th><th>代码</th><th>名称</th>
        <th>成交价</th><th>股数</th><th>金额</th>
        <th>买入价</th><th>手续费</th><th>实现盈亏</th><th>收益率</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ── 今日扫描状态 ────────────────────────────────────────────────────────────

async function loadScanStatus() {
  try {
    const res = await fetch('/api/tasks/summary');
    const { tasks } = await res.json();
    const t = (tasks || []).find(x => x.task_name === 'daily_signal');
    renderScanStatus(t);
  } catch (e) {
    console.error(e);
    document.getElementById('scanChip').textContent = '状态未知';
    document.getElementById('scanChip').className = 'scan-chip scan-chip-loading';
  }
}

function renderScanStatus(t) {
  const chip = document.getElementById('scanChip');
  const btn = document.getElementById('scanBtn');
  const hint = document.getElementById('scanHint');

  if (!t) {
    chip.textContent = '未注册';
    chip.className = 'scan-chip scan-chip-loading';
    btn.style.display = 'none';
    hint.textContent = '';
    return;
  }

  // 周末 → 非交易日
  const now = new Date();
  const dow = now.getDay();      // 0=日 6=六
  const isWeekend = dow === 0 || dow === 6;
  // 今天 17:30 阈值
  const due = new Date(now); due.setHours(17, 30, 0, 0);
  const beforeDue = now < due;

  btn.style.display = 'none';
  hint.textContent = '';

  if (t.ran_today_success) {
    chip.textContent = '✓ 今日已完成';
    chip.className = 'scan-chip scan-chip-success';
    hint.textContent = t.last_started_at ? `运行于 ${shortTime(t.last_started_at)}` : '';
  } else if (isWeekend) {
    chip.textContent = '· 非交易日';
    chip.className = 'scan-chip scan-chip-weekend';
    hint.textContent = '周末不扫描';
  } else if (beforeDue) {
    chip.textContent = '⏱ 等待 17:30 触发';
    chip.className = 'scan-chip scan-chip-pending';
    btn.style.display = '';   // 允许提前手动跑
    btn.textContent = '立即扫描';
    btn.disabled = false;
  } else if (t.last_status === 'failed' || t.last_status === 'timeout') {
    chip.textContent = `✗ ${t.last_status === 'timeout' ? '超时' : '失败'}`;
    chip.className = 'scan-chip scan-chip-failed';
    hint.textContent = (t.last_error_msg || '').slice(0, 80) || '点击右侧按钮重试';
    btn.style.display = '';
    btn.textContent = '重新扫描';
    btn.disabled = false;
  } else if (t.last_status === 'skipped') {
    chip.textContent = '⊘ 已跳过';
    chip.className = 'scan-chip scan-chip-skipped';
    hint.textContent = (t.last_error_msg || '').slice(0, 80) || '依赖未满足';
    btn.style.display = '';
    btn.textContent = '重试';
    btn.disabled = false;
  } else {
    chip.textContent = '? 待执行';
    chip.className = 'scan-chip scan-chip-pending';
    btn.style.display = '';
    btn.textContent = '立即扫描';
    btn.disabled = false;
  }

  // 末尾再叠一次 admin 守门：admin=false 强制锁住，不管 scan 流程怎么说
  applyAdminGuards();
}

async function triggerScan() {
  const btn = document.getElementById('scanBtn');
  const hint = document.getElementById('scanHint');
  btn.disabled = true;
  btn.textContent = '触发中…';
  try {
    // 用 adminFetch：403 抛 Error，避免误显示"已加入队列"假象
    const res = await adminFetch('/api/tasks/daily_signal/run', { method: 'POST' });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.detail || '触发失败');
    }
    const data = await res.json();
    if (data.status === 'skipped') {
      hint.textContent = `跳过：${data.reason || ''}`;
      btn.disabled = false;
      btn.textContent = '重试';
      return;
    }
    hint.textContent = '已加入队列，约 30s 后刷新看结果';
    // 几秒后拉一次摘要 + 成交流水
    setTimeout(async () => {
      await Promise.all([loadScanStatus(), loadAccount(), loadRuns(), loadTrades()]);
    }, 8000);
  } catch (e) {
    hint.textContent = `触发失败：${e.message || e}`;
    btn.disabled = false;
    btn.textContent = '立即扫描';
  }
}

function shortTime(iso) {
  try {
    const d = new Date(iso);
    return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  } catch { return iso; }
}

window.triggerScan = triggerScan;

// ── 策略参数：读取 + 编辑 + 保存 ──────────────────────────────────────────────

let _currentParams = null;  // 缓存当前服务端值，用于"取消"还原

async function loadParams() {
  try {
    const res = await fetch('/api/paper_trading/strategy_params');
    const data = await res.json();
    _currentParams = data.params;
    renderParamsBadge(data.params);
    fillParamsForm(data.params);
  } catch (e) {
    console.error(e);
  }
}

function renderParamsBadge(p) {
  // 顶部 tip-badge：把核心参数浓缩成一行，让用户随时知道当前用的是什么配置
  const badge = document.getElementById('paramsBadge');
  if (!badge || !p) return;
  const boards = (p.allow_boards || []).map(b => ({
    main: '主板', gem: '创业板', star: '科创板', bj: '北交所',
  }[b] || b)).join('+');
  const stop = Number(p.stop_loss_pct) > 0
    ? `止损 ${p.stop_loss_pct}%` : '止损关闭';
  badge.textContent =
    `${boards} · 市值 ${p.cap_min}~${p.cap_max}亿 · ${p.stock_num}只 · ${p.hold_days}天调仓 · ${stop}`;
}

function fillParamsForm(p) {
  if (!p) return;
  document.getElementById('pCapMin').value = p.cap_min;
  document.getElementById('pCapMax').value = p.cap_max;
  document.getElementById('pStockNum').value = p.stock_num;
  document.getElementById('pHoldDays').value = p.hold_days;
  document.getElementById('pStopLoss').value = p.stop_loss_pct;
  const boards = new Set(p.allow_boards || []);
  document.getElementById('pBoardMain').checked = boards.has('main');
  document.getElementById('pBoardGem').checked  = boards.has('gem');
  document.getElementById('pBoardStar').checked = boards.has('star');
  document.getElementById('pBoardBj').checked   = boards.has('bj');
}

function toggleParamsEditor() {
  const ed = document.getElementById('paramsEditor');
  const btn = document.getElementById('paramsToggleBtn');
  if (ed.style.display === 'none') {
    ed.style.display = '';
    btn.classList.add('open');
    btn.textContent = '⚙ 收起';
    document.getElementById('paramsMsg').textContent = '';
    document.getElementById('paramsMsg').className = 'params-msg';
    // 首次打开时立即拉一次预览
    refreshUniversePreview();
    // 输入变化时 debounce 重拉
    bindPreviewListeners();
  } else {
    ed.style.display = 'none';
    btn.classList.remove('open');
    btn.textContent = '⚙ 调整策略参数';
  }
}

// ── 市值范围实时预览（避免"保存后才发现没股票"）────────────────────────────

let _previewTimer = null;
let _previewBound = false;

function bindPreviewListeners() {
  if (_previewBound) return;
  ['pCapMin', 'pCapMax'].forEach(id => {
    document.getElementById(id).addEventListener('input', () => {
      if (_previewTimer) clearTimeout(_previewTimer);
      _previewTimer = setTimeout(refreshUniversePreview, 400);
    });
  });
  _previewBound = true;
}

async function refreshUniversePreview() {
  const box = document.getElementById('universePreview');
  const lo = Number(document.getElementById('pCapMin').value);
  const hi = Number(document.getElementById('pCapMax').value);

  if (!isFinite(lo) || !isFinite(hi) || lo < 0 || hi <= 0) {
    box.innerHTML = '<span class="preview-loading">请输入有效市值范围</span>';
    return;
  }
  if (lo >= hi) {
    box.innerHTML = '<span class="preview-count empty">⚠ 市值下限必须小于上限</span>';
    return;
  }

  box.innerHTML = '<span class="preview-loading">查询中…</span>';
  try {
    const res = await fetch(
      `/api/paper_trading/universe_preview?cap_min=${lo}&cap_max=${hi}`
    );
    const s = await res.json();
    renderUniversePreview(s, lo, hi);
  } catch (e) {
    box.innerHTML = `<span class="preview-count empty">预览失败：${e.message || e}</span>`;
  }
}

function renderUniversePreview(s, lo, hi) {
  const box = document.getElementById('universePreview');
  if (!s || !s.total) {
    box.innerHTML = `<span class="preview-count empty">⚠ 无法获取市值数据</span>
                     <span class="preview-suggest">${s?.source || '请检查 daily_update 任务'}</span>`;
    return;
  }
  const d = s.distribution;
  const n = s.in_range ?? 0;
  const cls = n > 0 ? 'ok' : 'empty';
  const icon = n > 0 ? '✓' : '⚠';

  let suggest = '';
  if (n === 0 && d) {
    const sLo = Math.max(1, Math.round(d.p25));
    const sHi = Math.max(sLo + 5, Math.round(d.p50));
    suggest = `<span class="preview-suggest">建议改成 ${sLo}~${sHi} 亿元附近</span>`;
  }

  let sampleLine = '';
  if (n > 0 && s.sample && s.sample.length) {
    const head = s.sample.slice(0, 5)
      .map(x => `${x.code}(${x.market_cap}亿)`).join(' · ');
    sampleLine = `<span class="preview-sample">示例: ${head}${n > 5 ? ' …' : ''}</span>`;
  }

  const distLine = d
    ? `<span class="preview-dist">全市场 ${s.total} 只 · P25=${d.p25}亿 / 中位=${d.p50}亿 / P75=${d.p75}亿</span>`
    : '';

  box.innerHTML =
    `<span class="preview-count ${cls}">${icon} ${lo}~${hi} 亿元内 ${n} 只股票</span>${distLine}${suggest}${sampleLine}`;
}

function cancelParamsEdit() {
  if (_currentParams) fillParamsForm(_currentParams);
  toggleParamsEditor();
}

async function saveParams() {
  const msg = document.getElementById('paramsMsg');
  const btn = document.getElementById('paramsSaveBtn');
  msg.textContent = '';
  msg.className = 'params-msg';

  const boards = [];
  if (document.getElementById('pBoardMain').checked) boards.push('main');
  if (document.getElementById('pBoardGem').checked)  boards.push('gem');
  if (document.getElementById('pBoardStar').checked) boards.push('star');
  if (document.getElementById('pBoardBj').checked)   boards.push('bj');
  if (boards.length === 0) {
    msg.textContent = '至少选择一个板块';
    msg.className = 'params-msg error';
    return;
  }

  const payload = {
    cap_min: Number(document.getElementById('pCapMin').value),
    cap_max: Number(document.getElementById('pCapMax').value),
    stock_num: parseInt(document.getElementById('pStockNum').value, 10),
    hold_days: parseInt(document.getElementById('pHoldDays').value, 10),
    stop_loss_pct: Number(document.getElementById('pStopLoss').value),
    allow_boards: boards,
  };

  // 前端基础校验：避免一次明显错误也要往后跑
  if (payload.cap_min >= payload.cap_max) {
    msg.textContent = '市值下限必须小于上限';
    msg.className = 'params-msg error';
    return;
  }

  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    const res = await fetch('/api/paper_trading/strategy_params', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      msg.textContent = data.detail || '保存失败';
      msg.className = 'params-msg error';
      return;
    }
    _currentParams = data.params;
    renderParamsBadge(data.params);
    msg.innerHTML = '✓ 已保存。下次扫描生效，或点 <a href="#" onclick="triggerScan();return false;" style="color:#0969da;">立即扫描</a>';
    msg.className = 'params-msg ok';
  } catch (e) {
    msg.textContent = '保存失败：' + (e.message || e);
    msg.className = 'params-msg error';
  } finally {
    btn.disabled = false;
    btn.textContent = '保存';
  }
}

window.toggleParamsEditor = toggleParamsEditor;
window.cancelParamsEdit   = cancelParamsEdit;
window.saveParams         = saveParams;

// ── 管理员 IP 白名单：权限检测 + 控件禁用 + 增删管理 ─────────────────────────

async function loadAdminStatus() {
  try {
    const res = await fetch('/api/admin/ip/me');
    _adminInfo = await res.json();
  } catch (e) {
    console.error('admin status:', e);
    _adminInfo = { ip: '—', is_admin: false, whitelist_empty: false };
  }
  renderAdminChip();
}

function renderAdminChip() {
  const chip = document.getElementById('adminChip');
  const mgmtBtn = document.getElementById('adminMgmtBtn');
  if (!chip) return;
  if (_adminInfo.is_admin) {
    chip.textContent = `✓ 管理员 (${_adminInfo.ip})`;
    chip.className = 'admin-chip admin-chip-admin';
    chip.title = '你的 IP 在白名单中，可以编辑参数 / 触发任务';
    if (mgmtBtn) mgmtBtn.style.display = '';
  } else {
    chip.textContent = `○ 只读 (${_adminInfo.ip})`;
    chip.className = 'admin-chip admin-chip-guest';
    chip.title = _adminInfo.whitelist_empty
      ? '白名单为空。首次触发任何写操作的 IP 将被自动加入。'
      : '你的 IP 不在白名单中，仅可浏览。需要修改请联系管理员添加 IP。';
    if (mgmtBtn) mgmtBtn.style.display = 'none';
  }
}

// 把所有写控件禁用 / 启用 —— admin=true 解锁，false 锁住并加 title 提示
function applyAdminGuards() {
  const lockTip = '你的 IP 不在白名单中，无法操作。请联系管理员将你的 IP 加入白名单。';
  const targets = [
    document.getElementById('paramsToggleBtn'),
    document.getElementById('scanBtn'),
  ];
  for (const el of targets) {
    if (!el) continue;
    if (_adminInfo.is_admin) {
      el.disabled = false;
      el.removeAttribute('data-locked');
      el.title = '';
    } else {
      el.disabled = true;
      el.setAttribute('data-locked', '1');
      el.title = lockTip;
    }
  }
}

// 包装 fetch，对 403 做统一处理（防止后端守门时前端还报谜之错）
async function adminFetch(url, options) {
  const res = await fetch(url, options);
  if (res.status === 403) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || '权限不足（IP 不在白名单）');
  }
  return res;
}

// ── 管理弹窗 ────────────────────────────────────────────────────────────────

async function openAdminModal() {
  if (!_adminInfo.is_admin) return;
  document.getElementById('adminModal').style.display = '';
  document.getElementById('adminCurrent').textContent = `当前 IP：${_adminInfo.ip}（管理员）`;
  document.getElementById('adminMsg').textContent = '';
  document.getElementById('adminMsg').className = 'admin-msg';
  document.getElementById('adminAddIp').value = '';
  document.getElementById('adminAddNote').value = '';
  await refreshAdminList();
}

function closeAdminModal() {
  document.getElementById('adminModal').style.display = 'none';
}

async function refreshAdminList() {
  const wrap = document.getElementById('adminList');
  wrap.innerHTML = '加载中…';
  try {
    const res = await adminFetch('/api/admin/ip');
    const data = await res.json();
    renderAdminList(data);
  } catch (e) {
    wrap.innerHTML = `<div class="admin-msg error">加载失败：${e.message || e}</div>`;
  }
}

function renderAdminList(data) {
  const wrap = document.getElementById('adminList');
  const ips = data.ips || [];
  if (!ips.length) {
    wrap.innerHTML = '<div class="no-data">白名单为空</div>';
    return;
  }
  const onlyOne = ips.length <= 1;
  wrap.innerHTML = ips.map(r => {
    const isMe = r.ip === data.current_ip;
    const cantDelete = isMe && onlyOne;
    return `
      <div class="admin-list-row ${isMe ? 'is-me' : ''}">
        <span class="ip">${r.ip}${isMe ? ' (本机)' : ''}</span>
        <span class="note">${r.note || '—'}</span>
        <span class="meta">${r.created_by_ip || '—'} · ${(r.created_at || '').slice(0,16).replace('T',' ')}</span>
        <button class="del-btn" onclick="deleteAdminIp('${r.ip}')"
                ${cantDelete ? 'disabled title="不能删除最后一个白名单 IP"' : ''}>
          删除
        </button>
      </div>`;
  }).join('');
}

async function addAdminIp() {
  const ip = document.getElementById('adminAddIp').value.trim();
  const note = document.getElementById('adminAddNote').value.trim();
  const msg = document.getElementById('adminMsg');
  const btn = document.getElementById('adminAddBtn');
  msg.textContent = ''; msg.className = 'admin-msg';
  if (!ip) {
    msg.textContent = '请输入 IP'; msg.className = 'admin-msg error';
    return;
  }
  btn.disabled = true; btn.textContent = '添加中…';
  try {
    const res = await adminFetch('/api/admin/ip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip, note }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.detail || '添加失败');
    }
    msg.textContent = `✓ 已添加 ${ip}`; msg.className = 'admin-msg ok';
    document.getElementById('adminAddIp').value = '';
    document.getElementById('adminAddNote').value = '';
    await refreshAdminList();
  } catch (e) {
    msg.textContent = `失败：${e.message || e}`; msg.className = 'admin-msg error';
  } finally {
    btn.disabled = false; btn.textContent = '添加';
  }
}

async function deleteAdminIp(ip) {
  if (!confirm(`确定要从白名单删除 ${ip} 吗？删除后该 IP 将无法编辑参数 / 触发任务。`)) return;
  const msg = document.getElementById('adminMsg');
  msg.textContent = ''; msg.className = 'admin-msg';
  try {
    const res = await adminFetch(`/api/admin/ip/${encodeURIComponent(ip)}`, {
      method: 'DELETE',
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.detail || '删除失败');
    }
    msg.textContent = `✓ 已删除 ${ip}`; msg.className = 'admin-msg ok';
    await refreshAdminList();
    // 如果删的是自己，重新拉一次权限
    if (ip === _adminInfo.ip) {
      await loadAdminStatus();
      applyAdminGuards();
      closeAdminModal();
    }
  } catch (e) {
    msg.textContent = `失败：${e.message || e}`; msg.className = 'admin-msg error';
  }
}

window.openAdminModal  = openAdminModal;
window.closeAdminModal = closeAdminModal;
window.addAdminIp      = addAdminIp;
window.deleteAdminIp   = deleteAdminIp;

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
