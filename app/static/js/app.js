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

// 最近一次回测的可分享内容。生成分享链接要把它 POST 回服务端，
// 而 runBacktest 里的 data 是局部变量,渲染完就没了
let lastShareable = null;

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
  document.getElementById('navSignal').addEventListener('click', () => switchMode('signal'));
  document.getElementById('navPortfolio').addEventListener('click', () => switchMode('portfolio'));

  // 首屏「用示例试跑一次」—— 新访客的 3 秒 aha:不填任何参数就看到完整报告
  // (含防过拟合结论)。复用策略卡片上的一键试跑,示例股同为 000001 平安银行。
  document.getElementById('shareImgBtn').addEventListener('click', onShareImage);
  document.getElementById('shareLinkBtn').addEventListener('click', onShareLink);

  document.getElementById('heroDemoBtn').addEventListener('click', () => {
    reportEvent('demo_click');
    if (currentMode !== 'signal') switchMode('signal');
    // 结果面板要等回测返回才 display:'',所以滚动必须排在 quickRun 之后
    Promise.resolve(quickRun('ma_cross')).then(() => {
      const panel = document.getElementById('resultsPanel');
      if (panel.style.display !== 'none') {
        panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  });

  window.addEventListener('resize', () => {
    klineChart?.resize();
    equityChart?.resize();
  });

  // ── 动态渲染内容的事件委托(容器本身是静态的，内容随策略/回测结果重绘)──────
  document.getElementById('signalStrategyGrid').addEventListener('click', e => {
    const quickBtn = e.target.closest('.sc-quickrun');
    if (quickBtn) { quickRun(quickBtn.dataset.id); return; }
    const card = e.target.closest('.strategy-card');
    if (card) addInstance(card.dataset.id);
  });

  document.getElementById('portfolioStrategyGrid').addEventListener('click', e => {
    const card = e.target.closest('.strategy-card');
    if (card) selectPortfolioStrategy(card.dataset.id);
  });

  document.getElementById('portfolioInstanceCard').addEventListener('change', e => {
    const input = e.target.closest('input[data-key]');
    if (input) updatePortfolioParam(input.dataset.key, input.value, input.dataset.type);
  });
  document.getElementById('portfolioInstanceCard').addEventListener('click', e => {
    if (e.target.closest('.portfolio-clear-btn')) clearPortfolioStrategy();
  });

  document.getElementById('instancesList').addEventListener('change', e => {
    const paramInput = e.target.closest('input[data-idx][data-key]');
    if (paramInput) { updateParam(+paramInput.dataset.idx, paramInput.dataset.key, paramInput.value); return; }
    const labelInput = e.target.closest('.instance-label-input');
    if (labelInput) updateLabel(+labelInput.dataset.idx, labelInput.value);
  });
  document.getElementById('instancesList').addEventListener('click', e => {
    const btn = e.target.closest('.instance-remove-btn');
    if (btn) removeInstance(+btn.dataset.idx);
  });

  document.getElementById('tradesWrap').addEventListener('click', e => {
    const tab = e.target.closest('.tab-btn');
    if (tab) { switchTab(+tab.dataset.idx, tab); return; }
    const filterBtn = e.target.closest('.tf-btn');
    if (filterBtn) filterTrades(+filterBtn.dataset.idx, filterBtn.dataset.type, filterBtn);
  });
  document.getElementById('tradesWrap').addEventListener('input', e => {
    const codeFilter = e.target.closest('.trades-code-filter');
    if (codeFilter) filterTradesByCode(+codeFilter.dataset.idx, codeFilter.value);
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
      <div class="strategy-card${hasAdded ? ' selected-card' : ''}" data-id="${s.id}">
        <div class="sc-name">
          ${esc(s.name)}
          ${hasAdded ? `<span class="sc-count-badge">${count}</span>` : ''}
        </div>
        <div class="sc-desc">${esc(s.description)}</div>
        <div class="sc-btn-row">
          <button class="btn btn-ghost sc-add sc-add-primary">${hasAdded ? `＋ 再添加（已选 ${count}）` : '＋ 添加'}</button>
          <button class="btn sc-quickrun" title="用推荐参数直接跑一遍示例回测，看看这个策略长什么样"
                  data-id="${s.id}">⚡ 试跑</button>
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
  // 已在列表里就直接跑;不在则先加 —— 加不进去(已达上限)就停,别跑成一堆
  // 用户没点的策略还把上面那条上限提示给覆盖掉。
  if (!instances.some(i => i.id === strategyId) && !addInstance(strategyId)) return;
  // 把 promise 交出去 —— 调用方(首屏 CTA)要等结果面板真正出现后再滚过去
  return runBacktest();
}

function renderPortfolioStrategyGrid() {
  document.getElementById('portfolioStrategyGrid').innerHTML = portfolioStrategies.map(s => {
    const isSelected = selectedPortfolio?.id === s.id;
    return `
      <div class="strategy-card${isSelected ? ' selected-card' : ''}"
           data-id="${s.id}">
        <div class="sc-name">
          ${esc(s.name)}
          ${isSelected ? '<span class="sc-count-badge">✓</span>' : ''}
        </div>
        <div class="sc-desc">${esc(s.description)}</div>
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
    ? `<div class="sd-row ${cls}"><span class="sd-label">${label}</span><span class="sd-text">${esc(text)}</span></div>`
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
      <label>${esc(schema.description)}</label>
      <input
        type="number"
        value="${inst.params[key]}"
        min="${schema.min}" max="${schema.max}"
        step="${schema.type === 'float' ? 0.1 : 1}"
        data-key="${key}" data-type="${schema.type}"
      >
    </div>
  `).join('');

  document.getElementById('portfolioInstanceCard').innerHTML = `
    <div class="portfolio-instance-card">
      <div class="portfolio-instance-header">
        <span class="portfolio-instance-title">${esc(inst.strategy.name)}</span>
        <button class="btn btn-danger portfolio-clear-btn" style="font-size:12px;padding:3px 8px">✕ 取消</button>
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
// 与后端 BacktestRequest.strategies 的 max_length 保持一致 —— 在这里拦一下
// 是为了给出人话提示，而不是让用户吃一个 422。
const MAX_INSTANCES = 20;

// 返回是否真的加进去了 —— quickRun 要据此决定还跑不跑，否则满额时会把
// 用户没选中的那 20 个跑一遍、结果里根本没有他点的那个策略。
function addInstance(strategyId) {
  const strategy = signalStrategies.find(s => s.id === strategyId);
  if (!strategy) return false;

  if (instances.length >= MAX_INSTANCES) {
    showError(`最多同时对比 ${MAX_INSTANCES} 个策略实例，请先移除一些再添加。`);
    return false;
  }

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
  return true;
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
    list.innerHTML = '<div class="empty-hint" id="emptyHint"><span class="empty-hint-icon" aria-hidden="true">🗂️</span><span>暂无已选策略，点击上方策略卡片即可添加</span></div>';
    return;
  }

  list.innerHTML = instances.map((inst, idx) => {
    const paramRows = Object.entries(inst.strategy.params || {}).map(([key, schema]) => `
      <div class="param-item">
        <label>${esc(schema.description)}</label>
        <input type="number" value="${inst.params[key]}"
          min="${schema.min}" max="${schema.max}"
          step="${schema.type === 'float' ? 0.1 : 1}"
          data-idx="${idx}" data-key="${key}">
      </div>
    `).join('');

    return `
      <div class="instance-card">
        <div class="instance-header">
          <input class="instance-label-input" type="text"
            value="${esc(inst.label)}"
            data-idx="${idx}">
          <span class="instance-badge">${esc(inst.strategy.name)}</span>
          <button class="btn btn-danger instance-remove-btn" style="font-size:12px;padding:3px 8px"
                  data-idx="${idx}">✕ 移除</button>
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
    robustness_check: document.getElementById('robustnessCheck').checked,
  };

  const runBtn = document.getElementById('runBtn');
  runBtn.disabled = true;
  showLoading(payload.robustness_check ? '正在回测并跑防过拟合检查，稍候…' : '正在回测，请稍候…');

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

    lastShareable = {
      stock_code: data.stock_code, stock_name: data.stock_name,
      start_date: start, end_date: end, initial_capital: capital,
      results: data.results, benchmark: data.benchmark,
    };
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
      lastShareable = {
        stock_name: `${selectedPortfolio.strategy.name}（选股回测）`,
        start_date: start, end_date: end,
        initial_capital: parseFloat(document.getElementById('pCapital').value) || null,
        results: evt.results, benchmark: evt.benchmark,
      };
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
        axisLabel: { color: '#57606a', fontSize: 10, formatter: makeWanAxisFormatter(vols) } },
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
  shareMsg('');   // 上一次的分享链接是上一次回测的,别留在新结果旁边
  renderMetrics(results, benchmark);
  renderEquityChart(results, benchmark);
  renderYearlyBreakdown(results, benchmark);
  renderRobustness(results);
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
    html += `<th class="${c.isBm ? 'bm-col' : ''}">${esc(c.strategy_name)}</th>`;
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
        `策略「${esc(r.strategy_name)}」出错：${esc(r.error)}</div>`;
    });
  }
  document.getElementById('metricsWrap').innerHTML = html;
}

