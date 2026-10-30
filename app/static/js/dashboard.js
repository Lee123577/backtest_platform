/**
 * 仪表盘首页
 * ==========
 * 登录后的概览落地页。复用现有接口，不加后端：
 *   /api/auth/me · /api/daily_review/latest
 *   /api/watchlist/config(需登录，取未读提醒数)
 * 访客态显示登录引导。
 */
(function () {
  "use strict";

  function getJson(url) {
    return fetch(url).then(function (r) {
      return r.ok ? r.json() : Promise.reject(r.status);
    });
  }
  var $ = function (id) { return document.getElementById(id); };
  // 展示名/头像统一由 SPAuth 给 —— 这里以前自己抄了一份 maskEmail,
  // 于是"欢迎回来，fo***@qq.com"跟顶栏、跟设好的昵称三处对不上。
  function nameOf(user) {
    return window.SPAuth ? window.SPAuth.displayName(user) : "";
  }

  var SHORTCUTS = [
    { href: "/daily_review", ico: "📈", label: "每日复盘", desc: "AI 收盘复盘" },
    { href: "/ai_hotsector", ico: "🔥", label: "AI板块观察", desc: "板块热度" },
    { href: "/watchlist", ico: "⭐", label: "自选盯盘", desc: "规则提醒" },
    { href: "/", ico: "📉", label: "单股策略", desc: "指标回测" },
    { href: "/?mode=portfolio", ico: "🧮", label: "组合策略", desc: "组合回测" },
    { href: "/paper_trading", ico: "💹", label: "模拟盘观察", desc: "模拟盘" },
  ];

  function renderShortcuts() {
    $("dashShortcuts").innerHTML = SHORTCUTS.map(function (s) {
      return '<a class="dash-shortcut" href="' + s.href + '">' +
        '<span class="sc-ico">' + s.ico + "</span><span><div class=\"sc-label\">" +
        esc(s.label) + '</div><div class="sc-desc">' + esc(s.desc) + "</div></span></a>";
    }).join("");
  }

  function renderHero(user) {
    if (user) {
      $("dashHello").innerHTML =
        (window.SPAuth ? window.SPAuth.avatarHtml(user, 34, "dash-hero-avatar") : "") +
        "<span>欢迎回来，" + esc(nameOf(user)) + "</span>";
      $("dashHeroSub").textContent = "这里是你的账户概览";
      $("dashHeroAction").innerHTML =
        '<button class="dash-hero-btn ghost" id="dashProfileBtn">编辑资料</button>';
      $("dashProfileBtn").addEventListener("click", function () {
        if (window.SPAuth) window.SPAuth.openProfile();
      });
    } else {
      $("dashHello").textContent = "欢迎使用 A 股量化平台";
      $("dashHeroSub").textContent = "登录后可使用历史复盘、自选盯盘规则提醒等功能";
      $("dashHeroAction").innerHTML = '<button class="dash-hero-btn" id="dashLoginBtn">登录 / 注册</button>';
      $("dashLoginBtn").addEventListener("click", function () {
        // 登录成功不再 location.reload():整页重刷要重新拉四个接口、白屏一下,
        // 而这页需要变的只是几个卡片。登录态由 SPAuth 广播回来,当场重画。
        if (window.SPAuth) window.SPAuth.requireLogin();
      });
    }
  }

  function renderReview(review) {
    var el = $("reviewBody");
    if (!review) {
      el.innerHTML = '<div class="dash-big muted">暂无</div>' +
        '<div class="dash-note">每交易日 17:45 自动生成</div>';
      return;
    }
    el.innerHTML = '<div style="font-size:15px;font-weight:600;line-height:1.4;">' +
      esc(review.title || (review.review_date + " A股复盘")) + "</div>" +
      '<div class="dash-note">' + esc(String(review.review_date)) + "</div>" +
      '<a class="dash-link" href="/daily_review">查看完整复盘 →</a>';
  }

  function renderAlerts(cfg, loggedIn) {
    var el = $("alertsBody");
    if (!loggedIn) {
      el.innerHTML = '<div class="dash-big muted">—</div>' +
        '<div class="dash-note">登录后配置自选盯盘</div>';
      return;
    }
    if (!cfg) {
      el.innerHTML = '<div class="dash-big muted">—</div>' +
        '<div class="dash-note">盯盘配置暂时加载不出来</div>' +
        '<a class="dash-cta ghost" href="/watchlist">去看看</a>';
      return;
    }
    var unread = cfg.unread || 0;
    el.innerHTML = '<div class="dash-big">' +
      (unread > 0 ? '<span class="dash-badge">' + unread + "</span> 条未读" : "0 条未读") +
      "</div>" +
      '<div class="dash-note">自选 ' + (cfg.stocks ? cfg.stocks.length : 0) + " 只 · 盯 " +
      (cfg.rules ? cfg.rules.length : 0) + " 个策略</div>" +
      '<a class="dash-link" href="/watchlist">查看提醒 →</a>';
  }

  // ── 初始化 ──────────────────────────────────────────────────────────────
  renderShortcuts();

  // 与登录态无关的复盘卡片:只拉一次,登录/登出都不用重来。
  // 不再在首屏摆 AI 历史跟踪的上涨占比 / 跑赢基准 —— 那是原个股模拟跟踪的
  // 历史收益,挂在已去个股的板块观察名下既张冠李戴,也正是合规审查要撤的展示。
  getJson("/api/daily_review/latest")
    .catch(function () { return { review: null }; })
    .then(function (res) { renderReview((res && res.review) || null); });

  // 与登录态有关的三块:每次身份变化(登录、登出、改昵称换头像)重画一遍。
  // /api/auth/me 不在这里打 —— SPAuth 全站共享那一次结果。
  function renderForUser(user) {
    var loggedIn = !!user;
    renderHero(user);
    if (!loggedIn) {
      renderAlerts(null, false);
      return;
    }
    getJson("/api/watchlist/config")
      .then(function (cfg) { renderAlerts(cfg, true); })
      .catch(function () { renderAlerts(null, true); });
  }

  if (window.SPAuth) {
    window.SPAuth.onChange(renderForUser);
  } else {
    getJson("/api/auth/me").catch(function () { return { user: null }; })
      .then(function (j) { renderForUser((j && j.user) || null); });
  }
})();
