'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let allStrategies = [];      // all strategies from /api/strategies
let signalStrategies = [];   // strategy_type === "signal"
let portfolioStrategies = []; // strategy_type === "portfolio"

let instances = [];          // selected single-stock strategy instances
let selectedPortfolio = null; // currently selected portfolio strategy instance

let klineChart = null;
let equityChart = null;
let currentMode = 'signal';  // 'signal' | 'portfolio'

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('endDate').value = todayStr();
  document.getElementById('pEndDate').value = todayStr();

  loadStrategies();

  const urlMode = new URLSearchParams(location.search).get('mode');
  if (urlMode === 'portfolio') switchMode('portfolio');

  document.getElementById('loadKlineBtn').addEventListener('click', loadKline);
  document.getElementById('runBtn').addEventListener('click', runBacktest);
  document.getElementById('runPortfolioBtn').addEventListener('click', runPortfolioBacktest);
  document.getElementById('stockCode').addEventListener('keydown', e => {
    if (e.key === 'Enter') loadKline();
  });

  window.addEventListener('resize', () => {
    klineChart?.resize();
    equityChart?.resize();
  });
});

function todayStr() {
  return new Date().toISOString().slice(0, 10);
}

// ── Mode switch ────────────────────────────────────────────────────────────────
function switchMode(mode) {
  currentMode = mode;
  document.getElementById('signalMode').style.display = mode === 'signal' ? '' : 'none';
  document.getElementById('portfolioMode').style.display = mode === 'portfolio' ? '' : 'none';
  document.getElementById('klinePanel').style.display = 'none';
  document.getElementById('resultsPanel').style.display = 'none';
  document.getElementById('navSignal').classList.toggle('active', mode === 'signal');
  document.getElementById('navPortfolio').classList.toggle('active', mode === 'portfolio');
}

// ── Strategy loading ──────────────────────────────────────────────────────────
async function loadStrategies() {
  try {
    const res = await fetch('/api/strategies');
    allStrategies = await res.json();
    signalStrategies = allStrategies.filter(s => s.strategy_type === 'signal');
    portfolioStrategies = allStrategies.filter(s => s.strategy_type === 'portfolio');
    renderSignalStrategyGrid();
    renderPortfolioStrategyGrid();
  } catch (e) {
    console.error('加载策略失败', e);
  }
}

function renderSignalStrategyGrid() {
  document.getElementById('signalStrategyGrid').innerHTML = signalStrategies.map(s => {
    const count = instances.filter(i => i.id === s.id).length;
    const hasAdded = count > 0;
    return `
      <div class="strategy-card${hasAdded ? ' selected-card' : ''}" onclick="addInstance('${s.id}')">
        <div class="sc-name">
          ${escHtml(s.name)}
          ${hasAdded ? `<span class="sc-count-badge">${count}</span>` : ''}
        </div>
        <div class="sc-desc">${escHtml(s.description)}</div>
        <div class="sc-btn-row">
          <button class="btn btn-ghost sc-add">${hasAdded ? `＋ 再添加（已选 ${count}）` : '＋ 添加'}</button>
          <button class="btn btn-outline sc-add" title="用推荐参数直接跑一遍示例回测，看看这个策略长什么样"
                  onclick="event.stopPropagation(); quickRun('${s.id}')">⚡ 一键试跑</button>
        </div>
      </div>
    `;
  }).join('');
}

// 「一键试跑」：不懂参数的新用户点一下就能看到完整回测结果。
// 股票代码没填(或不合法)时自动用示例股 000001 平安银行 —— 低价、流动性好，
// 10 万初始资金一定买得起；已填了合法代码则尊重用户的选择。
function quickRun(strategyId) {
  const codeEl = document.getElementById('stockCode');
  if (validateStockCode(codeEl.value.trim())) codeEl.value = '000001';
  if (!instances.some(i => i.id === strategyId)) addInstance(strategyId);
  runBacktest();
}