const LINE_COLORS = ['#0969da', '#e36209', '#cf222e', '#1a7f37', '#8250df', '#0550ae'];

// 固定「万 + 整数」的轴标签在数据跨度小的时候会把相邻刻度压成同一个字符串
// (10 万本金 0 笔成交那次,7 个刻度全是「¥10万」)。按实际跨度挑小数位:
// 跨度不足 1 万时「万」这个单位已经没有分辨率了,退回原始数值。
function makeWanAxisFormatter(values, prefix = '') {
  let min = Infinity, max = -Infinity;
  for (const v of values) {
    if (!Number.isFinite(v)) continue;
    if (v < min) min = v;
    if (v > max) max = v;
  }
  const range = max > min ? max - min : 0;
  if (range < 1e4) return v => prefix + Math.round(v).toLocaleString('zh-CN');
  // ECharts 默认切 ~5 段,用 range/5 近似刻度间距
  const decimals = range / 5 >= 1e4 ? 0 : 1;
  return v => prefix + (v / 1e4).toFixed(decimals) + '万';
}

function renderEquityChart(results, benchmark) {
  const el = document.getElementById('equityChart');
  if (equityChart) { equityChart.dispose(); equityChart = null; }
  equityChart = echarts.init(el);

  // 只在 lineStyle 里设色的话,图例小圆点读不到,会退回 ECharts 默认色板 ——
  // 图例说"基准=蓝紫、策略=绿",图上却是"基准=灰虚线、策略=蓝"。itemStyle
  // 才是图例取色的地方,两处一起设。
  const series = [];
  const allValues = [];
  if (benchmark?.equity_curve?.length) {
    benchmark.equity_curve.forEach(e => allValues.push(e.value));
    series.push({
      name: benchmark.strategy_name,
      type: 'line',
      data: benchmark.equity_curve.map(e => [e.date, e.value]),
      itemStyle: { color: '#999' },
      lineStyle: { color: '#999', type: 'dashed', width: 1.5 },
      symbol: 'none',
    });
  }
  results.filter(r => r.equity_curve).forEach((r, i) => {
    const color = LINE_COLORS[i % LINE_COLORS.length];
    r.equity_curve.forEach(e => allValues.push(e.value));
    series.push({
      name: r.strategy_name,
      type: 'line',
      data: r.equity_curve.map(e => [e.date, e.value]),
      itemStyle: { color },
      lineStyle: { color, width: 2 },
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
          s += `${p.marker}${esc(p.seriesName)}: <b>¥${Number(p.value[1]).toLocaleString()}</b><br/>`;
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
      axisLabel: { color: '#57606a', formatter: makeWanAxisFormatter(allValues, '¥') },
      splitLine: { lineStyle: { color: '#eaeef2' } },
    },
    dataZoom: [
      { type: 'inside' },
      { type: 'slider', bottom: 4, height: 18, textStyle: { color: '#57606a' }, borderColor: '#d0d7de', fillerColor: 'rgba(9,105,218,.08)' },
    ],
    series,
  }, true);
}

// ── Yearly return breakdown ─────────────────────────────────────────────────────
function renderYearlyBreakdown(results, benchmark) {
  const wrap = document.getElementById('yearlyWrap');
  const cols = [
    ...(benchmark?.yearly_returns?.length ? [{ ...benchmark, isBm: true }] : []),
    ...results.filter(r => !r.error && r.yearly_returns?.length),
  ];

  if (!cols.length) { wrap.innerHTML = '<div class="no-data">暂无逐年数据</div>'; return; }

  const years = [...new Set(cols.flatMap(c => c.yearly_returns.map(y => y.year)))].sort();
  if (years.length < 2) {
    wrap.innerHTML = '<div class="no-data">回测跨度不足两个自然年，无需逐年拆解</div>';
    return;
  }

  let html = '<div class="metrics-scroll"><table class="metrics-tbl"><thead><tr>';
  html += '<th style="text-align:left">年份</th>';
  cols.forEach(c => { html += `<th class="${c.isBm ? 'bm-col' : ''}">${esc(c.strategy_name)}</th>`; });
  html += '</tr></thead><tbody>';

  years.forEach(y => {
    html += `<tr><td class="row-label">${y}</td>`;
    cols.forEach(c => {
      const row = c.yearly_returns.find(yr => yr.year === y);
      const val = row?.return_pct;
      if (val === null || val === undefined) { html += '<td class="val-neutral">—</td>'; return; }
      const cls = val > 0 ? 'val-pos' : val < 0 ? 'val-neg' : 'val-neutral';
      html += `<td class="${cls}">${val > 0 ? '+' : ''}${val}%</td>`;
    });
    html += '</tr>';
  });

  html += '</tbody></table></div>';
  wrap.innerHTML = html;
}

// ── Robustness check (parameter sensitivity + in/out-of-sample split) ──────────
function round1(n) { return Math.round(n * 10) / 10; }

const OOS_METRIC_DEFS = [
  { key: 'total_return',  label: '总收益率',   unit: '%' },
  { key: 'annual_return', label: '年化收益率', unit: '%' },
  { key: 'max_drawdown',  label: '最大回撤',   unit: '%' },
  { key: 'sharpe_ratio',  label: '夏普比率',   unit: '' },
];

// ── 防过拟合结论 ──────────────────────────────────────────────────────────────
// 两张表格自己不会说话:用户看完"某参数扰动后年化 -18pp"也不知道这算好还是坏。
// 这里把两项检查各读成一句话,再取较差的一项作为总结论。
// 只描述数字读出来是什么,不做任何"该不该买"的判断 —— 页面另有免责声明。
const _LEVEL_RANK = { good: 0, warn: 1, bad: 2, unknown: -1 };

// 阈值与上面表格里的着色保持一致(>10pp 红、>5pp 黄),免得
// "结论说通过、表格里一片红"这种自相矛盾
function sensitivityVerdict(r) {
  const base = r.metrics?.annual_return;
  if (base == null || !r.sensitivity?.length) return null;
  let maxDelta = 0, counted = 0;
  r.sensitivity.forEach(row => row.variants.forEach(v => {
    if (v.error || v.annual_return == null) return;
    counted++;
    maxDelta = Math.max(maxDelta, Math.abs(v.annual_return - base));
  }));
  if (!counted) return null;
  const d = round1(maxDelta);
  if (maxDelta <= 5) {
    return { level: 'good', text: `参数 ±20% 扰动后年化最大变化 ${d}pp，对参数不敏感` };
  }
  if (maxDelta <= 10) {
    return { level: 'warn', text: `参数 ±20% 扰动后年化最大变化 ${d}pp，对参数中等敏感` };
  }
  return { level: 'bad', text: `参数 ±20% 扰动后年化最大变化 ${d}pp，对参数高度敏感 —— 换一组邻近参数结果就可能完全不同` };
}

function oosVerdict(r) {
  if (!r.oos_split) return null;
  const inAnn = r.oos_split.in_sample?.metrics?.annual_return;
  const outAnn = r.oos_split.out_of_sample?.metrics?.annual_return;
  if (inAnn == null || outAnn == null) return null;
  if (inAnn <= 0) {
    // 样本内本来就没赚钱,谈不上"过拟合",但同样不是个能用的结果
    return { level: 'warn', text: `样本内年化 ${inAnn}% 本身为负，这段历史上没有表现出效果` };
  }
  const keep = Math.round(outAnn / inAnn * 100);
  if (outAnn <= 0) {
    return { level: 'bad', text: `样本内年化 ${inAnn}%，样本外 ${outAnn}% 由盈转亏 —— 过拟合的典型信号` };
  }
  if (keep >= 70) {
    return { level: 'good', text: `样本外年化 ${outAnn}%，维持了样本内(${inAnn}%)的 ${keep}%` };
  }
  if (keep >= 30) {
    return { level: 'warn', text: `样本外年化 ${outAnn}%，只剩样本内(${inAnn}%)的 ${keep}%，衰减明显` };
  }
  return { level: 'bad', text: `样本外年化 ${outAnn}%，只剩样本内(${inAnn}%)的 ${keep}% —— 疑似过拟合` };
}

const _VERDICT_HEAD = {
  good: { cls: 'rb-verdict--good', icon: '✅', label: '两项检查通过' },
  warn: { cls: 'rb-verdict--warn', icon: '⚠️', label: '需要留意' },
  bad:  { cls: 'rb-verdict--bad',  icon: '🚩', label: '疑似过拟合' },
};

function renderVerdict(r) {
  const parts = [sensitivityVerdict(r), oosVerdict(r)].filter(Boolean);
  if (!parts.length) {
    return '<div class="rb-verdict rb-verdict--none">数据不足，无法给出防过拟合结论</div>';
  }
  const worst = parts.reduce((a, b) => _LEVEL_RANK[b.level] > _LEVEL_RANK[a.level] ? b : a);
  const head = _VERDICT_HEAD[worst.level] || _VERDICT_HEAD.warn;
  return `<div class="rb-verdict ${head.cls}">` +
    `<div class="rb-verdict-head">${head.icon} ${head.label}</div>` +
    '<ul class="rb-verdict-list">' +
    parts.map(p => `<li>${esc(p.text)}</li>`).join('') +
    '</ul></div>';
}

function renderRobustness(results) {
  const section = document.getElementById('robustnessSection');
  const wrap = document.getElementById('robustnessWrap');
  const withRobustness = results.filter(r => !r.error && r.sensitivity);

  if (!withRobustness.length) { section.style.display = 'none'; wrap.innerHTML = ''; return; }
  section.style.display = '';

  let html = '';
  withRobustness.forEach(r => {
    html += `<div class="robustness-block">`;
    html += `<div class="robustness-strategy-name">${esc(r.strategy_name)}</div>`;
    html += renderVerdict(r);

    if (r.sensitivity.length) {
      const baseAnnual = r.metrics?.annual_return;
      html += '<div class="robustness-subtitle">参数敏感性（单参数 ±20% 扰动，其余不变）</div>';
      html += '<div class="metrics-scroll"><table class="metrics-tbl"><thead><tr>' +
        '<th style="text-align:left">参数</th><th>基准值</th><th>基准年化</th>' +
        '<th>扰动值</th><th>扰动后年化</th><th>变化</th></tr></thead><tbody>';
      r.sensitivity.forEach(row => {
        const n = row.variants.length || 1;
        row.variants.forEach((v, vi) => {
          html += '<tr>';
          if (vi === 0) {
            html += `<td class="row-label" rowspan="${n}">${esc(row.param_label)}</td>`;
            html += `<td rowspan="${n}">${row.base_value}</td>`;
            html += `<td rowspan="${n}">${baseAnnual != null ? baseAnnual + '%' : '—'}</td>`;
          }
          if (v.error) {
            html += `<td>${v.value}</td><td colspan="2" class="val-neg">运行失败</td>`;
          } else {
            const delta = (baseAnnual != null && v.annual_return != null)
              ? round1(v.annual_return - baseAnnual) : null;
            const cls = delta == null ? 'val-neutral'
              : Math.abs(delta) > 10 ? 'val-neg' : Math.abs(delta) > 5 ? 'val-warn' : 'val-pos';
            html += `<td>${v.value}</td><td>${v.annual_return != null ? v.annual_return + '%' : '—'}</td>`;
            html += `<td class="${cls}">${delta != null ? (delta > 0 ? '+' : '') + delta + 'pp' : '—'}</td>`;
          }
          html += '</tr>';
        });
      });
      html += '</tbody></table></div>';
    }

    if (r.oos_split) {
      const { in_sample, out_of_sample } = r.oos_split;
      html += '<div class="robustness-subtitle">样本内 / 样本外拆分</div>';
      html += `<div class="oos-hint">样本内 ${in_sample.date_range[0]} ~ ${in_sample.date_range[1]}` +
        ` ・ 样本外 ${out_of_sample.date_range[0]} ~ ${out_of_sample.date_range[1]}` +
        `（两段各自从初始资金独立起跑，收益率可直接对比）</div>`;
      html += '<div class="metrics-scroll"><table class="metrics-tbl"><thead><tr>' +
        '<th style="text-align:left">指标</th><th>样本内</th><th>样本外</th></tr></thead><tbody>';
      OOS_METRIC_DEFS.forEach(def => {
        html += `<tr><td class="row-label">${def.label}</td>`;
        [in_sample.metrics[def.key], out_of_sample.metrics[def.key]].forEach(val => {
          if (val === null || val === undefined) { html += '<td class="val-neutral">—</td>'; return; }
          const cls = val > 0 ? 'val-pos' : val < 0 ? 'val-neg' : 'val-neutral';
          html += `<td class="${cls}">${val}${def.unit}</td>`;
        });
        html += '</tr>';
      });
      html += '</tbody></table></div>';
    } else {
      html += '<div class="no-data">回测跨度不足（建议半年以上），无法拆分样本内/外验证</div>';
    }

    html += '</div>';
  });

  wrap.innerHTML = html;
}

// ── 分享 ──────────────────────────────────────────────────────────────────────
// 跑完回测拿不走任何东西,每一次可能的口碑传播都断在这里。两条出口：
//   保存结果图 —— 画在 canvas 上,自带品牌与免责声明,适合发群/发帖
//   分享链接   —— 快照存服务端,给一个 /s/{token} 的公开页
function shareMsg(html) {
  document.getElementById('shareMsg').innerHTML = html;
}

// 生成结果图。曲线用 ECharts 自己导出的位图,再在上面补标题、关键指标、
// 品牌与免责声明 —— 免责声明必须留在图上:图会被单独转发,脱离页面语境。
function buildShareImage() {
  if (!equityChart || !lastShareable) return null;
  const chartUrl = equityChart.getDataURL({
    type: 'png', pixelRatio: 2, backgroundColor: '#ffffff',
  });
  return new Promise(resolve => {
    const img = new Image();
    img.onload = () => {
      const pad = 28, headH = 96, footH = 64;
      const W = img.width + pad * 2;
      const H = img.height + headH + footH + pad;
      const cv = document.createElement('canvas');
      cv.width = W; cv.height = H;
      const ctx = cv.getContext('2d');
      ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, W, H);

      const s = lastShareable;
      const name = s.stock_name || s.stock_code || '回测结果';
      ctx.fillStyle = '#1f2328';
      ctx.font = 'bold 30px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
      ctx.fillText(name, pad, 46);
      ctx.fillStyle = '#57606a';
      ctx.font = '17px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
      ctx.fillText(`${s.start_date} → ${s.end_date}　初始资金 ¥${Number(s.initial_capital || 0).toLocaleString('zh-CN')}`, pad, 76);

      ctx.drawImage(img, pad, headH);

      // 页脚:品牌 + 站点地址 + 免责声明
      const fy = headH + img.height + 30;
      ctx.fillStyle = '#185FA5';
      ctx.font = 'bold 19px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
      ctx.fillText('🕐 收盘 shoupan · shoupan.asia', pad, fy);
      ctx.fillStyle = '#8c959f';
      ctx.font = '14px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
      ctx.fillText('历史回测结果，不代表未来收益 · 仅供研究，不构成投资建议', pad, fy + 24);

      resolve(cv.toDataURL('image/png'));
    };
    img.onerror = () => resolve(null);
    img.src = chartUrl;
  });
}

