/**
 * App Shell —— 左侧边栏 + 顶栏 + 右上角账户
 * ==========================================
 * 渐进增强：任何页面引入 shell.css + shell.js（及 auth.js）即可。
 * 自动隐藏旧的顶部 <header> 横向导航，构建统一的侧边栏 + 顶栏布局。
 * 账户区复用 window.SPAuth（登录弹窗/登录态）；未登录显示"登录"(访客态)。
 */
(function () {
  "use strict";

  // ── 导航配置(分组) ────────────────────────────────────────────────────
  var NAV = [
    { title: "概览", items: [
      { label: "仪表盘", href: "/dashboard", ico: "🏠" },
      { label: "我的数据看板", href: "/my_board", ico: "📊" },
    ]},
    { title: "行情研究", items: [
      { label: "每日复盘", href: "/daily_review", ico: "📈" },
      { label: "AI热门板块", href: "/ai_hotsector", ico: "🔥" },
      { label: "大盘云图", href: "/cloudmap", ico: "🗺️" },
    ]},
    { title: "策略工具", items: [
      { label: "单股策略", href: "/", ico: "📉" },
      { label: "选股策略", href: "/?mode=portfolio", ico: "🧮" },
      { label: "实盘观察", href: "/paper_trading", ico: "💹" },
      { label: "自选盯盘", href: "/watchlist", ico: "⭐" },
    ]},
    // 订阅页(/subscribe)不进侧边栏,但**不是**下线状态 —— 页面/接口都在正常
    // 收单,入口走场景内引导(复盘付费墙、仪表盘会员卡、自选盯盘提醒),转化率
    // 比放一个常驻"开通会员"菜单项高;同时它照常进 sitemap 吃搜索流量。
    // 早先这里写的是"暂缓上线,不导流量进去",跟 main.py 的 _SITEMAP_PAGES
    // 自相矛盾,已按实际行为改正。
    // 运维页:仅管理员 IP 可见(默认 hidden,确认身份后才显示,避免闪现)。
    // 路径特意用 /admin/tasks 而不是 /tasks —— 这是内部运维监控,不该跟
    // 单股回测/看板等用户功能同层级挂在一起(旧路径 /tasks 307 跳转过来)。
    { title: "系统", adminOnly: true, items: [
      { label: "定时任务", href: "/admin/tasks", ico: "⏱️" },
    ]},
  ];

  var BRAND_NAME = "🕐 收盘 shoupan";
  var BRAND_SLOGAN = "用数据验证策略，让判断有据可依";

  // 当前路径匹配(处理 /?mode=portfolio 与 / 的区分)
  function isActive(href) {
    var path = location.pathname.replace(/\/$/, "") || "/";
    var qs = new URLSearchParams(location.search);
    var portfolio = qs.get("mode") === "portfolio";
    if (href === "/") return path === "/" && !portfolio;
    if (href === "/?mode=portfolio") return path === "/" && portfolio;
    return path === href.replace(/\/$/, "");
  }

  function buildSidebar() {
    var aside = document.createElement("aside");
    aside.className = "spx-sidebar";
    var html = '<a class="spx-brand" href="/dashboard" title="' + esc(BRAND_SLOGAN) + '">' +
      '<span class="spx-brand-name">' + esc(BRAND_NAME) + '</span>' +
      '<span class="spx-brand-slogan">' + esc(BRAND_SLOGAN) + '</span>' +
      "</a>" +
      '<nav class="spx-nav">';
    NAV.forEach(function (g) {
      // adminOnly 组默认 hidden,身份确认后才 reveal(见 revealAdminNav)
      html += '<div class="spx-group"' +
        (g.adminOnly ? ' data-admin-only="1" hidden' : "") + ">" +
        '<div class="spx-group-title">' + esc(g.title) + "</div>";
      g.items.forEach(function (it) {
        html += '<a class="spx-link' + (isActive(it.href) ? " active" : "") +
          '" href="' + esc(it.href) + '">' +
          '<span class="spx-ico">' + it.ico + "</span><span>" + esc(it.label) + "</span></a>";
      });
      html += "</div>";
    });
    html += "</nav>";
    aside.innerHTML = html;
    return aside;
  }

  function buildTopbar(title) {
    var bar = document.createElement("div");
    bar.className = "spx-topbar";
    bar.innerHTML =
      '<button class="spx-hamburger" aria-label="菜单">☰</button>' +
      '<span class="spx-title">' + esc(title) + "</span>" +
      '<span class="spx-spacer"></span>' +
      '<span class="spx-account" id="spxAccount"></span>';
    return bar;
  }

  // ── 账户区 ────────────────────────────────────────────────────────────
  // 账号展示脱敏：foo@qq.com → fo***@qq.com
  function maskEmail(addr) {
    var s = String(addr || "");
    var at = s.indexOf("@");
    if (at < 1) return s;
    var name = s.slice(0, at);
    return name.slice(0, name.length <= 2 ? 1 : 2) + "***" + s.slice(at);
  }

  function renderAccount(user) {
    var slot = document.getElementById("spxAccount");
    if (!slot) return;
    if (!user) {
      slot.innerHTML = '<button class="spx-account-btn" id="spxLogin">👤 登录</button>';
      document.getElementById("spxLogin").addEventListener("click", function () {
        if (window.SPAuth) {
          window.SPAuth.requireLogin().then(function (u) { if (u) refreshAccount(); });
        }
      });
      return;
    }
    slot.innerHTML =
      '<button class="spx-account-btn" id="spxAcctBtn">👤 ' + esc(maskEmail(user.email)) +
      ' ▾</button>' +
      '<div class="spx-menu" id="spxMenu" hidden>' +
      '<div class="spx-menu-sub">' + esc(maskEmail(user.email)) + "</div>" +
      '<a href="/dashboard">仪表盘</a>' +
      '<a href="/watchlist">自选盯盘</a>' +
      '<button id="spxLogout">退出登录</button>' +
      "</div>";
    var btn = document.getElementById("spxAcctBtn");
    var menu = document.getElementById("spxMenu");
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
    });
    document.addEventListener("click", function () { menu.hidden = true; });
    document.getElementById("spxLogout").addEventListener("click", function () {
      if (window.SPAuth) window.SPAuth.logout();
      renderAccount(null);
    });
  }

  function refreshAccount() {
    fetch("/api/auth/me").then(function (r) { return r.json(); })
      .then(function (j) { renderAccount(j.user || null); })
      .catch(function () { renderAccount(null); });
  }

  // ── 管理员菜单 ────────────────────────────────────────────────────────
  // 运维页(定时任务)只给管理员 IP 看:普通用户/访客看了没意义,还会暴露
  // 内部任务名与执行日志。白名单为空时(全新部署)放行 —— 否则没人看得到
  // 入口,也就无法走 admin_ip 的首次自举。
  function revealAdminNav() {
    fetch("/api/admin/ip/me")
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.is_admin && !j.whitelist_empty) return;
        document.querySelectorAll('.spx-group[data-admin-only]')
          .forEach(function (el) { el.hidden = false; });
      })
      .catch(function () { /* 拿不到就保持隐藏 */ });
  }

  // ── 组装 ──────────────────────────────────────────────────────────────
  function mount() {
    // 幂等判据看的是"侧边栏建没建",不是 .spx-shell 这个 class ——
    // 该 class 现在由各页 HTML 的 <body class="spx-shell"> 直接带上(见下方
    // 说明),用它当判据会让 mount() 一进来就 return,侧边栏永远建不出来。
    if (document.querySelector(".spx-sidebar")) return;
    var title = (document.title || "").replace(/\s*[-|].*$/, "").trim() || "控制台";

    var sidebar = buildSidebar();
    var topbar = buildTopbar(title);
    var mask = document.createElement("div");
    mask.className = "spx-mask";

    document.body.appendChild(sidebar);
    document.body.appendChild(topbar);
    document.body.appendChild(mask);
    // 兜底:正常情况下 HTML 已经写了 <body class="spx-shell">(首屏就隐藏旧
    // header,避免闪现);万一哪个新页面漏写,这里补上,行为跟以前一致。
    document.body.classList.add("spx-shell");

    // 移动端汉堡菜单
    var ham = topbar.querySelector(".spx-hamburger");
    ham.addEventListener("click", function () {
      sidebar.classList.toggle("open");
      mask.classList.toggle("show");
    });
    mask.addEventListener("click", function () {
      sidebar.classList.remove("open");
      mask.classList.remove("show");
    });

    refreshAccount();
    revealAdminNav();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
