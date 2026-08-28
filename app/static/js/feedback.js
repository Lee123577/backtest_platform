/**
 * 反馈组件(各页共用)
 * ====================
 * 引入 feedback.css + feedback.js 即自动在右下角挂一个"反馈"悬浮按钮，
 * 点开弹窗提交问题反馈 / 功能建议。匿名可提，登录用户后端自动带 user_id。
 *
 * 后端：POST /api/feedback {category, content, contact?}
 *
 * 弹窗外壳用的是 auth.css 里那套 .sp-* 类(遮罩/卡片/标题/输入框/提交键/
 * 提示行) —— 跟登录弹窗共用一份观感,改一处两处一起变,不会再各走各的。
 * 反馈独有的部分(分类选择、按钮行、多行输入)在 feedback.css。
 * 因此本脚本依赖页面已引入 auth.css;当前引 feedback.js 的页面都引了。
 */
(function () {
  "use strict";

  var CATS = [
    { code: "bug", label: "问题反馈" },
    { code: "feature", label: "功能建议" },
    { code: "other", label: "其他" },
  ];

  var mask = null, textarea, contactInput, submitBtn, msgEl, catEls;
  var category = "bug";

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

  function buildModal() {
    if (mask) return;
    mask = document.createElement("div");
    mask.className = "sp-modal-mask";
    mask.hidden = true;
    mask.innerHTML =
      '<div class="sp-modal" role="dialog" aria-modal="true">' +
      '  <button class="sp-modal-close" id="fbClose" aria-label="关闭">&times;</button>' +
      '  <div class="sp-modal-head">' +
      '    <div class="sp-logo">💬</div>' +
      '    <h3>意见反馈</h3>' +
      '    <p class="sp-sub">哪里做得不好，或想要什么新功能？欢迎告诉我们。</p>' +
      '  </div>' +
      '  <div class="fb-cats"></div>' +
      '  <textarea class="fb-textarea" id="fbContent" maxlength="2000" placeholder="请描述你遇到的问题或期待的功能…"></textarea>' +
      '  <div class="sp-field">' +
      '    <input id="fbContact" maxlength="100" placeholder="联系方式(选填，手机/微信/邮箱，方便回复你)">' +
      '  </div>' +
      '  <div class="sp-msg" id="fbMsg"></div>' +
      '  <div class="fb-actions">' +
      '    <button class="sp-submit" id="fbSubmit">提交</button>' +
      '    <button class="fb-cancel" id="fbCancel">取消</button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(mask);

    // 分类用 <button> 而不是 <div>:键盘能 Tab 到、回车能选,读屏也报得出来
    var catWrap = mask.querySelector(".fb-cats");
    catWrap.innerHTML = CATS.map(function (c, i) {
      return '<button type="button" class="fb-cat' + (i === 0 ? " active" : "") +
        '" data-cat="' + c.code + '">' + c.label + "</button>";
    }).join("");
    catEls = catWrap.querySelectorAll(".fb-cat");
    catEls.forEach(function (el) {
      el.addEventListener("click", function () {
        category = el.getAttribute("data-cat");
        catEls.forEach(function (x) { x.classList.remove("active"); });
        el.classList.add("active");
      });
    });

    textarea = mask.querySelector("#fbContent");
    contactInput = mask.querySelector("#fbContact");
    submitBtn = mask.querySelector("#fbSubmit");
    msgEl = mask.querySelector("#fbMsg");

    mask.querySelector("#fbCancel").addEventListener("click", close);
    mask.querySelector("#fbClose").addEventListener("click", close);
    mask.addEventListener("click", function (e) { if (e.target === mask) close(); });
    submitBtn.addEventListener("click", onSubmit);
  }

  function setMsg(text, ok) {
    msgEl.textContent = text || "";
    msgEl.className = "sp-msg" + (ok ? " ok" : "");
  }

  function open() {
    buildModal();
    setMsg("");
    submitBtn.disabled = false;
    submitBtn.textContent = "提交";
    mask.hidden = false;
    textarea.focus();
  }

  function close() {
    if (mask) mask.hidden = true;
  }

  function onSubmit() {
    var content = (textarea.value || "").trim();
    if (!content) { setMsg("请先填写反馈内容"); return; }
    submitBtn.disabled = true;
    submitBtn.textContent = "提交中…";
    postJson("/api/feedback", {
      category: category,
      content: content,
      contact: (contactInput.value || "").trim() || null,
    })
      .then(function () {
        setMsg("已收到，感谢你的反馈！", true);
        textarea.value = "";
        contactInput.value = "";
        setTimeout(close, 1200);
      })
      .catch(function (e) {
        setMsg(e.message);
        submitBtn.disabled = false;
        submitBtn.textContent = "提交";
      });
  }

  function mountFab() {
    var fab = document.createElement("button");
    fab.className = "fb-fab";
    // 回测页的「开始回测」是 position:sticky 贴底的整宽按钮,悬浮球压在它右端
    // 上面 —— 点按钮右侧 1/4 弹出来的是反馈框而不是跑回测。有贴底 CTA 的页面
    // 把悬浮球抬到 CTA 上方。(.btn-run 是全站唯一 bottom 锚定的 sticky 元素)
    if (document.querySelector(".btn-run")) fab.className += " fb-fab--raised";
    fab.innerHTML = "💬 反馈";
    fab.addEventListener("click", open);
    document.body.appendChild(fab);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mountFab);
  } else {
    mountFab();
  }

  window.SPFeedback = { open: open };
})();