async function onShareImage() {
  if (!lastShareable) return shareMsg('请先跑一次回测');
  shareMsg('正在生成…');
  try {
    const url = await buildShareImage();
    if (!url) return shareMsg('生成失败，请重试');
    const s = lastShareable;
    const a = document.createElement('a');
    a.href = url;
    a.download = `shoupan_${s.stock_code || 'backtest'}_${s.start_date}_${s.end_date}.png`;
    document.body.appendChild(a); a.click(); a.remove();
    shareMsg('已保存到下载目录');
    reportEvent('share_click', { kind: 'image' });
  } catch (e) {
    shareMsg('生成失败：' + esc(e.message));
  }
}

async function onShareLink() {
  if (!lastShareable) return shareMsg('请先跑一次回测');
  shareMsg('正在生成链接…');
  try {
    const res = await fetch('/api/backtest/share', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(lastShareable),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const full = location.origin + data.url;
    shareMsg(`分享链接：<a href="${esc(data.url)}" target="_blank" rel="noopener">${esc(full)}</a>`);
    copyToClipboard(full);
  } catch (e) {
    shareMsg('生成失败：' + esc(e.message));
  }
}

// navigator.clipboard 只在安全上下文可用,失败就算了 —— 链接已经显示在页面上
function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).catch(() => {});
  }
}

