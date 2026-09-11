/**
 * 自选盯盘页
 * ==========
 * 登录后管理自选股 + 盯盘策略；信号提醒是会员权益(未订阅显示订阅引导)。
 * 后端：/api/watchlist/{config,add,remove,rules,alerts,alerts/read}
 *
 * 页面底部还有一块「邮件通知」开关，走的是另一套后端 /api/notify/prefs ——
 * 放在这一页是因为盯盘就是这些邮件的来源场景，用户在这里配完策略，
 * 顺手就能决定要不要收信，不用再去找一个"设置"页。
 */
(function () {
  "use strict";

  var cfg = null;      // 最近一次 /config 结果
  var ltOffer = null;  // 终生会员名额概览(只在未订阅时拉,用来换付费墙文案)

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

  function putJson(url, body) {
    return fetch(url, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
        return j;
      });
    });
  }

  var $ = function (id) { return document.getElementById(id); };

  // 从个股报告页「加入自选盯盘」过来:/watchlist?add=600519
  // 未登录时先走登录闸,登录成功会重跑 init(),那时再补上这一手 ——
  // 所以这里读的是 URL 而不是一次性变量。
  function pendingAdd() {
    var v = new URLSearchParams(location.search).get("add") || "";
    return /^\d{6}$/.test(v) ? v : null;
  }

  // 加完就把参数从地址栏摘掉:留着的话刷新一次又加一遍,
  // 用户看到的是"我明明只点了一次"却反复提示已添加。
  function clearAddParam() {
    try {
      var u = new URL(location.href);
      u.searchParams.delete("add");
      history.replaceState(history.state, "", u.pathname + u.search + u.hash);
    } catch (e) { /* 老浏览器不支持就算了,不影响主流程 */ }
  }

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
      // 名额还有时把钩子换成"免费领" —— 免费的还没发完还喊"开通会员",
      // 等于在把人往外推
      var left = (ltOffer && ltOffer.remaining) || 0;
      wrap.innerHTML =
        '<div class="wl-paywall"><p>信号提醒是会员权益。开通后，每个交易日收盘系统会按你选的策略扫描自选股并在此提醒' +
        (left > 0 ? "，也可以发到你的邮箱" : "") + "。</p>" +
        '<button class="wl-paywall-btn" id="wlSubBtn">' +
        (left > 0 ? "🎁 免费领终生会员（还剩 " + left + " 个）" : "开通会员 · 查看套餐") +
        "</button></div>";
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
    // 输入框支持按名称联想，但提交的必须是代码 —— 打了名字没从下拉里选时
    // 会走到这里，提示要指向下拉而不是干巴巴地说"请输入 6 位代码"。
    if (!/^\d{6}$/.test(code)) {
      setAddMsg("请从下拉候选中选择股票，或直接输入 6 位代码");
      return;
    }
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

  // ── 邮件通知开关 ────────────────────────────────────────────────────────
  // 三项的文案要说清楚"什么时候会收到信"和"会不会打扰"——邮件通知最怕的是
  // 用户不知道自己开了什么，收到信第一反应是点垃圾邮件而不是退订。
  var NOTIFY_ITEMS = [
    ["watchlist_alert", "自选盯盘信号提醒",
     "收盘扫描命中你勾选的策略时，当天把命中的股票汇总成一封发给你（会员功能，一天最多一封）"],
    ["daily_review", "AI 每日复盘",
     "每个交易日收盘后，把当天复盘的标题和摘要发给你，全文回站内看"],
    ["ai_hotsector", "AI 热门板块",
     "当天的关注板块与选股名单；和上面的复盘合并在同一封信里，不会多发一封"],
  ];

  function renderPrefs(data) {
    var p = (data && data.prefs) || {};
    $("wlNotify").innerHTML =
      '<div class="wl-notify-list">' +
      NOTIFY_ITEMS.map(function (it) {
        return '<label class="wl-notify-item">' +
          '<input type="checkbox" data-kind="' + it[0] + '"' +
          (p[it[0]] ? " checked" : "") + ">" +
          '<span class="wl-notify-text"><b>' + esc(it[1]) + "</b>" +
          "<span>" + esc(it[2]) + "</span></span></label>";
      }).join("") +
      "</div>" +
      '<div class="wl-notify-foot">每封信都带一键退订链接；退订后这里的开关也会同步关掉。</div>';
    $("wlNotify").querySelectorAll("input[type=checkbox]").forEach(function (cb) {
      cb.addEventListener("change", function () { savePref(cb); });
    });
  }

  function savePref(cb) {
    // 开关类交互即改即存：多一个"保存"按钮只会让人以为没生效
    var body = {};
    body[cb.getAttribute("data-kind")] = cb.checked;
    setNotifyMsg("保存中…");
    putJson("/api/notify/prefs", body)
      .then(function (d) { setNotifyMsg("已保存", true); renderPrefs(d); })
      .catch(function (e) {
        // 存失败就把勾回退回去,不能让界面显示成"开了"其实没开
        cb.checked = !cb.checked;
        setNotifyMsg(e.message || "保存失败");
      });
  }

  function setNotifyMsg(t, ok) {
    var e = $("wlNotifyMsg");
    if (!e) return;
    e.textContent = t;
    e.className = "wl-notify-msg" + (ok ? " ok" : "");
    if (ok) setTimeout(function () { if (e.textContent === t) e.textContent = ""; }, 2000);
  }

  function loadPrefs() {
    getJson("/api/notify/prefs")
      .then(renderPrefs)
      .catch(function () {
        $("wlNotify").innerHTML =
          '<div class="no-data">通知设置暂时加载不出来，稍后刷新重试。</div>';
      });
  }

  function setAddMsg(t, ok) { var e = $("wlAddMsg"); e.textContent = t; e.className = "wl-add-msg" + (ok ? " ok" : ""); }
  function setSaveMsg(t, ok) { var e = $("wlSaveMsg"); e.textContent = t; e.className = "wl-save-msg" + (ok ? " ok" : ""); }

  function loadAlerts() {
    if (!cfg.subscribed) {
      // 只有未订阅才需要知道还剩几个名额;拉失败就退回原来的"开通会员"文案
      getJson("/api/subscription/status")
        .then(function (s) { ltOffer = s.lifetime_offer || null; })
        .catch(function () { ltOffer = null; })
        .then(function () { renderAlerts(null); });
      return;
    }
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
  // 免费名额只在登录闸上提一次。这里是全站最该说它的位置:能走到这一步的人
  // 已经明确表达了"我要盯这只票"的意图,而 /subscribe 一周只有 2 个人打开过 ——
  // 名额发不出去不是名额不够吸引,是根本没人看见。
  function loadGateOffer() {
    var box = $("wlGateOffer");
    if (!box) return;
    fetch("/api/subscription/status")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) {
        var o = s && s.lifetime_offer;
        if (!o || !o.remaining) return;      // 名额发完就不提,免得像画饼
        box.innerHTML = "🎁 前 " + o.total + " 名注册用户免费领<b>终生会员</b>，" +
          "还剩 <b>" + o.remaining + "</b> 个 —— 信号提醒是会员权益，登录后可直接领取。";
        box.style.display = "";
      })
      .catch(function () { /* 拿不到就不显示,不影响登录 */ });
  }

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
    loadGateOffer();
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
        loadPrefs();
        $("wlAddBtn").addEventListener("click", addStock);
        $("wlCodeInput").addEventListener("keydown", function (e) { if (e.key === "Enter") addStock(); });
        // 代码/名称联想。选中候选后直接入库,省掉用户再点一次"添加"。
        if (window.SPStockSuggest) {
          SPStockSuggest.attach($("wlCodeInput"), { onPick: function () { addStock(); } });
        }
        $("wlSaveRules").addEventListener("click", saveRules);
        $("wlReadBtn").addEventListener("click", markRead);

        var auto = pendingAdd();
        if (auto) {
          clearAddParam();
          // 已经在自选里就别报错,直接当成功 —— 用户的诉求是"让它在里面"
          if ((cfg.stocks || []).some(function (s) { return s.code === auto; })) {
            setAddMsg("已在自选中", true);
          } else {
            postJson("/api/watchlist/add", { code: auto })
              .then(function () { setAddMsg("已加入自选", true); reloadConfig(); })
              .catch(function (e) { setAddMsg(e.message); });
          }
        }
      })
      .catch(function (e) {
        if (e && e.unauth) showGate();
        else $("wlAlerts").innerHTML = '<div class="no-data">加载失败，请刷新重试</div>';
      });
  }

  init();
})();
