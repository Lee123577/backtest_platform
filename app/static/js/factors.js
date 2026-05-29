// 因子分析页：列表 → 选择 → 配置 → IC 分析 / 计算入库
(() => {
  const $ = (id) => document.getElementById(id);

  // 默认 end_date = 今天
  const today = new Date().toISOString().slice(0, 10);
  $('endDate').value = today;

  let factors = {};       // {name: {description, category}}
  let selected = null;    // 当前选中的 factor name
  let icChart = null;
  let heatmapChart = null;

  // ── Utils ────────────────────────────────────────────────────────────────

  function toast(msg, type = '') {
    const el = $('toast');
    el.className = `toast show ${type}`;
    el.textContent = msg;
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove('show'), 2400);
  }

  function setStatus(msg) {
    $('statusText').textContent = msg;
  }

  // ── 因子列表 ─────────────────────────────────────────────────────────────

  async function loadFactors() {
    try {
      const res = await fetch('/api/factors');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      factors = data.factors || {};
      renderFactorList();
    } catch (e) {
      $('factorList').innerHTML = `<div class="empty-hint">加载失败：${e.message}</div>`;
    }
  }

  function renderFactorList() {
    const wrap = $('factorList');
    const names = Object.keys(factors);
    if (!names.length) {
      wrap.innerHTML = '<div class="empty-hint">无已注册因子</div>';
      return;
    }
    wrap.innerHTML = names.map(name => {
      const f = factors[name];
      return `
        <div class="factor-item" data-name="${name}">
          <div>
            <span class="name">${name}</span>
            <span class="cat">${f.category}</span>
          </div>
          <div class="desc">${f.description}</div>
        </div>
      `;
    }).join('');

    wrap.querySelectorAll('.factor-item').forEach(el => {
      el.addEventListener('click', () => {
        wrap.querySelectorAll('.factor-item').forEach(x => x.classList.remove('active'));
        el.classList.add('active');
        selected = el.dataset.name;
        setStatus(`已选: ${selected}`);
      });
    });
  }

  // ── IC 分析 ──────────────────────────────────────────────────────────────

  async function analyze() {
    if (!selected) {
      toast('请先选择一个因子', 'error');
      return;
    }
    const params = new URLSearchParams({
      start_date: $('startDate').value,
      end_date: $('endDate').value,
      horizon: $('horizon').value,
      method: $('method').value,
    });

    setStatus('分析中…');
    $('analyzeBtn').disabled = true;
    try {
      const res = await fetch(`/api/factors/${selected}/ic?${params}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      renderResult(data);
      setStatus(`完成 (n_periods=${data.summary.n_periods})`);
    } catch (e) {
      toast(`分析失败: ${e.message}`, 'error');
      setStatus('');
    } finally {
      $('analyzeBtn').disabled = false;
    }
  }

  function renderResult(data) {
    $('emptyHint').style.display = 'none';
    $('resultZone').style.display = '';

    renderSummary(data.summary);
    // resultZone 刚从 display:none 切到显示，浏览器还没完成 layout，
    // 此刻 echarts.init 拿到的容器尺寸是 0×0，必须等下一帧让 layout
    // 跑完才 init / setOption，否则要切换浏览器 tab 触发 visibility
    // 变化才会重绘
    requestAnimationFrame(() => {
      renderICChart(data.series, data.horizon);
      renderHeatmap(data.monthly_heatmap);
    });
  }

  function renderSummary(s) {
    const cards = [
      { label: 'IC 均值', val: fmt(s.ic_mean), cls: gradeIC(s.ic_mean) },
      { label: 'IC 标准差', val: fmt(s.ic_std), cls: '' },
      { label: 'ICIR', val: fmt(s.icir, 3), cls: gradeICIR(s.icir) },
      { label: 't-stat', val: fmt(s.t_stat, 2), cls: gradeT(s.t_stat) },
      { label: 'Hit Rate', val: s.hit_rate != null ? s.hit_rate + '%' : '--', cls: '' },
      { label: '观测期数', val: s.n_periods, cls: '' },
    ];
    $('summaryCards').innerHTML = cards.map(c => `
      <div class="stat-card ${c.cls}">
        <div class="label">${c.label}</div>
        <div class="val">${c.val}</div>
      </div>
    `).join('');
  }

  function fmt(v, digits = 4) {
    if (v == null || isNaN(v)) return '--';
    return Number(v).toFixed(digits);
  }
  function gradeIC(v) {
    if (v == null || isNaN(v)) return '';
    const a = Math.abs(v);
    if (a >= 0.05) return 'good';
    if (a >= 0.03) return 'warn';
    return 'bad';
  }
  function gradeICIR(v) {
    if (v == null || isNaN(v)) return '';
    const a = Math.abs(v);
    if (a >= 1.0) return 'good';
    if (a >= 0.5) return 'warn';
    return 'bad';
  }
  function gradeT(v) {
    if (v == null || isNaN(v)) return '';
    const a = Math.abs(v);
    if (a >= 2) return 'good';
    if (a >= 1.5) return 'warn';
    return 'bad';
  }

  // ── ECharts: IC 时间序列 ─────────────────────────────────────────────────

  function renderICChart(series, horizon) {
    if (!icChart) icChart = echarts.init($('icChart'));
    const dates = series.map(s => s.date);
    const vals = series.map(s => s.ic);

    icChart.setOption({
      tooltip: {
        trigger: 'axis',
        formatter: (params) => {
          const p = params[0];
          return `${p.axisValue}<br/>IC: <b>${p.value.toFixed(4)}</b>`;
        },
      },
      grid: { left: 50, right: 16, top: 24, bottom: 32 },
      xAxis: {
        type: 'category', data: dates,
        axisLabel: { fontSize: 10 },
      },
      yAxis: {
        type: 'value',
        axisLabel: { fontSize: 10 },
        splitLine: { lineStyle: { type: 'dashed', color: '#e0e0e0' } },
      },
      series: [{
        type: 'line', data: vals,
        symbol: 'none', smooth: true,
        lineStyle: { width: 1.5, color: '#0969da' },
        areaStyle: { color: 'rgba(9,105,218,.12)' },
        markLine: {
          symbol: 'none', lineStyle: { color: '#cf222e', type: 'dashed', width: 1 },
          data: [{ yAxis: 0, label: { show: false } }],
        },
      }],
      title: {
        text: `Horizon = ${horizon} 个交易日`,
        textStyle: { fontSize: 11, color: '#57606a', fontWeight: 'normal' },
        right: 16, top: 6,
      },
    }, true);
  }

  // ── ECharts: 月度热图 ────────────────────────────────────────────────────

  function renderHeatmap(monthlyMap) {
    if (!heatmapChart) heatmapChart = echarts.init($('heatmapChart'));

    const years = Object.keys(monthlyMap).sort();
    if (!years.length) {
      heatmapChart.clear();
      heatmapChart.setOption({
        title: { text: '无足够数据生成热图', left: 'center', top: 'center',
                 textStyle: { color: '#57606a', fontSize: 13 } },
      });
      return;
    }
    const months = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12'];
    const data = [];
    let vmin = 0, vmax = 0;
    years.forEach((y, yi) => {
      months.forEach((m, mi) => {
        const v = monthlyMap[y][m];
        if (v != null) {
          data.push([mi, yi, v]);
          if (v < vmin) vmin = v;
          if (v > vmax) vmax = v;
        }
      });
    });
    // 对称化色带（围绕 0 居中）
    const absMax = Math.max(Math.abs(vmin), Math.abs(vmax), 0.01);

    heatmapChart.setOption({
      tooltip: {
        formatter: (p) => {
          const y = years[p.value[1]];
          const m = months[p.value[0]];
          return `${y}-${m}: <b>${p.value[2].toFixed(4)}</b>`;
        },
      },
      grid: { left: 60, right: 24, top: 36, bottom: 30 },
      xAxis: {
        type: 'category', data: months.map(m => m + '月'),
        splitArea: { show: true },
        axisLabel: { fontSize: 10 },
      },
      yAxis: {
        type: 'category', data: years,
        splitArea: { show: true },
        axisLabel: { fontSize: 10 },
      },
      visualMap: {
        min: -absMax, max: absMax,
        calculable: true, orient: 'horizontal',
        right: 0, top: 0,
        inRange: { color: ['#1a7f37', '#ffffff', '#cf222e'] },
        textStyle: { fontSize: 10 },
        precision: 3,
      },
      series: [{
        type: 'heatmap',
        data: data,
        label: { show: true, fontSize: 10, formatter: (p) => p.value[2].toFixed(2) },
        emphasis: { itemStyle: { shadowBlur: 6, shadowColor: 'rgba(0,0,0,.25)' } },
      }],
    }, true);
  }

  // ── 计算并入库 ──────────────────────────────────────────────────────────

  async function compute() {
    if (!selected) { toast('请先选择一个因子', 'error'); return; }
    const body = {
      start_date: $('startDate').value,
      end_date: $('endDate').value,
    };
    setStatus('计算中（可能需要 30s+）…');
    $('computeBtn').disabled = true;
    try {
      const res = await fetch(`/api/factors/${selected}/compute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      toast(`入库 ${data.rows_written} 条`, 'success');
      setStatus(`入库 ${data.rows_written} 条`);
    } catch (e) {
      toast(`计算失败: ${e.message}`, 'error');
      setStatus('');
    } finally {
      $('computeBtn').disabled = false;
    }
  }

  // ── Init ────────────────────────────────────────────────────────────────

  $('analyzeBtn').addEventListener('click', analyze);
  $('computeBtn').addEventListener('click', compute);
  window.addEventListener('resize', () => {
    icChart && icChart.resize();
    heatmapChart && heatmapChart.resize();
  });

  loadFactors();
})();
