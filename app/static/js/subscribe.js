/**
 * 订阅页
 * ======
 * 展示会员状态 + 套餐；点击套餐下单。支付宝接入前，下单只创建订单并提示
 * “支付功能接入中”(pay_ready=false)。接入后此处换成拉起二维码。
 */
(function () {
  "use strict";

  function getJson(url) {
    return fetch(url).then(function (r) { return r.json(); });
  }

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

  var statusEl = document.getElementById("subStatus");
  var gridEl = document.getElementById("planGrid");
  var msgEl = document.getElementById("subMsg");

  function renderStatus(st) {
    if (!st.logged_in) {
      statusEl.innerHTML =
        '<span class="sub-inactive">未登录 —— 登录后可开通会员</span> ' +
        '<button class="nav-btn" id="subLoginBtn" style="margin-left:8px;">登录</button>';
      var b = document.getElementById("subLoginBtn");
      if (b) b.addEventListener("click", function () {
        window.SPAuth.requireLogin().then(function (u) { if (u) load(); });
      });
      return;
    }
    if (st.subscribed) {
      statusEl.innerHTML =
        '<span class="sub-active">会员有效</span>，到期时间：' +
        esc((st.expires_at || "").slice(0, 10)) + "。续费可在剩余时长上叠加。";
    } else {
      statusEl.innerHTML =
        '<span class="sub-inactive">当前非会员</span>，开通后即可解锁全部历史内容。';
    }
  }

  function renderPlans(plans) {
    gridEl.innerHTML = (plans || []).map(function (p) {
      return '<div class="plan-card" data-plan="' + esc(p.code) + '">' +
        '<div class="plan-label">' + esc(p.label) + "</div>" +
        '<div class="plan-price">¥' + esc(p.price_yuan) + "</div>" +
        '<div class="plan-days">' + esc(p.days) + " 天</div>" +
        "</div>";
    }).join("");
    gridEl.querySelectorAll(".plan-card").forEach(function (el) {
      el.addEventListener("click", function () {
        onPickPlan(el.getAttribute("data-plan"));
      });
    });
  }

  function onPickPlan(plan) {
    // 未登录 → 先登录再下单
    if (!window.SPAuth.me()) {
      window.SPAuth.requireLogin().then(function (u) {
        if (u) onPickPlan(plan);
      });
      return;
    }
    msgEl.textContent = "正在创建订单…";
    postJson("/api/subscription/order", { plan: plan })
      .then(function (o) {
        if (o.pay_ready) {
          // 支付宝接入后：这里拉起二维码
          msgEl.textContent = "请扫码支付(订单 " + o.order_no + ")";
        } else {
          msgEl.textContent =
            "订单已创建（" + o.order_no + "，¥" + o.amount_yuan +
            "）。在线支付功能接入中，开通后即可扫码付款。";
        }
      })
      .catch(function (e) { msgEl.textContent = "下单失败：" + e.message; });
  }

  function load() {
    getJson("/api/subscription/status")
      .then(function (st) {
        renderStatus(st);
        renderPlans(st.plans);
      })
      .catch(function () {
        statusEl.textContent = "加载失败，请刷新重试";
      });
  }

  load();
})();
