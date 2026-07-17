/**
 * 我的数据看板
 * ============
 * 行情卡片(可切换股票/指数)+ 板块涨幅排行榜(4 个类目),卡片可以自由
 * 新增/删除,统一放在一块可自由拖拽/缩放的画布上,方便横向/纵向摆放比对。
 *
 * 卡片是"槽位"(id 是稳定的字符串,新增时随机生成),槽位里当前展示哪只
 * 股票/指数可以随时切换 —— 槽位 id 不随切换而变,拖拽/缩放不会因为换股票就丢。
 * 每张图表下方有可拖动的时间范围滑块(ECharts dataZoom)。
 *
 * 布局持久化:登录用户各自一份(存后端 user_board_layout,按账号区分);
 * 未登录统一用访客共享的默认布局 —— 未登录时保存的也是这份默认布局,
 * 下次(不论谁、登不登录)打开都能看到最近一次保存的样子(含新增/删除的卡片)。
 */
(function () {
  "use strict";

  // 首次访问(或重置布局后)展示的默认卡片集合;新增卡片没有默认股票,
  // 需要用户自己搜索选择。
  var DEFAULT_CARDS = [
    { id: "slot1", kind: "stock", code: "603993", type: "stock", name: "洛阳钼业" },
    { id: "slot2", kind: "stock", code: "000001", type: "index", name: "上证综合指数" },
    { id: "slot3", kind: "stock", code: null, type: null, name: null },
    { id: "rk_groups", kind: "rank" },
    { id: "rk_industry", kind: "rank" },
    { id: "rk_concept", kind: "rank" },
    { id: "rk_special", kind: "rank" },
  ];
  var DAYS_BACK = 180;   // 拉近半年(自然日),图表下方滑块可再拖出子区间
  var MAX_BOARD_CARDS = 50;

  var $ = function (id) { return document.getElementById(id); };
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function fmtDate(d) {
    return d.getFullYear() + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
  }
  function num(v, digits) {
    return v == null ? "—" : Number(v).toFixed(digits === undefined ? 2 : digits);
  }
  function tagLabel(type) { return type === "index" ? "指数" : "个股"; }

  // 当前画布上的卡片(持久化的核心:哪些卡片存在、什么类型、什么顺序)
  var boardCards = [];   // [{id, kind: "stock"|"rank"}]

  /* ── 行情卡片(可切换) ──────────────────────────────────────────────── */

  var slotState = {};    // cardId -> {code, type, name} | null(未选择),仅 stock 卡片有
  var slotCharts = {};   // cardId -> echarts 实例(切换时先 dispose 再重建)
  var slotResizeHandlers = {};  // cardId -> 当前绑定的 resize 回调,重建前先解绑,避免累积泄漏

  function titleHtml(cur) {
    if (!cur) return '<span class="mb-name mb-name--empty">未选择</span>';
    return '<span class="mb-name">' + esc(cur.name || cur.code) + "</span>" +
      '<span class="mb-code">' + esc(cur.code) + "</span>" +
      '<span class="mb-tag mb-tag--' + cur.type + '">' + tagLabel(cur.type) + "</span>";
  }

  function stockCardHtml(card) {
    return '<div class="mb-card mb-stock-card" id="mbCard_' + card.id + '" data-card-id="' + card.id + '">' +
      '<div class="mb-card-head">' +
      '<div class="mb-card-title" id="mbTitle_' + card.id + '"></div>' +
      '<button type="button" class="mb-switch-btn" id="mbSwitch_' + card.id + '" title="切换股票/指数">切换</button>' +
      '<span class="mb-drag-handle" title="拖动排列位置,便于横向/纵向比对">⠿</span>' +
      '<button type="button" class="mb-card-close" data-remove-card="' + esc(card.id) + '" title="删除这张卡片">✕</button>' +
      "</div>" +
      '<div class="mb-card-body" id="mbBody_' + card.id + '"></div>' +
      '<div class="mb-search-pop" id="mbPop_' + card.id + '" hidden>' +
      '<input type="text" class="mb-search-input" id="mbSearchInput_' + card.id +
      '" placeholder="搜代码或名称,如 600519 / 茅台" autocomplete="off">' +
      '<div class="mb-search-results" id="mbSearchResults_' + card.id + '"></div>' +
      "</div>" +
      '<span class="mb-resize-handle" title="拖动右下角调整卡片大小"></span>' +
      "</div>";
  }

  function emptyBodyHtml() {
    return '<div class="mb-empty-slot"><p>这张卡片还没选股票/指数</p>' +
      '<button type="button" class="mb-empty-btn" data-empty-pick="1">+ 选择股票或指数</button></div>';
  }

  function renderSlotTitle(slot) {
    $("mbTitle_" + slot.id).innerHTML = titleHtml(slotState[slot.id]);
    var cardEl = $("mbCard_" + slot.id);
    if (cardEl) cardEl.classList.toggle("mb-card--empty", !slotState[slot.id]);
  }

  function renderSlotBody(slot) {
    var body = $("mbBody_" + slot.id);
    if (!slotState[slot.id]) { body.innerHTML = emptyBodyHtml(); return; }
    body.innerHTML = '<div class="mb-loading">加载中…</div>';
  }

  function fillQuote(slot, rows) {
    var body = $("mbBody_" + slot.id);
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
      '<span class="mb-chg-chip ' + cls + '">' + (chg == null ? "—" : sign + num(chg) + "%") + "</span>" +
      "</div>" +
      '<div class="mb-meta">最新 ' + esc(last.date) + " · 近 " + rows.length + " 个交易日 · 拖动图表下方滑块可放大看某段时间</div>" +
      '<div class="mb-chart" id="mbChart_' + slot.id + '"></div>';

    drawChart(slot.id, rows, chg);
  }

  function drawChart(slotId, rows, chg) {
    if (!window.echarts) return;
    var el = $("mbChart_" + slotId);
    if (slotCharts[slotId]) { slotCharts[slotId].dispose(); }
    if (slotResizeHandlers[slotId]) {
      window.removeEventListener("resize", slotResizeHandlers[slotId]);
      delete slotResizeHandlers[slotId];
    }
    var chart = echarts.init(el);
    slotCharts[slotId] = chart;
    var up = chg == null || chg >= 0;
    var color = up ? "#cf222e" : "#1a7f37";
    var dates = rows.map(function (r) { return r.date; });
    var closes = rows.map(function (r) { return r.close; });

    chart.setOption({
      grid: { left: 52, right: 14, top: 14, bottom: 40 },
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
      dataZoom: [
        { type: "inside", start: 0, end: 100 },
        {
          type: "slider", start: 0, end: 100, height: 16, bottom: 4,
          borderColor: "transparent", backgroundColor: "#f6f8fa",
          fillerColor: "rgba(43,111,224,.15)", moveHandleSize: 0,
          handleStyle: { color: "#2b6fe0", borderColor: "#2b6fe0" },
          textStyle: { color: "#8a929c", fontSize: 10 },
        },
      ],
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
    var onResize = function () { chart.resize(); };
    slotResizeHandlers[slotId] = onResize;
    window.addEventListener("resize", onResize);
  }

  function loadSlot(slot) {
    var cur = slotState[slot.id];
    renderSlotBody(slot);
    if (!cur) { refreshCanvas(); return; }

    var end = new Date();
    var start = new Date(end.getTime() - DAYS_BACK * 86400000);
    var qs = "?start_date=" + fmtDate(start) + "&end_date=" + fmtDate(end);
    var url = (cur.type === "index" ? "/api/index/" : "/api/stock/") +
      encodeURIComponent(cur.code) + "/kline" + qs;

    fetch(url)
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (j) { fillQuote(slot, j.data || []); refreshCanvas(); })
      .catch(function (e) {
        $("mbBody_" + slot.id).innerHTML = '<div class="mb-error">加载失败：' + esc(e.message) + "</div>";
        refreshCanvas();
      });
  }

  /* ── 搜索切换 ──────────────────────────────────────────────────────── */

  var searchTimer = null;

  function closeAllPops(exceptId) {
    boardCards.forEach(function (c) {
      if (c.kind !== "stock") return;
      if (c.id !== exceptId) { var p = $("mbPop_" + c.id); if (p) p.hidden = true; }
    });
  }

  function openPop(slot) {
    closeAllPops(slot.id);
    closeAddMenu();
    var pop = $("mbPop_" + slot.id);
    pop.hidden = false;
    var input = $("mbSearchInput_" + slot.id);
    input.value = "";
    $("mbSearchResults_" + slot.id).innerHTML = '<div class="mb-search-hint">输入代码或名称搜索</div>';
    input.focus();
  }

  function doSearch(slot, q) {
    var resultsEl = $("mbSearchResults_" + slot.id);
    if (!q) { resultsEl.innerHTML = '<div class="mb-search-hint">输入代码或名称搜索</div>'; return; }
    resultsEl.innerHTML = '<div class="mb-search-hint">搜索中…</div>';
    fetch("/api/my_board/search?q=" + encodeURIComponent(q))
      .then(function (r) { return r.ok ? r.json() : { results: [] }; })
      .then(function (j) {
        var list = j.results || [];
        if (!list.length) { resultsEl.innerHTML = '<div class="mb-search-hint">没有匹配结果</div>'; return; }
        resultsEl.innerHTML = list.map(function (it) {
          return '<button type="button" class="mb-search-item" data-code="' + esc(it.code) +
            '" data-type="' + esc(it.type) + '" data-name="' + esc(it.name) + '">' +
            '<span class="mb-search-item-name">' + esc(it.name) + "</span>" +
            '<span class="mb-search-item-code">' + esc(it.code) + "</span>" +
            '<span class="mb-tag mb-tag--' + it.type + '">' + tagLabel(it.type) + "</span>" +
            "</button>";
        }).join("");
      })
      .catch(function () { resultsEl.innerHTML = '<div class="mb-search-hint">搜索失败，请稍后重试</div>'; });
  }

  function selectStock(slot, code, type, name) {
    slotState[slot.id] = { code: code, type: type, name: name };
    renderSlotTitle(slot);
    $("mbPop_" + slot.id).hidden = true;
    loadSlot(slot);
    saveSlotSelection(slot);
  }

  function bindSwitchUI(slot) {
    $("mbSwitch_" + slot.id).addEventListener("click", function (e) {
      e.stopPropagation();
      openPop(slot);
    });
    $("mbCard_" + slot.id).addEventListener("click", function (e) {
      if (e.target && e.target.getAttribute("data-empty-pick")) {
        e.stopPropagation();
        openPop(slot);
      }
    });
    $("mbPop_" + slot.id).addEventListener("click", function (e) { e.stopPropagation(); });
    var input = $("mbSearchInput_" + slot.id);
    input.addEventListener("input", function () {
      clearTimeout(searchTimer);
      var v = input.value.trim();
      searchTimer = setTimeout(function () { doSearch(slot, v); }, 250);
    });
    $("mbSearchResults_" + slot.id).addEventListener("click", function (e) {
      var item = e.target.closest(".mb-search-item");
      if (!item) return;
      selectStock(slot, item.getAttribute("data-code"), item.getAttribute("data-type"), item.getAttribute("data-name"));
    });
  }

  document.addEventListener("click", function () { closeAllPops(null); closeAddMenu(); });

  /* ── 板块涨幅排行榜(可增删的分类小卡片) ──────────────────────────────── */

  function pctHtml(v) {
    if (v == null) return '<span class="rk-pct rk-flat">—</span>';
    var cls = v > 0 ? "rk-up" : (v < 0 ? "rk-down" : "rk-flat");
    return '<span class="rk-pct ' + cls + '">' + (v > 0 ? "+" : "") + Number(v).toFixed(2) + "%</span>";
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
  function noBoardDataHtml() {
    return '<div class="no-data" style="padding:16px 0;line-height:1.8;">' +
      "当日板块数据缺失（行情源限流或不可用）。<br>" +
      '<span style="font-size:12px;">板块涨跌来自外部行情源，每交易日 15:10 抓取一次；' +
      "抓取失败时如实显示缺失，不会拿旧数据充数。</span></div>";
  }

  function groupsHtml(data) {
    if (!data.board_data_ok) return noBoardDataHtml();
    var html = data.groups.map(function (g, i) {
      var tops = (g.top || []).map(function (t) { return t.name; }).join("、");
      return row(i, g.name, g.avg_pct, g.board_count + " 个板块" + (tops ? " · 领涨:" + tops : ""));
    }).join("");
    var note = '<div class="rk-note">按组内行业板块的平均涨跌幅排序。' +
      (data.unmapped ? "有 " + data.unmapped + " 个板块未归入任何分组，未计入统计。" : "") + "</div>";
    return (html || '<div class="no-data">暂无数据</div>') + note;
  }

  function industryHtml(data) {
    if (!data.board_data_ok) return noBoardDataHtml();
    var top = data.industry_top.map(function (b, i) {
      return row(i, b.name, b.pct_change, (b.group || "未归类") + (b.leader ? " · 领涨 " + b.leader : ""),
        b.is_theme ? '<span class="rk-hot">热点</span>' : "");
    }).join("");
    var bottom = data.industry_bottom.map(function (b, i) {
      return row(i, b.name, b.pct_change, b.group || "未归类");
    }).join("");
    return '<div class="rk-group-label">涨幅前 ' + data.industry_top.length + "</div>" + top +
      '<div class="rk-group-label">跌幅前 ' + data.industry_bottom.length + "</div>" + bottom;
  }

  function conceptHtml(data) {
    if (!data.board_data_ok) return noBoardDataHtml();
    var html = data.concept_top.map(function (b, i) {
      return row(i, b.name, b.pct_change, b.leader ? "领涨 " + b.leader : "",
        b.is_theme ? '<span class="rk-hot">热点</span>' : "");
    }).join("");
    return (html || '<div class="no-data">暂无数据</div>') +
      '<div class="rk-note">题材概念按当日涨幅取前 N —— 资金炒作什么就冒出什么，' +
      "不靠关键词白名单筛（新热点常常名字里不含关键词）。</div>";
  }

  function specialHtml(data) {
    var html = data.special.map(function (s, i) {
      var members = (s.members || []).map(function (m) {
        return m.name + " " + (m.pct_change > 0 ? "+" : "") + m.pct_change + "%";
      }).join(" / ");
      var src = s.source === "local"
        ? '<span class="rk-src">本站行情库</span>'
        : '<span class="rk-src">东财</span>';
      return row(i, s.name, s.pct_change, members || s.note || "", src);
    }).join("");
    return html + '<div class="rk-note">权重蓝筹/中小成长取成分指数均值、ST 板块取全部 ST 个股均值 —— ' +
      "均来自本站行情库；红利板块本地无股息率数据，用东财红利/高股息概念板块近似。</div>";
  }

  // 排行榜卡片的固定类目全集(名称 + 渲染函数),这份表决定了"添加卡片"
  // 菜单里能选哪些排行榜类目 —— 与行情卡片不同,排行榜类目是有限、写死的。
  var RANK_INFO = {
    rk_groups: { name: "六大板块排行", render: groupsHtml },
    rk_industry: { name: "行业板块排行", render: industryHtml },
    rk_concept: { name: "题材概念排行", render: conceptHtml },
    rk_special: { name: "特殊概念排行", render: specialHtml },
  };
  var lastRankingData = null;   // 缓存最近一次排行榜响应,重新添加排行卡片时不用再等一次网络请求

  function rankCardHtml(card) {
    var name = (RANK_INFO[card.id] && RANK_INFO[card.id].name) || card.id;
    return '<div class="mb-card mb-rank-card" id="mbCard_' + card.id + '" data-card-id="' + card.id + '">' +
      '<div class="mb-card-head">' +
      '<span class="mb-name">' + esc(name) + "</span>" +
      '<span class="mb-rank-date" id="mbRankDate_' + card.id + '"></span>' +
      '<span class="mb-drag-handle" title="拖动排列位置,便于横向/纵向比对">⠿</span>' +
      '<button type="button" class="mb-card-close" data-remove-card="' + esc(card.id) + '" title="删除这张卡片">✕</button>' +
      "</div>" +
      '<div class="mb-rank-body" id="mbRankBody_' + card.id + '"><div class="mb-loading">加载中…</div></div>' +
      '<span class="mb-resize-handle" title="拖动右下角调整卡片大小"></span>' +
      "</div>";
  }

  function loadRanking() {
    var rankCards = boardCards.filter(function (c) { return c.kind === "rank"; });
    if (!rankCards.length) return;
    fetch("/api/sectors/ranking?top_n=10")
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (j) {
        lastRankingData = j;
        rankCards.forEach(function (c) {
          var dateEl = $("mbRankDate_" + c.id);
          if (dateEl) dateEl.textContent = j.trade_date || "";
          var bodyEl = $("mbRankBody_" + c.id);
          if (bodyEl && RANK_INFO[c.id]) bodyEl.innerHTML = RANK_INFO[c.id].render(j);
        });
        refreshCanvas();
      })
      .catch(function () {
        rankCards.forEach(function (c) {
          var bodyEl = $("mbRankBody_" + c.id);
          if (bodyEl) bodyEl.innerHTML = '<div class="no-data">板块数据加载失败，请稍后重试</div>';
        });
        refreshCanvas();
      });
  }

  /* ── 自由拖拽/缩放画布(布局存后端,登录按账号、未登录用访客共享默认布局) ── */

  var CANVAS_BREAKPOINT = 760;
  var CARD_W = 420;
  var GAP = 16;
  var SNAP = 8;
  var ROW_H = 420;
  var SAVE_DEBOUNCE = 500;
  var MIN_CARD_W = 300, MIN_CARD_H = 200;
  var MAX_CARD_W = 900, MAX_CARD_H = 900;

  var canvasState = null;
  var currentLayout = {};   // 从后端拉到的坐标/尺寸,拖拽/缩放/切换股票都往这同一份对象里写
  var saveTimer = null;

  function fetchBoard() {
    return fetch("/api/my_board/layout")
      .then(function (r) { return r.ok ? r.json() : { layout: {} }; })
      .then(function (j) { return j.layout || {}; })
      .catch(function () { return {}; });
  }

  function doSaveBoard() {
    return fetch("/api/my_board/layout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ layout: { cards: boardCards, positions: currentLayout } }),
    }).catch(function () { /* 静默失败,不打扰用户 */ });
  }

  function queueSaveBoard() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      saveTimer = null;
      doSaveBoard();
    }, SAVE_DEBOUNCE);
  }

  // 防抖计时器还没触发时用户就关闭/切走页面,会丢最后一次拖动/切换/增删 ——
  // 页面隐藏前用 sendBeacon 补发一次(不受页面卸载影响,比 fetch 更可靠)。
  function flushSaveBoard() {
    if (saveTimer == null) return;
    clearTimeout(saveTimer);
    saveTimer = null;
    var payload = JSON.stringify({ layout: { cards: boardCards, positions: currentLayout } });
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/api/my_board/layout", new Blob([payload], { type: "application/json" }));
    } else {
      doSaveBoard();
    }
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") flushSaveBoard();
  });
  window.addEventListener("pagehide", flushSaveBoard);

  function saveSlotSelection(slot) {
    var entry = currentLayout[slot.id] || {};
    var cardEl = $("mbCard_" + slot.id);
    if (cardEl) {
      entry.left = parseFloat(cardEl.style.left) || 0;
      entry.top = parseFloat(cardEl.style.top) || 0;
    }
    var cur = slotState[slot.id];
    if (cur) { entry.code = cur.code; entry.type = cur.type; entry.name = cur.name; }
    else { delete entry.code; delete entry.type; delete entry.name; }
    currentLayout[slot.id] = entry;
    queueSaveBoard();
  }

  function resizeCanvasHeight(grid, cards) {
    var maxBottom = 0;
    cards.forEach(function (card) {
      var bottom = card.offsetTop + card.offsetHeight;
      if (bottom > maxBottom) maxBottom = bottom;
    });
    grid.style.height = (maxBottom + 4) + "px";
  }

  function refreshCanvas() {
    if (canvasState) resizeCanvasHeight(canvasState.grid, canvasState.cards);
  }

  function bindDrag(grid, card, handle, allCards) {
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

      var maxLeft = Math.max(0, grid.clientWidth - w);
      card.style.left = Math.max(0, Math.min(left, maxLeft)) + "px";
      card.style.top = Math.max(0, top) + "px";
      resizeCanvasHeight(grid, allCards);
    }

    function onUp(e) {
      if (!dragging) return;
      dragging = false;
      card.classList.remove("dragging");
      clearGuides();
      var cardId = card.getAttribute("data-card-id");
      var entry = currentLayout[cardId] || {};
      entry.left = parseFloat(card.style.left) || 0;
      entry.top = parseFloat(card.style.top) || 0;
      currentLayout[cardId] = entry;
      queueSaveBoard();
      try { handle.releasePointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    }

    handle.addEventListener("pointerdown", onDown);
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  }

  function bindResize(grid, card, handle, allCards, cardId) {
    var startX, startY, origW, origH, resizing = false;

    function onDown(e) {
      resizing = true;
      startX = e.clientX; startY = e.clientY;
      origW = card.offsetWidth; origH = card.offsetHeight;
      card.classList.add("resizing");
      card.style.zIndex = 100;
      try { handle.setPointerCapture(e.pointerId); } catch (err) { /* 不支持就退化成普通拖动 */ }
      e.preventDefault();
      e.stopPropagation();
    }

    function onMove(e) {
      if (!resizing) return;
      var w = Math.min(MAX_CARD_W, Math.max(MIN_CARD_W, origW + (e.clientX - startX)));
      var h = Math.min(MAX_CARD_H, Math.max(MIN_CARD_H, origH + (e.clientY - startY)));
      card.style.width = w + "px";
      card.style.height = h + "px";
      if (slotCharts[cardId]) slotCharts[cardId].resize();
      resizeCanvasHeight(grid, allCards);
    }

    function onUp(e) {
      if (!resizing) return;
      resizing = false;
      card.classList.remove("resizing");
      var entry = currentLayout[cardId] || {};
      entry.width = card.offsetWidth;
      entry.height = card.offsetHeight;
      currentLayout[cardId] = entry;
      queueSaveBoard();
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
    var perRow = Math.max(1, Math.floor((grid.clientWidth + GAP) / (CARD_W + GAP)));

    cards.forEach(function (card, i) {
      var saved = currentLayout[card.getAttribute("data-card-id")];
      var pos = saved || defaultPos(i, perRow);
      card.style.left = pos.left + "px";
      card.style.top = pos.top + "px";
      card.style.zIndex = 10 + i;
      if (saved && saved.width) card.style.width = saved.width + "px";
      if (saved && saved.height) card.style.height = saved.height + "px";
    });
    resizeCanvasHeight(grid, cards);

    cards.forEach(function (card) {
      var cardId = card.getAttribute("data-card-id");
      var handle = card.querySelector(".mb-drag-handle");
      if (handle) bindDrag(grid, card, handle, cards);
      var resizeHandle = card.querySelector(".mb-resize-handle");
      if (resizeHandle) bindResize(grid, card, resizeHandle, cards, cardId);
    });

    window.addEventListener("resize", function () {
      cards.forEach(function (card) {
        var maxLeft = Math.max(0, grid.clientWidth - card.offsetWidth);
        var left = Math.min(Math.max(0, parseFloat(card.style.left) || 0), maxLeft);
        card.style.left = left + "px";
      });
      resizeCanvasHeight(grid, cards);
    });

    return { grid: grid, cards: cards };
  }

  function bindResetButton() {
    var btn = $("mbResetLayout");
    if (!btn) return;
    btn.addEventListener("click", function () {
      btn.disabled = true;
      clearTimeout(saveTimer);   // 丢弃还没发出的旧防抖保存,避免它在重置之后又把布局改回去
      saveTimer = null;
      fetch("/api/my_board/layout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ layout: {} }),
      }).catch(function () { /* 忽略,反正下面都会 reload */ })
        .then(function () { window.location.reload(); });
    });
  }

  /* ── 新增/删除卡片 ─────────────────────────────────────────────────── */

  var cardIdCounter = 0;
  function genCardId() {
    cardIdCounter += 1;
    return "card_" + Date.now().toString(36) + "_" + cardIdCounter;
  }

  function nextAppendPos() {
    var cards = canvasState ? canvasState.cards : [];
    var maxBottom = 0;
    cards.forEach(function (c) {
      var top = parseFloat(c.style.top) || 0;
      var bottom = top + c.offsetHeight;
      if (bottom > maxBottom) maxBottom = bottom;
    });
    return { left: 0, top: cards.length ? maxBottom + GAP : 0 };
  }

  function mountCardHtml(html) {
    var grid = $("mbGrid");
    var tmp = document.createElement("div");
    tmp.innerHTML = html;
    var el = tmp.firstElementChild;
    grid.appendChild(el);
    return el;
  }

  function placeNewCard(cardEl) {
    if (!canvasState) return;
    var pos = nextAppendPos();
    cardEl.style.left = pos.left + "px";
    cardEl.style.top = pos.top + "px";
    cardEl.style.zIndex = 10 + canvasState.cards.length;
    canvasState.cards.push(cardEl);
    var handle = cardEl.querySelector(".mb-drag-handle");
    if (handle) bindDrag(canvasState.grid, cardEl, handle, canvasState.cards);
    var resizeHandle = cardEl.querySelector(".mb-resize-handle");
    if (resizeHandle) bindResize(canvasState.grid, cardEl, resizeHandle, canvasState.cards, cardEl.getAttribute("data-card-id"));
    refreshCanvas();
  }

  function addStockCard() {
    if (boardCards.length >= MAX_BOARD_CARDS) return;
    var id = genCardId();
    var card = { id: id, kind: "stock" };
    boardCards.push(card);
    var el = mountCardHtml(stockCardHtml(card));
    slotState[id] = null;
    renderSlotTitle(card);
    renderSlotBody(card);
    bindSwitchUI(card);
    placeNewCard(el);
    queueSaveBoard();
    closeAddMenu();
    openPop(card);
  }

  function addRankCard(rankId) {
    if (boardCards.length >= MAX_BOARD_CARDS) return;
    if (!RANK_INFO[rankId] || boardCards.some(function (c) { return c.id === rankId; })) return;
    var card = { id: rankId, kind: "rank" };
    boardCards.push(card);
    var el = mountCardHtml(rankCardHtml(card));
    placeNewCard(el);
    queueSaveBoard();
    closeAddMenu();
    if (lastRankingData) {
      var dateEl = $("mbRankDate_" + rankId);
      if (dateEl) dateEl.textContent = lastRankingData.trade_date || "";
      var bodyEl = $("mbRankBody_" + rankId);
      if (bodyEl) bodyEl.innerHTML = RANK_INFO[rankId].render(lastRankingData);
      refreshCanvas();
    } else {
      loadRanking();
    }
  }

  function removeCard(cardId) {
    var idx = -1;
    for (var i = 0; i < boardCards.length; i++) {
      if (boardCards[i].id === cardId) { idx = i; break; }
    }
    if (idx === -1) return;
    var card = boardCards[idx];
    var el = $("mbCard_" + cardId);
    boardCards.splice(idx, 1);

    if (canvasState && el) {
      var ci = canvasState.cards.indexOf(el);
      if (ci !== -1) canvasState.cards.splice(ci, 1);
    }
    if (card.kind === "stock") {
      if (slotCharts[cardId]) { slotCharts[cardId].dispose(); delete slotCharts[cardId]; }
      if (slotResizeHandlers[cardId]) {
        window.removeEventListener("resize", slotResizeHandlers[cardId]);
        delete slotResizeHandlers[cardId];
      }
      delete slotState[cardId];
    }
    if (el) el.remove();
    delete currentLayout[cardId];
    refreshCanvas();
    queueSaveBoard();
  }

  /* ── "添加卡片"菜单 ────────────────────────────────────────────────── */

  function hiddenRankIds() {
    var present = {};
    boardCards.forEach(function (c) { if (c.kind === "rank") present[c.id] = true; });
    return Object.keys(RANK_INFO).filter(function (id) { return !present[id]; });
  }

  function renderAddMenu() {
    var pop = $("mbAddPop");
    if (!pop) return;
    var hidden = hiddenRankIds();
    var html = '<button type="button" class="mb-add-item" data-add="stock">+ 新增行情卡片</button>';
    hidden.forEach(function (id) {
      html += '<button type="button" class="mb-add-item" data-add="rank" data-rank-id="' +
        esc(id) + '">+ ' + esc(RANK_INFO[id].name) + "</button>";
    });
    pop.innerHTML = html;
  }

  function openAddMenu() {
    closeAllPops(null);
    renderAddMenu();
    $("mbAddPop").hidden = false;
  }
  function closeAddMenu() {
    var p = $("mbAddPop");
    if (p) p.hidden = true;
  }

  function bindAddMenu() {
    var btn = $("mbAddCard");
    var pop = $("mbAddPop");
    if (!btn || !pop) return;
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (pop.hidden) openAddMenu(); else closeAddMenu();
    });
    pop.addEventListener("click", function (e) {
      e.stopPropagation();
      var item = e.target.closest("[data-add]");
      if (!item) return;
      if (item.getAttribute("data-add") === "stock") addStockCard();
      else addRankCard(item.getAttribute("data-rank-id"));
    });
  }

  /* ── 入口 ──────────────────────────────────────────────────────────── */

  function cardHtmlFor(card) {
    return card.kind === "rank" ? rankCardHtml(card) : stockCardHtml(card);
  }

  function load() {
    var grid = $("mbGrid");
    bindResetButton();
    bindAddMenu();
    grid.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-remove-card]");
      if (!btn) return;
      e.stopPropagation();
      removeCard(btn.getAttribute("data-remove-card"));
    });

    fetchBoard().then(function (saved) {
      var savedCards = Array.isArray(saved.cards)
        ? saved.cards.filter(function (c) {
            return c && typeof c.id === "string" && (c.kind === "stock" || c.kind === "rank");
          })
        : null;
      currentLayout = (saved.positions && typeof saved.positions === "object")
        ? saved.positions
        : (savedCards !== null ? {} : (saved && typeof saved === "object" ? saved : {}));
      boardCards = savedCards !== null
        ? savedCards
        : DEFAULT_CARDS.map(function (c) { return { id: c.id, kind: c.kind }; });

      grid.innerHTML = boardCards.map(cardHtmlFor).join("");

      boardCards.forEach(function (card) {
        if (card.kind !== "stock") return;
        var def = DEFAULT_CARDS.filter(function (d) { return d.id === card.id; })[0];
        var saved2 = currentLayout[card.id];
        if (saved2 && saved2.code && saved2.type) {
          slotState[card.id] = { code: saved2.code, type: saved2.type, name: saved2.name || saved2.code };
        } else if (def && def.code) {
          slotState[card.id] = { code: def.code, type: def.type, name: def.name };
        } else {
          slotState[card.id] = null;
        }
        renderSlotTitle(card);
        bindSwitchUI(card);
        loadSlot(card);
      });

      canvasState = initCanvas(grid);
      loadRanking();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