function renderPortfolioStrategyGrid() {
  document.getElementById('portfolioStrategyGrid').innerHTML = portfolioStrategies.map(s => {
    const isSelected = selectedPortfolio?.id === s.id;
    return `
      <div class="strategy-card${isSelected ? ' selected-card' : ''}"
           onclick="selectPortfolioStrategy('${s.id}')">
        <div class="sc-name">
          ${escHtml(s.name)}
          ${isSelected ? '<span class="sc-count-badge">✓</span>' : ''}
        </div>
        <div class="sc-desc">${escHtml(s.description)}</div>
        <button class="btn ${isSelected ? 'btn-accent' : 'btn-ghost'} sc-add">
          ${isSelected ? '✓ 已选择' : '✓ 选择'}
        </button>
      </div>
    `;
  }).join('');
}

// ── Portfolio strategy selection ──────────────────────────────────────────────
function selectPortfolioStrategy(id) {
  const strategy = portfolioStrategies.find(s => s.id === id);
  if (!strategy) return;

  const params = {};
  for (const [k, schema] of Object.entries(strategy.params || {})) {
    params[k] = schema.default;
  }
  selectedPortfolio = { id, strategy, params, label: strategy.name };

  renderPortfolioStrategyGrid(); // re-render to show selected state
  renderPortfolioStrategyDetail();
  renderPortfolioInstanceCard();
  document.getElementById('portfolioSelectedArea').style.display = '';
  document.getElementById('runPortfolioBtn').disabled = false;
}

// 渲染「策略说明」面板(用后端 detail 结构化字段)
function renderPortfolioStrategyDetail() {
  const el = document.getElementById('portfolioStrategyDetail');
  const d = selectedPortfolio?.strategy?.detail;
  if (!d) { el.innerHTML = ''; return; }
  const row = (label, text, cls = '') => text
    ? `<div class="sd-row ${cls}"><span class="sd-label">${label}</span><span class="sd-text">${escHtml(text)}</span></div>`
    : '';
  el.innerHTML = `
    <div class="strategy-detail-box">
      ${row('选股逻辑', d.logic)}
      ${row('调仓规则', d.rebalance)}
      ${row('回测口径', d.selection)}
      ${row('基准对标', d.benchmark)}
      ${row('风险提示', d.risk, 'sd-warn')}
    </div>`;
}

function renderPortfolioInstanceCard() {
  if (!selectedPortfolio) return;
  const inst = selectedPortfolio;

  const paramRows = Object.entries(inst.strategy.params || {}).map(([key, schema]) => `
    <div class="param-item">
      <label>${escHtml(schema.description)}</label>
      <input
        type="number"
        value="${inst.params[key]}"
        min="${schema.min}" max="${schema.max}"
        step="${schema.type === 'float' ? 0.1 : 1}"
        onchange="updatePortfolioParam('${key}', this.value, '${schema.type}')"
      >
    </div>
  `).join('');

  document.getElementById('portfolioInstanceCard').innerHTML = `
    <div class="portfolio-instance-card">
      <div class="portfolio-instance-header">
        <span class="portfolio-instance-title">${escHtml(inst.strategy.name)}</span>
        <button class="btn btn-danger" style="font-size:12px;padding:3px 8px"
                onclick="clearPortfolioStrategy()">✕ 取消</button>
      </div>
      <div class="portfolio-instance-params">${paramRows}</div>
    </div>
  `;
}

function updatePortfolioParam(key, rawVal, type) {
  if (!selectedPortfolio) return;
  const val = type === 'float' ? parseFloat(rawVal) : parseInt(rawVal, 10);
  selectedPortfolio.params[key] = isNaN(val) ? selectedPortfolio.strategy.params[key].default : val;
}

function clearPortfolioStrategy() {
  selectedPortfolio = null;
  document.getElementById('portfolioSelectedArea').style.display = 'none';
  document.getElementById('portfolioStrategyDetail').innerHTML = '';
  document.getElementById('runPortfolioBtn').disabled = true;
  renderPortfolioStrategyGrid();
}

