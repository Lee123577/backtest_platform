/**
 * 反馈组件(各页共用)
 * ====================
 * 引入 feedback.css + feedback.js 即自动在右下角挂一个"反馈"悬浮按钮，
 * 点开弹窗提交问题反馈 / 功能建议。匿名可提，登录用户后端自动带 user_id。
 *
 * 后端：POST /api/feedback {category, content, contact?}
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
    mask.className = "fb-mask";
    mask.hidden = true;
    mask.innerHTML =
      '<div class="fb-modal" role="dialog" aria-modal="true">' +
      '  <h3>意见反馈</h3>' +
      '  <p class="fb-sub">哪里做得不好，或想要什么新功能？欢迎告诉我们。</p>' +
      '  <div class="fb-cats"></div>' +
      '  <textarea id="fbContent" maxlength="2000" placeholder="请描述你遇到的问题或期待的功能…"></textarea>' +
      '  <input id="fbContact" maxlength="100" placeholder="联系方式(选填，手机/微信/邮箱，方便回复你)">' +
      '  <div class="fb-msg" id="fbMsg"></div>' +
      '  <div class="fb-actions">' +
      '    <button class="fb-submit" id="fbSubmit">提交</button>' +
      '    <button class="fb-cancel" id="fbCancel">取消</button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(mask);

    var catWrap = mask.querySelector(".fb-cats");
    catWrap.innerHTML = CATS.map(function (c, i) {
      return '<div class="fb-cat' + (i === 0 ? " active" : "") +
        '" data-cat="' + c.code + '">' + c.label + "</div>";
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
    mask.addEventListener("click", function (e) { if (e.target === mask) close(); });
    submitBtn.addEventListener("click", onSubmit);
  }

  function setMsg(text, ok) {
    msgEl.textContent = text || "";
    msgEl.className = "fb-msg" + (ok ? " ok" : "");
  }

  function open() {
    buildModal();
    setMsg("");
    submitBtn.disabled = false;
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
      });
  }

  function mountFab() {
    var fab = document.createElement("button");
    fab.className = "fb-fab";
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
