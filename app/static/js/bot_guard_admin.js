/**
 * 运维页 · 爬虫拦截面板(仅管理员)
 * ==================================
 * 展示 app/bot_guard.py 的进程内状态:模式、按原因计数、当前封禁、最近拦截。
 * 误封时点「解封」—— 同时清掉这个 IP 的页面计数,不会下一个请求又封回去。
 * 数据在进程内存里,服务重启后清零。
 */
(function () {
  "use strict";

  var body = document.getElementById("bgBody");
  var modeEl = document.getElementById("bgMode");
  if (!body) return;

  var REASON = {
    bad_ua: "脚本/爬虫 UA",
    empty_ua: "空 UA",
    fake_crawler: "冒充搜索引擎",
    no_js: "只看页面不跑 JS",
    too_fast: "页面请求过快",
    crawler_rate: "声明的爬虫超速",
    verified_crawler: "已验证搜索引擎(放行)",
  };
  var MODE = { block: "拦截中", log: "只记录不拦截", off: "已关闭" };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtTime(ts) {
    var d = new Date(ts * 1000);
    var p = function (n) { return String(n).padStart(2, "0"); };
    return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function fmtLeft(s) {
    return s >= 60 ? Math.round(s / 60) + " 分钟" : s + " 秒";
  }

  function render(d) {
    modeEl.textContent = (MODE[d.mode] || d.mode) + " · 跟踪 " + d.tracked_ips + " 个 IP";
    var counts = Object.keys(d.counts || {}).sort(function (a, b) { return d.counts[b] - d.counts[a]; });
    var chips = counts.length
      ? counts.map(function (k) {
          return '<span class="bg-chip' + (k === "verified_crawler" ? " bg-chip--ok" : "") + '">' +
            esc(REASON[k] || k) + " <b>" + d.counts[k] + "</b></span>";
        }).join("")
      : '<span class="bg-empty">启动以来还没有拦截记录</span>';

    var bans = (d.active_bans || []).length
      ? '<table class="bg-table"><thead><tr><th>IP</th><th>原因</th><th>剩余</th><th></th></tr></thead><tbody>' +
        d.active_bans.map(function (b) {
          return "<tr><td>" + esc(b.ip) + "</td><td>" + esc(REASON[b.reason] || b.reason) +
            "</td><td>" + fmtLeft(b.seconds_left) + '</td><td><button class="af-btn" data-unban="' +
            esc(b.ip) + '">解封</button></td></tr>';
        }).join("") + "</tbody></table>"
      : '<div class="bg-empty">当前没有封禁中的 IP</div>';

    var recent = (d.recent || []).length
      ? '<table class="bg-table"><thead><tr><th>时间</th><th>IP</th><th>原因</th><th>路径</th><th>UA</th></tr></thead><tbody>' +
        d.recent.slice(0, 50).map(function (r) {
          return "<tr><td>" + fmtTime(r.ts) + "</td><td>" + esc(r.ip) + "</td><td>" +
            esc(REASON[r.reason] || r.reason) + (r.enforced ? "" : "（未拦）") + "</td><td>" + esc(r.path) +
            '</td><td class="bg-ua" title="' + esc(r.ua) + '">' + esc(r.ua) + "</td></tr>";
        }).join("") + "</tbody></table>"
      : '<div class="bg-empty">暂无</div>';

    body.innerHTML =
      '<div class="bg-chips">' + chips + "</div>" +
      '<div class="bg-sub">封禁中</div>' + bans +
      '<div class="bg-sub">最近拦截（最新 50 条）</div><div class="bg-scroll">' + recent + "</div>";
  }

  function load() {
    fetch("/api/admin/bot_guard")
      .then(function (r) {
        if (r.status === 401 || r.status === 403) throw new Error("仅管理员可见");
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(render)
      .catch(function (e) { body.innerHTML = '<div class="no-data">' + esc(e.message) + "</div>"; });
  }

  body.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-unban]");
    if (!btn) return;
    btn.disabled = true;
    fetch("/api/admin/bot_guard/unban", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ip: btn.getAttribute("data-unban") }),
    }).then(load).catch(function () { btn.disabled = false; });
  });

  document.getElementById("bgRefresh").addEventListener("click", load);
  load();
})();