// 前端埋点:只上报服务端看不见的动作。失败静默,绝不打扰用户
function reportEvent(event, meta) {
  fetch('/api/event', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event, meta }),
  }).catch(() => {});
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
    tabs += `<button class="tab-btn${i === 0 ? ' active' : ''}" data-idx="${i}">
      ${esc(r.strategy_name)}</button>`;

    const buyCount = r.trades.filter(t => t.type === '买入').length;
    const sellCount = r.trades.length - buyCount;

    const rows = r.trades.map(t => `
      <tr class="${t.type === '买入' ? 'buy-row' : 'sell-row'}" data-ttype="${t.type === '买入' ? 'buy' : 'sell'}" data-code="${t.code || ''}">
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
        <div class="trades-toolbar">
          <div class="trades-filter" role="group">
            <button class="tf-btn active" data-idx="${i}" data-type="all">全部 ${r.trades.length}</button>
            <button class="tf-btn" data-idx="${i}" data-type="buy">买入 ${buyCount}</button>
            <button class="tf-btn" data-idx="${i}" data-type="sell">卖出 ${sellCount}</button>
          </div>
          ${showCode ? `<input class="trades-code-filter" placeholder="按代码筛选" data-idx="${i}">` : ''}
        </div>
        <div class="trades-scroll">
          <table class="trades-tbl" id="tt-${i}">
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

function filterTrades(idx, type, btn) {
  const table = document.getElementById(`tt-${idx}`);
  table.dataset.typeFilter = type;
  btn.parentElement.querySelectorAll('.tf-btn').forEach(b => b.classList.toggle('active', b === btn));
  _applyTradeFilters(table);
}

function filterTradesByCode(idx, code) {
  const table = document.getElementById(`tt-${idx}`);
  table.dataset.codeFilter = code.trim().toLowerCase();
  _applyTradeFilters(table);
}

function _applyTradeFilters(table) {
  const type = table.dataset.typeFilter || 'all';
  const code = table.dataset.codeFilter || '';
  table.querySelectorAll('tbody tr').forEach(tr => {
    const typeOk = type === 'all' || tr.dataset.ttype === type;
    const codeOk = !code || (tr.dataset.code || '').toLowerCase().includes(code);
    tr.style.display = (typeOk && codeOk) ? '' : 'none';
  });
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
  document.getElementById('settingsError').innerHTML = `<div class="error-box">${esc(msg)}</div>`;
}
function clearError() { document.getElementById('settingsError').innerHTML = ''; }

function showPortfolioError(msg) {
  document.getElementById('portfolioError').innerHTML = `<div class="error-box">${esc(msg)}</div>`;
}
function clearPortfolioError() { document.getElementById('portfolioError').innerHTML = ''; }

function scrollTo(id) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

