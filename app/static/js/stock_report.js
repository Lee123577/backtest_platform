/* 个股 AI 分析报告页
   =====================
   页面所有内容（身份/评分/指标/策略表/正文）都由服务端直出，见
   app/stock_report/render.py。这个脚本只干一件事：没有报告时让访客点按钮
   触发生成，成功后 reload 让服务端把完整页面渲染出来。

   为什么用 reload 而不是在前端拼 DOM：生成本来就要等 30 秒，多花 200ms 重载
   毫无感知，换来的是渲染逻辑只有一份 —— 不必在 JS 里再实现一遍 markdown
   渲染、评分条、指标网格和策略表，也就不存在"首屏直出"与"二次渲染"排版
   不一致的问题。每日复盘那边维护着 render.py 和 daily_review.js 两份
   markdown 渲染器，是个持续的同步负担，这里不重蹈覆辙。 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function generate(btn, code) {
    btn.disabled = true;
    btn.textContent = "分析中…";
    var hint = $("srGenHint");
    if (hint) hint.textContent = "正在读取行情、跑 9 种策略回测并交给 AI 解读，约需 30 秒";

    fetch("/api/stock_report/" + encodeURIComponent(code) + "/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    }).then(function (r) {
      if (r.ok) {
        window.location.reload();
        return null;
      }
      return r.json().catch(function () { return {}; }).then(function (j) {
        btn.disabled = false;
        btn.textContent = "生成 AI 分析";
        if (hint) hint.textContent = j.detail || "生成失败，请稍后再试";
      });
    }).catch(function () {
      btn.disabled = false;
      btn.textContent = "生成 AI 分析";
      if (hint) hint.textContent = "网络异常，请稍后再试";
    });
  }

  // 单一入口：绑定只能发生一次 —— 绑两次的话点一下会发两个生成请求，
  // 而生成是花钱的动作，还会各占一次 IP 限流额度。
  function boot() {
    var init = $("srInit");
    if (!init) return;
    if (init.getAttribute("data-ssr") === "1") return;   // 已有报告，没有按钮要绑
    var code = init.getAttribute("data-code") || "";
    var btn = $("srGenBtn");
    if (btn && code) {
      btn.addEventListener("click", function () { generate(btn, code); });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
