/**
 * 仪表盘首页
 * ==========
 * 登录后的概览落地页。复用现有接口，不加后端：
 *   /api/auth/me · /api/subscription/status · /api/daily_review/latest
 *   /api/watchlist/config(需登录，取未读提醒数)
 * 访客态显示登录引导。
 */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function getJson(url) {
    return fetch(url).then(function (r) {
      return r.ok ? r.json() : Promise.reject(r.status);
    });
  }
  var $ = function (id) { return document.getElementById(id); };
  function maskPhone(p) { return String(p || "").replace(/(\d{3})\d{4}(\d{4})/, "$1****$2"); }

  var SHORTCUTS = [
    { href: "/daily_review", ico: "📈", label: "每日复盘", desc: "AI 收盘复盘" },
    { href: "/ai_hotsector", ico: "🔥", label: "AI热门板块", desc: "每日选股战绩" },
    { href: "/watchlist", ico: "⭐", label: "自选盯盘", desc: "信号提醒" },
    { href: "/cloudmap", ico: "🗺️", label: "大盘云图", desc: "市场全景" },
    { href: "/", ico: "📉", label: "单股策略", desc: "指标回测" },
    { href: "/?mode=portfolio", ico: "🧮", label: "选股策略", desc: "组合回测" },
    { href: "/paper_trading", ico: "💹", label: "实盘观察", desc: "模拟盘" },
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
      $("dashHello").textContent = "欢迎回来，" + maskPhone(user.phone);
      $("dashHeroSub").textContent = "这里是你的账户概览";
      $("dashHeroAction").innerHTML = "";
    } else {
      $("dashHello").textContent = "欢迎使用 A 股量化平台";
      $("dashHeroSub").textContent = "登录后解锁历史复盘、自选盯盘信号提醒等会员内容";
      $("dashHeroAction").innerHTML = '<button class="dash-hero-btn" id="dashLoginBtn">登录 / 注册</button>';
      $("dashLoginBtn").addEventListener("click", function () {
        if (window.SPAuth) window.SPAuth.requireLogin().then(function (u) { if (u) location.reload(); });
      });
    }
  }

  function renderMember(sub, loggedIn) {
    var el = $("memberBody");
    if (!loggedIn) {
      el.innerHTML = '<div class="dash-big muted">未登录</div>' +
        '<div class="dash-note">登录后可开通会员</div>';
      return;
    }
    if (sub && sub.subscribed) {
      var d = (sub.expires_at || "").slice(0, 10);
      var days = "";
      if (d) {
        var left = Math.ceil((new Date(d) - new Date()) / 86400000);
        days = left >= 0 ? "剩 " + left + " 天" : "";
      }
      el.innerHTML = '<div class="dash-big ok">会员有效</div>' +
        '<div class="dash-note">到期 ' + esc(d) + (days ? " · " + days : "") + "</div>" +
        '<a class="dash-link" href="/subscribe">续费 / 管理 →</a>';
    } else {
      el.innerHTML = '<div class="dash-big muted">未开通</div>' +
        '<div class="dash-note">开通后解锁历史复盘与信号提醒</div>' +
        '<a class="dash-cta" href="/subscribe">立即开通</a>';
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
    if (!cfg || !cfg.subscribed) {
      el.innerHTML = '<div class="dash-big muted">会员功能</div>' +
        '<div class="dash-note">开通后收盘自动提醒买卖信号</div>' +
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

  Promise.all([
    getJson("/api/auth/me").catch(function () { return { user: null }; }),
    getJson("/api/subscription/status").catch(function () { return null; }),
    getJson("/api/daily_review/latest").catch(function () { return { review: null }; }),
  ]).then(function (res) {
    var user = (res[0] && res[0].user) || null;
    var sub = res[1];
    var review = (res[2] && res[2].review) || null;
    var loggedIn = !!user;

    renderHero(user);
    renderMember(sub, loggedIn);
    renderReview(review);

    if (loggedIn) {
      getJson("/api/watchlist/config")
        .then(function (cfg) { renderAlerts(cfg, true); })
        .catch(function () { renderAlerts(null, true); });
    } else {
      renderAlerts(null, false);
    }
  });
})();
