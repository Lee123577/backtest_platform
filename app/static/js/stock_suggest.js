/**
 * 股票代码/名称联想 —— 挂到任意输入框上
 * ==========================================
 * 起因：首页回测和自选盯盘都硬校验 6 位数字代码，不知道代码的人根本进不来。
 *
 * 用法：
 *   SPStockSuggest.attach(document.getElementById('stockCode'), {
 *     onPick: function (item) { ... }   // item = {code, name}
 *   });
 *
 * 不用原生 <datalist>：Safari 对它支持得很差，而且没法同时显示"代码 + 名称"
 * 两列、也拿不到选中项的名称（只能拿到 value）。
 *
 * CSP 是 script-src 'self'，所以这里是独立文件、无内联脚本。
 */
(function () {
  "use strict";

  var DEBOUNCE_MS = 180;
  var MIN_CHARS = 1;
  var activePanel = null;   // 全局同时只开一个

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function closeActive() {
    if (activePanel) {
      activePanel.hidden = true;
      activePanel = null;
    }
  }

  document.addEventListener("click", function (e) {
    if (activePanel && !activePanel.contains(e.target) &&
        e.target !== activePanel._input) {
      closeActive();
    }
  });

  function attach(input, opts) {
    if (!input || input._spSuggestBound) return;
    input._spSuggestBound = true;
    opts = opts || {};

    // 输入框可能在 position:static 的容器里，包一层定位上下文放下拉面板
    var wrap = document.createElement("span");
    wrap.className = "sp-sug-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    var panel = document.createElement("div");
    panel.className = "sp-sug-panel";
    panel.hidden = true;
    panel._input = input;
    wrap.appendChild(panel);

    var items = [];      // 当前结果
    var cursor = -1;     // 键盘高亮位置
    var timer = null;
    var seq = 0;         // 请求序号：只认最后一次，防乱序响应覆盖新结果

    function render() {
      if (!items.length) {
        panel.hidden = true;
        activePanel = (activePanel === panel) ? null : activePanel;
        return;
      }
      panel.innerHTML = items.map(function (it, i) {
        return '<button type="button" class="sp-sug-item' +
          (i === cursor ? " active" : "") + '" data-i="' + i + '">' +
          '<span class="sp-sug-name">' + esc(it.name) + "</span>" +
          '<span class="sp-sug-code">' + esc(it.code) + "</span></button>";
      }).join("");
      panel.hidden = false;
      activePanel = panel;
    }

    function pick(i) {
      var it = items[i];
      if (!it) return;
      input.value = it.code;
      items = [];
      cursor = -1;
      render();
      closeActive();
      if (opts.onPick) opts.onPick(it);
      // 让原有的 change/input 监听器(比如首页那个查名字的)照常收到通知
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function query(q) {
      var mine = ++seq;
      fetch("/api/stock/search?q=" + encodeURIComponent(q) + "&limit=8")
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (mine !== seq) return;          // 已有更新的请求发出，丢弃这次
          items = (j && j.results) || [];
          cursor = -1;
          render();
        })
        .catch(function () { /* 联想失败不打扰用户，照常可以手打代码 */ });
    }

    input.setAttribute("autocomplete", "off");

    input.addEventListener("input", function () {
      var q = input.value.trim();
      clearTimeout(timer);
      if (q.length < MIN_CHARS) { items = []; render(); return; }
      timer = setTimeout(function () { query(q); }, DEBOUNCE_MS);
    });

    // 键盘处理挂在**父容器的捕获阶段**，不是 input 自己身上。
    // 原因：这些输入框大多已经绑了自己的 Enter 行为(首页 Enter=查看K线、
    // 自选盯盘 Enter=添加)。同一个元素上的多个监听器按注册顺序跑，
    // preventDefault 拦不住兄弟监听器，而在目标元素上 capture 标志也不改变
    // 顺序。挂到祖先节点的捕获阶段才能抢在前面，stopPropagation 才拦得住。
    //
    // 只在"确实消费了这个按键"时才拦截：下拉没开、或 Enter 时没有高亮项，
    // 一律放行，页面原有行为不变。
    wrap.addEventListener("keydown", function (e) {
      if (e.target !== input) return;
      if (panel.hidden || !items.length) return;

      var consumed = false;
      if (e.key === "ArrowDown") {
        cursor = (cursor + 1) % items.length; render(); consumed = true;
      } else if (e.key === "ArrowUp") {
        cursor = (cursor - 1 + items.length) % items.length; render(); consumed = true;
      } else if (e.key === "Enter") {
        if (cursor >= 0) { pick(cursor); consumed = true; }
      } else if (e.key === "Escape") {
        items = []; render(); consumed = true;
      }

      if (consumed) {
        e.preventDefault();
        e.stopPropagation();
      }
    }, true);

    panel.addEventListener("mousedown", function (e) {
      // 用 mousedown 而不是 click：input 的 blur 会先触发，click 可能落空
      var btn = e.target.closest(".sp-sug-item");
      if (!btn) return;
      e.preventDefault();
      pick(parseInt(btn.getAttribute("data-i"), 10));
    });
  }

  window.SPStockSuggest = { attach: attach };
})();
