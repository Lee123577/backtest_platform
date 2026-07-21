/**
 * 自选盯盘页
 * ==========
 * 登录后管理自选股 + 盯盘策略；信号提醒是会员权益(未订阅显示订阅引导)。
 * 后端：/api/watchlist/{config,add,remove,rules,alerts,alerts/read}
 */
(function () {
  "use strict";

  var cfg = null; // 最近一次 /config 结果

  function getJson(url) {
    return fetch(url).then(function (r) {
      if (r.status === 401) throw { unauth: true };
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
        return j;
      });
    });
  }
  function postJson(url, body) {
    return fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
        return j;
      });
    });
  }

  var $ = function (id) { return document.getElementById(id); };

  // ── 渲染：自选股 ────────────────────────────────────────────────────────
  function renderStocks() {
    $("wlCount").textContent = "（" + cfg.stocks.length + "/" + cfg.max_watchlist + "）";
    if (!cfg.stocks.length) {
      $("wlStocks").innerHTML = '<div class="no-data">还没有自选股，上方添加代码开始盯盘。</div>';
      return;
    }
    $("wlStocks").innerHTML = cfg.stocks.map(function (s) {
      return '<span class="wl-chip">' + esc(s.name || "") +
        ' <span style="color:var(--txt2)">' + esc(s.code) + "</span>" +
        '<span class="wl-chip-x" data-code="' + esc(s.code) + '">×</span></span>';
    }).join("");
    $("wlStocks").querySelectorAll(".wl-chip-x").forEach(function (x) {
      x.addEventListener("click", function () { removeStock(x.getAttribute("data-code")); });
    });
  }

  // ── 渲染：盯盘策略 ──────────────────────────────────────────────────────
  function renderStrategies() {
    var chosen = {};
    cfg.rules.forEach(function (id) { chosen[id] = true; });
    $("wlStrategies").innerHTML = cfg.strategies.map(function (s) {
      return '<label class="wl-strat' + (chosen[s.id] ? " on" : "") + '">' +
        '<input type="checkbox" value="' + esc(s.id) + '"' + (chosen[s.id] ? " checked" : "") + ">" +
        "<span>" + esc(s.name) + "</span></label>";
    }).join("");
    $("wlStrategies").querySelectorAll('input[type=checkbox]').forEach(function (cb) {
      cb.addEventListener("change", function () {
        cb.closest(".wl-strat").classList.toggle("on", cb.checked);
      });
    });
  }

  // ── 渲染：信号提醒(会员) ─────────────────────────────────────────────────
  function renderAlerts(data) {
    var wrap = $("wlAlerts");
    if (!cfg.subscribed) {
      $("wlReadBtn").style.display = "none";
      wrap.innerHTML =
        '<div class="wl-paywall"><p>信号提醒是会员权益。开通后，每个交易日收盘系统会按你选的策略扫描自选股并在此提醒。</p>' +
        '<button class="wl-paywall-btn" id="wlSubBtn">开通会员 · 查看套餐</button></div>';
      var b = $("wlSubBtn");
      if (b) b.addEventListener("click", function () { window.location.href = "/subscribe"; });
      return;
    }
    var alerts = (data && data.alerts) || [];
    $("wlReadBtn").style.display = alerts.some(function (a) { return !a.is_read; }) ? "" : "none";
    if (!alerts.length) {
      wrap.innerHTML = '<div class="no-data">暂无信号提醒。配置好自选股与盯盘策略后，收盘扫描命中会出现在这里。</div>';
      return;
    }
    wrap.innerHTML = alerts.map(function (a) {
      var buy = a.signal === "buy";
      return '<div class="wl-alert' + (a.is_read ? "" : " unread") + '">' +
        '<span class="wl-alert-sig ' + (buy ? "wl-buy" : "wl-sell") + '">' + (buy ? "买入" : "卖出") + "</span>" +
        '<div class="wl-alert-main"><div class="wl-alert-name">' + esc(a.name || "") +
        ' <span style="color:var(--txt2)">' + esc(a.code) + '</span></div>' +
        '<div class="wl-alert-meta">' + esc(a.strategy_name || a.strategy_id) + " 信号</div></div>" +
        '<span class="wl-alert-date">' + esc(String(a.trade_date)) + "</span></div>";
    }).join("");
  }

  // ── 动作 ────────────────────────────────────────────────────────────────
  function addStock() {
    var code = ($("wlCodeInput").value || "").trim();
    if (!/^\d{6}$/.test(code)) { setAddMsg("请输入 6 位股票代码"); return; }
    postJson("/api/watchlist/add", { code: code })
      .then(function () { $("wlCodeInput").value = ""; setAddMsg("已添加", true); reloadConfig(); })
      .catch(function (e) { setAddMsg(e.message); });
  }
  function removeStock(code) {
    postJson("/api/watchlist/remove", { code: code }).then(reloadConfig);
  }
  function saveRules() {
    var ids = [].slice.call($("wlStrategies").querySelectorAll("input:checked"))
      .map(function (cb) { return cb.value; });
    postJson("/api/watchlist/rules", { strategy_ids: ids })
      .then(function () { setSaveMsg("已保存", true); cfg.rules = ids; })
      .catch(function (e) { setSaveMsg(e.message); });
  }
  function markRead() {
    postJson("/api/watchlist/alerts/read", {}).then(loadAlerts);
  }

  function setAddMsg(t, ok) { var e = $("wlAddMsg"); e.textContent = t; e.className = "wl-add-msg" + (ok ? " ok" : ""); }
  function setSaveMsg(t, ok) { var e = $("wlSaveMsg"); e.textContent = t; e.className = "wl-save-msg" + (ok ? " ok" : ""); }

  function loadAlerts() {
    if (!cfg.subscribed) { renderAlerts(null); return; }
    getJson("/api/watchlist/alerts?limit=50")
      .then(renderAlerts)
      .catch(function () { renderAlerts({ alerts: [] }); });
  }

  function reloadConfig() {
    return getJson("/api/watchlist/config").then(function (c) {
      cfg = c;
      renderStocks();
      renderStrategies();
      loadAlerts();
    });
  }

  // ── 未登录挡板：拉公开的 /api/strategies(首页回测用的同一份策略注册表，
  // 盯盘策略是它的子集)渲染真实规则说明，而不是空喊"登录后可用" ──────────────
  var _gateStrategiesLoaded = false;
  function loadGateStrategies() {
    if (_gateStrategiesLoaded) return;
    _gateStrategiesLoaded = true;
    fetch("/api/strategies").then(function (r) { return r.json(); }).then(function (list) {
      var signals = (list || []).filter(function (s) { return s.strategy_type === "signal"; });
      if (!signals.length) throw new Error("empty");
      $("wlGateStrategies").innerHTML = signals.map(function (s) {
        return '<div class="wl-gate-strat"><b>' + esc(s.name) + "</b>" +
          '<span>' + esc(s.description) + "</span></div>";
      }).join("");
    }).catch(function () {
      $("wlGateStrategies").innerHTML = '<div class="no-data">暂时加载不到，登录后仍可正常勾选</div>';
    });
  }

  // ── 初始化 ──────────────────────────────────────────────────────────────
  function showGate() {
    $("wlLoginGate").style.display = "";
    $("wlBody").style.display = "none";
    loadGateStrategies();
    var b = $("wlLoginBtn");
    if (b) b.addEventListener("click", function () {
      window.SPAuth.requireLogin().then(function (u) { if (u) init(); });
    });
  }

  function init() {
    getJson("/api/watchlist/config")
      .then(function (c) {
        cfg = c;
        $("wlLoginGate").style.display = "none";
        $("wlBody").style.display = "";
        renderStocks();
        renderStrategies();
        loadAlerts();
        $("wlAddBtn").addEventListener("click", addStock);
        $("wlCodeInput").addEventListener("keydown", function (e) { if (e.key === "Enter") addStock(); });
        $("wlSaveRules").addEventListener("click", saveRules);
        $("wlReadBtn").addEventListener("click", markRead);
      })
      .catch(function (e) {
        if (e && e.unauth) showGate();
        else $("wlAlerts").innerHTML = '<div class="no-data">加载失败，请刷新重试</div>';
      });
  }

  init();
})();
