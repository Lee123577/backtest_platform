/**
 * AI 每日复盘页
 * =============
 * /api/daily_review/latest    → 概览卡片(context) + 复盘正文(content_md)
 * /api/daily_review/history   → 历史列表,点击加载往期
 *
 * 正文是 LLM 生成的 markdown —— 先整体 HTML 转义再做受限的 markdown
 * 变换(标题/加粗/列表/段落),不引入完整 md 库,杜绝 XSS。
 * 服务端有一份同款实现(app/daily_review/render.py)负责首屏直出,改这里记得同步。
 *
 * 路由:每篇复盘有独立路径 /daily_review/YYYY-MM-DD(可被搜索引擎收录)。
 * 服务端已直出正文(所有复盘对所有人开放),本脚本只负责:
 *   1) 概览卡片(数据快照)
 *   2) 站内切换往期时的局部刷新(pushState,不整页重载)
 */
(function () {
  "use strict";

  var initEl = document.getElementById("drInit");
  var ssrDate = initEl ? initEl.getAttribute("data-review-date") : "";
  var hasSsr = initEl ? initEl.getAttribute("data-ssr") === "1" : false;

  var currentDate = ssrDate || null; // 正在展示的 review_date
  var latestDate = null;  // 最新一篇复盘日期(浏览器后退回首页时用)

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
      '<div class="sum-card"><div class="sum-label">AI 观察样本模拟结算(T买T+1卖)</div><div class="sum-val">' +
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
        '<div class="no-data">还没有生成过复盘 —— 每个交易日 17:45 数据入库后自动生成。</div>';
      dateEl.textContent = "";
      renderSummary(null);
      return;
    }
    currentDate = String(review.review_date);
    dateEl.textContent = currentDate;
    // 站内切换往期是 pushState,不整页重载 —— 标题得自己跟上,
    // 否则收藏/分享出去的还是上一篇的标题
    document.title = (review.title || (currentDate + " A股复盘")) +
      "（" + currentDate + "） | shoupan";
    if (review.status === "failed") {
      // 与服务端直出(_dr_body)同口径:公开页不出现"生成失败"这类事故字样
      titleEl.textContent = currentDate + " A股复盘";
      bodyEl.innerHTML = '<div class="no-data">该交易日复盘正在补录，' +
        "可先查看最近一个交易日的复盘内容。</div>";
    } else {
      titleEl.textContent = review.title || (currentDate + " A股复盘");
      // 服务端那份带股票名链接(名字索引在后端,搬不过来);拿不到再自己渲染
      bodyEl.innerHTML = review.content_html ||
        renderMarkdown(review.content_md || "");
    }
    renderSummary(review.context);
    highlightHistory();
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
      // 服务端已经直出过列表时不要清空 —— 接口抖一下就把好好的链接
      // 换成"暂无历史复盘",既丢了导航也丢了给爬虫的内链
      if (wrap.querySelector(".dr-history-item")) return;
      wrap.innerHTML = '<div class="no-data">暂无历史复盘</div>';
      return;
    }
    // 历史按日期倒序 → 第一条即最新一篇
    latestDate = String(rows[0].review_date);
    // 每项都是真链接 —— 服务端也直出同样的 <a>,爬虫顺着能抓到每篇往期;
    // 下面再拦截点击做局部刷新,兼顾可收录与不整页重载
    wrap.innerHTML = rows.map(function (r) {
      var d = String(r.review_date);
      return '<a class="dr-history-item" href="/daily_review/' + esc(d) +
        '" data-date="' + esc(d) + '">' +
        '<span class="dr-history-date">' + esc(d) + "</span>" +
        '<span class="dr-history-title">' + esc(r.title || "") + "</span>" +
        (r.status === "failed" ? '<span class="dr-history-failed">待更新</span>' : "") +
        "</a>";
    }).join("");
    bindHistoryLinks(wrap);
    highlightHistory();
  }

  // 拦截站内往期链接:新标签页/中键/带修饰键的点击照常交给浏览器
  function bindHistoryLinks(wrap) {
    wrap.querySelectorAll(".dr-history-item").forEach(function (el) {
      el.addEventListener("click", function (ev) {
        if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey ||
            ev.button !== 0) return;
        ev.preventDefault();
        var d = el.getAttribute("data-date");
        if (d === currentDate) return;
        loadByDate(d, true);
      });
    });
  }

  // 复盘详情打开埋点。from_email:URL 带 utm_medium=email 即认为来自邮件推送。
  function reportDigestOpen(dateStr) {
    try {
      if (window.SPTrack) {
        SPTrack.event("digest_open", {
          date: dateStr,
          from_email: new URLSearchParams(location.search).get("utm_medium") === "email",
        });
      }
    } catch (e) { /* 埋点失败不影响渲染 */ }
  }

  function loadByDate(dateStr, push) {
    fetchJson("/api/daily_review/" + encodeURIComponent(dateStr))
      .then(function (data) {
        renderReview(data.review);
        // 复盘详情真的加载出来了才记 digest_open —— 邮件推送点进来的人
        // 落地就落在这一天,from_email 由 URL 的 utm_medium=email 判定,
        // 用来衡量"邮件链路到底有没有把人带来"。
        reportDigestOpen(dateStr);
        if (push) {
          history.pushState({ date: dateStr }, "", "/daily_review/" + dateStr);
        }
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

  // 路径里的日期:/daily_review/2026-07-08 → "2026-07-08"
  function pathDate() {
    var m = location.pathname.match(/^\/daily_review\/(\d{4}-\d{2}-\d{2})\/?$/);
    return m ? m[1] : null;
  }

  // 老的 hash 链接(/daily_review#2026-07-08)已发出去过,不能直接失效 ——
  // 悄悄换成等价的真实路径,收藏和外链继续可用
  function hashDate() {
    var m = location.hash.match(/^#(\d{4}-\d{2}-\d{2})$/);
    return m ? m[1] : null;
  }

  // 浏览器前进/后退在往期复盘之间切换
  window.addEventListener("popstate", function () {
    var d = pathDate();
    if (d && d !== currentDate) loadByDate(d, false);
    else if (!d && latestDate && latestDate !== currentDate) {
      loadByDate(latestDate, false);
    }
  });

  // ── 初始化 ────────────────────────────────────────────────────────────
  // 服务端已直出的部分不要重复渲染：正文首屏已经在页面上，这里只补概览卡片
  // (数据快照，服务端不直出)。服务端直出的列表先接上交互，别等接口回来。
  bindHistoryLinks(document.getElementById("drHistoryWrap"));
  highlightHistory();

  (function init() {
    var legacy = hashDate();
    if (legacy) {
      // 老 hash 链接 → 换成真实路径后按该日期加载
      history.replaceState({ date: legacy }, "", "/daily_review/" + legacy);
      loadByDate(legacy, false);
    } else if (!hasSsr) {
      // 服务端没数据(DB 未就绪/表未建)：退回纯前端加载，页面不至于空着
      var p = pathDate();
      if (p) {
        loadByDate(p, false);
      } else {
        fetchJson("/api/daily_review/latest")
          .then(function (data) { renderReview(data.review); })
          .catch(function () { renderReview(null); });
      }
    } else {
      // 首屏正文已就位，只补概览卡片
      fetchJson("/api/daily_review/" + encodeURIComponent(ssrDate))
        .then(function (data) {
          if (data.review) renderSummary(data.review.context);
        })
        .catch(function () { renderSummary(null); });
    }

    fetchJson("/api/daily_review/history?limit=30")
      .then(function (data) { renderHistory(data.history); })
      .catch(function () { renderHistory([]); });
  })();
})();
