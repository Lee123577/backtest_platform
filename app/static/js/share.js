/**
 * 回测结果分享页
 * ===============
 * /api/backtest/share/{token} → 快照(参数 + 各策略指标 + 抽稀净值曲线)
 *
 * 快照里没有 K 线和成交明细(见 app/share/service.py 的裁剪说明)，
 * 所以这页只画净值曲线 + 指标表 + 防过拟合结论，比结果页轻。
 */
(function () {
  "use strict";

  var METRIC_DEFS = [
    { key: "total_return",      label: "总收益率",     unit: "%",  kind: "pct" },
    { key: "annual_return",     label: "年化收益率",   unit: "%",  kind: "pct" },
    { key: "max_drawdown",      label: "最大回撤",     unit: "%",  kind: "dd" },
    { key: "max_drawdown_days", label: "最大回撤天数", unit: "天", kind: "neutral" },
    { key: "sharpe_ratio",      label: "夏普比率",     unit: "",   kind: "neutral" },
    { key: "win_rate",          label: "胜率",         unit: "%",  kind: "pct" },
    { key: "trade_count",       label: "完整交易次数", unit: "次", kind: "neutral" },
    { key: "final_value",       label: "最终资产",     unit: "元", kind: "money" },
  ];
  var LINE_COLORS = ["#0969da", "#e36209", "#cf222e", "#1a7f37", "#8250df", "#0550ae"];

  function token() {
    var m = location.pathname.match(/^\/s\/([A-Za-z0-9_-]{1,32})\/?$/);
    return m ? m[1] : null;
  }

  function cellClass(kind, val) {
    if (val === null || val === undefined) return "val-neutral";
    if (kind === "pct") return val > 0 ? "val-pos" : val < 0 ? "val-neg" : "val-neutral";
    if (kind === "dd") return val ? "val-neg" : "val-neutral";
    return "";
  }

  function fmt(val, def) {
    if (val === null || val === undefined) return "—";
    if (def.kind === "money") return "¥" + Number(val).toLocaleString("zh-CN");
    return val + def.unit;
  }

  function renderMetrics(snap) {
    var rows = snap.strategies.slice();
    if (snap.benchmark && snap.benchmark.equity && snap.benchmark.equity.length) {
      rows.push({ name: snap.benchmark.name, metrics: snap.benchmark.metrics });
    }
    var html = '<table class="metrics-tbl"><thead><tr><th style="text-align:left">指标</th>';
    rows.forEach(function (r) { html += "<th>" + esc(r.name) + "</th>"; });
    html += "</tr></thead><tbody>";
    METRIC_DEFS.forEach(function (def) {
      html += '<tr><td class="row-label">' + esc(def.label) + "</td>";
      rows.forEach(function (r) {
        var v = (r.metrics || {})[def.key];
        html += '<td class="' + cellClass(def.kind, v) + '">' + esc(fmt(v, def)) + "</td>";
      });
      html += "</tr>";
    });
    html += "</tbody></table>";
    document.getElementById("shMetrics").innerHTML = html;
  }

  function renderChart(snap) {
    var el = document.getElementById("shChart");
    var chart = echarts.init(el);
    var series = [];
    if (snap.benchmark && snap.benchmark.equity && snap.benchmark.equity.length) {
      series.push({
        name: snap.benchmark.name, type: "line", data: snap.benchmark.equity,
        itemStyle: { color: "#999" },
        lineStyle: { color: "#999", type: "dashed", width: 1.5 }, symbol: "none",
      });
    }
    snap.strategies.forEach(function (s, i) {
      var color = LINE_COLORS[i % LINE_COLORS.length];
      series.push({
        name: s.name, type: "line", data: s.equity,
        itemStyle: { color: color }, lineStyle: { color: color, width: 2 },
        symbol: "none",
      });
    });
    chart.setOption({
      tooltip: { trigger: "axis" },
      legend: { top: 0, textStyle: { color: "#57606a" } },
      grid: { left: 60, right: 20, top: 34, bottom: 30 },
      xAxis: { type: "category", axisLabel: { color: "#57606a" } },
      yAxis: {
        type: "value", scale: true,
        axisLabel: {
          color: "#57606a",
          formatter: function (v) { return "¥" + (v / 1e4).toFixed(1) + "万"; },
        },
        splitLine: { lineStyle: { color: "#eaeef2" } },
      },
      series: series,
    });
    window.addEventListener("resize", function () { chart.resize(); });
  }

  // 结论文案与结果页(app.js 的 renderVerdict)口径一致：只描述数字，不做建议
  function verdictOf(rb) {
    var parts = [], level = "good";
    function worse(l) {
      var rank = { good: 0, warn: 1, bad: 2 };
      if (rank[l] > rank[level]) level = l;
    }
    if (rb.max_param_delta !== undefined && rb.max_param_delta !== null) {
      var d = rb.max_param_delta;
      if (d <= 5) parts.push("参数 ±20% 扰动后年化最大变化 " + d + "pp，对参数不敏感");
      else if (d <= 10) { worse("warn"); parts.push("参数 ±20% 扰动后年化最大变化 " + d + "pp，对参数中等敏感"); }
      else { worse("bad"); parts.push("参数 ±20% 扰动后年化最大变化 " + d + "pp，对参数高度敏感"); }
    }
    var i = rb.in_annual, o = rb.out_annual;
    if (i !== undefined && i !== null && o !== undefined && o !== null) {
      if (i <= 0) { worse("warn"); parts.push("样本内年化 " + i + "% 本身为负，这段历史上没有表现出效果"); }
      else if (o <= 0) { worse("bad"); parts.push("样本内年化 " + i + "%，样本外 " + o + "% 由盈转亏"); }
      else {
        var keep = Math.round(o / i * 100);
        if (keep >= 70) parts.push("样本外年化 " + o + "%，维持了样本内(" + i + "%)的 " + keep + "%");
        else if (keep >= 30) { worse("warn"); parts.push("样本外年化 " + o + "%，只剩样本内的 " + keep + "%，衰减明显"); }
        else { worse("bad"); parts.push("样本外年化 " + o + "%，只剩样本内的 " + keep + "% —— 疑似过拟合"); }
      }
    }
    return parts.length ? { level: level, parts: parts } : null;
  }

  var HEADS = {
    good: { cls: "rb-verdict--good", txt: "✅ 两项检查通过" },
    warn: { cls: "rb-verdict--warn", txt: "⚠️ 需要留意" },
    bad:  { cls: "rb-verdict--bad",  txt: "🚩 疑似过拟合" },
  };

  function renderRobust(snap) {
    var blocks = "";
    snap.strategies.forEach(function (s) {
      if (!s.robustness) return;
      var v = verdictOf(s.robustness);
      if (!v) return;
      var head = HEADS[v.level];
      blocks += '<div class="robustness-block">' +
        '<div class="robustness-strategy-name">' + esc(s.name) + "</div>" +
        '<div class="rb-verdict ' + head.cls + '">' +
        '<div class="rb-verdict-head">' + head.txt + "</div>" +
        '<ul class="rb-verdict-list">' +
        v.parts.map(function (p) { return "<li>" + esc(p) + "</li>"; }).join("") +
        "</ul></div></div>";
    });
    if (!blocks) return;
    document.getElementById("shRobustSection").style.display = "";
    document.getElementById("shRobust").innerHTML = blocks;
  }

  function render(data) {
    var snap = data.snapshot;
    var name = snap.stock_name || snap.stock_code || "";
    document.getElementById("shTitle").textContent =
      name + " " + (snap.start_date || "") + " → " + (snap.end_date || "");
    document.title = name + " 回测结果 | shoupan";
    document.getElementById("shMeta").textContent =
      "初始资金 ¥" + Number(snap.capital || 0).toLocaleString("zh-CN") +
      " · 生成于 " + (data.created_at || "").slice(0, 10);
    renderMetrics(snap);
    renderChart(snap);
    renderRobust(snap);
  }

  function fail(msg) {
    document.getElementById("shTitle").textContent = "";
    document.getElementById("shMetrics").innerHTML =
      '<div class="no-data">' + esc(msg) + "</div>";
  }

  var t = token();
  if (!t) { fail("链接格式不正确"); return; }
  fetch("/api/backtest/share/" + encodeURIComponent(t))
    .then(function (r) {
      if (r.status === 404) throw new Error("这份分享不存在或已被删除");
      if (!r.ok) throw new Error("加载失败（HTTP " + r.status + "）");
      return r.json();
    })
    .then(render)
    .catch(function (e) { fail(e.message); });
})();
