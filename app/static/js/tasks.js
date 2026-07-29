/* 定时任务监控页 */

const REFRESH_MS = 30_000;   // 任务状态几分钟才变一次，15s 轮询太激进（配合 N+1 修复前更明显）
let _timer = null;
let _adminInfo = { ip: '—', is_admin: false, whitelist_empty: false };
let _allRuns = [];       // 缓存当前过滤条件下的全量记录，详情弹窗直接复用，避免二次请求
let _allTasks = [];      // 缓存任务清单，给 schedule 解析 / 下次运行计算用

// ── 权限检测 ────────────────────────────────────────────────────────────────

async function loadAdminStatus() {
  try {
    const res = await fetch('/api/admin/ip/me');
    _adminInfo = await res.json();
  } catch (e) {
    console.error('admin status:', e);
  }
  renderAdminChip();
}

function renderAdminChip() {
  const chip = document.getElementById('adminChip');
  if (!chip) return;
  if (_adminInfo.is_admin) {
    chip.textContent = `✓ 管理员 (${_adminInfo.ip})`;
    chip.className = 'admin-chip admin-chip-admin';
    chip.title = '你的 IP 在白名单中，可手动触发任务';
  } else {
    chip.textContent = `○ 只读 (${_adminInfo.ip})`;
    chip.className = 'admin-chip admin-chip-guest';
    chip.title = _adminInfo.whitelist_empty
      ? '白名单为空。首次触发任务的 IP 将被自动加入。'
      : '你的 IP 不在白名单中，「立即重跑」按钮被禁用。请到实盘观察页通过管理弹窗添加。';
  }
}

// 给每个任务卡片上的「立即重跑」按钮加锁
function applyAdminGuards() {
  const lockTip = '你的 IP 不在白名单中，无法触发任务。请联系管理员添加你的 IP。';
  document.querySelectorAll('.rerun-btn').forEach(btn => {
    if (_adminInfo.is_admin) {
      btn.disabled = false;
      btn.removeAttribute('data-locked');
      btn.title = '';
    } else {
      btn.disabled = true;
      btn.setAttribute('data-locked', '1');
      btn.title = lockTip;
    }
  });
}

const STATUS_CN = {
  success: '成功', failed: '失败', timeout: '超时',
  running: '运行中', skipped: '跳过',
};

