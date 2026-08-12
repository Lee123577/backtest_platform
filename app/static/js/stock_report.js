/* 个股 AI 分析报告页
   =====================
   有报告时正文由服务端直出(SEO),这个脚本只管两件事:
     1. 没有报告时,让访客点一下按钮触发生成(POST,受服务端限流和日额度约束)
     2. 生成成功后把结果渲染进来,不刷新页面

   markdown 渲染与 app/daily_review/render.py 是同一套受限语法 —— 改一边
   记得改另一边,否则服务端直出和这里的二次渲染排版会不一致。 */
(function () {
  "use strict";

  var CODE = "";
  var HAS_REPORT = false;

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* ── 受限 markdown → HTML(与 render.py 同款) ────────────────────────── */

  function inline(s) {
    return s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
            .replace(/`([^`]+)`/g, "<code>$1</code>");
  }

  function renderMarkdown(md) {
    if (!md) return "";
    var lines = esc(md).split("\n");
    var out = [], para = [], inList = false;

    function flushPara() {
      if (para.length) { out.push("<p>" + inline(para.join(" ")) + "</p>"); para = []; }
    }
    function closeList() {
      if (inList) { out.push("</ul>"); inList = false; }
    }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].replace(/\r$/, "").replace(/\s+$/, "");
      var h = /^(#{1,5})\s+(.*)$/.exec(line);
      if (h) {
        flushPara(); closeList();
        var lvl = Math.min(h[1].length + 1, 5);
        out.push("<h" + lvl + ">" + inline(h[2]) + "</h" + lvl + ">");
        continue;
      }
      // 点号编号必须带空格,否则 "1.5倍" 这类行首小数会被当成列表
      var li = /^\s*[-*]\s+(.*)$/.exec(line) ||
               /^\s*\d+、\s*(.*)$/.exec(line) ||
               /^\s*\d+\.\s+(.*)$/.exec(line);
      if (li) {
        flushPara();
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push("<li>" + inline(li[1]) + "</li>");
        continue;
      }
      if (!line.trim()) { flushPara(); closeList(); continue; }
      para.push(line.trim());
    }
    flushPara(); closeList();
    return out.join("");
  }

  /* ── 评分条 ─────────────────────────────────────────────────────────── */

  var TREND_CLASS = { "看多": "sr-trend-up", "震荡": "sr-trend-flat", "看空": "sr-trend-down" };

  function scoreHtml(rep) {
    if (rep.score == null && !rep.trend) return "";
    var html = '<div class="sr-score">';
    if (rep.score != null) {
      html += '<div class="sr-score-num">' + esc(rep.score) + "<small>/100</small></div>";
    }
    if (rep.trend) {
      html += '<span class="sr-trend ' + (TREND_CLASS[rep.trend] || "sr-trend-flat") +
              '">' + esc(rep.trend) + "</span>";
    }
    if (rep.score_reason) {
      html += '<div class="sr-score-reason">' + esc(rep.score_reason) + "</div>";
    }
    return html + "</div>";
  }

  function paint(rep) {
    var titleEl = $("srTitle");
    if (titleEl && rep.title) titleEl.textContent = rep.title;
    var metaEl = $("srMeta");
    if (metaEl && rep.report_date) {
      metaEl.textContent = "数据截至 " + rep.report_date;
    }
    // 评分条是服务端直出时插在正文前面的独立节点,这里没有就补一个
    var scoreEl = document.querySelector(".sr-score");
    var scoreMarkup = scoreHtml(rep);
    if (scoreEl) {
      scoreEl.outerHTML = scoreMarkup;
    } else if (scoreMarkup) {
      var content = $("srContent");
      if (content) content.insertAdjacentHTML("beforebegin", scoreMarkup);
    }
    var body = $("srContent");
    if (body) body.innerHTML = renderMarkdown(rep.content_md);
  }

  /* ── 生成 ───────────────────────────────────────────────────────────── */

  function setActions(html) {
    var box = $("srActions");
    if (box) box.innerHTML = html;
  }

  function generate(btn) {
    btn.disabled = true;
    btn.textContent = "分析中…";
    var hint = $("srGenHint");
    if (hint) hint.textContent = "正在读取行情、跑 9 种策略回测并交给 AI 解读，约需 30 秒";

    fetch("/api/stock_report/" + encodeURIComponent(CODE) + "/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        return { ok: r.ok, status: r.status, body: j };
      });
    }).then(function (res) {
      if (res.ok) {
        var empty = document.querySelector(".sr-empty");
        if (empty) empty.remove();
        paint(res.body);
        setActions("");
        return;
      }
      btn.disabled = false;
      btn.textContent = "生成 AI 分析";
      var msg = (res.body && res.body.detail) || "生成失败，请稍后再试";
      if (hint) hint.textContent = msg;
    }).catch(function () {
      btn.disabled = false;
      btn.textContent = "生成 AI 分析";
      if (hint) hint.textContent = "网络异常，请稍后再试";
    });
  }

  // 单一入口:绑定只能发生一次 —— 绑两次的话点一下会发两个生成请求,
  // 而生成是花钱的动作,还会各占一次 IP 限流额度。
  function boot() {
    var init = $("srInit");
    if (!init) return;
    CODE = init.getAttribute("data-code") || "";
    HAS_REPORT = init.getAttribute("data-ssr") === "1";
    if (HAS_REPORT) return;      // 已有报告,服务端直出即可,没有按钮要绑
    var btn = $("srGenBtn");
    if (btn) btn.addEventListener("click", function () { generate(btn); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
