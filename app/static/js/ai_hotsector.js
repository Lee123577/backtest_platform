// 板块热度观察页面逻辑：拉 /api/ai_hotsector/today 渲染板块热度卡。
// 只陈列板块层面的客观数据（板块名 / 当日涨跌幅 / 领涨股 / 成分股数 / AI 观察记录），
// 不产生任何个股 DOM。原 AI 个股模拟跟踪的历史表现(收益/上涨占比/资金曲线/批次)
// 已整块下线：对外公示历史跟踪收益正是合规审查要去掉的东西，旧接口只留给管理员。

// 板块涨跌幅/领涨股涨幅来自快照，本身就是百分数(如 1.23 表示 +1.23%)，
// 不能再乘 100 —— 与个股涨跌幅(小数口径)区分开。
function fmtSnapshotPct(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}%`;
}

// esc() 现在是 util.js 里的全站共用实现(必须比本文件先加载)。
// DeepSeek 返回的板块名/观察记录是模型生成的自由文本，插入 innerHTML 前必须转义，
// 否则模型输出里一旦带 HTML 就是存储型 XSS

document.addEventListener('DOMContentLoaded', () => {
  loadToday();
});

// ── 今日板块热度观察 ──────────────────────────────────────────────────────

async function loadToday() {
  const wrap = document.getElementById('hsTodayWrap');
  const dateEl = document.getElementById('hsTodayDate');
  try {
    const res = await fetch('/api/ai_hotsector/today');
    const { boards, stale, data_date } = await res.json();
    const list = boards || [];
    if (!list.length) {
      if (dateEl) dateEl.textContent = '';
      wrap.innerHTML = '<div class="no-data">尚未生成板块热度观察，请等待 15:05 定时任务，或在 /tasks 页面手动触发 ai_hotsector_predict</div>';
      return;
    }

    // 回退到更早一批时后端带 stale=true，必须在标题处标注**真实数据日期**，
    // 否则用户会以为这是今天的观察记录。
    if (dateEl) {
      if (stale) {
        dateEl.innerHTML = `（数据日期：${esc(data_date)}，当日观察生成中）`;
      } else {
        dateEl.textContent = `（${data_date}）`;
      }
    }

    const cards = list.map(b => renderBoardCard(b)).join('');
    wrap.innerHTML = `<div class="sector-grid">${cards}</div>`;
  } catch (e) {
    console.error(e);
    wrap.innerHTML = '<div class="no-data">加载失败</div>';
  }
}

// 板块热度卡：板块名 / 当日涨跌幅 / 领涨股与涨幅 / 成分股数 / AI 客观观察记录。
function renderBoardCard(b) {
  const hasPct = b.pct_change !== null && b.pct_change !== undefined;
  const pctCls = hasPct ? (Number(b.pct_change) > 0 ? 'pos' : Number(b.pct_change) < 0 ? 'neg' : '') : '';
  const leaderPct = (b.leader_pct !== null && b.leader_pct !== undefined)
    ? ` <span class="hs-leader-pct ${pctCls}">${fmtSnapshotPct(b.leader_pct)}</span>` : '';
  const leader = b.leader
    ? `领涨 ${esc(b.leader)}${leaderPct}`
    : '领涨股暂缺';
  const count = (b.stock_count !== null && b.stock_count !== undefined)
    ? `成分股 ${esc(b.stock_count)} 只` : '';
  const meta = count ? `${leader} · ${count}` : leader;
  return `
    <div class="sector-card">
      <div class="sector-card-head">
        <div><span class="sector-rank">${esc(b.rank)}</span><span class="sector-name">${esc(b.name)}</span></div>
        <span class="sector-agg ${pctCls}">${hasPct ? fmtSnapshotPct(b.pct_change) : '—'}</span>
      </div>
      <div class="hs-board-meta">${meta}</div>
      <div class="sector-reason">${esc(b.note || '')}</div>
    </div>
  `;
}
