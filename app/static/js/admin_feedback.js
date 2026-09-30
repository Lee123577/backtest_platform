/**
 * 运维页 · 用户反馈面板(仅管理员)
 * ==================================
 * 列出反馈、改处理状态、把同意公开的评价精选到首页评价墙。
 *
 * 「上墙」按钮只对 public_ok=1 的条目可点 —— 后端 SQL 里同样卡着这一条,
 * 这里禁用只是让管理员一眼看出哪些能上、哪些不能,不是唯一的闸。
 * 非管理员打开运维页时接口回 401/403,面板显示提示,不影响页面其它部分。
 */
(function () {
  "use strict";

  var list = document.getElementById("afList");
  var filter = document.getElementById("afFilter");
  if (!list || !filter) return;

  var CAT = { bug: "问题", feature: "建议", praise: "评价", other: "其他" };
  var STATUS = { new: "未处理", read: "已读", done: "已处理" };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
        return j;
      });
    });
  }

  function row(f) {
    var when = String(f.created_at || "").replace("T", " ").slice(0, 16);
    var pub = Number(f.public_ok) === 1;
    var featured = Number(f.featured) === 1;
    var statusOpts = Object.keys(STATUS).map(function (k) {
      return '<option value="' + k + '"' + (f.status === k ? " selected" : "") + ">" + STATUS[k] + "</option>";
    }).join("");
    var wallBtn = pub
      ? '<button class="af-btn' + (featured ? " af-btn--on" : "") + '" data-act="feature" data-id="' + f.id +
        '" data-on="' + (featured ? "1" : "0") + '">' + (featured ? "★ 已上墙" : "☆ 上墙") + "</button>"
      : '<button class="af-btn" disabled title="提交人没有勾选同意公开">不可公开</button>';
    return (
      '<div class="af-item' + (featured ? " af-item--featured" : "") + '">' +
      '<div class="af-meta">' +
      '<span class="af-cat af-cat--' + esc(f.category) + '">' + esc(CAT[f.category] || f.category) + "</span>" +
      '<span class="af-id">#' + f.id + "</span>" +
      "<span>" + esc(when) + "</span>" +
      (f.page ? '<span class="af-page">' + esc(f.page) + "</span>" : "") +
      (pub ? '<span class="af-pub">同意公开' + (f.display_name ? " · 署名「" + esc(f.display_name) + "」" : "") + "</span>" : "") +
      "</div>" +
      '<div class="af-content">' + esc(f.content) + "</div>" +
      '<div class="af-foot">' +
      (f.contact ? '<span class="af-contact">联系方式：' + esc(f.contact) + "</span>" : '<span class="af-contact">未留联系方式</span>') +
      '<span class="af-actions">' + wallBtn +
      '<select class="af-status" data-id="' + f.id + '">' + statusOpts + "</select></span>" +
      "</div></div>"
    );
  }

  function load() {
    var qs = filter.value ? "?limit=100&status=" + encodeURIComponent(filter.value) : "?limit=100";
    fetch("/api/feedback/list" + qs)
      .then(function (r) {
        if (r.status === 401 || r.status === 403) throw new Error("仅管理员可见");
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (j) {
        var items = j.feedback || [];
        list.innerHTML = items.length
          ? '<div class="af-list">' + items.map(row).join("") + "</div>"
          : '<div class="no-data">这个状态下没有反馈</div>';
      })
      .catch(function (e) {
        list.innerHTML = '<div class="no-data">' + esc(e.message) + "</div>";
      });
  }

  list.addEventListener("click", function (e) {
    var btn = e.target.closest('[data-act="feature"]');
    if (!btn) return;
    var on = btn.getAttribute("data-on") !== "1";
    btn.disabled = true;
    post("/api/feedback/" + btn.getAttribute("data-id") + "/feature", { featured: on })
      .then(load)
      .catch(function (err) { btn.disabled = false; alert(err.message); });
  });

  list.addEventListener("change", function (e) {
    var sel = e.target.closest(".af-status");
    if (!sel) return;
    sel.disabled = true;
    post("/api/feedback/" + sel.getAttribute("data-id") + "/status", { status: sel.value })
      .then(function () { sel.disabled = false; })
      .catch(function (err) { sel.disabled = false; alert(err.message); load(); });
  });

  filter.addEventListener("change", load);
  load();
})();
