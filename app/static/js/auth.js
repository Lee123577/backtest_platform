/**
 * 登录 + 个人资料组件(各页共用)
 * ==============================
 * 邮箱验证码登录弹窗、个人资料弹窗(昵称/头像)、导航账号入口。
 * 任何页面引入 auth.css + auth.js 即可：
 *   - 页面 header 里放一个 <span id="spAuthSlot"></span>，本脚本渲染账号入口
 *   - 需要登录才可用的操作里调 window.SPAuth.requireLogin().then(user => ...)
 *   - 关心登录态的页面调 window.SPAuth.onChange(fn) —— 登录/登出/改资料都会回调
 *
 * 后端：/api/auth/{send_code,login,logout,me,profile,avatar}。会话是 httponly
 * cookie，前端拿不到也不需要拿，只认 /api/auth/me 的结果。
 *
 * 两件以前做错、这次一起改掉的事：
 *   1) **登录成功后页面不变** —— 旧版只重画了自己那个 slot，顶栏账户区、
 *      仪表盘正文都还是访客态,用户以为没登上。现在登录态是本模块唯一的
 *      事实来源,变了就广播(onChange + document 上的 sp:authchange 事件),
 *      各页当场重画,不需要整页 reload。
 *   2) **每页两次 /api/auth/me** —— auth.js 和 shell.js 各拉一次,两次都要
 *      查会话表。现在合并成一个共享 Promise(ready()),谁要谁取同一份。
 */