// ── Signal strategy instance management ───────────────────────────────────────
function addInstance(strategyId) {
  const strategy = signalStrategies.find(s => s.id === strategyId);
  if (!strategy) return;

  const params = {};
  for (const [k, schema] of Object.entries(strategy.params || {})) {
    params[k] = schema.default;
  }

  const sameCount = instances.filter(i => i.id === strategyId).length;
  const label = sameCount > 0 ? `${strategy.name} #${sameCount + 1}` : strategy.name;
  instances.push({ id: strategyId, strategy, params, label });
  renderInstances();
  renderSignalStrategyGrid();
  updateRunBtn();
}

function removeInstance(idx) {
  instances.splice(idx, 1);
  renderInstances();
  renderSignalStrategyGrid();
  updateRunBtn();
}

function updateParam(idx, key, rawVal) {
  const schema = instances[idx].strategy.params[key];
  const val = schema.type === 'float' ? parseFloat(rawVal) : parseInt(rawVal, 10);
  instances[idx].params[key] = isNaN(val) ? schema.default : val;
}

function updateLabel(idx, val) {
  instances[idx].label = val;
}

function renderInstances() {
  const list = document.getElementById('instancesList');

  if (instances.length === 0) {
    list.innerHTML = '<div class="empty-hint" id="emptyHint">请从上方选择策略并添加</div>';
    return;
  }

  list.innerHTML = instances.map((inst, idx) => {
    const paramRows = Object.entries(inst.strategy.params || {}).map(([key, schema]) => `
      <div class="param-item">
        <label>${escHtml(schema.description)}</label>
        <input type="number" value="${inst.params[key]}"
          min="${schema.min}" max="${schema.max}"
          step="${schema.type === 'float' ? 0.1 : 1}"
          onchange="updateParam(${idx}, '${key}', this.value)">
      </div>
    `).join('');

    return `
      <div class="instance-card">
        <div class="instance-header">
          <input class="instance-label-input" type="text"
            value="${escHtml(inst.label)}"
            onchange="updateLabel(${idx}, this.value)">
          <span class="instance-badge">${escHtml(inst.strategy.name)}</span>
          <button class="btn btn-danger" style="font-size:12px;padding:3px 8px"
                  onclick="removeInstance(${idx})">✕ 移除</button>
        </div>
        <div class="instance-params">
          ${paramRows || '<span class="no-params-hint">无可配置参数</span>'}
        </div>
      </div>
    `;
  }).join('');
}

function updateRunBtn() {
  document.getElementById('runBtn').disabled = instances.length === 0;
}

// ── Input validation helpers ──────────────────────────────────────────────────
function validateStockCode(code) {
  if (!code) return '请输入股票代码';
  if (!/^\d{6}$/.test(code)) return '股票代码须为6位数字（如 000001）';
  return null;
}

function validateDateRange(start, end) {
  if (!start || !end) return '请填写完整的开始和结束日期';
  if (new Date(start) >= new Date(end)) return '开始日期必须早于结束日期';
  return null;
}

function validatePositive(val, label) {
  if (!Number.isFinite(val) || val <= 0) return `${label}必须为大于 0 的数字`;
  return null;
}

// ── Load K-line (signal mode) ──────────────────────────────────────────────────
async function loadKline() {
  const code = getCode();
  const codeErr = validateStockCode(code);
  if (codeErr) return showError(codeErr);

  const { start, end, adjust } = getDateParams();
  const dateErr = validateDateRange(start, end);
  if (dateErr) return showError(dateErr);

  const btn = document.getElementById('loadKlineBtn');
  btn.disabled = true;
  showLoading('获取K线数据…');
  clearError();

  try {
    const res = await fetch(
      `/api/stock/${code}/kline?start_date=${start}&end_date=${end}&adjust=${adjust}`
    );
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '请求失败');

    await fetchStockTag(code);
    document.getElementById('klinePanel').style.display = '';
    renderKline(data.data, `${document.getElementById('stockTag').textContent} (${code})`);
    klineChart?.resize();
    scrollTo('klinePanel');
  } catch (e) {
    showError(e.message);
  } finally {
    hideLoading();
    btn.disabled = false;
  }
}

