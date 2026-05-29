// Walk-Forward 寻优页：动态参数网格 + 散点 + 稳定性 + 窗口表
(() => {
  const $ = (id) => document.getElementById(id);

  // 默认 end_date = 今天
  $('endDate').value = new Date().toISOString().slice(0, 10);

  let strategies = [];   // 单股策略列表（来自 /api/strategies）
  let curStrategy = null;
  let scatter = null;

  // ── Utils ────────────────────────────────────────────────────────────────

  function toast(msg, type = '') {
    const el = $('toast');
    el.className = `toast show ${type}`;
    el.textContent = msg;
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove('show'), 2400);
  }

  function setStatus(msg) { $('statusText').textContent = msg; }

  function parseCsvNumbers(s) {
    return (s || '').split(/[,\s]+/).map(x => x.trim()).filter(Boolean)
      .map(x => {
        const n = Number(x);
        return Number.isFinite(n) ? n : null;
      })
      .filter(x => x !== null);
  }

  // ── 策略列表 ─────────────────────────────────────────────────────────────

  async function loadStrategies() {
    try {
      const res = await fetch('/api/strategies');
      const data = await res.json();
      // 只保留 signal 型（单股），portfolio 不在 walk_forward API 支持
      strategies = (Array.isArray(data) ? data : data.strategies || [])
        .filter(s => s.strategy_type === 'signal' || s.params);
      const sel = $('strategy');
      sel.innerHTML = strategies.map(s =>
        `<option value="${s.id}">${s.name || s.id}</option>`
      ).join('');
      sel.addEventListener('change', () => onStrategyChange(sel.value));
      if (strategies.length) onStrategyChange(strategies[0].id);
    } catch (e) {
      toast(`加载策略失败: ${e.message}`, 'error');
    }
  }

  function onStrategyChange(id) {
    curStrategy = strategies.find(s => s.id === id);
    if (!curStrategy) return;
    renderParamGrid();
  }

  // ── 参数网格（动态） ─────────────────────────────────────────────────────

  function renderParamGrid() {
    const wrap = $('paramGrid');
    const params = curStrategy.params || {};
    const keys = Object.keys(params);
    if (!keys.length) {
      wrap.innerHTML = '<div style="font-size:12px;color:var(--txt2);">该策略无可调参数</div>';
      updateComboCount();
      return;
    }
    wrap.innerHTML = keys.map(key => {
      const meta = params[key];
      const def = meta.default;
      const suggested = suggestValues(def, meta.min, meta.max);
      return `
        <div class="param-row" data-key="${key}">
          <span class="param-name">${meta.description || key}</span>
          <input type="text" class="param-values" value="${suggested.join(',')}"
                 placeholder="逗号分隔">
          <button class="del-btn" title="该参数固定为默认值" data-action="reset">↺</button>
        </div>
      `;
    }).join('');

    wrap.querySelectorAll('.param-values').forEach(el => {
      el.addEventListener('input', updateComboCount);
    });
    wrap.querySelectorAll('.del-btn').forEach(el => {
      el.addEventListener('click', (e) => {
        const row = e.target.closest('.param-row');
        const key = row.dataset.key;
        const meta = (curStrategy.params || {})[key];
        row.querySelector('.param-values').value = String(meta.default);
        updateComboCount();
      });
    });
    updateComboCount();
  }

  function suggestValues(def, mn, mx) {
    // 围绕默认值给 3-4 个候选：default、default*0.5、default*1.5、default*2
    if (typeof def !== 'number') return [def];
    const candidates = [def, Math.round(def * 0.5), Math.round(def * 1.5), Math.round(def * 2)];
    const filt = candidates.filter((v, i, arr) =>
      v > 0 && v >= (mn ?? 0) && v <= (mx ?? Infinity) && arr.indexOf(v) === i
    );
    filt.sort((a, b) => a - b);
    return filt;
  }

  function collectParamGrid() {
    const grid = {};
    $('paramGrid').querySelectorAll('.param-row').forEach(row => {
      const key = row.dataset.key;
      const vals = parseCsvNumbers(row.querySelector('.param-values').value);
      if (vals.length) grid[key] = vals;
    });
    return grid;
  }

  function updateComboCount() {
    const grid = collectParamGrid();
    let n = 1;
    Object.values(grid).forEach(arr => { n *= Math.max(arr.length, 1); });
    const el = $('comboCount');
    el.textContent = `(${n} 个组合)`;
    if (n > 100) {
      el.style.color = 'var(--warn)';
      el.style.fontWeight = '600';
    } else {
      el.style.color = 'var(--txt2)';
      el.style.fontWeight = '400';
    }
  }

  // ── 提交运行 ─────────────────────────────────────────────────────────────

  async function runWF() {
    const code = $('code').value.trim();
    if (!code) { toast('请输入股票代码', 'error'); return; }
    if (!curStrategy) { toast('请选择策略', 'error'); return; }
    const grid = collectParamGrid();
    if (!Object.keys(grid).length) {
      toast('参数网格为空', 'error'); return;
    }

    const body = {
      code,
      strategy_id: curStrategy.id,
      param_grid: grid,
      start_date: $('startDate').value,
      end_date: $('endDate').value,
      is_days: parseInt($('isDays').value),
      oos_days: parseInt($('oosDays').value),
      objective: $('objective').value,
    };

    setStatus('运行中…（按窗口数和参数组合数可能 1-5 分钟）');
    $('runBtn').disabled = true;
    $('warnZone').innerHTML = '';

    try {
      const res = await fetch('/api/walk_forward', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);

      renderResult(data);
      setStatus(`完成 (${data.summary?.n_windows ?? 0} 个窗口)`);
    } catch (e) {
      toast(`执行失败: ${e.message}`, 'error');
      setStatus('');
    } finally {
      $('runBtn').disabled = false;
    }
  }

  // ── 渲染结果 ─────────────────────────────────────────────────────────────

  function renderResult(data) {
    if ((data.warnings || []).length) {
      $('warnZone').innerHTML = data.warnings.map(w =>
        `<div class="warn-banner">⚠️ ${w}</div>`
      ).join('');
    }
    $('emptyHint').style.display = 'none';
    $('resultZone').style.display = '';

    renderSummary(data.summary);
    renderScatter(data.windows, data.summary?.objective);
    renderStability(data.param_stability);
    renderWindowTable(data.windows);
  }

  function renderSummary(s) {
    if (!s) { $('summaryCards').innerHTML = ''; return; }
    const cards = [
      { label: '窗口数', val: s.n_windows },
      { label: '选优指标', val: s.objective },
      { label: 'IS 均值', val: fmt(s.is_avg_metric), cls: gradeMetric(s.is_avg_metric) },
      { label: 'OOS 均值', val: fmt(s.oos_avg_metric), cls: gradeMetric(s.oos_avg_metric) },
      { label: 'OOS/IS 衰减率', val: fmt(s.decay_ratio, 3), cls: gradeDecay(s.decay_ratio) },
      { label: 'OOS 累计收益', val: (s.oos_cumulative_return_pct ?? '--') + '%',
        cls: gradeMetric(s.oos_cumulative_return_pct) },
      { label: '参数组合', val: s.n_param_combos,
        cls: s.n_param_combos > 100 ? 'warn' : '' },
    ];
    $('summaryCards').innerHTML = cards.map(c => `
      <div class="stat-card ${c.cls || ''}">
        <div class="label">${c.label}</div>
        <div class="val">${c.val}</div>
      </div>
    `).join('');
  }

  function fmt(v, d = 2) {
    if (v == null || isNaN(v)) return '--';
    return Number(v).toFixed(d);
  }
  function gradeMetric(v) {
    if (v == null || isNaN(v)) return '';
    if (v > 0) return 'good';
    if (v < 0) return 'bad';
    return '';
  }
  function gradeDecay(v) {
    if (v == null || isNaN(v)) return '';
    if (v >= 0.7) return 'good';
    if (v >= 0.3) return 'warn';
    return 'bad';
  }

  // 散点：x = IS metric, y = OOS metric
  function renderScatter(windows, objective) {
    if (!scatter) scatter = echarts.init($('scatterChart'));
    const points = (windows || []).map((w, i) => ({
      value: [w.is_metric, w.oos_metric],
      label_: `窗口 ${i + 1}: ${w.is_start} ~ ${w.oos_end}<br/>` +
              `参数: ${JSON.stringify(w.best_params)}<br/>` +
              `IS=${w.is_metric}, OOS=${w.oos_metric}<br/>` +
              `OOS 收益: ${w.oos_return}%`,
    }));
    const allVals = points.flatMap(p => p.value);
    const lo = Math.min(...allVals, 0);
    const hi = Math.max(...allVals, 0);

    scatter.setOption({
      tooltip: {
        formatter: (p) => p.data.label_,
      },
      grid: { left: 50, right: 16, top: 24, bottom: 40 },
      xAxis: {
        name: `IS ${objective || ''}`, type: 'value',
        nameLocation: 'middle', nameGap: 28,
        axisLabel: { fontSize: 10 },
        splitLine: { lineStyle: { type: 'dashed', color: '#e0e0e0' } },
      },
      yAxis: {
        name: `OOS ${objective || ''}`, type: 'value',
        nameLocation: 'middle', nameGap: 36,
        axisLabel: { fontSize: 10 },
        splitLine: { lineStyle: { type: 'dashed', color: '#e0e0e0' } },
      },
      series: [
        // 散点
        {
          type: 'scatter', data: points, symbolSize: 12,
          itemStyle: { color: '#0969da', opacity: 0.7 },
          emphasis: { itemStyle: { color: '#cf222e', borderColor: '#cf222e' } },
        },
        // y = x 对角线（理想情况）
        {
          type: 'line', symbol: 'none',
          data: [[lo, lo], [hi, hi]],
          lineStyle: { type: 'dashed', color: '#1a7f37', width: 1 },
          tooltip: { show: false },
        },
      ],
    }, true);
  }

  function renderStability(stab) {
    const div = $('paramStability');
    const keys = Object.keys(stab || {});
    if (!keys.length) { div.innerHTML = '<div class="empty-hint">无</div>'; return; }
    const tables = keys.map(k => {
      const counts = stab[k];
      const total = Object.values(counts).reduce((a, b) => a + b, 0);
      const rows = Object.entries(counts)
        .sort((a, b) => b[1] - a[1])
        .map(([v, n]) => {
          const pct = total ? n / total * 100 : 0;
          return `
            <tr>
              <td style="font-family:SFMono-Regular,Consolas,monospace;">${v}</td>
              <td>
                <span class="stab-bar" style="width:${Math.max(pct * 1.5, 4)}px;"></span>
                ${n} (${pct.toFixed(0)}%)
              </td>
            </tr>
          `;
        }).join('');
      return `
        <div style="margin-bottom:10px;">
          <div style="font-size:12px;font-weight:600;color:var(--txt);margin-bottom:4px;">${k}</div>
          <table class="stab-table">${rows}</table>
        </div>
      `;
    }).join('');
    div.innerHTML = tables;
  }

  function renderWindowTable(windows) {
    const tbl = $('windowTable');
    if (!windows || !windows.length) { tbl.innerHTML = ''; return; }
    const rows = windows.map((w, i) => `
      <tr>
        <td>${i + 1}</td>
        <td>${w.is_start} ~ ${w.is_end}</td>
        <td>${w.oos_start} ~ ${w.oos_end}</td>
        <td style="font-family:SFMono-Regular,Consolas,monospace;font-size:10px;">
          ${JSON.stringify(w.best_params).replace(/[{}"]/g, '')}
        </td>
        <td class="num">${w.is_metric}</td>
        <td class="num">${w.oos_metric}</td>
        <td class="num ${w.oos_return >= 0 ? 'pos' : 'neg'}">${w.oos_return}%</td>
        <td class="num neg">${w.oos_max_dd}%</td>
      </tr>
    `).join('');
    tbl.innerHTML = `
      <thead>
        <tr>
          <th>#</th><th>IS 区间</th><th>OOS 区间</th><th>最优参数</th>
          <th>IS</th><th>OOS</th><th>OOS 收益</th><th>OOS 回撤</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    `;
  }

  // ── Init ────────────────────────────────────────────────────────────────

  $('runBtn').addEventListener('click', runWF);
  window.addEventListener('resize', () => scatter && scatter.resize());
  loadStrategies();
})();
