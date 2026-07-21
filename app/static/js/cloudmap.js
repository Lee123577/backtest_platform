/* 大盘云图 —— 原生 Canvas 2D squarified treemap
 * ================================================
 * 不再用 ECharts,自己用 Canvas 2D 手绘热力图:按板块(category)分组,组内个股
 * 按流通市值做 squarified treemap(矩形尽量接近正方形),颜色按涨跌幅上色。
 * 融入站点标准布局(左侧栏由 shell.js 注入),浅色风格,只在中间展示云图。
 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };

  var canvas = $("treemap");
  var ctx = canvas.getContext("2d");
  var tooltip = $("cmTooltip");
  var wrap = $("cmCanvasWrap");

  var latestData = null;
  var cats = [];        // 布局后的分组:[{name, rect, headerRect, items:[{...,rect}]}]
  var cells = [];       // 命中测试用扁平列表:[{rect, d}]
  var hovered = null;   // 当前 hover 的 cell,避免同格重复刷 tooltip
  var dpr = Math.max(1, window.devicePixelRatio || 1);

  var HEADER = 18;      // 每个板块顶部标题条高度(px)
  var GAP = 1.5;        // 个股格子之间的白色间隙

  // ── 涨跌色阶:-4%~+4% 线性插值,超出钳位到端点(绿跌→灰平→红涨) ──────────
  var COLOR_STOPS = [
    [-4, [48, 204, 90]], [-3, [47, 170, 81]], [-2, [49, 137, 78]],
    [-1, [56, 105, 79]], [0, [65, 69, 84]], [1, [120, 69, 81]],
    [2, [165, 66, 74]], [3, [206, 61, 65]], [4, [246, 53, 56]],
  ];
  var _colorCache = {};   // 按 0.1% 分桶缓存,省掉 2900+ 次字符串拼接/GC
  function gradeColor(pct) {
    if (pct == null || isNaN(pct)) return "rgb(65,69,84)";
    var p = Math.max(-4, Math.min(4, pct));
    var key = Math.round(p * 10);
    var cached = _colorCache[key];
    if (cached) return cached;
    for (var i = 0; i < COLOR_STOPS.length - 1; i++) {
      var p0 = COLOR_STOPS[i][0], c0 = COLOR_STOPS[i][1];
      var p1 = COLOR_STOPS[i + 1][0], c1 = COLOR_STOPS[i + 1][1];
      if (p >= p0 && p <= p1) {
        var t = (p - p0) / (p1 - p0);
        return (_colorCache[key] = "rgb(" + Math.round(c0[0] + (c1[0] - c0[0]) * t) + "," +
          Math.round(c0[1] + (c1[1] - c0[1]) * t) + "," +
          Math.round(c0[2] + (c1[2] - c0[2]) * t) + ")");
      }
    }
    return "rgb(65,69,84)";
  }

  function fmtAmount(v) {
    if (!v || isNaN(v)) return "—";
    if (v >= 1e12) return (v / 1e12).toFixed(2) + " 万亿";
    if (v >= 1e8) return (v / 1e8).toFixed(0) + " 亿";
    if (v >= 1e4) return (v / 1e4).toFixed(0) + " 万";
    return Math.round(v).toString();
  }
  function pctText(v) { return (v > 0 ? "+" : "") + Number(v).toFixed(2) + "%"; }

  // ── Squarified treemap 布局(Bruls et al.) ─────────────────────────────
  // nodes 需已按 .value 降序;给每个 node 写 .rect={x,y,w,h}。沿短边逐行铺,
  // 每行在"加入下一个矩形会让长宽比变差"时收尾,从而所有格子尽量接近正方形。
  function squarify(nodes, X, Y, W, H) {
    var total = 0, i;
    for (i = 0; i < nodes.length; i++) total += nodes[i].value;
    if (total <= 0 || W <= 0 || H <= 0) return;
    var scale = (W * H) / total;   // 面积/市值
    var x = X, y = Y, w = W, h = H, n = nodes.length;
    i = 0;
    while (i < n) {
      var shortSide = Math.min(w, h);
      var rowStart = i, rowSum = 0, bestWorst = Infinity;
      // nodes 已按 value 降序 → 行内最大恒为行首(rowMax)、最小恒为当前元素,
      // 无需每次内层重新遍历求 max/min(那是 O(n²));直接取,整体降到 O(n)。
      var rowMax = nodes[i].value * scale;
      while (i < n) {
        var v = nodes[i].value * scale;   // 降序:当前即行内最小
        var newSum = rowSum + v;
        // 这一行若收在此处的最差长宽比
        var worst = Math.max(
          (shortSide * shortSide * rowMax) / (newSum * newSum),
          (newSum * newSum) / (shortSide * shortSide * v)
        );
        if (worst > bestWorst) break;   // 再加就更差,收尾
        bestWorst = worst; rowSum = newSum; i++;
      }
      var thick = rowSum / shortSide;   // 行厚度(沿长边)
      var off = 0;
      for (var k2 = rowStart; k2 < i; k2++) {
        var len = (nodes[k2].value * scale) / thick;
        if (w >= h) nodes[k2].rect = { x: x, y: y + off, w: thick, h: len };
        else nodes[k2].rect = { x: x + off, y: y, w: len, h: thick };
        off += len;
      }
      if (w >= h) { x += thick; w -= thick; } else { y += thick; h -= thick; }
    }
  }

  // ── 布局:外层按板块、内层按个股 ───────────────────────────────────────
  function build() {
    cats = []; cells = []; hovered = null;
    if (!latestData || !latestData.items) return;
    var W = canvas.clientWidth, H = canvas.clientHeight;
    if (W <= 0 || H <= 0) return;

    var groups = {};
    latestData.items.forEach(function (it) {
      if (!(it.market_cap > 0)) return;
      var cat = it.category || "其他";
      (groups[cat] || (groups[cat] = [])).push(it);
    });

    cats = Object.keys(groups).map(function (name) {
      var arr = groups[name].slice().sort(function (a, b) { return b.market_cap - a.market_cap; });
      var total = 0, sumPct = 0;
      arr.forEach(function (it) { total += it.market_cap; sumPct += it.pct_change; });
      return { name: name, value: total, avgPct: arr.length ? sumPct / arr.length : 0, items: arr };
    }).filter(function (c) { return c.value > 0; })
      .sort(function (a, b) { return b.value - a.value; });

    squarify(cats, 0, 0, W, H);

    cats.forEach(function (c) {
      if (!c.rect) return;
      var r = c.rect;
      var hh = Math.min(HEADER, r.h);
      c.headerRect = { x: r.x, y: r.y, w: r.w, h: hh };
      var iy = r.y + hh, ih = r.h - hh;
      if (ih > 4 && r.w > 4) {
        c.items.forEach(function (it) { it.value = it.market_cap; });
        squarify(c.items, r.x, iy, r.w, ih);
        c.items.forEach(function (it) { if (it.rect) cells.push({ rect: it.rect, d: it }); });
      }
    });
  }

  // ── 绘制 ───────────────────────────────────────────────────────────────
  function draw() {
    var W = canvas.clientWidth, H = canvas.clientHeight;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#ffffff";   // 间隙/背景色
    ctx.fillRect(0, 0, W, H);

    // 文字用描边勾边(strokeText)代替 shadowBlur —— 阴影是逐像素高斯模糊、
    // 上千个标签时非常贵;描边只多画一遍轮廓,便宜得多,可读性相当。
    ctx.lineJoin = "round";
    ctx.miterLimit = 2;
    var labels = [];   // 文字延后统一画,避免和 fillRect 反复切换填充态

    cats.forEach(function (c) {
      // 个股格子
      c.items.forEach(function (it) {
        if (!it.rect) return;
        var r = it.rect;
        var w = r.w - GAP, h = r.h - GAP;
        if (w < 0.75 || h < 0.75) return;   // 亚像素格子肉眼不可见,不画
        ctx.fillStyle = gradeColor(it.pct_change);
        ctx.fillRect(r.x + GAP, r.y + GAP, w, h);
        if (w >= 40 && h >= 22) labels.push(it);   // 够大的攒起来最后画字
      });
    });

    // 统一画标签(名称 + 涨跌幅)
    ctx.textAlign = "center";
    ctx.strokeStyle = "rgba(0,0,0,.55)";
    for (var li = 0; li < labels.length; li++) {
      var it = labels[li], r = it.rect;
      var w = r.w - GAP, h = r.h - GAP;
      var cx = r.x + r.w / 2, cy = r.y + r.h / 2;
      if (h >= 34 && w >= 46) {
        ctx.textBaseline = "alphabetic";
        ctx.font = "600 11px system-ui,-apple-system,'Segoe UI',sans-serif";
        var nm = clip(it.name, w);
        ctx.lineWidth = 3; ctx.strokeText(nm, cx, cy - 2);
        ctx.fillStyle = "#fff"; ctx.fillText(nm, cx, cy - 2);
        ctx.font = "600 10px system-ui,-apple-system,sans-serif";
        var pt = pctText(it.pct_change);
        ctx.lineWidth = 3; ctx.strokeText(pt, cx, cy + 12);
        ctx.fillStyle = "#fff"; ctx.fillText(pt, cx, cy + 12);
      } else {
        ctx.textBaseline = "middle";
        ctx.font = "600 10px system-ui,-apple-system,sans-serif";
        var nm2 = clip(it.name, w);
        ctx.lineWidth = 3; ctx.strokeText(nm2, cx, cy);
        ctx.fillStyle = "#fff"; ctx.fillText(nm2, cx, cy);
      }
    }

    // 板块标题条(画在最上层)
    ctx.textBaseline = "middle";
    cats.forEach(function (c) {
      if (!(c.headerRect && c.headerRect.h >= 10)) return;
      var hr = c.headerRect;
      ctx.fillStyle = "#eef1f4";
      ctx.fillRect(hr.x, hr.y, hr.w, hr.h);
      ctx.fillStyle = "#57606a";
      ctx.textAlign = "left";
      ctx.font = "700 12px system-ui,-apple-system,sans-serif";
      ctx.fillText(clip(c.name, hr.w - 60), hr.x + 8, hr.y + hr.h / 2);
      if (hr.w > 80) {
        ctx.fillStyle = c.avgPct > 0 ? "#cf222e" : c.avgPct < 0 ? "#1a7f37" : "#8a929c";
        ctx.textAlign = "right";
        ctx.font = "700 11px system-ui,sans-serif";
        ctx.fillText(pctText(c.avgPct), hr.x + hr.w - 8, hr.y + hr.h / 2);
      }
    });
  }

  // 文本按可用宽度截断(避免溢出格子)
  function clip(text, maxW) {
    if (!text) return "";
    if (ctx.measureText(text).width <= maxW) return text;
    var s = text;
    while (s.length > 1 && ctx.measureText(s + "…").width > maxW) s = s.slice(0, -1);
    return s.length > 1 ? s + "…" : s;
  }

  // ── hover 命中 + tooltip ───────────────────────────────────────────────
  function hitTest(px, py) {
    if (hovered) {
      var r = hovered.rect;
      if (px >= r.x && px <= r.x + r.w && py >= r.y && py <= r.y + r.h) return hovered;
    }
    for (var i = 0; i < cells.length; i++) {
      var rc = cells[i].rect;
      if (px >= rc.x && px <= rc.x + rc.w && py >= rc.y && py <= rc.y + rc.h) return cells[i];
    }
    return null;
  }

  // 浏览器本身已把 mousemove 合并到每帧,无需再 rAF 节流;真正的开销是每次
  // getBoundingClientRect 触发的强制 reflow —— 用 canvasBox 缓存边界,尺寸/
  // 滚动变化时才失效重取。
  var canvasBox = null;
  function onMove(e) {
    if (!canvasBox) canvasBox = canvas.getBoundingClientRect();
    var px = e.clientX - canvasBox.left, py = e.clientY - canvasBox.top;
    var hit = hitTest(px, py);
    if (!hit) { tooltip.hidden = true; hovered = null; return; }
    hovered = hit;
    var d = hit.d;
    var pctCol = d.pct_change > 0 ? "#cf222e" : d.pct_change < 0 ? "#1a7f37" : "#8a929c";
    tooltip.innerHTML =
      '<div class="cm-tt-name">' + esc(d.name) + ' <span class="cm-tt-code">' + esc(d.code) + '</span></div>' +
      '<div class="cm-tt-row"><span>涨跌</span><b style="color:' + pctCol + '">' + pctText(d.pct_change) + '</b></div>' +
      '<div class="cm-tt-row"><span>收盘</span><b>' + (d.close != null ? d.close : "—") + '</b></div>' +
      '<div class="cm-tt-row"><span>市值</span><b>' + Number(d.market_cap).toFixed(1) + ' 亿</b></div>' +
      '<div class="cm-tt-row"><span>成交额</span><b>' + fmtAmount(d.amount) + '</b></div>';
    tooltip.hidden = false;
    // 定位:默认在光标右下,靠边则翻转,避免溢出容器
    var tw = tooltip.offsetWidth, th = tooltip.offsetHeight;
    var ww = wrap.clientWidth, wh = wrap.clientHeight;
    var left = px + 14, top = py + 14;
    if (left + tw > ww) left = px - tw - 14;
    if (top + th > wh) top = py - th - 14;
    tooltip.style.left = Math.max(0, left) + "px";
    tooltip.style.top = Math.max(0, top) + "px";
  }
  function onLeave() { tooltip.hidden = true; hovered = null; }

  // ── 尺寸/重绘 ──────────────────────────────────────────────────────────
  function resize() {
    var W = canvas.clientWidth, H = canvas.clientHeight;
    if (W <= 0 || H <= 0) return;
    dpr = Math.max(1, window.devicePixelRatio || 1);
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    canvasBox = null;   // 尺寸变了,hover 命中的边界缓存失效
    build();
    draw();
  }

  // resize 用 rAF 合并:拖动窗口时 ResizeObserver 高频触发,每帧只重算一次布局
  var resizeRaf = 0;
  function scheduleResize() {
    if (resizeRaf) return;
    resizeRaf = requestAnimationFrame(function () { resizeRaf = 0; resize(); });
  }

  // ── 图例 ───────────────────────────────────────────────────────────────
  function renderLegend() {
    $("legend").innerHTML = COLOR_STOPS.map(function (s) {
      var label = (s[0] > 0 ? "+" : "") + s[0] + "%";
      return '<span class="cm-legend-swatch" style="background:rgb(' + s[1].join(",") + ')">' + label + "</span>";
    }).join("");
  }

  // ── 数据请求 ───────────────────────────────────────────────────────────
  function load() {
    var minCap = $("minCapFilter").value;
    $("loading").hidden = false;
    $("error").hidden = true;
    fetch("/api/cloudmap/data?market=all&min_cap=" + encodeURIComponent(minCap))
      .then(function (r) {
        if (!r.ok) return r.json().catch(function () { return { detail: "HTTP " + r.status }; })
          .then(function (j) { throw new Error(j.detail || ("HTTP " + r.status)); });
        return r.json();
      })
      .then(function (j) {
        latestData = j;
        $("cmDate").textContent = j.trade_date ? "数据日期 " + j.trade_date : "";
        var n = (j.items || []).length;
        $("cmCount").textContent = n ? n + " 只个股 · 面积＝市值 · 颜色＝涨跌" : "";
        resize();
      })
      .catch(function (e) {
        $("error").textContent = "加载失败：" + e.message;
        $("error").hidden = false;
      })
      .finally(function () { $("loading").hidden = true; });
  }

  // ── 事件 & 入口 ────────────────────────────────────────────────────────
  $("minCapFilter").addEventListener("change", load);
  $("refreshBtn").addEventListener("click", load);
  canvas.addEventListener("mousemove", onMove);
  canvas.addEventListener("mouseleave", onLeave);
  // 用 ResizeObserver 而非 window.resize:后者在容器 reflow 前就触发,会读到旧的
  // clientWidth,导致 cells 坐标空间和拉伸后的画布不一致(hover 命中错位)。
  // ResizeObserver 回调拿到的是布局稳定后的真实尺寸,重建才准。
  if (window.ResizeObserver) {
    new ResizeObserver(scheduleResize).observe(wrap);
  } else {
    window.addEventListener("resize", scheduleResize);
  }
  window.addEventListener("scroll", function () { canvasBox = null; }, { passive: true });

  renderLegend();
  load();
})();
