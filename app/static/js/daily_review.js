/**
 * AI 每日复盘页
 * =============
 * /api/daily_review/latest    → 概览卡片(context) + 复盘正文(content_md)
 * /api/daily_review/history   → 历史列表,点击加载往期
 *
 * 正文是 LLM 生成的 markdown —— 先整体 HTML 转义再做受限的 markdown
 * 变换(标题/加粗/列表/段落),不引入完整 md 库,杜绝 XSS。
 */
(function () {
  "use strict";

  var currentDate = null; // 正在展示的 review_date
  var subscribed = false; // 当前用户是否会员(付费墙用)
  var latestDate = null;  // 最新一篇复盘日期(对所有人免费,不加锁)

  // ── 工具(esc() 用 util.js 里全站共用的实现)──────────────────────────────
  function fetchJson(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function pctHtml(v, digits) {
    if (v === null || v === undefined) return "—";
    var d = digits === undefined ? 2 : digits;
    var cls = v > 0 ? "dr-up" : (v < 0 ? "dr-down" : "");
    var sign = v > 0 ? "+" : "";
    return '<span class="' + cls + '">' + sign + Number(v).toFixed(d) + "%</span>";
  }

  function amountText(yi) {
    if (yi === null || yi === undefined) return "—";
    return yi >= 10000 ? (yi / 10000).toFixed(2) + " 万亿" : Math.round(yi) + " 亿";
  }

  // ── 受限 markdown 渲染(输入先整体转义,输出无原始 HTML 注入面) ────────
  function renderMarkdown(md) {
    function inline(s) {
      return s
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/`([^`]+)`/g, "<code>$1</code>");
    }
    var lines = esc(md).split(/\r?\n/);
    var html = "", inList = false, para = [];
    function flushPara() {
      if (para.length) {
        html += "<p>" + inline(para.join(" ")) + "</p>";
        para = [];
      }
    }
    function closeList() {
      if (inList) { html += "</ul>"; inList = false; }
    }
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].replace(/\s+$/, "");
      var h = line.match(/^(#{1,5})\s+(.*)$/);
      if (h) {
        flushPara(); closeList();
        // 页面已有 h1,md 的 # 从 h2 起步,最深 h5
        var lvl = Math.min(h[1].length + 1, 5);
        html += "<h" + lvl + ">" + inline(h[2]) + "</h" + lvl + ">";
        continue;
      }
      // 顿号编号("1、内容")中文习惯不带空格;点号编号必须带空格,
      // 免得把 "1.5倍" 这类行首小数误判成列表
      var li = line.match(/^\s*[-*]\s+(.*)$/) ||
               line.match(/^\s*\d+、\s*(.*)$/) ||
               line.match(/^\s*\d+\.\s+(.*)$/);
      if (li) {
        flushPara();
        if (!inList) { html += "<ul>"; inList = true; }
        html += "<li>" + inline(li[1]) + "</li>";
        continue;
      }
      if (!line.trim()) { flushPara(); closeList(); continue; }
      para.push(line.trim());
    }
    flushPara(); closeList();
    return html;
  }

  // ── 概览卡片 ──────────────────────────────────────────────────────────
  function renderSummary(ctx) {
    var grid = document.getElementById("drSummaryGrid");
    if (!ctx) {
      grid.innerHTML = '<div class="no-data">暂无当日数据快照</div>';
      return;
    }
    var cards = [];
    (ctx.indices || []).forEach(function (ix) {
      cards.push(
        '<div class="sum-card"><div class="sum-label">' + esc(ix.name) + "</div>" +
        '<div class="sum-val">' + Number(ix.close).toFixed(2) +
        ' <small>' + pctHtml(ix.pct_change) + "</small></div></div>"
      );
    });
    var b = ctx.breadth || {};
    cards.push(
      '<div class="sum-card"><div class="sum-label">涨 / 跌家数</div><div class="sum-val">' +
      '<span class="dr-up">' + (b.up != null ? b.up : "—") + "</span> / " +
      '<span class="dr-down">' + (b.down != null ? b.down : "—") + "</span></div></div>"
    );
    cards.push(
      '<div class="sum-card"><div class="sum-label">大涨 / 大跌(±9.8%)</div><div class="sum-val">' +
      '<span class="dr-up">' + (b.strong_up != null ? b.strong_up : "—") + "</span> / " +
      '<span class="dr-down">' + (b.strong_down != null ? b.strong_down : "—") + "</span></div></div>"
    );
    var amtHtml = amountText(b.total_amount_yi);
    if (b.prev_amount_yi && b.total_amount_yi) {
      var delta = (b.total_amount_yi - b.prev_amount_yi) / b.prev_amount_yi * 100;
      amtHtml += " <small>较昨日 " + pctHtml(delta, 1) + "</small>";
    }
    cards.push(
      '<div class="sum-card"><div class="sum-label">全市场成交额</div><div class="sum-val">' +
      amtHtml + "</div></div>"
    );
    var hs = (ctx.ai_hotsector || {}).settled;
    cards.push(
      '<div class="sum-card"><div class="sum-label">AI 选股结算(昨买今卖)</div><div class="sum-val">' +
      (hs
        ? hs.win_count + "/" + hs.total_count + " 涨 <small>" +
          pctHtml(hs.day_return_pct) + "</small>"
        : "—") +
      "</div></div>"
    );
    grid.innerHTML = cards.join("");
  }

  // ── 复盘正文 ──────────────────────────────────────────────────────────
  function renderReview(review) {
    var titleEl = document.getElementById("drTitle");
    var bodyEl = document.getElementById("drContent");
    var dateEl = document.getElementById("drDate");
    if (!review) {
      titleEl.textContent = "暂无复盘";
      bodyEl.innerHTML =
        '<div class="no-data">还没有生成过复盘 —— 每个交易日 17:45 数据入库后自动生成,' +
        "也可在「定时任务」页手动触发 daily_review_generate。</div>";
      dateEl.textContent = "";
      renderSummary(null);
      return;
    }
    currentDate = String(review.review_date);
    dateEl.textContent = currentDate;
    if (review.locked) {
      // 付费墙:历史篇未订阅 —— 标题当钩子,正文换成订阅引导
      titleEl.textContent = review.title || (currentDate + " A股复盘");
      bodyEl.innerHTML =
        '<div class="dr-paywall">' +
        '<div class="dr-paywall-title">🔒 订阅解锁历史复盘</div>' +
        '<p>历史复盘的完整正文与当日数据快照为会员内容，最新一篇可免费查看。</p>' +
        '<button class="dr-paywall-btn" id="drSubBtn">开通会员 · 查看套餐</button>' +
        "</div>";
      var btn = document.getElementById("drSubBtn");
      if (btn) btn.addEventListener("click", goSubscribe);
      renderSummary(null);
      highlightHistory();
      return;
    }
    if (review.status === "failed") {
      titleEl.textContent = currentDate + " 生成失败";
      bodyEl.innerHTML = '<div class="no-data">' +
        esc(review.error_msg || "生成失败") + "</div>";
    } else {
      titleEl.textContent = review.title || (currentDate + " A股复盘");
      bodyEl.innerHTML = renderMarkdown(review.content_md || "");
    }
    renderSummary(review.context);
    highlightHistory();
  }

  // 去订阅页(未登录时订阅页自身会先引导登录)
  function goSubscribe() {
    window.location.href = "/subscribe";
  }

  // ── 历史列表 ──────────────────────────────────────────────────────────
  function highlightHistory() {
    var items = document.querySelectorAll(".dr-history-item");
    items.forEach(function (el) {
      el.classList.toggle("active", el.getAttribute("data-date") === currentDate);
    });
  }

  function renderHistory(rows) {
    var wrap = document.getElementById("drHistoryWrap");
    if (!rows || !rows.length) {
      wrap.innerHTML = '<div class="no-data">暂无历史复盘</div>';
      return;
    }
    // 历史按日期倒序 → 第一条即最新一篇(对所有人免费)
    latestDate = String(rows[0].review_date);
    wrap.innerHTML = rows.map(function (r) {
      var d = String(r.review_date);
      var locked = !subscribed && d !== latestDate && r.status !== "failed";
      return '<div class="dr-history-item" data-date="' + esc(d) + '">' +
        '<span class="dr-history-date">' + esc(d) + "</span>" +
        '<span class="dr-history-title">' + esc(r.title || "") + "</span>" +
        (r.status === "failed" ? '<span class="dr-history-failed">生成失败</span>' : "") +
        (locked ? '<span class="dr-history-lock">🔒</span>' : "") +
        "</div>";
    }).join("");
    wrap.querySelectorAll(".dr-history-item").forEach(function (el) {
      el.addEventListener("click", function () {
        loadByDate(el.getAttribute("data-date"));
      });
    });
    highlightHistory();
  }

  function loadByDate(dateStr) {
    fetchJson("/api/daily_review/" + encodeURIComponent(dateStr))
      .then(function (data) {
        renderReview(data.review);
        // 可分享链接:/daily_review#2026-07-08 直达往期
        // (renderReview 已更新 currentDate,hashchange 里会据此跳过重复加载)
        if (location.hash.slice(1) !== dateStr) location.hash = dateStr;
      })
      .catch(function (e) {
        currentDate = null;
        document.getElementById("drTitle").textContent = dateStr + " 加载失败";
        document.getElementById("drDate").textContent = "";
        document.getElementById("drContent").innerHTML =
          '<div class="no-data">加载失败: ' + esc(e.message) + "</div>";
        renderSummary(null);
        highlightHistory();
      });
  }

  function hashDate() {
    var m = location.hash.match(/^#(\d{4}-\d{2}-\d{2})$/);
    return m ? m[1] : null;
  }

  // 浏览器前进/后退在往期复盘之间切换
  window.addEventListener("hashchange", function () {
    var d = hashDate();
    if (d && d !== currentDate) loadByDate(d);
  });

  function refreshSub() {
    return fetchJson("/api/subscription/status")
      .then(function (s) { subscribed = !!s.subscribed; })
      .catch(function () { subscribed = false; });
  }

  // ── 初始化 ────────────────────────────────────────────────────────────
  // 先定订阅态，再渲染历史(锁标记依赖 subscribed)与正文
  refreshSub().then(function () {
    var initDate = hashDate();
    if (initDate) {
      loadByDate(initDate);
    } else {
      fetchJson("/api/daily_review/latest")
        .then(function (data) { renderReview(data.review); })
        .catch(function () { renderReview(null); });
    }
    fetchJson("/api/daily_review/history?limit=30")
      .then(function (data) { renderHistory(data.history); })
      .catch(function () { renderHistory([]); });
  });
})();
