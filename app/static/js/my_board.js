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
    return '<div class="mb-card" id="mbCard' + i + '">' +
      '<div class="mb-card-head">' +
      '<span class="mb-name">' + esc(it.name) + "</span>" +
      '<span class="mb-code">' + esc(it.code) + "</span>" +
      '<span class="mb-tag">' + esc(it.tag) + "</span>" +
      "</div>" +
      '<div id="mbBody' + i + '"><div class="mb-loading">加载中…</div></div>' +
      "</div>";
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
        .then(function (j) { render(i, it, j.data || []); })
        .catch(function (e) {
          document.getElementById("mbBody" + i).innerHTML =
            '<div class="mb-error">加载失败：' + esc(e.message) + "</div>";
        });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
