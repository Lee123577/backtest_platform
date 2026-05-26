/* 定时任务监控页 */

const REFRESH_MS = 15_000;
let _timer = null;

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

async function loadSummary() {
  try {
    const res = await fetch('/api/tasks/summary');
    const { tasks } = await res.json();
    renderSummary(tasks || []);
    populateTaskFilter(tasks || []);
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
      ? `<div class="kv-line"><span>错误</span><span class="v" style="color:#cf222e;font-size:11px;max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(t.last_error_msg)}</span></div>`
      : '';
    return `
      <div class="task-card">
        <div class="name">${t.task_name} ${ran}</div>
        <div class="desc">${t.description || ''}</div>
        <div class="kv-line"><span>调度</span><span class="v">${t.schedule}${t.depends_on ? ` · 依赖 ${t.depends_on}` : ''}</span></div>
        <div class="kv-line"><span>最近一次</span><span class="v">${fmtDt(t.last_started_at)} ${statusPill(t.last_status)}</span></div>
        <div class="kv-line"><span>耗时</span><span class="v">${fmtDur(t.last_duration_ms)}</span></div>
        <div class="kv-line"><span>近 30 天</span><span class="v">${t.recent_success}/${t.recent_total} 成功（${sr}）· 失败 ${t.recent_failed}</span></div>
        ${errLine}
        <button class="rerun-btn" onclick="triggerRun('${t.task_name}', this)">▶ 立即重跑</button>
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

async function loadRuns() {
  const task = document.getElementById('taskFilter').value;
  const url = `/api/tasks/runs?limit=100${task ? '&task=' + encodeURIComponent(task) : ''}`;
  try {
    const res = await fetch(url);
    const { runs } = await res.json();
    renderRuns(runs || []);
  } catch (e) {
    document.getElementById('runsWrap').innerHTML =
      `<div class="error-box">加载失败：${e.message || e}</div>`;
  }
}

function renderRuns(runs) {
  if (!runs.length) {
    document.getElementById('runsWrap').innerHTML = '<div class="no-data">暂无记录</div>';
    return;
  }
  const rows = runs.map(r => {
    const trig = r.trigger_type === 'manual'
      ? '<span class="tag-stop">手动</span>'
      : '<span class="tag-hold">cron</span>';
    return `
      <tr class="row-link" onclick="showDetail(${r.id})">
        <td>${fmtDt(r.started_at)}</td>
        <td>${r.task_name}</td>
        <td>${statusPill(r.status)}</td>
        <td>${trig}</td>
        <td>${fmtDur(r.duration_ms)}</td>
        <td>${r.exit_code === null ? '—' : r.exit_code}</td>
        <td style="color:#57606a;font-size:12px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
          ${escapeHtml(r.error_msg || (r.stdout_tail || '').split('\n').slice(-1)[0] || '')}
        </td>
      </tr>`;
  }).join('');
  document.getElementById('runsWrap').innerHTML = `
    <table class="pt-tbl">
      <thead><tr>
        <th>开始时间</th><th>任务</th><th>状态</th><th>触发</th>
        <th>耗时</th><th>退出码</th><th>摘要（点击行看详情）</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function showDetail(id) {
  const modal = document.getElementById('runDetailModal');
  modal.style.display = '';
  document.getElementById('modalContent').innerHTML = '加载中…';
  try {
    const res = await fetch(`/api/tasks/runs?limit=500`);
    const { runs } = await res.json();
    const r = (runs || []).find(x => x.id === id);
    if (!r) throw new Error('找不到运行记录');
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
      ${r.error_msg ? `<h4 style="margin:14px 0 6px;color:#cf222e">错误</h4><pre class="tail-pre">${escapeHtml(r.error_msg)}</pre>` : ''}
      ${r.stdout_tail ? `<h4 style="margin:14px 0 6px">stdout（末尾）</h4><pre class="tail-pre">${escapeHtml(r.stdout_tail)}</pre>` : ''}
      ${r.stderr_tail ? `<h4 style="margin:14px 0 6px">stderr（末尾）</h4><pre class="tail-pre">${escapeHtml(r.stderr_tail)}</pre>` : ''}
    `;
  } catch (e) {
    document.getElementById('modalContent').innerHTML =
      `<div class="error-box">加载失败：${e.message || e}</div>`;
  }
}

function closeDetail() {
  document.getElementById('runDetailModal').style.display = 'none';
}

async function triggerRun(name, btn) {
  if (!confirm(`确认要立即重跑 ${name} 吗？`)) return;
  btn.disabled = true;
  btn.textContent = '提交中…';
  try {
    const res = await fetch(`/api/tasks/${encodeURIComponent(name)}/run`, { method: 'POST' });
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

function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function load() {
  loadSummary();
  loadRuns();
  if (_timer) clearInterval(_timer);
  _timer = setInterval(() => { loadSummary(); loadRuns(); }, REFRESH_MS);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (_timer) { clearInterval(_timer); _timer = null; }
    } else if (!_timer) {
      loadSummary(); loadRuns();
      _timer = setInterval(() => { loadSummary(); loadRuns(); }, REFRESH_MS);
    }
  });
}

window.triggerRun = triggerRun;
window.showDetail = showDetail;
window.closeDetail = closeDetail;
window.loadRuns = loadRuns;
load();