function fmtDt(s) {
  if (!s) return '—';
  // 接受 ISO 字符串，输出 'MM-DD HH:MM:SS'
  const d = new Date(s);
  if (isNaN(d.getTime())) return s;
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtDur(ms) {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms/1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return `${m}m${s}s`;
}
function statusPill(status) {
  const cls = `st-${status || 'none'}`;
  return `<span class="status-pill ${cls}">${STATUS_CN[status] || status || '未运行'}</span>`;
}

// 解析后端 schedule 字符串（"weekday:HH:MM" / "daily:HH:MM"）算下一次预计运行
// 返回 { dt: Date, label: '今天 17:30' / '明天 17:00' / '周三 17:30' }
function nextRunOf(schedule) {
  if (!schedule || typeof schedule !== 'string') return null;
  const m = schedule.match(/^(weekday|daily):(\d{1,2}):(\d{1,2})$/);
  if (!m) return null;
  const [, kind, hh, mm] = m;
  const h = parseInt(hh, 10), mi = parseInt(mm, 10);
  const now = new Date();
  // 从今天开始往后找最多 8 天，命中第一个合法日（满足 weekday 限制 + 时间未过）
  for (let i = 0; i < 8; i++) {
    const d = new Date(now);
    d.setDate(now.getDate() + i);
    d.setHours(h, mi, 0, 0);
    if (i === 0 && d <= now) continue;     // 今天但时刻已过
    const dow = d.getDay();                // 0=日 6=六
    if (kind === 'weekday' && (dow === 0 || dow === 6)) continue;
    // 算 label
    const today0 = new Date(now); today0.setHours(0, 0, 0, 0);
    const target0 = new Date(d);  target0.setHours(0, 0, 0, 0);
    const diffDays = Math.round((target0 - today0) / 86400000);
    const hm = `${String(h).padStart(2,'0')}:${String(mi).padStart(2,'0')}`;
    let label;
    if (diffDays === 0)      label = `今天 ${hm}`;
    else if (diffDays === 1) label = `明天 ${hm}`;
    else {
      const cnDow = ['日', '一', '二', '三', '四', '五', '六'];
      label = `周${cnDow[d.getDay()]} ${hm}`;
    }
    return { dt: d, label };
  }
  return null;
}

async function loadSummary() {
  try {
    const res = await fetch('/api/tasks/summary');
    const { tasks } = await res.json();
    _allTasks = tasks || [];
    renderSummary(_allTasks);
    populateTaskFilter(_allTasks);
    applyAdminGuards();   // renderSummary 重新生成按钮，需要重新加锁
  } catch (e) {
    console.error(e);
    document.getElementById('tasksGrid').innerHTML =
      `<div class="error-box">加载失败：${e.message || e}</div>`;
  }
  document.getElementById('updatedAt').textContent =
    `更新于 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`;
}

function renderSummary(tasks) {
  if (!tasks.length) {
    document.getElementById('tasksGrid').innerHTML =
      '<div class="no-data">还没有任务运行记录</div>';
    return;
  }
  document.getElementById('tasksGrid').innerHTML = tasks.map(t => {
    const sr = t.success_rate === null ? '—' : `${(t.success_rate * 100).toFixed(0)}%`;
    const ran = t.ran_today_success
      ? '<span class="ran-today ran-yes">今天已成功</span>'
      : '<span class="ran-today ran-no">今天未跑</span>';
    const errLine = t.last_status && t.last_status !== 'success' && t.last_error_msg
      ? `<div class="kv-line"><span>错误</span><span class="v" style="color:#cf222e;font-size:11px;max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(t.last_error_msg)}</span></div>`
      : '';
    // 下次预计运行：今天已成功则不显示（避免误导用户以为还会再跑一次）
    const nxt = t.ran_today_success ? null : nextRunOf(t.schedule);
    const nextLine = nxt
      ? `<div class="kv-line"><span>下次预计</span><span class="v" style="color:#0969da">${nxt.label}</span></div>`
      : '';
    return `
      <div class="task-card">
        <div class="name">${t.task_name} ${ran}</div>
        <div class="desc">${t.description || ''}</div>
        <div class="kv-line"><span>调度</span><span class="v">${t.schedule}${t.depends_on ? ` · 依赖 ${t.depends_on}` : ''}</span></div>
        ${nextLine}
        <div class="kv-line"><span>最近一次</span><span class="v">${fmtDt(t.last_started_at)} ${statusPill(t.last_status)}</span></div>
        <div class="kv-line"><span>耗时</span><span class="v">${fmtDur(t.last_duration_ms)}</span></div>
        <div class="kv-line"><span>近 30 天</span><span class="v">${t.recent_success}/${t.recent_total} 成功（${sr}）· 失败 ${t.recent_failed}</span></div>
        ${errLine}
        <button class="rerun-btn" data-name="${esc(t.task_name)}">▶ 立即重跑</button>
      </div>`;
  }).join('');
}

function populateTaskFilter(tasks) {
  const sel = document.getElementById('taskFilter');
  const current = sel.value;
  const opts = ['<option value="">全部任务</option>']
    .concat(tasks.map(t => `<option value="${t.task_name}">${t.task_name}</option>`));
  sel.innerHTML = opts.join('');
  if (current) sel.value = current;
}

const RUNS_PAGE = 30;    // 每页条数
let _runsOffset = 0;     // 已拉取的偏移
let _runsHasMore = false;

// 分页拉取：reset=true 换过滤条件从头拉；reset=false 是"加载更多"追加下一页。
// task/status 均为服务端过滤,offset 分页 —— 不再一次性拉全量。
async function loadRuns(reset = true) {
  const task = document.getElementById('taskFilter').value;
  const status = document.getElementById('statusFilter')?.value || '';
  if (reset) {
    _allRuns = [];
    _runsOffset = 0;
    _runsHasMore = false;
    document.getElementById('runsWrap').innerHTML = '<div class="no-data">加载中…</div>';
  }
  const params = new URLSearchParams({ limit: RUNS_PAGE, offset: _runsOffset });
  if (task) params.set('task', task);
  if (status) params.set('status', status);
  try {
    const res = await fetch('/api/tasks/runs?' + params.toString());
    const { runs, has_more } = await res.json();
    _allRuns = _allRuns.concat(runs || []);
    _runsOffset += (runs || []).length;
    _runsHasMore = !!has_more;
    renderRuns(_allRuns);
  } catch (e) {
    if (reset) {
      document.getElementById('runsWrap').innerHTML =
        `<div class="error-box">加载失败：${e.message || e}</div>`;
    }
  }
}

// 兼容旧调用点(状态过滤已移到服务端 → 重新从头分页拉取)
function renderRunsFiltered() { loadRuns(true); }
window.renderRunsFiltered = renderRunsFiltered;

function renderRuns(runs) {
  const hint = document.getElementById('runsHint');
  if (hint) {
    hint.textContent = _allRuns.length
      ? `（已加载 ${_allRuns.length} 条${_runsHasMore ? '，可加载更多' : '，已全部'}）`
      : '';
  }
  if (!runs.length) {
    document.getElementById('runsWrap').innerHTML = '<div class="no-data">无符合条件的记录</div>';
    return;
  }
  const rows = runs.map(r => {
    const trig = r.trigger_type === 'manual'
      ? '<span class="tag-stop">手动</span>'
      : '<span class="tag-hold">cron</span>';
    return `
      <tr class="row-link" data-id="${r.id}">
        <td>${fmtDt(r.started_at)}</td>
        <td>${r.task_name}</td>
        <td>${statusPill(r.status)}</td>
        <td>${trig}</td>
        <td>${fmtDur(r.duration_ms)}</td>
        <td>${r.exit_code === null ? '—' : r.exit_code}</td>
        <td style="color:#57606a;font-size:12px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
          ${esc(r.error_msg || (r.stdout_tail || '').split('\n').slice(-1)[0] || '')}
        </td>
      </tr>`;
  }).join('');
  const moreBtn = _runsHasMore
    ? `<div style="text-align:center;margin-top:12px;">
         <button class="ds-refresh-btn" id="loadMoreRunsBtn">加载更多</button>
       </div>`
    : '';
  document.getElementById('runsWrap').innerHTML = `
    <table class="pt-tbl">
      <thead><tr>
        <th>开始时间</th><th>任务</th><th>状态</th><th>触发</th>
        <th>耗时</th><th>退出码</th><th>摘要（点击行看详情）</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>${moreBtn}`;
}

function showDetail(id) {
  const modal = document.getElementById('runDetailModal');
  modal.style.display = '';
  // 直接从已缓存的 _allRuns 找；列表里看到的所有 id 这里都有
  const r = _allRuns.find(x => x.id === id);
  if (!r) {
    document.getElementById('modalContent').innerHTML =
      `<div class="error-box">记录不在当前列表中（可能已被刷新移除），请重试。</div>`;
    return;
  }
  document.getElementById('modalTitle').textContent =
    `${r.task_name} · ${fmtDt(r.started_at)} · ${STATUS_CN[r.status] || r.status}`;
  document.getElementById('modalContent').innerHTML = `
    <div class="kv-grid">
      <div class="kv-item"><div class="kv-label">触发类型</div><div class="kv-val">${r.trigger_type}</div></div>
      <div class="kv-item"><div class="kv-label">计划时刻</div><div class="kv-val">${fmtDt(r.scheduled_at)}</div></div>
      <div class="kv-item"><div class="kv-label">开始</div><div class="kv-val">${fmtDt(r.started_at)}</div></div>
      <div class="kv-item"><div class="kv-label">结束</div><div class="kv-val">${fmtDt(r.finished_at)}</div></div>
      <div class="kv-item"><div class="kv-label">耗时</div><div class="kv-val">${fmtDur(r.duration_ms)}</div></div>
      <div class="kv-item"><div class="kv-label">退出码</div><div class="kv-val">${r.exit_code === null ? '—' : r.exit_code}</div></div>
      <div class="kv-item"><div class="kv-label">主机</div><div class="kv-val">${r.host || '—'}</div></div>
    </div>
    ${r.error_msg ? `<h4 style="margin:14px 0 6px;color:#cf222e">错误</h4><pre class="tail-pre">${esc(r.error_msg)}</pre>` : ''}
    ${r.stdout_tail ? `<h4 style="margin:14px 0 6px">stdout（末尾）</h4><pre class="tail-pre">${esc(r.stdout_tail)}</pre>` : ''}
    ${r.stderr_tail ? `<h4 style="margin:14px 0 6px">stderr（末尾）</h4><pre class="tail-pre">${esc(r.stderr_tail)}</pre>` : ''}
  `;
}

function closeDetail() {
  document.getElementById('runDetailModal').style.display = 'none';
}

async function triggerRun(name, btn) {
  if (!_adminInfo.is_admin) {
    alert('你的 IP 不在白名单中，无法触发任务。');
    return;
  }
  if (!confirm(`确认要立即重跑 ${name} 吗？`)) return;
  btn.disabled = true;
  btn.textContent = '提交中…';
  try {
    const res = await fetch(`/api/tasks/${encodeURIComponent(name)}/run`, { method: 'POST' });
    if (res.status === 403) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.detail || 'IP 不在白名单');
    }
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();

    if (data.status === 'skipped') {
      // 依赖未满足等前置原因 —— 立即告诉用户为什么没跑
      alert(`未执行：${data.reason}`);
      btn.disabled = false;
      btn.textContent = '▶ 立即重跑';
      loadSummary(); loadRuns();   // 把这条 skipped 拉出来给用户看
      return;
    }

    // 真的丢线程池跑了，等几秒再刷
    btn.textContent = '运行中…';
    setTimeout(() => {
      loadSummary(); loadRuns();
      btn.disabled = false; btn.textContent = '▶ 立即重跑';
    }, 3000);
  } catch (e) {
    alert(`触发失败：${e.message || e}`);
    btn.disabled = false;
    btn.textContent = '▶ 立即重跑';
  }
}