// ── Single-stock backtest ──────────────────────────────────────────────────────
async function runBacktest() {
  const code = getCode();
  const codeErr = validateStockCode(code);
  if (codeErr) return showError(codeErr);
  if (instances.length === 0) return showError('请至少添加一个策略');

  const { start, end, adjust } = getDateParams();
  const dateErr = validateDateRange(start, end);
  if (dateErr) return showError(dateErr);

  const capital = parseFloat(document.getElementById('capital').value);
  const capErr = validatePositive(capital, '初始资金');
  if (capErr) return showError(capErr);

  clearError();

  const slippagePct = parseFloat(document.getElementById('slippage').value) || 0;
  const stopLossPct = parseFloat(document.getElementById('stopLoss').value) || 0;
  const takeProfitPct = parseFloat(document.getElementById('takeProfit').value) || 0;

  const payload = {
    stock_code: code,
    start_date: start, end_date: end,
    initial_capital: capital, adjust,
    strategies: instances.map(inst => ({
      strategy_id: inst.id, params: inst.params, label: inst.label,
    })),
    slippage_rate: slippagePct / 100,
    stop_loss: stopLossPct > 0 ? stopLossPct / 100 : null,
    take_profit: takeProfitPct > 0 ? takeProfitPct / 100 : null,
  };

  const runBtn = document.getElementById('runBtn');
  runBtn.disabled = true;
  showLoading('正在回测，请稍候…');

  try {
    const res = await fetch('/api/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '回测失败');

    const title = `${data.stock_name || ''} (${data.stock_code})`;
    document.getElementById('stockTag').textContent = data.stock_name || '';

    // Show panels BEFORE chart init so ECharts can measure real dimensions
    document.getElementById('klinePanel').style.display = '';
    document.getElementById('resultsPanel').style.display = '';

    renderKline(data.kline, title);
    klineChart?.resize();

    renderResults(data.results, data.benchmark, `${data.stock_name || data.stock_code}  ${start} → ${end}`);
    equityChart?.resize();

    // 资金不足警告（如 600519 这种 1393 元股 + 10w 资金 → 0 交易）
    if (data.capital_warning) {
      showError(data.capital_warning);
    }

    scrollTo('resultsPanel');
  } catch (e) {
    showError(e.message);
  } finally {
    hideLoading();
    runBtn.disabled = instances.length === 0;
  }
}

// ── Portfolio backtest (SSE streaming) ───────────────────────────────────────
async function runPortfolioBacktest() {
  if (!selectedPortfolio) return showPortfolioError('请先选择一个选股策略');

  const start = document.getElementById('pStartDate').value;
  const end = document.getElementById('pEndDate').value;
  const dateErr = validateDateRange(start, end);
  if (dateErr) return showPortfolioError(dateErr);

  const capital = parseFloat(document.getElementById('pCapital').value);
  const capErr = validatePositive(capital, '初始资金');
  if (capErr) return showPortfolioError(capErr);

  clearPortfolioError();

  showLoading('正在准备回测…');
  showProgressBar(0);

  // 回测口径
  const boards = Array.from(document.querySelectorAll('.pBoard:checked')).map(c => c.value);
  if (boards.length === 0) return showPortfolioError('请至少选择一个板块');

  const payload = {
    strategy_id: selectedPortfolio.id,
    params: selectedPortfolio.params,
    label: selectedPortfolio.label,
    start_date: start,
    end_date: end,
    initial_capital: capital,
    point_in_time: document.getElementById('pPointInTime').checked,
    allow_boards: boards,
    exclude_st: document.getElementById('pExcludeSt').checked,
    benchmark_code: document.getElementById('pBenchmark').value || null,
  };

  const runBtn = document.getElementById('runPortfolioBtn');
  runBtn.disabled = true;

  // Process a single SSE 'data: …' line. Throws on error events.
  const processEvent = (line) => {
    let evt;
    try { evt = JSON.parse(line.slice(6)); } catch { return; }

    if (evt.type === 'progress') {
      showLoading(evt.msg || '处理中…');
      // 不定量阶段(批量加载/回测计算无法分步上报)→ 流动条纹;
      // 否则按精确百分比填充
      if (evt.indeterminate) showProgressBarIndeterminate();
      else if (evt.pct != null) showProgressBar(evt.pct);
    } else if (evt.type === 'error') {
      throw new Error(evt.msg);
    } else if (evt.type === 'result') {
      document.getElementById('klinePanel').style.display = 'none';
      document.getElementById('resultsPanel').style.display = '';

      const subtitle = `${selectedPortfolio.strategy.name}  市值${evt.cap_range}  ` +
        `共${evt.universe_count}只股票  ${start} → ${end}`;
      renderResults(evt.results, evt.benchmark, subtitle, true);
      equityChart?.resize();

      const result = evt.results?.[0];
      if (result?.holdings_log?.length) renderHoldingsLog(result.holdings_log);

      scrollTo('resultsPanel');
    }
  };

  try {
    const res = await fetch('/api/portfolio_backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || '请求失败');
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      const events = buf.split('\n\n');
      buf = events.pop();   // last chunk may be incomplete

      for (const block of events) {
        const line = block.split('\n').find(l => l.startsWith('data: '));
        if (line) processEvent(line);
      }
    }

    // Flush any trailing event that arrived without a final \n\n
    buf += decoder.decode();
    if (buf.trim()) {
      const line = buf.split('\n').find(l => l.startsWith('data: '));
      if (line) processEvent(line);
    }
  } catch (e) {
    showPortfolioError(e.message);
  } finally {
    hideLoading();
    hideProgressBar();
    runBtn.disabled = false;
  }
}

// ── K-line chart ──────────────────────────────────────────────────────────────
function renderKline(data, title) {
  const el = document.getElementById('klineChart');
  if (klineChart) { klineChart.dispose(); klineChart = null; }
  klineChart = echarts.init(el, 'dark');
  document.getElementById('klinePanelTitle').textContent = `K线图  ${title}`;

  const dates  = data.map(d => d.date);
  const ohlc   = data.map(d => [d.open, d.close, d.low, d.high]);
  const vols   = data.map(d => d.volume);
  const upColors = data.map(d => d.close >= d.open ? '#ef5350' : '#26a69a');

  // Adaptive default window: show ~25% of range, clamped to [20, 120] bars
  const visible = Math.min(120, Math.max(20, Math.floor(data.length * 0.25)));
  const startPct = data.length > visible
    ? Math.round((1 - visible / data.length) * 100)
    : 0;

  klineChart.setOption({
    backgroundColor: '#ffffff', animation: false,
    grid: [
      { left: 60, right: 20, top: 30, bottom: 120 },
      { left: 60, right: 20, top: '78%', bottom: 60 },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0,
        axisLabel: { show: false }, axisLine: { lineStyle: { color: '#d0d7de' } }, splitLine: { show: false } },
      { type: 'category', data: dates, gridIndex: 1,
        axisLabel: { color: '#57606a', fontSize: 10 }, axisLine: { lineStyle: { color: '#d0d7de' } }, splitLine: { show: false } },
    ],
    yAxis: [
      { type: 'value', scale: true, gridIndex: 0,
        splitLine: { lineStyle: { color: '#eaeef2' } }, axisLabel: { color: '#57606a' } },
      { type: 'value', gridIndex: 1, splitLine: { show: false },
        axisLabel: { color: '#57606a', fontSize: 10, formatter: v => (v / 1e4).toFixed(0) + '万' } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: startPct, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], bottom: 10, height: 22,
        textStyle: { color: '#57606a' }, borderColor: '#d0d7de', fillerColor: 'rgba(9,105,218,.08)' },
    ],
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'cross' },
      backgroundColor: '#ffffff', borderColor: '#d0d7de',
      textStyle: { color: '#24292f', fontSize: 12 },
      formatter(params) {
        const i = params[0]?.dataIndex;
        if (i == null) return '';
        const d = data[i];
        const chg = d.open > 0 ? ((d.close - d.open) / d.open * 100).toFixed(2) : '-';
        const color = d.close >= d.open ? '#ef5350' : '#26a69a';
        return `<b>${d.date}</b><br/>
          开 ${d.open}  收 <span style="color:${color}">${d.close}</span><br/>
          高 ${d.high}  低 ${d.low}<br/>
          涨幅 <span style="color:${color}">${chg}%</span><br/>
          量 ${(d.volume / 1e4).toFixed(2)}万手`;
      },
    },
    series: [
      { name: 'K线', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0, data: ohlc,
        itemStyle: { color: '#ef5350', color0: '#26a69a', borderColor: '#ef5350', borderColor0: '#26a69a' } },
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: vols,
        itemStyle: { color: params => upColors[params.dataIndex] }, barMaxWidth: 8 },
    ],
  }, true);
}

