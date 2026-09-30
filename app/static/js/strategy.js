/**
 * 策略专题页 /strategy/{id} 的参数表单
 * ======================================
 * 表单本身是服务端直出的 <form action="/" method="get">,没有这个脚本也能用 ——
 * 浏览器会把 strategy / code / p_xxx 拼成查询串跳到首页,由 app.js 的
 * applyDeepLink 接住。这里只做三件锦上添花的事:
 *   1. 改过的参数高亮,一眼看出和默认值哪里不同;「恢复默认」一键还原
 *   2. 提交前校验范围,越界时就地提示,而不是跳过去再吃一个后端报错
 *   3. 股票代码框挂上名称联想(不知道代码的人也能用)
 */
(function () {
  "use strict";

  var form = document.getElementById("stForm");
  if (!form) return;
  var msg = document.getElementById("stFormMsg");
  var inputs = Array.prototype.slice.call(form.querySelectorAll("input[data-default]"));
  var codeEl = document.getElementById("stCode");

  function markChanged(el) {
    var changed = String(el.value) !== String(el.getAttribute("data-default"));
    el.classList.toggle("is-changed", changed);
  }

  inputs.forEach(function (el) {
    el.addEventListener("input", function () { markChanged(el); msg.textContent = ""; });
  });

  document.getElementById("stReset").addEventListener("click", function () {
    inputs.forEach(function (el) {
      el.value = el.getAttribute("data-default");
      markChanged(el);
    });
    msg.textContent = "";
  });

  if (codeEl && window.SPStockSuggest) {
    SPStockSuggest.attach(codeEl, {
      onPick: function (item) { codeEl.value = item.code; msg.textContent = ""; },
    });
  }

  function labelOf(el) {
    var lab = form.querySelector('label[for="' + el.id + '"]');
    return lab ? lab.textContent : el.name;
  }

  form.addEventListener("submit", function (e) {
    for (var i = 0; i < inputs.length; i++) {
      var el = inputs[i];
      var v = Number(el.value);
      var lo = Number(el.min), hi = Number(el.max);
      if (el.value === "" || isNaN(v)) {
        e.preventDefault();
        msg.textContent = "「" + labelOf(el) + "」需要填一个数字";
        el.focus();
        return;
      }
      if ((el.min !== "" && v < lo) || (el.max !== "" && v > hi)) {
        e.preventDefault();
        msg.textContent = "「" + labelOf(el) + "」要在 " + el.min + " ~ " + el.max + " 之间";
        el.focus();
        return;
      }
    }
    if (codeEl && !/^\d{6}$/.test(codeEl.value.trim())) {
      e.preventDefault();
      msg.textContent = "股票代码是 6 位数字，比如 000001；也可以输入名称从下拉里选";
      codeEl.focus();
      return;
    }
    if (codeEl) codeEl.value = codeEl.value.trim();
    // 没改过的参数不带上,地址栏干净一些(首页缺省就用默认值)
    inputs.forEach(function (el) {
      if (!el.classList.contains("is-changed")) el.disabled = true;
    });
    if (window.SPTrack) {
      var sid = form.querySelector('input[name="strategy"]');
      SPTrack.event("strategy_try", { strategy: sid ? sid.value : "" });
    }
    // bfcache 返回时把禁用掉的输入框还原,否则再点一次就少了参数
    window.addEventListener("pageshow", function () {
      inputs.forEach(function (el) { el.disabled = false; });
    }, { once: true });
  });
})();