// ── 今日数据入库详情 + 访问统计 ─────────────────────────────────────────────

function pct(actual, expected) {
  if (!expected || expected <= 0) return null;
  return Math.min(100, (actual / expected) * 100);
}

function gradeByPct(p) {
  if (p == null) return 'idle';
  if (p >= 99) return 'ok';
  if (p >= 60) return 'warn';
  return 'bad';
}

function statusLabel(grade) {
  return { ok: '完成', warn: '部分', bad: '缺失', idle: '—' }[grade] || '—';
}

async function loadDataStatus() {
  try {
    const res = await fetch('/api/data_status/today');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderDataStatus(data);
  } catch (e) {
    document.getElementById('dsGrid').innerHTML =
      `<div class="no-data">加载失败：${esc(e.message)}</div>`;
  }
}

function renderDataStatus(data) {
  // 顶部规则徽章
  const rule = document.getElementById('dsRule');
  if (data.is_today) {
    rule.textContent = `🟢 ${data.cutoff_hour}:00 后，查看当日 ${data.target_date}`;
    rule.className = 'ds-rule now-today';
  } else {
    rule.textContent = `🟡 ${data.cutoff_hour}:00 前，查看前一交易日 ${data.target_date}`;
    rule.className = 'ds-rule now-prev';
  }
  document.getElementById('dsTip').textContent = data.rule + '（按 IP 去重）';

  // 卡片网格
  const grid = document.getElementById('dsGrid');
  if (!data.items || !data.items.length) {
    grid.innerHTML = '<div class="no-data">无数据</div>';
    return;
  }
  grid.innerHTML = data.items.map(it => {
    const p = pct(it.actual, it.expected);
    const grade = gradeByPct(p);
    const pctText = p == null ? '—' : `${p.toFixed(0)}%`;
    const widthStyle = p == null ? '0%' : `${p}%`;
    const expectedText = it.expected == null ? 'N/A' : Number(it.expected).toLocaleString();
    const actualText = Number(it.actual).toLocaleString();
    const missingHTML = (it.missing > 0)
      ? `<span class="ds-missing">缺 ${Number(it.missing).toLocaleString()}</span>` : '';
    return `
      <div class="ds-card">
        <div class="ds-card-head">
          <span class="ds-icon">${it.icon || '📊'}</span>
          <span class="ds-name">${esc(it.name)}</span>
          <span class="ds-status ${grade}">${statusLabel(grade)}</span>
        </div>
        <div class="ds-numbers">
          <span class="ds-actual">${actualText}</span>
          <span class="ds-slash">/</span>
          <span class="ds-expected">${expectedText}</span>
          <span class="ds-unit">${esc(it.unit || '')}</span>
          ${missingHTML}
        </div>
        <div class="ds-bar">
          <div class="ds-bar-fill ${grade}" style="width:${widthStyle};"></div>
        </div>
        <div class="ds-note">${esc(it.note || '')} · ${pctText}</div>
      </div>
    `;
  }).join('');
}