// ── Backtest results ──────────────────────────────────────────────────────────
function renderResults(results, benchmark, subtitle, isPortfolio = false) {
  document.getElementById('resultSubtitle').textContent = subtitle;
  renderMetrics(results, benchmark);
  renderEquityChart(results, benchmark);
  renderTrades(results, isPortfolio);
}

const METRIC_DEFS = [
  { key: 'total_return',      label: '总收益率',      unit: '%',  kind: 'pct' },
  { key: 'annual_return',     label: '年化收益率',    unit: '%',  kind: 'pct' },
  { key: 'max_drawdown',      label: '最大回撤',      unit: '%',  kind: 'dd'  },
  { key: 'max_drawdown_days', label: '最大回撤天数',  unit: '天', kind: 'neutral' },
  { key: 'sharpe_ratio',      label: '夏普比率',      unit: '',   kind: 'sharpe' },
  { key: 'sortino_ratio',     label: 'Sortino比率',   unit: '',   kind: 'sharpe' },
  { key: 'calmar_ratio',      label: 'Calmar比率',    unit: '',   kind: 'sharpe' },
  { key: 'win_rate',          label: '胜率',          unit: '%',  kind: 'pct' },
  { key: 'trade_count',       label: '完整交易次数',  unit: '次', kind: 'neutral' },
  { key: 'final_value',       label: '最终资产',      unit: '元', kind: 'money' },
];

