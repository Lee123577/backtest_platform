/**
 * 我的数据看板
 * ============
 * 关注标的的近半年走势。个股走 /api/stock/{code}/kline，
 * 指数走 /api/index/{code}/kline —— 两者不能混用：000001 在个股表是
 * 平安银行，在指数表才是上证综指。
 *
 * 加标的：往下面 ITEMS 里加一条即可。
 */
(function () {
  "use strict";

  var ITEMS = [
    { type: "stock", code: "603993", name: "洛阳钼业", tag: "个股" },
    { type: "index", code: "000001", name: "上证综合指数", tag: "指数" },
  ];

  var DAYS_BACK = 180;   // 拉近半年(自然日)

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function fmtDate(d) {
    return d.getFullYear() + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
  }
  function num(v, digits) {
    return v == null ? "—" : Number(v).toFixed(digits === undefined ? 2 : digits);
  }

  function cardHtml(it, i) {
    return '<div class="mb-card" id="mbCard' + i + '" data-code="' + esc(it.code) + '">' +
      '<div class="mb-card-head">' +
      '<span class="mb-name">' + esc(it.name) + "</span>" +
      '<span class="mb-code">' + esc(it.code) + "</span>" +
      '<span class="mb-tag">' + esc(it.tag) + "</span>" +
      '<span class="mb-drag-handle" title="拖动排列位置,便于横向/纵向比对">⠿</span>' +
      "</div>" +
      '<div id="mbBody' + i + '"><div class="mb-loading">加载中…</div></div>' +
      "</div>";
  }

  /**
   * 自由拖拽画布 —— 卡片改绝对定位,拖动手柄松手后固定位置(存 localStorage)。
   * 靠近其它卡片边缘会自动吸附,方便横向/纵向对齐比对。窄屏不启用,退回堆叠布局。
   */
  var CANVAS_BREAKPOINT = 760;
  var LAYOUT_KEY = "mb_canvas_layout_v1";
  var CARD_W = 420;
  var GAP = 16;
  var SNAP = 8;
  var ROW_H = 400;

  function loadLayout() {
    try { return JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}"); }
    catch (e) { return {}; }
  }
  function saveLayout(layout) {
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch (e) { /* 存储不可用就不持久化 */ }
  }

  function resizeCanvasHeight(grid, cards) {
    var maxBottom = 0;
    cards.forEach(function (card) {
      var bottom = card.offsetTop + card.offsetHeight;
      if (bottom > maxBottom) maxBottom = bottom;
    });
    grid.style.height = (maxBottom + 4) + "px";
  }

  function bindDrag(grid, card, handle, allCards, layout) {
    var startX, startY, origLeft, origTop, dragging = false;
    var guides = [];

    function clearGuides() {
      guides.forEach(function (g) { g.remove(); });
      guides = [];
    }
    function showGuide(type, pos) {
      var g = document.createElement("div");
      g.className = "mb-guide " + type;
      if (type === "v") g.style.left = pos + "px"; else g.style.top = pos + "px";
      grid.appendChild(g);
      guides.push(g);
    }

    function onDown(e) {
      dragging = true;
      startX = e.clientX; startY = e.clientY;
      origLeft = parseFloat(card.style.left) || 0;
      origTop = parseFloat(card.style.top) || 0;
      card.classList.add("dragging");
      card.style.zIndex = 100;
      try { handle.setPointerCapture(e.pointerId); } catch (err) { /* 不支持就退化成普通拖动 */ }
      e.preventDefault();
    }

    function onMove(e) {
      if (!dragging) return;
      var left = origLeft + (e.clientX - startX);
      var top = origTop + (e.clientY - startY);
      var w = card.offsetWidth, h = card.offsetHeight;

      clearGuides();
      var snappedX = false, snappedY = false;
      allCards.forEach(function (other) {
        if (other === card) return;
        var ol = parseFloat(other.style.left) || 0;
        var ot = parseFloat(other.style.top) || 0;
        var ow = other.offsetWidth, oh = other.offsetHeight;
        if (!snappedX) {
          if (Math.abs(left - ol) < SNAP) { left = ol; snappedX = true; showGuide("v", ol); }
          else if (Math.abs(left - (ol + ow + GAP)) < SNAP) { left = ol + ow + GAP; snappedX = true; showGuide("v", left); }
          else if (Math.abs((left + w + GAP) - ol) < SNAP) { left = ol - GAP - w; snappedX = true; showGuide("v", ol); }
        }
        if (!snappedY) {
          if (Math.abs(top - ot) < SNAP) { top = ot; snappedY = true; showGuide("h", ot); }
          else if (Math.abs(top - (ot + oh + GAP)) < SNAP) { top = ot + oh + GAP; snappedY = true; showGuide("h", top); }
        }
      });

      card.style.left = Math.max(0, left) + "px";
      card.style.top = Math.max(0, top) + "px";
      resizeCanvasHeight(grid, allCards);
    }

    function onUp(e) {
      if (!dragging) return;
      dragging = false;
      card.classList.remove("dragging");
      clearGuides();
      var code = card.getAttribute("data-code");
      layout[code] = { left: parseFloat(card.style.left) || 0, top: parseFloat(card.style.top) || 0 };
      saveLayout(layout);
      try { handle.releasePointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    }

    handle.addEventListener("pointerdown", onDown);
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  }

  function defaultPos(i, perRow) {
    return { left: (i % perRow) * (CARD_W + GAP), top: Math.floor(i / perRow) * ROW_H };
  }

  function initCanvas(grid) {
    if (window.innerWidth <= CANVAS_BREAKPOINT) return null;   // 窄屏走堆叠布局,不启用画布
    grid.classList.add("mb-canvas-mode");

    var cards = Array.prototype.slice.call(grid.querySelectorAll(".mb-card"));
    if (!cards.length) return null;
    var layout = loadLayout();
    var perRow = Math.max(1, Math.floor((grid.clientWidth + GAP) / (CARD_W + GAP)));

    cards.forEach(function (card, i) {
      var saved = layout[card.getAttribute("data-code")];
      var pos = saved || defaultPos(i, perRow);
      card.style.left = pos.left + "px";
      card.style.top = pos.top + "px";
      card.style.zIndex = 10 + i;
    });
    resizeCanvasHeight(grid, cards);

    cards.forEach(function (card) {
      var handle = card.querySelector(".mb-drag-handle");
      if (handle) bindDrag(grid, card, handle, cards, layout);
    });

    window.addEventListener("resize", function () {
      var maxLeft = Math.max(0, grid.clientWidth - CARD_W);
      cards.forEach(function (card) {
        var left = Math.min(Math.max(0, parseFloat(card.style.left) || 0), maxLeft);
        card.style.left = left + "px";
      });
      resizeCanvasHeight(grid, cards);
    });

    var resetBtn = document.getElementById("mbResetLayout");
    if (resetBtn) {
      resetBtn.addEventListener("click", function () {
        layout = {};
        saveLayout(layout);
        cards.forEach(function (card, i) {
          var pos = defaultPos(i, perRow);
          card.style.left = pos.left + "px";
          card.style.top = pos.top + "px";
        });
        resizeCanvasHeight(grid, cards);
      });
    }

    return { grid: grid, cards: cards };
  }

  function render(i, it, rows) {
    var body = document.getElementById("mbBody" + i);
    if (!rows || rows.length < 2) {
      body.innerHTML = '<div class="mb-error">暂无行情数据</div>';
      return;
    }
    var last = rows[rows.length - 1];
    var prev = rows[rows.length - 2];
    var chg = last.close != null && prev.close ? (last.close - prev.close) / prev.close * 100 : null;
    var cls = chg == null ? "mb-flat" : (chg > 0 ? "mb-up" : (chg < 0 ? "mb-down" : "mb-flat"));
    var sign = chg > 0 ? "+" : "";

    body.innerHTML =
      '<div class="mb-quote">' +
      '<span class="mb-close ' + cls + '">' + num(last.close) + "</span>" +
      '<span class="mb-chg ' + cls + '">' + (chg == null ? "—" : sign + num(chg) + "%") + "</span>" +
      "</div>" +
      '<div class="mb-meta">最新 ' + esc(last.date) + " · 近 " + rows.length + " 个交易日</div>" +
      '<div class="mb-chart" id="mbChart' + i + '"></div>';

    drawChart("mbChart" + i, rows, chg);
  }

  function drawChart(elId, rows, chg) {
    if (!window.echarts) return;
    var el = document.getElementById(elId);
    var chart = echarts.init(el);
    var up = chg == null || chg >= 0;
    var color = up ? "#cf222e" : "#1a7f37";
    var dates = rows.map(function (r) { return r.date; });
    var closes = rows.map(function (r) { return r.close; });

    chart.setOption({
      grid: { left: 52, right: 14, top: 14, bottom: 26 },
      tooltip: {
        trigger: "axis",
        formatter: function (p) {
          var d = p[0];
          return d.axisValue + "<br/>收盘 <b>" + num(d.data) + "</b>";
        },
      },
      xAxis: {
        type: "category",
        data: dates,
        boundaryGap: false,
        axisLine: { lineStyle: { color: "#d0d7de" } },
        axisLabel: { color: "#8a929c", fontSize: 11 },
      },
      yAxis: {
        type: "value",
        scale: true,
        splitLine: { lineStyle: { color: "#eef1f4" } },
        axisLabel: { color: "#8a929c", fontSize: 11 },
      },
      series: [{
        type: "line",
        data: closes,
        showSymbol: false,
        lineStyle: { width: 2, color: color },
        itemStyle: { color: color },
        areaStyle: {
          color: {
            type: "linear", x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: up ? "rgba(207,34,46,.18)" : "rgba(26,127,55,.18)" },
              { offset: 1, color: "rgba(255,255,255,0)" },
            ],
          },
        },
      }],
    });
    window.addEventListener("resize", function () { chart.resize(); });
  }

  function load() {
    var grid = document.getElementById("mbGrid");
    grid.innerHTML = ITEMS.map(cardHtml).join("");
    var canvas = initCanvas(grid);
    function refreshCanvas() {
      if (canvas) resizeCanvasHeight(canvas.grid, canvas.cards);
    }

    var end = new Date();
    var start = new Date(end.getTime() - DAYS_BACK * 86400000);
    var qs = "?start_date=" + fmtDate(start) + "&end_date=" + fmtDate(end);

    ITEMS.forEach(function (it, i) {
      var url = (it.type === "index" ? "/api/index/" : "/api/stock/") +
        encodeURIComponent(it.code) + "/kline" + qs;
      fetch(url)
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then(function (j) { render(i, it, j.data || []); refreshCanvas(); })
        .catch(function (e) {
          document.getElementById("mbBody" + i).innerHTML =
            '<div class="mb-error">加载失败：' + esc(e.message) + "</div>";
          refreshCanvas();
        });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();

/**
 * 板块涨幅排行榜
 * ==============
 * 读 /api/sectors/ranking(每日快照表,不实时抓东财 —— 它对同 IP 限流)。
 * 四个页签:六大板块 / 行业板块 / 题材概念 / 特殊概念。
 */
(function () {
  "use strict";

  var data = null;
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function pctHtml(v) {
    if (v == null) return '<span class="rk-pct rk-flat">—</span>';
    var cls = v > 0 ? "rk-up" : (v < 0 ? "rk-down" : "rk-flat");
    return '<span class="rk-pct ' + cls + '">' + (v > 0 ? "+" : "") +
      Number(v).toFixed(2) + "%</span>";
  }
  function rankNo(i) {
    var cls = i === 0 ? " top1" : i === 1 ? " top2" : i === 2 ? " top3" : "";
    return '<span class="rk-no' + cls + '">' + (i + 1) + "</span>";
  }
  function row(i, name, pct, sub, extra) {
    return '<div class="rk-row">' + rankNo(i) +
      '<div class="rk-name">' + esc(name) + (extra || "") +
      (sub ? '<div class="rk-sub">' + esc(sub) + "</div>" : "") +
      "</div>" + pctHtml(pct) + "</div>";
  }

  function renderGroups() {
    if (!data.board_data_ok) return noBoardData();
    var html = data.groups.map(function (g, i) {
      var tops = (g.top || []).map(function (t) { return t.name; }).join("、");
      return row(i, g.name, g.avg_pct,
        g.board_count + " 个板块" + (tops ? " · 领涨:" + tops : ""));
    }).join("");
    var note = '<div class="rk-note">按组内行业板块的平均涨跌幅排序。' +
      (data.unmapped ? "有 " + data.unmapped + " 个板块未归入任何分组，未计入统计。" : "") +
      "</div>";
    $("rkBody").innerHTML = (html || '<div class="no-data">暂无数据</div>') + note;
  }

  function renderIndustry() {
    if (!data.board_data_ok) return noBoardData();
    var top = data.industry_top.map(function (b, i) {
      return row(i, b.name, b.pct_change,
        (b.group || "未归类") + (b.leader ? " · 领涨 " + b.leader : ""),
        b.is_theme ? '<span class="rk-hot">热点</span>' : "");
    }).join("");
    var bottom = data.industry_bottom.map(function (b, i) {
      return row(i, b.name, b.pct_change, b.group || "未归类");
    }).join("");
    $("rkBody").innerHTML =
      '<div class="rk-note" style="margin:0 0 8px;font-weight:600;color:var(--txt);">涨幅前 ' +
      data.industry_top.length + "</div>" + top +
      '<div class="rk-note" style="margin:16px 0 8px;font-weight:600;color:var(--txt);">跌幅前 ' +
      data.industry_bottom.length + "</div>" + bottom;
  }

  function renderConcept() {
    if (!data.board_data_ok) return noBoardData();
    var html = data.concept_top.map(function (b, i) {
      return row(i, b.name, b.pct_change, b.leader ? "领涨 " + b.leader : "",
        b.is_theme ? '<span class="rk-hot">热点</span>' : "");
    }).join("");
    $("rkBody").innerHTML = (html || '<div class="no-data">暂无数据</div>') +
      '<div class="rk-note">题材概念按当日涨幅取前 N —— 资金炒作什么就冒出什么，' +
      "不靠关键词白名单筛（新热点常常名字里不含关键词）。</div>";
  }

  function renderSpecial() {
    var html = data.special.map(function (s, i) {
      var members = (s.members || []).map(function (m) {
        return m.name + " " + (m.pct_change > 0 ? "+" : "") + m.pct_change + "%";
      }).join(" / ");
      var src = s.source === "local"
        ? '<span class="rk-src">本站行情库</span>'
        : '<span class="rk-src">东财</span>';
      return row(i, s.name, s.pct_change, members || s.note || "", src);
    }).join("");
    $("rkBody").innerHTML = html +
      '<div class="rk-note">权重蓝筹/中小成长取成分指数均值、ST 板块取全部 ST 个股均值 —— ' +
      "均来自本站行情库；红利板块本地无股息率数据，用东财红利/高股息概念板块近似。</div>";
  }

  function noBoardData() {
    $("rkBody").innerHTML =
      '<div class="no-data" style="padding:24px 0;line-height:1.8;">' +
      "当日板块数据缺失（行情源限流或不可用）。<br>" +
      '<span style="font-size:12px;">板块涨跌来自外部行情源，每交易日 15:10 抓取一次；' +
      "抓取失败时如实显示缺失，不会拿旧数据充数。</span></div>";
  }

  var RENDER = { groups: renderGroups, industry: renderIndustry,
                 concept: renderConcept, special: renderSpecial };

  function switchTab(tab) {
    document.querySelectorAll(".rk-tab").forEach(function (t) {
      t.classList.toggle("active", t.getAttribute("data-tab") === tab);
    });
    (RENDER[tab] || renderGroups)();
  }

  function init() {
    var tabs = $("rkTabs");
    if (!tabs) return;   // 页面没有排行榜区块
    fetch("/api/sectors/ranking?top_n=10")
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (j) {
        data = j;
        $("rkDate").textContent = j.trade_date || "";
        tabs.querySelectorAll(".rk-tab").forEach(function (t) {
          t.addEventListener("click", function () {
            switchTab(t.getAttribute("data-tab"));
          });
        });
        switchTab("groups");
      })
      .catch(function () {
        $("rkBody").innerHTML = '<div class="no-data">板块数据加载失败，请稍后重试</div>';
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else { init(); }
})();
