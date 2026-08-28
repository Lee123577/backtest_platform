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

  // hero 里的 AI 战绩数据条：拿 AI 热门板块的累计战绩当信任背书。
  // 样本太少(结算<30只)或接口失败就保持隐藏，不给空壳。
  function renderHeroStats(st) {
    var el = $("dashHeroStats");
    if (!st || !st.total_count || st.total_count < 30 || st.win_rate == null) return;
    var pills = [];
    pills.push('<span class="dash-hero-stat">AI 选股累计胜率 <b>' +
      (st.win_rate * 100).toFixed(1) + "%</b>（" + st.win_count + "/" + st.total_count + "）</span>");
    if (st.cum_return_after_fee != null && st.benchmark_cum_return != null) {
      var diff = (st.cum_return_after_fee - st.benchmark_cum_return) * 100;
      var word = diff >= 0 ? "跑赢" : "跑输";
      pills.push('<span class="dash-hero-stat">扣费后 ' + word + "中证1000 <b>" +
        Math.abs(diff).toFixed(1) + "</b> 个百分点</span>");
    }
    pills.push('<a class="dash-hero-stat" href="/ai_hotsector">完整战绩公开可查 →</a>');
    el.innerHTML = pills.join("");
    el.hidden = false;
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
      $("dashHeroSub").textContent = "登录后解锁历史复盘、自选盯盘信号提醒等会员内容";
      $("dashHeroAction").innerHTML = '<button class="dash-hero-btn" id="dashLoginBtn">登录 / 注册</button>';
      $("dashLoginBtn").addEventListener("click", function () {
        // 登录成功不再 location.reload():整页重刷要重新拉四个接口、白屏一下,
        // 而这页需要变的只是几个卡片。登录态由 SPAuth 广播回来,当场重画。
        if (window.SPAuth) window.SPAuth.requireLogin();
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

  // 与登录态无关的三块:只拉一次,登录/登出都不用重来。
  Promise.all([
    getJson("/api/daily_review/latest").catch(function () { return { review: null }; }),
    getJson("/api/ai_hotsector/stats").catch(function () { return null; }),
  ]).then(function (res) {
    renderReview((res[0] && res[0].review) || null);
    renderHeroStats(res[1]);
  });

  // 与登录态有关的三块:每次身份变化(登录、登出、改昵称换头像)重画一遍。
  // /api/auth/me 不在这里打 —— SPAuth 全站共享那一次结果。
  function renderForUser(user) {
    var loggedIn = !!user;
    renderHero(user);
    if (!loggedIn) {
      renderMember(null, false);
      renderAlerts(null, false);
      return;
    }
    getJson("/api/subscription/status")
      .then(function (sub) { renderMember(sub, true); })
      .catch(function () { renderMember(null, true); });
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