function renderMetrics(results, benchmark) {
  const cols = [
    ...(benchmark ? [{ ...benchmark, isBm: true }] : []),
    ...results.filter(r => !r.error),
  ];
  const errored = results.filter(r => r.error);

  let html = '<div class="metrics-scroll"><table class="metrics-tbl"><thead><tr>';
  html += '<th style="text-align:left">指标</th>';
  cols.forEach(c => {
    html += `<th class="${c.isBm ? 'bm-col' : ''}">${escHtml(c.strategy_name)}</th>`;
  });
  html += '</tr></thead><tbody>';

  METRIC_DEFS.forEach(def => {
    html += `<tr><td class="row-label">${def.label}</td>`;
    cols.forEach(c => {
      const val = (c.metrics || {})[def.key];
      if (val === null || val === undefined) { html += '<td class="val-neutral">—</td>'; return; }

      let cls = 'val-neutral';
      if (def.kind === 'pct') {
        cls = val > 0 ? 'val-pos' : val < 0 ? 'val-neg' : 'val-neutral';
      } else if (def.kind === 'dd') {
        cls = val > -10 ? 'val-pos' : val > -25 ? 'val-warn' : 'val-neg';
      } else if (def.kind === 'sharpe') {
        cls = val > 1 ? 'val-pos' : val > 0 ? 'val-neutral' : 'val-neg';
      }

      const display = def.kind === 'money'
        ? '¥' + Number(val).toLocaleString('zh-CN', { maximumFractionDigits: 0 })
        : val;
      html += `<td class="${cls}">${display}${def.kind !== 'money' ? def.unit : ''}</td>`;
    });
    html += '</tr>';
  });

  html += '</tbody></table></div>';
  if (errored.length) {
    errored.forEach(r => {
      html += `<div class="error-box" style="margin-top:8px">` +
        `策略「${escHtml(r.strategy_name)}」出错：${escHtml(r.error)}</div>`;
    });
  }
  document.getElementById('metricsWrap').innerHTML = html;
}

