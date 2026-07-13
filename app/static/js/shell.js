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
    { title: "会员", items: [
      { label: "购买订阅", href: "/subscribe", ico: "💎" },
    ]},
    { title: "系统", items: [
      { label: "定时任务", href: "/tasks", ico: "⏱️" },
    ]},
  ];

  var BRAND = "📈 量化平台";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

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
    var html = '<a class="spx-brand" href="/dashboard">' + esc(BRAND) + "</a>" +
      '<nav class="spx-nav">';
    NAV.forEach(function (g) {
      html += '<div class="spx-group-title">' + esc(g.title) + "</div>";
      g.items.forEach(function (it) {
        html += '<a class="spx-link' + (isActive(it.href) ? " active" : "") +
          '" href="' + esc(it.href) + '">' +
          '<span class="spx-ico">' + it.ico + "</span><span>" + esc(it.label) + "</span></a>";
      });
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
  function maskPhone(p) {
    return String(p || "").replace(/(\d{3})\d{4}(\d{4})/, "$1****$2");
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
      '<button class="spx-account-btn" id="spxAcctBtn">👤 ' + esc(maskPhone(user.phone)) +
      ' ▾</button>' +
      '<div class="spx-menu" id="spxMenu" hidden>' +
      '<div class="spx-menu-sub">' + esc(maskPhone(user.phone)) + "</div>" +
      '<a href="/dashboard">仪表盘</a>' +
      '<a href="/subscribe">我的订阅</a>' +
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

  // ── 组装 ──────────────────────────────────────────────────────────────
  function mount() {
    if (document.body.classList.contains("spx-shell")) return;
    var title = (document.title || "").replace(/\s*[-|].*$/, "").trim() || "控制台";

    var sidebar = buildSidebar();
    var topbar = buildTopbar(title);
    var mask = document.createElement("div");
    mask.className = "spx-mask";

    document.body.appendChild(sidebar);
    document.body.appendChild(topbar);
    document.body.appendChild(mask);
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
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
