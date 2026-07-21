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

  // 首次访问(或重置布局后)展示的默认卡片集合 —— 只放数据全部来自本站
  // 行情库、保证有内容的卡片,新用户第一屏不该出现空卡或"数据缺失"。
  // 依赖外部行情源的板块排行(rk_groups/rk_industry/rk_concept)不进默认,
  // 想看的用户从"添加卡片"菜单自己加。
  var DEFAULT_CARDS = [
    { id: "idx_overview", kind: "indices" },
    { id: "dr_summary", kind: "review" },
    { id: "hs_today", kind: "hotsector" },
    { id: "slot1", kind: "stock", code: "603993", type: "stock", name: "洛阳钼业" },
    { id: "slot2", kind: "stock", code: "000001", type: "index", name: "上证综合指数" },
    { id: "rk_special", kind: "rank" },
  ];
  var DAYS_BACK = 180;   // 拉近半年(自然日),图表下方滑块可再拖出子区间
  var MAX_BOARD_CARDS = 10;   // 与后端 service.MAX_CARDS 保持一致
  var MAX_COMPARE_STOCKS = 6;
  var COMPARE_COLORS = ["#0969da", "#cf222e", "#1a7f37", "#9a6700", "#8250df", "#bf3989"];

  var $ = function (id) { return document.getElementById(id); };
  // esc() 用 util.js 里全站共用的实现(必须比本文件先加载)
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
    if (!body) return;   // 请求还没回来卡片就被删了
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
          fillerColor: "rgba(9,105,218,.15)", moveHandleSize: 0,
          handleStyle: { color: "#0969da", borderColor: "#0969da" },
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
        var body = $("mbBody_" + slot.id);
        if (body) body.innerHTML = '<div class="mb-error">加载失败：' + esc(e.message) + "</div>";
        refreshCanvas();
      });
  }

  /* ── 搜索切换 ──────────────────────────────────────────────────────── */

  var searchTimer = null;

  function closeAllPops(exceptId) {
    boardCards.forEach(function (c) {
      if (c.kind !== "stock" && c.kind !== "compare") return;
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
    if (!resultsEl) return;   // 防抖计时器触发前卡片被删了
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

  /* ── 多股对比卡片(同时对比多支股票/指数区间涨跌幅曲线) ──────────────────
     卡片本身比行情卡片大一圈(见 CSS .mb-compare-card),方便看清多条曲线;
     由于各股票绝对价格量级差异很大(几元 vs 几百元),不直接画收盘价,
     而是换算成"相对区间首日收盘价的涨跌幅 %",这样才有可比性。         */

  var compareState = {};          // cardId -> [{code,type,name}, ...]
  var compareCharts = {};         // cardId -> echarts 实例
  var compareResizeHandlers = {}; // cardId -> 当前绑定的 resize 回调

  function compareCardHtml(card) {
    return '<div class="mb-card mb-compare-card" id="mbCard_' + card.id + '" data-card-id="' + card.id + '">' +
      '<div class="mb-card-head">' +
      '<span class="mb-name">多股对比</span>' +
      '<span class="mb-compare-count" id="mbCmpCount_' + card.id + '"></span>' +
      '<button type="button" class="mb-switch-btn" id="mbCmpAdd_' + card.id + '" title="添加股票/指数到对比">+ 添加</button>' +
      '<span class="mb-drag-handle" title="拖动排列位置,便于横向/纵向比对">⠿</span>' +
      '<button type="button" class="mb-card-close" data-remove-card="' + esc(card.id) + '" title="删除这张卡片">✕</button>' +
      "</div>" +
      '<div class="mb-compare-chips" id="mbCmpChips_' + card.id + '"></div>' +
      '<div class="mb-card-body" id="mbCmpBody_' + card.id + '"></div>' +
      '<div class="mb-search-pop" id="mbPop_' + card.id + '" hidden>' +
      '<input type="text" class="mb-search-input" id="mbSearchInput_' + card.id +
      '" placeholder="搜代码或名称,如 600519 / 茅台" autocomplete="off">' +
      '<div class="mb-search-results" id="mbSearchResults_' + card.id + '"></div>' +
      "</div>" +
      '<span class="mb-resize-handle" title="拖动右下角调整卡片大小"></span>' +
      "</div>";
  }

  function compareChipsHtml(cardId) {
    var list = compareState[cardId] || [];
    if (!list.length) return "";
    return list.map(function (it, i) {
      // 移除按钮要同时带 code 和 type:股票和指数存在同码(000001 是上证
      // 指数也是平安银行),只按 code 删会把两条一起误删
      return '<span class="mb-cmp-chip" style="--cmp-color:' + COMPARE_COLORS[i % COMPARE_COLORS.length] + '">' +
        '<span class="mb-cmp-dot"></span>' + esc(it.name || it.code) +
        '<button type="button" class="mb-cmp-remove" data-cmp-remove="' + esc(it.code) +
        '" data-cmp-remove-type="' + esc(it.type) + '" title="从对比中移除">✕</button>' +
        "</span>";
    }).join("");
  }

  function renderCompareChips(cardId) {
    var el = $("mbCmpChips_" + cardId);
    if (el) el.innerHTML = compareChipsHtml(cardId);
    var countEl = $("mbCmpCount_" + cardId);
    if (countEl) {
      var n = (compareState[cardId] || []).length;
      countEl.textContent = n ? n + "/" + MAX_COMPARE_STOCKS : "";
    }
  }

  function drawCompareChart(cardId, list, resultsList) {
    var body = $("mbCmpBody_" + cardId);
    if (!body) return;   // 请求还没回来卡片就被删了
    var hasAny = resultsList.some(function (r) { return r && r.length >= 2; });
    if (!hasAny) { body.innerHTML = '<div class="mb-error">暂无行情数据</div>'; return; }

    body.innerHTML = '<div class="mb-meta">近 ' + DAYS_BACK + ' 天涨跌幅对比（以区间首日收盘价为基准）· 拖动图表下方滑块可放大看某段时间</div>' +
      '<div class="mb-chart mb-compare-chart" id="mbCmpChart_' + cardId + '"></div>';
    if (!window.echarts) return;

    var el = $("mbCmpChart_" + cardId);
    if (compareCharts[cardId]) { compareCharts[cardId].dispose(); }
    if (compareResizeHandlers[cardId]) {
      window.removeEventListener("resize", compareResizeHandlers[cardId]);
      delete compareResizeHandlers[cardId];
    }
    var chart = echarts.init(el);
    compareCharts[cardId] = chart;

    var series = [];
    var legendNames = [];
    list.forEach(function (it, i) {
      var rows = resultsList[i] || [];
      var valid = rows.filter(function (r) { return r.date && r.close != null; });
      if (valid.length < 2) return;
      var base = valid[0].close;
      var data = valid.map(function (r) {
        return [r.date, base ? (r.close - base) / base * 100 : 0];
      });
      var name = it.name || it.code;
      legendNames.push(name);
      series.push({
        name: name,
        type: "line",
        data: data,
        showSymbol: false,
        lineStyle: { width: 2, color: COMPARE_COLORS[i % COMPARE_COLORS.length] },
        itemStyle: { color: COMPARE_COLORS[i % COMPARE_COLORS.length] },
      });
    });

    chart.setOption({
      grid: { left: 52, right: 16, top: 36, bottom: 46 },
      legend: {
        top: 0, left: 0, itemWidth: 10, itemHeight: 10,
        textStyle: { fontSize: 11, color: "#57606a" },
        data: legendNames,
      },
      tooltip: {
        trigger: "axis",
        formatter: function (params) {
          if (!params || !params.length) return "";
          var lines = [params[0].axisValueLabel || params[0].axisValue];
          params.forEach(function (p) {
            var v = p.data && p.data[1] != null ? p.data[1] : null;
            var sign = v != null && v > 0 ? "+" : "";
            lines.push(p.marker + p.seriesName + " " + (v == null ? "—" : sign + num(v) + "%"));
          });
          return lines.join("<br/>");
        },
      },
      xAxis: {
        type: "time",
        axisLine: { lineStyle: { color: "#d0d7de" } },
        axisLabel: { color: "#8a929c", fontSize: 11 },
      },
      yAxis: {
        type: "value",
        scale: true,
        splitLine: { lineStyle: { color: "#eef1f4" } },
        axisLabel: { color: "#8a929c", fontSize: 11, formatter: "{value}%" },
      },
      dataZoom: [
        { type: "inside", start: 0, end: 100 },
        {
          type: "slider", start: 0, end: 100, height: 16, bottom: 6,
          borderColor: "transparent", backgroundColor: "#f6f8fa",
          fillerColor: "rgba(9,105,218,.15)", moveHandleSize: 0,
          handleStyle: { color: "#0969da", borderColor: "#0969da" },
          textStyle: { color: "#8a929c", fontSize: 10 },
        },
      ],
      series: series,
    });

    var onResize = function () { chart.resize(); };
    compareResizeHandlers[cardId] = onResize;
    window.addEventListener("resize", onResize);
  }

  function loadCompare(card) {
    var list = compareState[card.id] || [];
    renderCompareChips(card.id);
    var body = $("mbCmpBody_" + card.id);
    if (!body) return;

    if (!list.length) {
      body.innerHTML = '<div class="mb-empty-slot"><p>这张卡片还没添加对比的股票/指数</p>' +
        '<button type="button" class="mb-empty-btn" data-cmp-empty-pick="1">+ 添加股票或指数</button></div>';
      refreshCanvas();
      return;
    }
    body.innerHTML = '<div class="mb-loading">加载中…</div>';

    var end = new Date();
    var start = new Date(end.getTime() - DAYS_BACK * 86400000);
    var qs = "?start_date=" + fmtDate(start) + "&end_date=" + fmtDate(end);

    Promise.all(list.map(function (it) {
      var url = (it.type === "index" ? "/api/index/" : "/api/stock/") +
        encodeURIComponent(it.code) + "/kline" + qs;
      return fetch(url)
        .then(function (r) { return r.ok ? r.json() : { data: [] }; })
        .then(function (j) { return j.data || []; })
        .catch(function () { return []; });
    })).then(function (results) {
      if (!compareState[card.id]) return;   // 请求还没回来卡片就被删了
      drawCompareChart(card.id, list, results);
      refreshCanvas();
    });
  }

  function saveCompareSelection(card) {
    var entry = currentLayout[card.id] || {};
    var cardEl = $("mbCard_" + card.id);
    if (cardEl) {
      entry.left = parseFloat(cardEl.style.left) || 0;
      entry.top = parseFloat(cardEl.style.top) || 0;
    }
    entry.codes = (compareState[card.id] || []).map(function (it) {
      return { code: it.code, type: it.type, name: it.name };
    });
    currentLayout[card.id] = entry;
    queueSaveBoard();
  }

  function addToCompare(card, code, type, name) {
    var list = compareState[card.id] = compareState[card.id] || [];
    if (list.some(function (it) { return it.code === code && it.type === type; })) return;
    if (list.length >= MAX_COMPARE_STOCKS) { showToast("最多同时对比 " + MAX_COMPARE_STOCKS + " 支股票/指数"); return; }
    list.push({ code: code, type: type, name: name });
    var input = $("mbSearchInput_" + card.id);
    if (input) input.value = "";
    var resultsEl = $("mbSearchResults_" + card.id);
    if (resultsEl) resultsEl.innerHTML = '<div class="mb-search-hint">输入代码或名称搜索</div>';
    loadCompare(card);
    saveCompareSelection(card);
  }

  function removeFromCompare(card, code, type) {
    var list = compareState[card.id] || [];
    compareState[card.id] = list.filter(function (it) {
      return !(it.code === code && it.type === type);
    });
    loadCompare(card);
    saveCompareSelection(card);
  }

  function bindCompareUI(card) {
    $("mbCmpAdd_" + card.id).addEventListener("click", function (e) {
      e.stopPropagation();
      openPop(card);
    });
    $("mbCard_" + card.id).addEventListener("click", function (e) {
      if (e.target && e.target.getAttribute("data-cmp-empty-pick")) {
        e.stopPropagation();
        openPop(card);
      }
    });
    $("mbPop_" + card.id).addEventListener("click", function (e) { e.stopPropagation(); });
    var input = $("mbSearchInput_" + card.id);
    input.addEventListener("input", function () {
      clearTimeout(searchTimer);
      var v = input.value.trim();
      searchTimer = setTimeout(function () { doSearch(card, v); }, 250);
    });
    $("mbSearchResults_" + card.id).addEventListener("click", function (e) {
      var item = e.target.closest(".mb-search-item");
      if (!item) return;
      addToCompare(card, item.getAttribute("data-code"), item.getAttribute("data-type"), item.getAttribute("data-name"));
    });
    $("mbCmpChips_" + card.id).addEventListener("click", function (e) {
      var btn = e.target.closest("[data-cmp-remove]");
      if (!btn) return;
      removeFromCompare(card, btn.getAttribute("data-cmp-remove"), btn.getAttribute("data-cmp-remove-type"));
    });
  }

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

  /* ── 单例卡片(每种全站只有一张,没有可配置内容,数据直接来自各自模块的
     现成接口):每日复盘摘要 / AI热门板块战绩 / 指数速览。跟排行卡片一样
     id 是写死的,"添加卡片"菜单里已存在就不再出现,不会重复添加。      */

  var SINGLETON_DEFS = [
    { id: "dr_summary", kind: "review", label: "每日复盘摘要" },
    { id: "hs_today", kind: "hotsector", label: "AI热门板块战绩" },
    { id: "idx_overview", kind: "indices", label: "指数速览" },
  ];

  function pctSpan(v, digits) {
    if (v == null) return '<span class="mb-flat">—</span>';
    var cls = v > 0 ? "mb-up" : (v < 0 ? "mb-down" : "mb-flat");
    return '<span class="' + cls + '">' + (v > 0 ? "+" : "") + num(v, digits) + "%</span>";
  }

  // 每日复盘摘要
  function reviewCardHtml(card) {
    return '<div class="mb-card mb-review-card" id="mbCard_' + card.id + '" data-card-id="' + card.id + '">' +
      '<div class="mb-card-head">' +
      '<span class="mb-name">每日复盘摘要</span>' +
      '<span class="mb-rank-date" id="mbDrDate_' + card.id + '"></span>' +
      '<span class="mb-drag-handle" title="拖动排列位置,便于横向/纵向比对">⠿</span>' +
      '<button type="button" class="mb-card-close" data-remove-card="' + esc(card.id) + '" title="删除这张卡片">✕</button>' +
      "</div>" +
      '<div class="mb-review-body" id="mbDrBody_' + card.id + '"><div class="mb-loading">加载中…</div></div>' +
      '<span class="mb-resize-handle" title="拖动右下角调整卡片大小"></span>' +
      "</div>";
  }

  function renderReviewCard(cardId, review) {
    var dateEl = $("mbDrDate_" + cardId);
    var body = $("mbDrBody_" + cardId);
    if (!body) return;   // 请求还没回来卡片就被删了
    if (dateEl) dateEl.textContent = review ? String(review.review_date || "") : "";
    if (!review) { body.innerHTML = '<div class="no-data">暂无复盘数据</div>'; return; }

    var ctx = review.context || {};
    var idxHtml = (ctx.indices || []).slice(0, 4).map(function (ix) {
      return '<div class="dr-idx-chip"><span class="dr-idx-name">' + esc(ix.name) + "</span>" +
        '<span class="dr-idx-val">' + num(ix.close) + "</span>" + pctSpan(ix.pct_change) + "</div>";
    }).join("");
    var b = ctx.breadth || {};
    var hs = (ctx.ai_hotsector || {}).settled;

    body.innerHTML =
      '<div class="mb-review-title">' + esc(review.title || "今日复盘") + "</div>" +
      '<div class="dr-idx-row">' + (idxHtml || '<div class="no-data">暂无指数数据</div>') + "</div>" +
      '<div class="rk-row"><div class="rk-name">涨 / 跌家数</div>' +
      '<span class="rk-pct rk-up">' + (b.up != null ? b.up : "—") + "</span>&nbsp;/&nbsp;" +
      '<span class="rk-pct rk-down">' + (b.down != null ? b.down : "—") + "</span></div>" +
      '<div class="rk-row"><div class="rk-name">AI 选股结算(昨买今卖)</div>' +
      (hs
        ? '<span class="rk-pct">' + hs.win_count + "/" + hs.total_count + " 涨</span>"
        : '<span class="rk-pct rk-flat">—</span>') + "</div>" +
      '<a class="mb-review-link" href="/daily_review">查看完整复盘 →</a>';
  }

  function loadReviewCard(card) {
    fetch("/api/daily_review/latest")
      .then(function (r) { return r.ok ? r.json() : { review: null }; })
      .then(function (j) { renderReviewCard(card.id, j.review); refreshCanvas(); })
      .catch(function () { renderReviewCard(card.id, null); refreshCanvas(); });
  }

  // AI热门板块战绩
  function hotsectorCardHtml(card) {
    return '<div class="mb-card mb-hotsector-card" id="mbCard_' + card.id + '" data-card-id="' + card.id + '">' +
      '<div class="mb-card-head">' +
      '<span class="mb-name">AI热门板块战绩</span>' +
      '<span class="mb-rank-date" id="mbHsDate_' + card.id + '"></span>' +
      '<span class="mb-drag-handle" title="拖动排列位置,便于横向/纵向比对">⠿</span>' +
      '<button type="button" class="mb-card-close" data-remove-card="' + esc(card.id) + '" title="删除这张卡片">✕</button>' +
      "</div>" +
      '<div class="mb-rank-body" id="mbHsBody_' + card.id + '"><div class="mb-loading">加载中…</div></div>' +
      '<span class="mb-resize-handle" title="拖动右下角调整卡片大小"></span>' +
      "</div>";
  }

  function renderHotsectorCard(cardId, pick, stats) {
    var dateEl = $("mbHsDate_" + cardId);
    var body = $("mbHsBody_" + cardId);
    if (!body) return;   // 请求还没回来卡片就被删了
    if (dateEl) dateEl.textContent = pick ? String(pick.pick_date || "") : "";
    if (!pick || !pick.stocks || !pick.stocks.length) {
      body.innerHTML = '<div class="no-data">今日暂无选股</div>';
      return;
    }

    var bySector = {};
    var order = [];
    pick.stocks.forEach(function (s) {
      if (!bySector[s.sector_name]) { bySector[s.sector_name] = []; order.push(s.sector_name); }
      bySector[s.sector_name].push(s);
    });
    var html = order.map(function (sector) {
      var rows = bySector[sector].map(function (s, i) {
        var extra = s.is_win == null ? "" :
          (s.is_win ? '<span class="mb-hs-tag mb-hs-tag--win">已结算·涨</span>' : '<span class="mb-hs-tag mb-hs-tag--lose">已结算·跌</span>');
        var pct = s.pct_change != null ? Number(s.pct_change) * 100 : null;
        return row(i, (s.name || s.code) + "(" + s.code + ")", pct, s.stock_reason, extra);
      }).join("");
      return '<div class="rk-group-label">' + esc(sector) + "</div>" + rows;
    }).join("");
    var statLine = stats && stats.total_count
      ? '<div class="rk-note">累计胜率 ' + stats.win_count + "/" + stats.total_count +
        "(" + (stats.win_rate != null ? num(stats.win_rate * 100, 1) : "—") + "%) · 累计收益 " +
        (stats.cum_return != null ? num(stats.cum_return * 100) + "%" : "—") + "</div>"
      : "";
    body.innerHTML = html + statLine;
  }

  function loadHotsectorCard(card) {
    Promise.all([
      fetch("/api/ai_hotsector/today").then(function (r) { return r.ok ? r.json() : { pick: null }; })
        .catch(function () { return { pick: null }; }),
      fetch("/api/ai_hotsector/stats").then(function (r) { return r.ok ? r.json() : null; })
        .catch(function () { return null; }),
    ]).then(function (res) {
      renderHotsectorCard(card.id, res[0].pick, res[1]);
      refreshCanvas();
    });
  }

  // 指数速览
  var OVERVIEW_INDICES = [
    { code: "000001", name: "上证指数" },
    { code: "399006", name: "创业板指" },
    { code: "000300", name: "沪深300" },
    { code: "000905", name: "中证500" },
    { code: "000852", name: "中证1000" },
    { code: "000016", name: "上证50" },
  ];

  function indicesCardHtml(card) {
    return '<div class="mb-card mb-indices-card" id="mbCard_' + card.id + '" data-card-id="' + card.id + '">' +
      '<div class="mb-card-head">' +
      '<span class="mb-name">指数速览</span>' +
      '<span class="mb-rank-date" id="mbIxDate_' + card.id + '"></span>' +
      '<span class="mb-drag-handle" title="拖动排列位置,便于横向/纵向比对">⠿</span>' +
      '<button type="button" class="mb-card-close" data-remove-card="' + esc(card.id) + '" title="删除这张卡片">✕</button>' +
      "</div>" +
      '<div class="mb-indices-grid" id="mbIxBody_' + card.id + '"><div class="mb-loading">加载中…</div></div>' +
      '<span class="mb-resize-handle" title="拖动右下角调整卡片大小"></span>' +
      "</div>";
  }

  function renderIndicesCard(cardId, resultsList) {
    var body = $("mbIxBody_" + cardId);
    if (!body) return;   // 请求还没回来卡片就被删了
    var dateEl = $("mbIxDate_" + cardId);
    var latestDate = "";
    var html = OVERVIEW_INDICES.map(function (ix, i) {
      var rows = resultsList[i] || [];
      if (!rows.length) {
        return '<div class="mb-ix-tile"><div class="mb-ix-name">' + esc(ix.name) + '</div><div class="mb-ix-val">—</div></div>';
      }
      var last = rows[rows.length - 1];
      var prev = rows.length > 1 ? rows[rows.length - 2] : null;
      if (last.date && last.date > latestDate) latestDate = last.date;
      var chg = prev && prev.close ? (last.close - prev.close) / prev.close * 100 : null;
      var cls = chg == null ? "mb-flat" : (chg > 0 ? "mb-up" : (chg < 0 ? "mb-down" : "mb-flat"));
      var sign = chg != null && chg > 0 ? "+" : "";
      return '<div class="mb-ix-tile"><div class="mb-ix-name">' + esc(ix.name) + '</div>' +
        '<div class="mb-ix-val ' + cls + '">' + num(last.close) + "</div>" +
        '<div class="mb-ix-chg ' + cls + '">' + (chg == null ? "—" : sign + num(chg) + "%") + "</div></div>";
    }).join("");
    if (dateEl) dateEl.textContent = latestDate;
    body.innerHTML = html;
  }

  function loadIndicesCard(card) {
    var end = new Date();
    var start = new Date(end.getTime() - 15 * 86400000);
    var qs = "?start_date=" + fmtDate(start) + "&end_date=" + fmtDate(end);
    Promise.all(OVERVIEW_INDICES.map(function (ix) {
      return fetch("/api/index/" + encodeURIComponent(ix.code) + "/kline" + qs)
        .then(function (r) { return r.ok ? r.json() : { data: [] }; })
        .then(function (j) { return j.data || []; })
        .catch(function () { return []; });
    })).then(function (results) {
      renderIndicesCard(card.id, results);
      refreshCanvas();
    });
  }

  function loadSingleton(card) {
    if (card.kind === "review") loadReviewCard(card);
    else if (card.kind === "hotsector") loadHotsectorCard(card);
    else if (card.kind === "indices") loadIndicesCard(card);
  }

  function singletonCardHtml(card) {
    if (card.kind === "review") return reviewCardHtml(card);
    if (card.kind === "hotsector") return hotsectorCardHtml(card);
    return indicesCardHtml(card);
  }

  function hiddenSingletonDefs() {
    var present = {};
    boardCards.forEach(function (c) { present[c.id] = true; });
    return SINGLETON_DEFS.filter(function (d) { return !present[d.id]; });
  }

  function addSingletonCard(id) {
    if (boardCards.length >= MAX_BOARD_CARDS) return;
    var def = SINGLETON_DEFS.filter(function (d) { return d.id === id; })[0];
    if (!def || boardCards.some(function (c) { return c.id === id; })) return;
    var card = { id: def.id, kind: def.kind };
    boardCards.push(card);
    updateEmptyCanvasHint();
    var el = mountCardHtml(singletonCardHtml(card));
    placeNewCard(el);
    queueSaveBoard();
    closeAddMenu();
    loadSingleton(card);
  }

  /* ── 自由拖拽/缩放画布(布局存后端,登录按账号、未登录用访客共享默认布局) ── */

  var CANVAS_BREAKPOINT = 760;
  var CARD_W = 420;
  var GAP = 16;
  var SNAP = 8;
  var ROW_H = 420;
  var SAVE_DEBOUNCE = 500;
  var MIN_CARD_W = 300, MIN_CARD_H = 300;
  var EMPTY_CANVAS_H = 160;
  var MAX_CARD_W = 900, MAX_CARD_H = 900;

  var canvasState = null;
  var currentLayout = {};   // 从后端拉到的坐标/尺寸,拖拽/缩放/切换股票都往这同一份对象里写
  var saveTimer = null;
  var zCounter = 100;       // 每次拖拽/缩放递增,保证"最后操作的卡片在最上层"

  function fetchBoard() {
    return fetch("/api/my_board/layout")
      .then(function (r) { return r.ok ? r.json() : { layout: {} }; })
      .then(function (j) { return j.layout || {}; })
      .catch(function () { return {}; });
  }

  // 通用底部提示条(保存失败/操作到达上限等一次性反馈),同一时刻只留一条,
  // 避免连续触发时堆叠出一排。
  var toastTimer = null, toastEl = null;
  function showToast(text, ms) {
    if (toastEl) { toastEl.remove(); clearTimeout(toastTimer); }
    toastEl = document.createElement("div");
    toastEl.className = "mb-save-error";
    toastEl.textContent = text;
    document.body.appendChild(toastEl);
    toastTimer = setTimeout(function () {
      toastTimer = null;
      if (toastEl) { toastEl.remove(); toastEl = null; }
    }, ms || 3000);
  }

  // 保存被服务端拒绝(校验 400 之类)是持续性的:用户继续拖半天,其实一笔
  // 都没存上,必须让他知道。节流 8s 提示一次,别每次防抖保存都弹。
  var saveErrTimer = null;
  function notifySaveError() {
    if (saveErrTimer) return;
    showToast("布局保存失败，最近的改动可能不会保留", 8000);
    saveErrTimer = setTimeout(function () { saveErrTimer = null; }, 8000);
  }

  function doSaveBoard() {
    return fetch("/api/my_board/layout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ layout: { cards: boardCards, positions: currentLayout } }),
      keepalive: true,   // 防抖触发后立刻关页,请求也别被浏览器取消
    }).then(function (r) {
      if (!r.ok) notifySaveError();
    }).catch(function () { /* 网络抖动是暂时的,静默,下次保存自然重试 */ });
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
    if (!cards.length) { grid.style.height = EMPTY_CANVAS_H + "px"; return; }
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

  // 卡片全部删光后画布不能留一片看不出所以然的空白 —— 补一句提示,
  // 引导去右上角"添加卡片"重新加回来。
  function emptyCanvasHintHtml() {
    return '<div class="mb-empty-canvas" id="mbEmptyCanvas">这里空了，点右上角"添加卡片"新增一张</div>';
  }

  function updateEmptyCanvasHint() {
    var grid = $("mbGrid");
    if (!grid) return;
    var existing = $("mbEmptyCanvas");
    if (boardCards.length === 0) {
      if (!existing) grid.insertAdjacentHTML("beforeend", emptyCanvasHintHtml());
    } else if (existing) {
      existing.remove();
    }
    refreshCanvas();
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
      zCounter += 1;
      card.style.zIndex = zCounter;
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
      zCounter += 1;
      card.style.zIndex = zCounter;
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
      if (compareCharts[cardId]) compareCharts[cardId].resize();
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

  // 行式装箱:按当前视觉顺序(先 top 后 left)排,一行放不下就换行,行高取
  // 该行最高的卡片 —— 卡片尺寸各异(对比卡 760 宽、用户还可以任意 resize),
  // 固定网格摆法要么重叠要么留大缝,按实际渲染尺寸装箱才整齐。卡片高度由
  // CSS 统一保底(内容超出在卡片内滚动),所以量出来的尺寸在数据加载前后
  // 一致,不会"加载完长高压住下一行"。
  function packCards(cards, gridWidth, startY) {
    var sorted = cards.slice().sort(function (a, b) {
      var at = parseFloat(a.style.top) || 0, bt = parseFloat(b.style.top) || 0;
      if (Math.abs(at - bt) > 40) return at - bt;
      return (parseFloat(a.style.left) || 0) - (parseFloat(b.style.left) || 0);
    });
    var x = 0, y = startY || 0, rowH = 0;
    sorted.forEach(function (card) {
      var w = card.offsetWidth, h = card.offsetHeight;
      if (x > 0 && x + w > gridWidth) { x = 0; y += rowH + GAP; rowH = 0; }
      card.style.left = x + "px";
      card.style.top = y + "px";
      x += w + GAP;
      if (h > rowH) rowH = h;
    });
  }

  function initCanvas(grid) {
    if (window.innerWidth <= CANVAS_BREAKPOINT) return null;   // 窄屏走堆叠布局,不启用画布
    grid.classList.add("mb-canvas-mode");

    var cards = Array.prototype.slice.call(grid.querySelectorAll(".mb-card"));
    var unplaced = [];

    cards.forEach(function (card, i) {
      var saved = currentLayout[card.getAttribute("data-card-id")];
      card.style.zIndex = 10 + i;
      if (saved && saved.width) card.style.width = saved.width + "px";
      if (saved && saved.height) card.style.height = saved.height + "px";
      if (saved) {
        card.style.left = saved.left + "px";
        card.style.top = saved.top + "px";
      } else {
        unplaced.push(card);
      }
    });
    // 没有保存过坐标的卡片(新访客默认布局/别处新加的卡片种类):从已摆放
    // 卡片的下方开始装箱,不跟用户摆好的位置打架。
    if (unplaced.length) {
      var startY = 0;
      cards.forEach(function (c) {
        if (unplaced.indexOf(c) !== -1) return;
        var bottom = (parseFloat(c.style.top) || 0) + c.offsetHeight;
        if (bottom + GAP > startY) startY = bottom + GAP;
      });
      packCards(unplaced, grid.clientWidth, startY);
    }
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

  // 一键整理:按真实渲染尺寸重新装箱(此时内容已加载,量到的就是成品高度),
  // 拖乱了/删出窟窿了点一下就恢复整齐,不用一张张手动对齐。
  function bindTidyButton() {
    var btn = $("mbTidyLayout");
    if (!btn) return;
    btn.addEventListener("click", function () {
      if (!canvasState || !canvasState.cards.length) return;
      packCards(canvasState.cards, canvasState.grid.clientWidth, 0);
      canvasState.cards.forEach(function (card) {
        var cardId = card.getAttribute("data-card-id");
        var entry = currentLayout[cardId] || {};
        entry.left = parseFloat(card.style.left) || 0;
        entry.top = parseFloat(card.style.top) || 0;
        currentLayout[cardId] = entry;
      });
      refreshCanvas();
      queueSaveBoard();
    });
  }

  function bindResetButton() {
    var btn = $("mbResetLayout");
    if (!btn) return;
    btn.addEventListener("click", function () {
      if (!confirm("确定要重置为默认布局吗？你新增/删除的卡片和调整过的位置、大小都会丢失，且无法撤销。")) return;
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
    updateEmptyCanvasHint();
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

  function addCompareCard() {
    if (boardCards.length >= MAX_BOARD_CARDS) return;
    var id = genCardId();
    var card = { id: id, kind: "compare" };
    boardCards.push(card);
    updateEmptyCanvasHint();
    var el = mountCardHtml(compareCardHtml(card));
    compareState[id] = [];
    bindCompareUI(card);
    renderCompareChips(id);
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
    updateEmptyCanvasHint();
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
    } else if (card.kind === "compare") {
      if (compareCharts[cardId]) { compareCharts[cardId].dispose(); delete compareCharts[cardId]; }
      if (compareResizeHandlers[cardId]) {
        window.removeEventListener("resize", compareResizeHandlers[cardId]);
        delete compareResizeHandlers[cardId];
      }
      delete compareState[cardId];
    }
    if (el) el.remove();
    delete currentLayout[cardId];
    updateEmptyCanvasHint();
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
    // 到上限就不再列可添加项,明说原因 —— 比点了没反应强
    if (boardCards.length >= MAX_BOARD_CARDS) {
      pop.innerHTML = '<div class="mb-add-full">卡片已达上限（' + MAX_BOARD_CARDS +
        ' 张），删除一些再添加</div>';
      return;
    }
    var hidden = hiddenRankIds();
    var html = '<div class="mb-add-label">行情图表</div>' +
      '<button type="button" class="mb-add-item" data-add="stock">+ 行情卡片</button>' +
      '<button type="button" class="mb-add-item" data-add="compare">+ 多股对比</button>';
    if (hidden.length) {
      html += '<div class="mb-add-label">板块排行</div>';
      hidden.forEach(function (id) {
        html += '<button type="button" class="mb-add-item" data-add="rank" data-rank-id="' +
          esc(id) + '">+ ' + esc(RANK_INFO[id].name) + "</button>";
      });
    }
    var hiddenSingles = hiddenSingletonDefs();
    if (hiddenSingles.length) {
      html += '<div class="mb-add-label">数据摘要</div>';
      hiddenSingles.forEach(function (d) {
        html += '<button type="button" class="mb-add-item" data-add="singleton" data-singleton-id="' +
          esc(d.id) + '">+ ' + esc(d.label) + "</button>";
      });
    }
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
      else if (item.getAttribute("data-add") === "compare") addCompareCard();
      else if (item.getAttribute("data-add") === "singleton") addSingletonCard(item.getAttribute("data-singleton-id"));
      else addRankCard(item.getAttribute("data-rank-id"));
    });
  }

  /* ── 入口 ──────────────────────────────────────────────────────────── */

  function cardHtmlFor(card) {
    if (card.kind === "rank") return rankCardHtml(card);
    if (card.kind === "compare") return compareCardHtml(card);
    if (card.kind === "review" || card.kind === "hotsector" || card.kind === "indices") {
      return singletonCardHtml(card);
    }
    return stockCardHtml(card);
  }

  function load() {
    var grid = $("mbGrid");
    bindResetButton();
    bindTidyButton();
    bindAddMenu();
    grid.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-remove-card]");
      if (!btn) return;
      e.stopPropagation();
      var cardId = btn.getAttribute("data-remove-card");
      var card = boardCards.filter(function (c) { return c.id === cardId; })[0];
      var cur = card && card.kind === "stock" ? slotState[cardId] : null;
      var msg = cur
        ? "确定删除「" + (cur.name || cur.code) + "」这张行情卡片吗？删除后需要重新搜索选择股票/指数，且不可撤销。"
        : (card && card.kind === "compare"
          ? "确定删除这张对比卡片吗？已添加的对比股票也会一并清空，且不可撤销。"
          : "确定删除这张卡片吗？删除后不可撤销。");
      if (!confirm(msg)) return;
      removeCard(cardId);
    });

    fetchBoard().then(function (saved) {
      var savedCards = Array.isArray(saved.cards)
        ? saved.cards.filter(function (c) {
            return c && typeof c.id === "string" && (c.kind === "stock" || c.kind === "rank" ||
              c.kind === "compare" || c.kind === "review" || c.kind === "hotsector" || c.kind === "indices");
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
        if (card.kind === "compare") {
          var savedCmp = currentLayout[card.id];
          compareState[card.id] = (savedCmp && Array.isArray(savedCmp.codes)) ? savedCmp.codes : [];
          bindCompareUI(card);
          loadCompare(card);
          return;
        }
        if (card.kind === "review" || card.kind === "hotsector" || card.kind === "indices") {
          loadSingleton(card);
          return;
        }
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
      updateEmptyCanvasHint();
      loadRanking();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