const LINE_COLORS = ['#0969da', '#e36209', '#cf222e', '#1a7f37', '#8250df', '#0550ae'];

function renderEquityChart(results, benchmark) {
  const el = document.getElementById('equityChart');
  if (equityChart) { equityChart.dispose(); equityChart = null; }
  equityChart = echarts.init(el);

  const series = [];
  if (benchmark?.equity_curve?.length) {
    series.push({
      name: benchmark.strategy_name,
      type: 'line',
      data: benchmark.equity_curve.map(e => [e.date, e.value]),
      lineStyle: { color: '#999', type: 'dashed', width: 1.5 },
      symbol: 'none',
    });
  }
  results.filter(r => r.equity_curve).forEach((r, i) => {
    series.push({
      name: r.strategy_name,
      type: 'line',
      data: r.equity_curve.map(e => [e.date, e.value]),
      lineStyle: { color: LINE_COLORS[i % LINE_COLORS.length], width: 2 },
      symbol: 'none',
    });
  });

  equityChart.setOption({
    backgroundColor: '#ffffff', animation: false,
    legend: { textStyle: { color: '#57606a' }, top: 8 },
    grid: { left: 70, right: 20, top: 46, bottom: 50 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#ffffff', borderColor: '#d0d7de',
      textStyle: { color: '#24292f', fontSize: 12 },
      formatter(params) {
        let s = `<b>${params[0].axisValue}</b><br/>`;
        params.forEach(p => {
          s += `${p.marker}${escHtml(p.seriesName)}: <b>¥${Number(p.value[1]).toLocaleString()}</b><br/>`;
        });
        return s;
      },
    },
    xAxis: {
      type: 'time', axisLabel: { color: '#57606a' },
      axisLine: { lineStyle: { color: '#d0d7de' } }, splitLine: { show: false },
    },
    yAxis: {
      type: 'value', scale: true,
      axisLabel: { color: '#57606a', formatter: v => '¥' + (v / 1e4).toFixed(0) + '万' },
      splitLine: { lineStyle: { color: '#eaeef2' } },
    },
    dataZoom: [
      { type: 'inside' },
      { type: 'slider', bottom: 4, height: 18, textStyle: { color: '#57606a' }, borderColor: '#d0d7de', fillerColor: 'rgba(9,105,218,.08)' },
    ],
    series,
  }, true);
}

// ── Trade history ─────────────────────────────────────────────────────────────
function renderTrades(results, isPortfolio) {
  const wrap = document.getElementById('tradesWrap');
  const withTrades = results.filter(r => r.trades?.length > 0);

  if (!withTrades.length) {
    wrap.innerHTML = '<div class="no-data">所选策略在此期间无完整交易记录</div>';
    return;
  }

  const showCode = isPortfolio; // portfolio trades have a code column

  let tabs = '<div class="trades-tabs">';
  let panels = '';

  withTrades.forEach((r, i) => {
    tabs += `<button class="tab-btn${i === 0 ? ' active' : ''}" onclick="switchTab(${i}, this)">
      ${escHtml(r.strategy_name)}</button>`;

    const rows = r.trades.map(t => `
      <tr class="${t.type === '买入' ? 'buy-row' : 'sell-row'}">
        <td>${t.date}</td>
        ${showCode ? `<td class="code-cell">${t.code || ''}</td>` : ''}
        <td><span class="${t.type === '买入' ? 'badge-buy' : 'badge-sell'}">${t.type}</span></td>
        <td>${(+t.price).toFixed(3)}</td>
        <td>${t.shares}</td>
        <td>¥${Number(t.amount).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
        <td>¥${(+t.commission).toFixed(2)}</td>
        <td>¥${Number(t.capital).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
      </tr>
    `).join('');

    panels += `
      <div class="trade-panel${i === 0 ? ' active' : ''}" id="tp-${i}">
        <div class="trades-scroll">
          <table class="trades-tbl">
            <thead><tr>
              <th>日期</th>
              ${showCode ? '<th>代码</th>' : ''}
              <th>类型</th><th>成交价</th><th>股数</th>
              <th>金额</th><th>手续费/税</th><th>剩余资金</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>`;
  });

  tabs += '</div>';
  wrap.innerHTML = tabs + panels;
}