// ── 转化漏斗 ────────────────────────────────────────────────────────────────
// 数据来自自建的 user_event_log + user_visit_log,不接第三方分析。
// 每层显示绝对数 + 相对上一层的转化率;上一层为 0 时显示"—"而不是 0%,
// "没有上游"和"上游全流失"不是一回事。
async function loadFunnel() {
  const row = document.getElementById('funnelRow');
  const days = document.getElementById('funnelRange').value;
  try {
    const res = await fetch(`/api/analytics/funnel?days=${encodeURIComponent(days)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderFunnel(await res.json());
  } catch (e) {
    row.innerHTML = `<div class="no-data">加载失败：${esc(e.message)}</div>`;
    document.getElementById('funnelChannels').innerHTML = '';
  }
}

function renderFunnel(data) {
  const steps = data.steps || [];
  document.getElementById('funnelRow').innerHTML = steps.map(s => `
    <div class="funnel-step">
      <div class="funnel-step-label">${esc(s.label)}</div>
      <div class="funnel-step-val">${Number(s.count || 0).toLocaleString()}</div>
      <div class="funnel-step-rate">${s.rate == null ? '—' : s.rate + '%'}</div>
    </div>
  `).join('<div class="funnel-arrow">→</div>');

  const ch = data.channels || [];
  const wrap = document.getElementById('funnelChannels');
  if (!ch.length) {
    wrap.innerHTML = '<div class="no-data">该区间还没有带渠道标记的事件。' +
      '推广链接加上 ?utm_source=xxx 即可归因。</div>';
    return;
  }
  wrap.innerHTML =
    '<div class="funnel-ch-title">渠道拆分</div>' +
    '<div class="metrics-scroll"><table class="metrics-tbl"><thead><tr>' +
    '<th style="text-align:left">来源</th><th style="text-align:left">事件</th><th>次数</th>' +
    '</tr></thead><tbody>' +
    ch.map(r => `<tr><td class="row-label">${esc(r.source)}</td>` +
      `<td>${esc(r.event)}</td><td>${Number(r.n).toLocaleString()}</td></tr>`).join('') +
    '</tbody></table></div>';
}

async function loadTraffic() {
  try {
    const res = await fetch('/api/data_status/traffic_today');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderTraffic(data);
  } catch (e) {
    document.getElementById('trafficPv').textContent = '—';
    document.getElementById('trafficUv').textContent = '—';
    document.getElementById('trafficDetail').innerHTML =
      `<div>加载失败：${esc(e.message)}</div>`;
  }
}

function renderTraffic(data) {
  document.getElementById('trafficPv').textContent = Number(data.pv || 0).toLocaleString();
  document.getElementById('trafficUv').textContent = Number(data.uv || 0).toLocaleString();
  document.getElementById('trafficPvSub').textContent =
    `${data.date} · 不含 favicon / 静态资源`;
  const uvAll = Number(data.uv_all || 0);
  const uv = Number(data.uv || 0);
  const inner = Math.max(uvAll - uv, 0);
  document.getElementById('trafficUvSub').textContent =
    `按 IP 去重${inner > 0 ? `（另含 ${inner} 个内网/Unknown）` : ''}`;

  // 24h 趋势 —— 用 Unicode block 文字图 + 时间轴，去掉次要的 路径/地区 行
  const byHour = data.by_hour || [];
  const peakHour = byHour.indexOf(Math.max(...byHour, 0));
  const peakN = byHour[peakHour] || 0;
  const max = Math.max(...byHour, 1);
  const bars = '▁▂▃▄▅▆▇█';
  // 每小时一个 .hour-cell span，data-tip 给 CSS hover tooltip
  const trendLine = byHour.map((n, h) => {
    const idx = Math.min(bars.length - 1, Math.floor((n / max) * (bars.length - 1)));
    const ch = n > 0 ? bars[idx] : '·';
    const label = String(h).padStart(2, '0');
    return `<span class="hour-cell" data-tip="${label}:00 · ${n} 次">${ch}</span>`;
  }).join('');

  // 时间刻度：每 6 小时一个标签 (0/6/12/18)
  const hours = [0, 6, 12, 18];
  const axisLine = Array.from({ length: 24 }, (_, h) => {
    return hours.includes(h) ? String(h).padStart(2, '0') : '  ';
  }).join(' ');

  document.getElementById('trafficDetail').innerHTML = `
    <div style="font-size:12px;color:var(--txt2);margin-bottom:6px;">
      📊 今日 24h 访问趋势
      ${peakN > 0 ? `<span style="margin-left:10px;color:var(--accent);font-weight:600;">峰值 ${peakHour}:00 · ${peakN}</span>` : ''}
      <span style="margin-left:8px;font-size:10px;">悬停柱体看具体小时</span>
    </div>
    <div style="font-family:SFMono-Regular,Consolas,Liberation Mono,Menlo,monospace;
                font-size:22px;letter-spacing:5px;line-height:1.1;color:var(--accent);">
      ${trendLine}
    </div>
    <div style="font-family:SFMono-Regular,Consolas,Liberation Mono,Menlo,monospace;
                font-size:10px;letter-spacing:5px;color:var(--txt2);margin-top:2px;">
      ${axisLine}
    </div>
  `;
}

async function triggerDailyRefresh() {
  const btn = document.getElementById('dsRefreshAll');
  if (!_adminInfo.is_admin) {
    alert('IP 不在白名单，无权限触发。请到「实盘观察」页通过管理弹窗加白名单。');
    return;
  }
  if (!confirm('将触发 daily_update 全量补齐（K 线 / stock_info / 因子等），约 15-30 分钟。确定？')) return;

  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = '⏳ 触发中…';
  try {
    const res = await fetch('/api/tasks/daily_update/run', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    btn.textContent = data.status === 'queued' ? '✓ 已入队' : `${data.status}`;
    setTimeout(() => {
      btn.textContent = orig; btn.disabled = false;
      loadDataStatus(); loadSummary(); loadRuns();
    }, 2400);
  } catch (e) {
    alert(`触发失败：${e.message}`);
    btn.textContent = orig; btn.disabled = false;
  }
}

// ── 启动 ────────────────────────────────────────────────────────────────────

// 非管理员直接访问 /tasks:整页挡掉，不拉任何数据、不起轮询。
// 运维页会暴露内部任务名与执行日志(stdout/stderr/报错细节)，不对外。
function renderNoPermission() {
  const main = document.querySelector('main');
  if (!main) return;
  main.innerHTML = `
    <div class="panel">
      <div class="panel-title">无访问权限</div>
      <div class="no-data" style="padding:32px 0;text-align:center;line-height:1.9;">
        「定时任务」是运维页面，仅限管理员访问。<br>
        <span style="font-size:12px;color:#8a929c;">你的 IP：${esc(_adminInfo.ip || '—')}</span>
      </div>
    </div>`;
}

async function load() {
  // 权限先拉，避免按钮一闪一锁
  await loadAdminStatus();
  // 白名单为空时放行(全新部署的自举场景，见 admin_ip 模块说明)
  if (!_adminInfo.is_admin && !_adminInfo.whitelist_empty) {
    renderNoPermission();
    return;
  }
  loadSummary();
  loadRuns();
  loadDataStatus();
  loadTraffic();
  loadFunnel();
  document.getElementById('funnelRange').addEventListener('change', loadFunnel);
  document.getElementById('dsRefreshAll').addEventListener('click', triggerDailyRefresh);
  // 包一层箭头函数：直接传 loadRuns 会把 Event 对象当成 reset 参数传进去
  document.getElementById('taskFilter').addEventListener('change', () => loadRuns());
  document.getElementById('statusFilter').addEventListener('change', () => loadRuns());
  document.getElementById('runDetailModalClose').addEventListener('click', closeDetail);

  // ── 动态渲染内容的事件委托(容器本身是静态的，内容随任务/运行记录重绘)────
  document.getElementById('tasksGrid').addEventListener('click', e => {
    const btn = e.target.closest('.rerun-btn');
    if (btn) triggerRun(btn.dataset.name, btn);
  });
  document.getElementById('runsWrap').addEventListener('click', e => {
    const moreBtn = e.target.closest('#loadMoreRunsBtn');
    if (moreBtn) { loadRuns(false); return; }
    const row = e.target.closest('.row-link');
    if (row) showDetail(+row.dataset.id);
  });

  if (_timer) clearInterval(_timer);
  _timer = setInterval(() => {
    loadSummary(); loadRuns();
    loadDataStatus(); loadTraffic();
  }, REFRESH_MS);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (_timer) { clearInterval(_timer); _timer = null; }
    } else if (!_timer) {
      loadSummary(); loadRuns(); loadDataStatus(); loadTraffic();
      _timer = setInterval(() => {
        loadSummary(); loadRuns();
        loadDataStatus(); loadTraffic();
      }, REFRESH_MS);
    }
  });
}

window.triggerRun = triggerRun;
window.showDetail = showDetail;
window.closeDetail = closeDetail;
window.loadRuns = loadRuns;
load();