window.SPAuth = (function () {
  "use strict";

  var currentUser = null;
  var mePromise = null;     // 共享的 /api/auth/me,全站只发一次
  var pending = null;       // requireLogin 的 resolve，登录成功后回调
  var cooldownTimer = null;
  var listeners = [];       // onChange 订阅者

  function postJson(url, body, method) {
    return fetch(url, {
      method: method || "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(readJson);
  }

  function readJson(r) {
    return r.json().catch(function () { return {}; }).then(function (j) {
      if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
      return j;
    });
  }

  // ── 登录态广播 ───────────────────────────────────────────────────────────
  // 谁改了 currentUser 都要走这里,别的地方一律只读 —— 否则又会出现
  // "某个角落还显示着上一个身份"的老问题。
  function setUser(user) {
    currentUser = user || null;
    renderSlot();
    listeners.forEach(function (fn) {
      try { fn(currentUser); } catch (e) { /* 单个订阅者出错不连累其他人 */ }
    });
    try {
      document.dispatchEvent(new CustomEvent("sp:authchange", {
        detail: { user: currentUser },
      }));
    } catch (e) { /* 老浏览器没有 CustomEvent 构造器,onChange 那条路照常 */ }
  }

  function onChange(fn) {
    if (typeof fn !== "function") return;
    listeners.push(fn);
    // 已经知道登录态就立刻回调一次,调用方不用自己再判断时序
    if (mePromise) mePromise.then(function () { fn(currentUser); });
  }

  function ready() {
    if (!mePromise) {
      mePromise = fetch("/api/auth/me")
        .then(function (r) { return r.json(); })
        .then(function (j) { return j.user || null; })
        .catch(function () { return null; })
        .then(function (u) { currentUser = u; renderSlot(); return u; });
    }
    return mePromise;
  }

  // ── 展示用的名字与头像 ───────────────────────────────────────────────────
  // 账号展示脱敏：foo@qq.com → fo***@qq.com(只在"账号"那一行用,别处用昵称)
  function maskEmail(addr) {
    var s = String(addr || "");
    var at = s.indexOf("@");
    if (at < 1) return s;
    var name = s.slice(0, at);
    return name.slice(0, name.length <= 2 ? 1 : 2) + "***" + s.slice(at);
  }

  function displayName(user) {
    if (!user) return "";
    if (user.display_name) return user.display_name;
    // 没设昵称就用邮箱前缀 —— 比 fo***@qq.com 像个人名,也不把完整地址摊开
    var s = String(user.email || "");
    var at = s.indexOf("@");
    var local = at > 0 ? s.slice(0, at) : s;
    return local.length <= 12 ? local : local.slice(0, 12) + "…";
  }

  // 默认头像:按账号算一个稳定的渐变色 + 名字首字。
  // 不生成文件、不占磁盘,新注册的人一进来就有个像样的头像,
  // 而且同一个人在任何页面看到的都是同一个颜色。
  var AVATAR_GRADIENTS = [
    ["#60a5fa", "#2563eb"], ["#34d399", "#059669"], ["#fbbf24", "#d97706"],
    ["#f87171", "#dc2626"], ["#a78bfa", "#7c3aed"], ["#22d3ee", "#0891b2"],
    ["#fb7185", "#e11d48"], ["#94a3b8", "#475569"],
  ];

  function hashOf(s) {
    var h = 0;
    s = String(s || "");
    for (var i = 0; i < s.length; i++) {
      h = ((h << 5) - h + s.charCodeAt(i)) | 0;
    }
    return Math.abs(h);
  }

  function gradientFor(user) {
    var key = (user && (user.email || user.id)) || "guest";
    var g = AVATAR_GRADIENTS[hashOf(key) % AVATAR_GRADIENTS.length];
    return "linear-gradient(135deg," + g[0] + "," + g[1] + ")";
  }

  function initialOf(user) {
    var n = displayName(user);
    // 用 Array.from 而不是 n[0]:emoji/生僻字是代理对,取半个会渲染成方块
    var chars = Array.from ? Array.from(n) : n.split("");
    var c = chars[0] || "?";
    return /[a-z]/.test(c) ? c.toUpperCase() : c;
  }

  /** 头像 HTML。size 单位 px；有自定义头像发 <img>，否则发默认色块。 */
  function avatarHtml(user, size, extraClass) {
    var s = size || 28;
    var cls = "sp-avatar" + (extraClass ? " " + extraClass : "");
    var box = "width:" + s + "px;height:" + s + "px;";
    if (user && user.avatar_url) {
      return '<img class="' + cls + '" src="' + esc(user.avatar_url) +
        '" alt="" style="' + box + '">';
    }
    return '<span class="' + cls + ' sp-avatar-default" style="' + box +
      "font-size:" + Math.round(s * 0.44) + "px;background:" + gradientFor(user) +
      '">' + esc(initialOf(user)) + "</span>";
  }

  // ── 轻提示 ───────────────────────────────────────────────────────────────
  var toastEl = null, toastTimer = null;

  function toast(text, isErr) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "sp-toast";
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = text;
    toastEl.className = "sp-toast show" + (isErr ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastEl.className = "sp-toast";
    }, 2600);
  }

  // ── 登录弹窗 ─────────────────────────────────────────────────────────────
  var mask = null, emailInput, codeInput, codeBtn, submitBtn, msgEl;

  // 邮箱校验与后端 service.normalize_email 同口径(宽松但拒空白/控制字符)
  var EMAIL_RE = /^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/;

  function validEmail(v) { return v.length <= 190 && EMAIL_RE.test(v); }

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
      '    <p class="sp-sub">邮箱验证码登录，未注册将自动创建账号</p>' +
      '  </div>' +
      '  <div class="sp-field">' +
      '    <input id="spEmail" type="email" maxlength="190" placeholder="请输入邮箱" autocomplete="email" inputmode="email">' +
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

    emailInput = mask.querySelector("#spEmail");
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
    emailInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !codeBtn.disabled) onSendCode();
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
    var email = (emailInput.value || "").trim().toLowerCase();
    if (!validEmail(email)) { setMsg("请输入正确的邮箱地址"); return; }
    codeBtn.disabled = true;
    codeBtn.textContent = "发送中…";
    postJson("/api/auth/send_code", { email: email })
      .then(function (j) {
        // 明确提示去垃圾箱找 —— 邮箱验证码最常见的失败不是没发出去，
        // 而是发出去了但用户在收件箱里找不到。
        setMsg("验证码已发送，请查收邮件（也看一下垃圾箱）", true);
        startCooldown(j.cooldown || 60);
        codeInput.focus();
      })
      .catch(function (e) {
        setMsg(e.message);
        codeBtn.disabled = false;
        codeBtn.textContent = "获取验证码";
      });
  }

  function onSubmit() {
    var email = (emailInput.value || "").trim().toLowerCase();
    var code = (codeInput.value || "").trim();
    if (!validEmail(email)) { setMsg("请输入正确的邮箱地址"); return; }
    if (!/^\d{6}$/.test(code)) { setMsg("请输入 6 位验证码"); return; }
    submitBtn.disabled = true;
    submitBtn.textContent = "登录中…";
    postJson("/api/auth/login", { email: email, code: code })
      .then(function (j) {
        // 登录接口已经把用户信息带回来了,不必再打一次 /me ——
        // 少一个来回,弹窗关掉的瞬间顶栏就是新身份。
        mePromise = Promise.resolve(j.user);
        var wasPending = pending;
        pending = null;
        close(true);
        setUser(j.user);
        if (wasPending) wasPending(j.user);
        if (j.is_new) {
          toast("账号已创建，已为你分配一个默认头像");
          // 新用户直接进个人资料:这是"设昵称/换头像"最自然的时机
          setTimeout(function () { openProfile(); }, 450);
        } else {
          toast("已登录，" + displayName(j.user));
        }
      })
      .catch(function (e) {
        setMsg(e.message);
        submitBtn.disabled = false;
        submitBtn.textContent = "登录 / 注册";
      });
  }

  function open() {
    buildModal();
    setMsg("");
    submitBtn.disabled = false;
    submitBtn.textContent = "登录 / 注册";
    mask.hidden = false;
    emailInput.focus();
  }

  /** close(true) = 登录成功后的关闭，不要把 pending 当成"用户放弃了" */
  function close(succeeded) {
    if (mask) mask.hidden = true;
    clearInterval(cooldownTimer);
    if (codeBtn) { codeBtn.disabled = false; codeBtn.textContent = "获取验证码"; }
    if (!succeeded && pending) { var p = pending; pending = null; p(null); }
  }

  // ── 个人资料弹窗 ─────────────────────────────────────────────────────────
  var pMask = null, pAvatar, pFile, pName, pCount, pEmail, pMsg, pSave, pReset;

  function buildProfile() {
    if (pMask) return;
    pMask = document.createElement("div");
    pMask.className = "sp-modal-mask";
    pMask.hidden = true;
    pMask.innerHTML =
      '<div class="sp-modal sp-profile" role="dialog" aria-modal="true">' +
      '  <button class="sp-modal-close" aria-label="关闭">&times;</button>' +
      '  <div class="sp-modal-head"><h3>个人资料</h3>' +
      '    <p class="sp-sub">昵称和头像会显示在你的账号入口</p></div>' +
      '  <div class="sp-prof-avatar">' +
      '    <button class="sp-avatar-wrap" id="spAvatarBtn" title="更换头像">' +
      '      <span id="spAvatarBox"></span><span class="sp-avatar-edit">更换</span>' +
      '    </button>' +
      '    <input type="file" id="spAvatarFile" accept="image/png,image/jpeg,image/gif,image/webp" hidden>' +
      '    <div class="sp-prof-hint">支持 JPG / PNG / GIF / WebP，2MB 以内' +
      '      <button class="sp-linkbtn" id="spAvatarReset" hidden>恢复默认头像</button></div>' +
      '  </div>' +
      '  <label class="sp-prof-label" for="spName">昵称 <span id="spNameCount"></span></label>' +
      '  <div class="sp-field">' +
      '    <input id="spName" type="text" maxlength="16" placeholder="留空则用邮箱前缀">' +
      '  </div>' +
      '  <label class="sp-prof-label">登录邮箱</label>' +
      '  <div class="sp-prof-static" id="spProfEmail"></div>' +
      '  <div class="sp-msg" id="spProfMsg"></div>' +
      '  <button class="sp-submit" id="spProfSave">保存</button>' +
      '</div>';
    document.body.appendChild(pMask);

    pAvatar = pMask.querySelector("#spAvatarBox");
    pFile = pMask.querySelector("#spAvatarFile");
    pName = pMask.querySelector("#spName");
    pCount = pMask.querySelector("#spNameCount");
    pEmail = pMask.querySelector("#spProfEmail");
    pMsg = pMask.querySelector("#spProfMsg");
    pSave = pMask.querySelector("#spProfSave");
    pReset = pMask.querySelector("#spAvatarReset");

    pMask.querySelector(".sp-modal-close").addEventListener("click", closeProfile);
    pMask.addEventListener("click", function (e) { if (e.target === pMask) closeProfile(); });
    pMask.querySelector("#spAvatarBtn").addEventListener("click", function () { pFile.click(); });
    pFile.addEventListener("change", onPickAvatar);
    pReset.addEventListener("click", onResetAvatar);
    pSave.addEventListener("click", onSaveProfile);
    pName.addEventListener("input", updateCount);
    pName.addEventListener("keydown", function (e) {
      if (e.key === "Enter") onSaveProfile();
    });
  }

  function updateCount() {
    var n = Array.from ? Array.from(pName.value).length : pName.value.length;
    pCount.textContent = n + "/16";
  }

  function setProfMsg(text, ok) {
    pMsg.textContent = text || "";
    pMsg.className = "sp-msg" + (ok ? " ok" : "");
  }

  function paintProfile() {
    pAvatar.innerHTML = avatarHtml(currentUser, 88);
    pReset.hidden = !(currentUser && currentUser.avatar_url);
    pEmail.textContent = maskEmail(currentUser && currentUser.email);
  }

  function openProfile() {
    if (!currentUser) { return requireLogin().then(function (u) { if (u) openProfile(); }); }
    buildProfile();
    setProfMsg("");
    pName.value = (currentUser && currentUser.display_name) || "";
    updateCount();
    paintProfile();
    pSave.disabled = false;
    pSave.textContent = "保存";
    pMask.hidden = false;
    pName.focus();
  }

  function closeProfile() {
    if (pMask) pMask.hidden = true;
  }

  var MAX_AVATAR_BYTES = 2 * 1024 * 1024;
  var ALLOWED_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];

  /**
   * 上传前在本地裁成正方形并缩到 512 —— 手机随手拍一张就是 4~8MB,
   * 直接传既慢又容易撞 2MB 上限,服务器也要多解一次大图。
   * 裁切口径与服务端 ImageOps.fit 一致(居中、不放大)。
   * 任何一步不支持/出错都退回原文件,不因为"优化"把功能搞没。
   */
  function shrink(file) {
    return new Promise(function (resolve) {
      if (file.size <= 300 * 1024 || !window.URL || !window.URL.createObjectURL) {
        return resolve(file);
      }
      var url = URL.createObjectURL(file);
      var img = new Image();
      var done = function (out) { URL.revokeObjectURL(url); resolve(out || file); };
      img.onerror = function () { done(null); };
      img.onload = function () {
        try {
          var side = Math.min(img.naturalWidth, img.naturalHeight);
          var out = Math.min(512, side);
          var cv = document.createElement("canvas");
          cv.width = out;
          cv.height = out;
          var ctx = cv.getContext("2d");
          if (!ctx || !cv.toBlob) return done(null);
          ctx.drawImage(img,
            (img.naturalWidth - side) / 2, (img.naturalHeight - side) / 2, side, side,
            0, 0, out, out);
          // PNG 源保持 PNG,不然透明区会被压成黑块
          var type = file.type === "image/png" ? "image/png" : "image/jpeg";
          cv.toBlob(function (blob) {
            done(blob && blob.size < file.size ? blob : null);
          }, type, 0.88);
        } catch (e) { done(null); }
      };
      img.src = url;
    });
  }

  function onPickAvatar() {
    var file = pFile.files && pFile.files[0];
    pFile.value = "";              // 允许连着选同一个文件重试
    if (!file) return;
    if (ALLOWED_TYPES.indexOf(file.type) < 0) {
      setProfMsg("只支持 JPG / PNG / GIF / WebP 图片");
      return;
    }
    if (file.size > 8 * 1024 * 1024) {   // 本地就拦住离谱的大文件,别白传一趟
      setProfMsg("图片太大了，请换一张(2MB 以内)");
      return;
    }
    setProfMsg("上传中…", true);
    shrink(file).then(function (blob) {
      if (blob.size > MAX_AVATAR_BYTES) {
        setProfMsg("图片不能超过 2MB，请换一张");
        return;
      }
      return fetch("/api/auth/avatar", {
        method: "POST",
        headers: { "Content-Type": blob.type || "application/octet-stream" },
        body: blob,
      }).then(readJson).then(function (j) {
        setProfMsg("头像已更新", true);
        setUser(Object.assign({}, currentUser, { avatar_url: j.avatar_url }));
        paintProfile();
        toast("头像已更新");
      });
    }).catch(function (e) {
      setProfMsg(e.message || "上传失败，请稍后重试");
    });
  }

  function onResetAvatar() {
    setProfMsg("处理中…", true);
    fetch("/api/auth/avatar", { method: "DELETE" }).then(readJson)
      .then(function () {
        setProfMsg("已恢复默认头像", true);
        setUser(Object.assign({}, currentUser, { avatar_url: null }));
        paintProfile();
      })
      .catch(function (e) { setProfMsg(e.message || "操作失败"); });
  }

  function onSaveProfile() {
    var name = (pName.value || "").trim();
    pSave.disabled = true;
    pSave.textContent = "保存中…";
    postJson("/api/auth/profile", { display_name: name }, "PUT")
      .then(function (j) {
        setUser(Object.assign({}, currentUser, { display_name: j.display_name }));
        closeProfile();
        toast("资料已保存");
      })
      .catch(function (e) { setProfMsg(e.message || "保存失败"); })
      .then(function () {
        pSave.disabled = false;
        pSave.textContent = "保存";
      });
  }

  // ── 导航账号入口(旧版页面的 #spAuthSlot) ────────────────────────────────
  function renderSlot() {
    var slot = document.getElementById("spAuthSlot");
    if (!slot) return;
    if (currentUser) {
      slot.innerHTML = '<button class="sp-auth-btn sp-auth-acct" id="spAcct">' +
        avatarHtml(currentUser, 24) +
        '<span class="sp-auth-name">' + esc(displayName(currentUser)) + "</span></button>";
      // 点头像进个人资料,不再是 confirm("退出登录？") —— 那个 confirm 是
      // 用户最常误触的一步:想看看自己的账号,结果弹一个"要退出吗"。
      slot.querySelector("#spAcct").addEventListener("click", openProfile);
    } else {
      slot.innerHTML = '<button class="sp-auth-btn" id="spLogin">登录</button>';
      slot.querySelector("#spLogin").addEventListener("click", function () { open(); });
    }
  }

  function logout() {
    return postJson("/api/auth/logout", {})
      .catch(function () { /* 服务端删会话失败也要把前端切回访客态 */ })
      .then(function () {
        mePromise = Promise.resolve(null);
        setUser(null);
        toast("已退出登录");
      });
  }

  // requireLogin: 已登录直接 resolve；否则弹窗，登录成功后 resolve(user)、
  // 用户关闭则 resolve(null)（调用方据此决定是否继续）
  function requireLogin() {
    return ready().then(function (u) {
      if (u) return u;
      return new Promise(function (resolve) {
        pending = resolve;
        open();
      });
    });
  }

  ready();   // 首屏就拉一次登录态,后面谁问都吃这一份

  // 各页都是在 </body> 前引入本脚本,slot 早就在了;万一哪个页面提前引,
  // DOM 齐了再补画一次(renderSlot 幂等)。
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderSlot);
  }

  return {
    ready: ready,
    onChange: onChange,
    requireLogin: requireLogin,
    openLogin: open,
    openProfile: openProfile,
    logout: logout,
    me: function () { return currentUser; },
    displayName: displayName,
    maskEmail: maskEmail,
    avatarHtml: avatarHtml,
    toast: toast,
  };
})();