function switchTab(idx, btn) {
  document.querySelectorAll('.trade-panel').forEach((el, i) => el.classList.toggle('active', i === idx));
  document.querySelectorAll('.tab-btn').forEach((el, i) => el.classList.toggle('active', i === idx));
}

// ── Holdings log (portfolio mode only) ───────────────────────────────────────
function renderHoldingsLog(holdingsLog) {
  const wrap = document.getElementById('tradesWrap');
  const logHtml = `
    <div class="section-title" style="margin-top:20px">换仓记录</div>
    <div class="holdings-table-wrap">
      <table class="holdings-tbl">
        <thead><tr><th>换仓日期</th><th>持仓股票</th></tr></thead>
        <tbody>
          ${holdingsLog.map(entry => `
            <tr>
              <td>${entry.date}</td>
              <td>${entry.stocks.map(c => `<span class="stock-chip">${c}</span>`).join('')}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
  wrap.innerHTML += logHtml;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function getCode() { return document.getElementById('stockCode').value.trim(); }
function getDateParams() {
  return {
    start: document.getElementById('startDate').value,
    end: document.getElementById('endDate').value,
    adjust: document.getElementById('adjust').value,
  };
}

async function fetchStockTag(code) {
  try {
    const res = await fetch(`/api/stock/${code}/info`);
    const data = await res.json();
    document.getElementById('stockTag').textContent = data.name || '';
  } catch (_) {}
}

function showLoading(msg) {
  document.getElementById('loadingMsg').textContent = msg || '加载中…';
  document.getElementById('loading').style.display = 'flex';
}
function hideLoading() { document.getElementById('loading').style.display = 'none'; }

function showProgressBar(pct) {
  const bar = document.getElementById('progressBar');
  if (!bar) return;
  bar.style.display = 'block';
  bar.querySelector('.pb-track').classList.remove('indeterminate');
  bar.querySelector('.pb-fill').style.width = `${Math.min(100, pct)}%`;
  bar.querySelector('.pb-label').textContent = `${Math.round(pct)}%`;
}
// 不定量进度:阶段在跑但无法分步上报(批量加载行情 / 回测计算 / 取基准)。
// 用流动条纹表示"处理中",避免百分比假装精确又长时间卡住。
function showProgressBarIndeterminate() {
  const bar = document.getElementById('progressBar');
  if (!bar) return;
  bar.style.display = 'block';
  bar.querySelector('.pb-track').classList.add('indeterminate');
  bar.querySelector('.pb-fill').style.width = '';   // 由 CSS 动画接管
  bar.querySelector('.pb-label').textContent = '处理中…';
}
function hideProgressBar() {
  const bar = document.getElementById('progressBar');
  if (!bar) return;
  bar.style.display = 'none';
  bar.querySelector('.pb-track').classList.remove('indeterminate');
}

function showError(msg) {
  document.getElementById('settingsError').innerHTML = `<div class="error-box">${escHtml(msg)}</div>`;
}
function clearError() { document.getElementById('settingsError').innerHTML = ''; }

function showPortfolioError(msg) {
  document.getElementById('portfolioError').innerHTML = `<div class="error-box">${escHtml(msg)}</div>`;
}
function clearPortfolioError() { document.getElementById('portfolioError').innerHTML = ''; }

function scrollTo(id) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function escHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
