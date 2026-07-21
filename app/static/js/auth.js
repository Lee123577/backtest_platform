/**
 * 登录组件(各页共用)
 * ====================
 * 手机验证码登录弹窗 + 导航账号入口。任何页面引入 auth.css + auth.js 即可：
 *   - 页面 header 里放一个 <span id="spAuthSlot"></span>，本脚本渲染"登录/我的"
 *   - 需要登录才可用的操作里调 window.SPAuth.requireLogin().then(user => ...)
 *
 * 后端：/api/auth/{send_code,login,logout,me}。会话是 httponly cookie，
 * 前端拿不到也不需要拿，只认 /api/auth/me 的结果。
 */
window.SPAuth = (function () {
  "use strict";

  var currentUser = null;
  var pending = null;       // requireLogin 的 resolve，登录成功后回调
  var cooldownTimer = null;

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
        return j;
      });
    });
  }

  // ── 弹窗 DOM(懒建，全站一个) ─────────────────────────────────────────────
  var mask = null, phoneInput, codeInput, codeBtn, submitBtn, msgEl;

  function buildModal() {
    if (mask) return;
    mask = document.createElement("div");
    mask.className = "sp-modal-mask";
    mask.hidden = true;
    mask.innerHTML =
      '<div class="sp-modal" role="dialog" aria-modal="true">' +
      '  <button class="sp-modal-close" aria-label="关闭">&times;</button>' +
      '  <div class="sp-modal-head">' +
      '    <div class="sp-logo">📈</div>' +
      '    <h3>登录 / 注册</h3>' +
      '    <p class="sp-sub">手机号验证码登录，未注册将自动创建账号</p>' +
      '  </div>' +
      '  <div class="sp-field">' +
      '    <input id="spPhone" type="tel" maxlength="11" placeholder="请输入手机号" autocomplete="tel">' +
      '    <button class="sp-code-btn" id="spCodeBtn">获取验证码</button>' +
      '  </div>' +
      '  <div class="sp-field">' +
      '    <input id="spCode" type="tel" maxlength="6" placeholder="请输入 6 位验证码" autocomplete="one-time-code">' +
      '  </div>' +
      '  <div class="sp-msg" id="spMsg"></div>' +
      '  <button class="sp-submit" id="spSubmit">登录 / 注册</button>' +
      '  <div class="sp-agreement">登录即表示同意本站服务条款。本站内容仅供研究，不构成投资建议。</div>' +
      '</div>';
    document.body.appendChild(mask);

    phoneInput = mask.querySelector("#spPhone");
    codeInput = mask.querySelector("#spCode");
    codeBtn = mask.querySelector("#spCodeBtn");
    submitBtn = mask.querySelector("#spSubmit");
    msgEl = mask.querySelector("#spMsg");

    mask.querySelector(".sp-modal-close").addEventListener("click", close);
    mask.addEventListener("click", function (e) { if (e.target === mask) close(); });
    codeBtn.addEventListener("click", onSendCode);
    submitBtn.addEventListener("click", onSubmit);
    codeInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") onSubmit();
    });
  }

  function setMsg(text, ok) {
    msgEl.textContent = text || "";
    msgEl.className = "sp-msg" + (ok ? " ok" : "");
  }

  function startCooldown(sec) {
    var left = sec;
    codeBtn.disabled = true;
    clearInterval(cooldownTimer);
    codeBtn.textContent = left + " 秒后重发";
    cooldownTimer = setInterval(function () {
      left -= 1;
      if (left <= 0) {
        clearInterval(cooldownTimer);
        codeBtn.disabled = false;
        codeBtn.textContent = "获取验证码";
      } else {
        codeBtn.textContent = left + " 秒后重发";
      }
    }, 1000);
  }

  function onSendCode() {
    var phone = (phoneInput.value || "").trim();
    if (!/^1[3-9]\d{9}$/.test(phone)) { setMsg("请输入正确的手机号"); return; }
    codeBtn.disabled = true;
    postJson("/api/auth/send_code", { phone: phone })
      .then(function (j) {
        setMsg("验证码已发送", true);
        startCooldown(j.cooldown || 60);
        codeInput.focus();
      })
      .catch(function (e) {
        setMsg(e.message);
        codeBtn.disabled = false;
      });
  }

  function onSubmit() {
    var phone = (phoneInput.value || "").trim();
    var code = (codeInput.value || "").trim();
    if (!/^1[3-9]\d{9}$/.test(phone)) { setMsg("请输入正确的手机号"); return; }
    if (!/^\d{6}$/.test(code)) { setMsg("请输入 6 位验证码"); return; }
    submitBtn.disabled = true;
    postJson("/api/auth/login", { phone: phone, code: code })
      .then(function (j) {
        currentUser = j.user;
        renderSlot();
        close();
        if (pending) { pending(currentUser); pending = null; }
      })
      .catch(function (e) {
        setMsg(e.message);
        submitBtn.disabled = false;
      });
  }

  function open() {
    buildModal();
    setMsg("");
    submitBtn.disabled = false;
    mask.hidden = false;
    phoneInput.focus();
  }

  function close() {
    if (mask) mask.hidden = true;
    clearInterval(cooldownTimer);
    if (codeBtn) { codeBtn.disabled = false; codeBtn.textContent = "获取验证码"; }
    // 关闭而未登录成功 → 兑现一个 reject 语义(pending 置空，调用方 .catch 处理)
    if (pending) { var p = pending; pending = null; p(null); }
  }

  // ── 导航账号入口 ──────────────────────────────────────────────────────────
  function renderSlot() {
    var slot = document.getElementById("spAuthSlot");
    if (!slot) return;
    if (currentUser) {
      var masked = String(currentUser.phone).replace(/(\d{3})\d{4}(\d{4})/, "$1****$2");
      slot.innerHTML =
        '<span class="sp-auth-btn nav-btn" id="spAcct">' + esc(masked) + '</span>';
      slot.querySelector("#spAcct").addEventListener("click", function () {
        if (confirm("退出登录？")) logout();
      });
    } else {
      slot.innerHTML = '<span class="sp-auth-btn nav-btn" id="spLogin">登录</span>';
      slot.querySelector("#spLogin").addEventListener("click", function () { open(); });
    }
  }

  function logout() {
    postJson("/api/auth/logout", {}).finally(function () {
      currentUser = null;
      renderSlot();
    });
  }

  // requireLogin: 已登录直接 resolve；否则弹窗，登录成功后 resolve(user)、
  // 用户关闭则 resolve(null)（调用方据此决定是否继续）
  function requireLogin() {
    return new Promise(function (resolve) {
      if (currentUser) { resolve(currentUser); return; }
      pending = resolve;
      open();
    });
  }

  // ── 初始化：拉一次 /me 定登录态 ─────────────────────────────────────────────
  function init() {
    fetch("/api/auth/me").then(function (r) { return r.json(); })
      .then(function (j) { currentUser = j.user || null; })
      .catch(function () { currentUser = null; })
      .finally(renderSlot);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  return {
    requireLogin: requireLogin,
    openLogin: open,
    logout: logout,
    me: function () { return currentUser; },
  };
})();
