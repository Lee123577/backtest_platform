/**
 * 我的数据看板
 * ============
 * 版式:Stat-Led —— 上证综指的当日收盘做页面头号读数(hero),
 * 关注的个股次之,板块排行榜放进全页唯一的暗色带。
 *
 * 数据口径:个股走 /api/stock/{code}/kline,指数走 /api/index/{code}/kline
 * —— 两者不能混用:000001 在个股表是平安银行,在指数表才是上证综指。
 *
 * 加关注标的:往下面 ITEMS 里加一条即可。
 */
(function () {
  "use strict";

  /* hero 固定是大盘;ITEMS 是"我关注的",不放指数 */
  var HERO = { type: "index", code: "000001", name: "上证综合指数" };

  var ITEMS = [
    { type: "stock", code: "603993", name: "洛阳钼业", tag: "个股" },
  ];

  var DAYS_BACK = 180;   // 拉近半年(自然日)

  /* ECharts 的颜色只能由 JS 传:zrender 自己解析颜色字符串,不认 oklch(),
   * 渐变色标尤其会直接解析失败。这里是 tokens.css 里 --color-up/-down 的
   * sRGB 近似值,改 token 时记得同步这里。 */
  var CHART = {
    up: "#cf3a2c", down: "#1c8551",
    upFill: "rgba(207,58,44,.16)", downFill: "rgba(28,133,81,.16)",
    fade: "rgba(255,255,255,0)",   // 渐变的透明终点,不是白色底
    axis: "#dfe4ec", split: "#eef1f6", label: "#7c8494",
  };

  var REDUCED = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

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
  function dirClass(chg) {
    return chg == null ? "mb-flat" : (chg > 0 ? "mb-up" : (chg < 0 ? "mb-down" : "mb-flat"));
  }
  function signed(v) {
    return v == null ? "—" : (v > 0 ? "+" : "") + num(v) + "%";
  }
  /* 涨跌幅 = 最后一根相对前一根的收盘涨跌 */
  function lastChange(rows) {
    var last = rows[rows.length - 1], prev = rows[rows.length - 2];
    return (last.close != null && prev.close) ? (last.close - prev.close) / prev.close * 100 : null;
  }
  function kUrl(it) {
    var end = new Date();
    var start = new Date(end.getTime() - DAYS_BACK * 86400000);
    return (it.type === "index" ? "/api/index/" : "/api/stock/") +
      encodeURIComponent(it.code) + "/kline" +
      "?start_date=" + fmtDate(start) + "&end_date=" + fmtDate(end);
  }
  function getKline(it) {
    return fetch(kUrl(it))
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (j) { return j.data || []; });
  }

  /* 数字从 0 跑到目标值,约 500ms。只跑 hero 那一个巨型读数 */
  function tick(el, target) {
    /* 先把真值写死:后台标签页里 rAF 根本不触发,动画不能是读数出现的前提 */
    el.textContent = num(target);
    if (REDUCED || !window.requestAnimationFrame) return;
    var t0 = null, dur = 500;
    function step(ts) {
      if (t0 === null) t0 = ts;
      var p = Math.min((ts - t0) / dur, 1);
      var eased = 1 - Math.pow(1 - p, 3);          // ease-out,不回弹
      el.textContent = num(target * eased);
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function drawChart(elId, rows, chg, opt) {
    if (!window.echarts) return;
    var el = document.getElementById(elId);
    if (!el) return;
    var chart = echarts.init(el);
    var up = chg == null || chg >= 0;
    var color = up ? CHART.up : CHART.down;

    chart.setOption({
      grid: { left: 52, right: 10, top: 12, bottom: 24 },
      animationDuration: REDUCED ? 0 : 600,
      tooltip: {
        trigger: "axis",
        formatter: function (p) {
          return p[0].axisValue + "<br/>收盘 <b>" + num(p[0].data) + "</b>";
        },
      },
      xAxis: {
        type: "category",
        data: rows.map(function (r) { return r.date; }),
        boundaryGap: false,
        axisLine: { lineStyle: { color: CHART.axis } },
        axisTick: { show: false },
        axisLabel: { color: CHART.label, fontSize: 11 },
      },
      yAxis: {
        type: "value",
        scale: true,
        splitLine: { lineStyle: { color: CHART.split } },
        axisLabel: { color: CHART.label, fontSize: 11 },
      },
      series: [{
        type: "line",
        data: rows.map(function (r) { return r.close; }),
        showSymbol: false,
        lineStyle: { width: opt && opt.hero ? 2 : 1.5, color: color },
        itemStyle: { color: color },
        areaStyle: {
          color: {
            type: "linear", x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: up ? CHART.upFill : CHART.downFill },
              { offset: 1, color: CHART.fade },
            ],
          },
        },
      }],
    });
    window.addEventListener("resize", function () { chart.resize(); });
  }

  /* ── hero ────────────────────────────────────────────────────────── */
  function heroFail(msg) {
    document.getElementById("heroFigure").textContent = "—";
    document.getElementById("heroTitle").textContent = "大盘读数暂时取不到。";
    document.getElementById("heroMeta").textContent = msg;
  }

  function renderHero(rows) {
    if (!rows || rows.length < 2) return heroFail("行情库里没有 000001 的近半年数据。");

    var last = rows[rows.length - 1];
    var chg = lastChange(rows);
    var cls = dirClass(chg);

    var chgEl = document.getElementById("heroChg");
    chgEl.textContent = signed(chg);
    chgEl.className = "mb-figure-chg tnum " + cls;

    var figEl = document.getElementById("heroFigure");
    figEl.className = "mb-figure tnum " + cls;
    tick(figEl, last.close);

    var word = chg == null ? "收在这里" : (chg > 0 ? "红盘收在这里" : (chg < 0 ? "绿盘收在这里" : "平收在这里"));
    document.getElementById("heroTitle").textContent = "上证综指 " + last.date + " " + word + "。";
    document.getElementById("heroMeta").textContent =
      "近 " + rows.length + " 个交易日 · 区间 " + num(Math.min.apply(null, rows.map(function (r) { return r.close; }))) +
      " – " + num(Math.max.apply(null, rows.map(function (r) { return r.close; })));
    document.getElementById("heroCap").textContent = "收盘价 · " + rows[0].date + " → " + last.date;

    drawChart("heroChart", rows, chg, { hero: true });
  }

  /* ── 关注标的卡 ──────────────────────────────────────────────────── */
  function cardHtml(it, i) {
    return '<div class="mb-card" id="mbCard' + i + '">' +
      '<div class="mb-card-head">' +
      '<span class="mb-name">' + esc(it.name) + "</span>" +
      '<span class="mb-code tnum">' + esc(it.code) + "</span>" +
      '<span class="mb-tag">' + esc(it.tag) + "</span>" +
      "</div>" +
      '<div id="mbBody' + i + '"><div class="mb-loading">读取行情库…</div></div>' +
      "</div>";
  }

  function renderCard(i, rows) {
    var body = document.getElementById("mbBody" + i);
    if (!rows || rows.length < 2) {
      body.innerHTML = '<div class="mb-error">行情库里没有这只标的的近半年数据。</div>';
      return;
    }
    var last = rows[rows.length - 1];
    var chg = lastChange(rows);
    var cls = dirClass(chg);

    body.innerHTML =
      '<div class="mb-quote">' +
      '<span class="mb-close ' + cls + '">' + num(last.close) + "</span>" +
      '<span class="mb-chg ' + cls + '">' + signed(chg) + "</span>" +
      "</div>" +
      '<div class="mb-meta">最新 ' + esc(last.date) + " · 近 " + rows.length + " 个交易日</div>" +
      '<div class="mb-chart" id="mbChart' + i + '" role="img" aria-label="' +
      esc(ITEMS[i].name) + '近半年收盘价走势"></div>';

    drawChart("mbChart" + i, rows, chg);
  }

  function revealAll() {
    document.body.classList.add("mb-js");   // 有它 .reveal 才敢从 opacity:0 起步
    var els = document.querySelectorAll(".reveal");
    function showAll() { els.forEach(function (el) { el.classList.add("is-in"); }); }

    /* 后台标签页里 IntersectionObserver 不回调 —— 淡入是锦上添花,
     * 绝不能成为"内容能不能被看见"的前提,所以这两种情况直接全显。 */
    if (REDUCED || !window.IntersectionObserver || document.hidden) return showAll();

    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add("is-in"); io.unobserve(e.target); }
      });
    }, { rootMargin: "0px 0px -8% 0px" });
    els.forEach(function (el) { io.observe(el); });

    /* 兜底:2 秒后还有没露出来的(IO 被节流/异常),直接显示,宁可不要动效 */
    setTimeout(showAll, 2000);
  }

  function load() {
    revealAll();

    getKline(HERO).then(renderHero).catch(function (e) { heroFail("加载失败:" + e.message); });

    var grid = document.getElementById("mbGrid");
    grid.innerHTML = ITEMS.map(cardHtml).join("");
    ITEMS.forEach(function (it, i) {
      getKline(it)
        .then(function (rows) { renderCard(i, rows); })
        .catch(function (e) {
          document.getElementById("mbBody" + i).innerHTML =
            '<div class="mb-error">加载失败:' + esc(e.message) + "</div>";
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
 * 板块涨幅排行榜(暗色带)
 * ======================
 * 读 /api/sectors/ranking(每日快照表,不实时抓行情源 —— 它对同 IP 限流)。
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
  function row(i, name, pct, sub, extra) {
    return '<div class="rk-row' + (i === 0 ? " rk-row--lead" : "") + '">' +
      '<span class="rk-no">' + (i + 1) + "</span>" +
      '<div class="rk-name">' + esc(name) + (extra || "") +
      (sub ? '<div class="rk-sub">' + esc(sub) + "</div>" : "") +
      "</div>" + pctHtml(pct) + "</div>";
  }
  function label(t) { return '<p class="rk-group-label">' + esc(t) + "</p>"; }
  function note(t) { return '<p class="rk-note">' + t + "</p>"; }

  function renderGroups() {
    if (!data.board_data_ok) return noBoardData();
    var html = data.groups.map(function (g, i) {
      var tops = (g.top || []).map(function (t) { return t.name; }).join("、");
      return row(i, g.name, g.avg_pct,
        g.board_count + " 个板块" + (tops ? " · 领涨 " + tops : ""));
    }).join("");
    $("rkBody").innerHTML = (html || '<div class="rk-empty">暂无数据</div>') +
      note("按组内行业板块的平均涨跌幅排序。" +
        (data.unmapped ? "有 " + data.unmapped + " 个板块未归入任何分组,未计入统计。" : ""));
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
      label("涨幅前 " + data.industry_top.length) + top +
      label("跌幅前 " + data.industry_bottom.length) + bottom;
  }

  function renderConcept() {
    if (!data.board_data_ok) return noBoardData();
    var html = data.concept_top.map(function (b, i) {
      return row(i, b.name, b.pct_change, b.leader ? "领涨 " + b.leader : "",
        b.is_theme ? '<span class="rk-hot">热点</span>' : "");
    }).join("");
    $("rkBody").innerHTML = (html || '<div class="rk-empty">暂无数据</div>') +
      note("题材概念按当日涨幅取前 N —— 资金炒作什么就冒出什么,不靠关键词白名单筛" +
        "(新热点常常名字里不含关键词)。");
  }

  function renderSpecial() {
    var html = data.special.map(function (s, i) {
      var members = (s.members || []).map(function (m) {
        return m.name + " " + (m.pct_change > 0 ? "+" : "") + m.pct_change + "%";
      }).join(" / ");
      var src = s.source === "local"
        ? '<span class="rk-src">本站行情库</span>'
        : '<span class="rk-src">外部行情源</span>';
      return row(i, s.name, s.pct_change, members || s.note || "", src);
    }).join("");
    $("rkBody").innerHTML = html +
      note("权重蓝筹 / 中小成长取成分指数均值,ST 板块取全部 ST 个股均值 —— 均来自本站行情库。");
  }

  function noBoardData() {
    $("rkBody").innerHTML =
      '<div class="rk-empty">当日板块数据缺失(行情源限流或不可用)。<br>' +
      "板块涨跌来自外部行情源,每交易日 15:10 抓取一次;抓取失败时如实显示缺失," +
      "不会拿旧数据充数。</div>";
  }

  var RENDER = { groups: renderGroups, industry: renderIndustry,
                 concept: renderConcept, special: renderSpecial };

  function switchTab(tab) {
    document.querySelectorAll(".rk-tab").forEach(function (t) {
      var on = t.getAttribute("data-tab") === tab;
      t.classList.toggle("is-on", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
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
        $("rkBody").innerHTML = '<div class="rk-empty">板块数据加载失败,请稍后重试。</div>';
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else { init(); }
})();
