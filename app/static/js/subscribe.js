/**
 * 订阅页
 * ======
 * 展示会员状态 + 套餐；点击套餐下单。当前无在线支付(pay_ready=false)：下单
 * 生成订单号，引导用户拿订单号加 QQ 人工开通。接入支付宝后 pay_ready 置 true，
 * 走 else 分支拉起二维码。
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

  // 复制订单号。navigator.clipboard 只在安全上下文(https/localhost)可用，
  // 走 http 访问时回退到临时 textarea + execCommand，两条路都失败就提示手动复制。
  function copyText(text, btn) {
    function done(ok) {
      btn.textContent = ok ? "已复制 ✓" : "复制失败，请手动选中";
      setTimeout(function () { btn.textContent = "复制订单号"; }, 2000);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text)
        .then(function () { done(true); })
        .catch(function () { done(false); });
      return;
    }
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    done(ok);
  }

  var statusEl = document.getElementById("subStatus");
  var gridEl = document.getElementById("planGrid");
  var msgEl = document.getElementById("subMsg");
  var contactEl = document.getElementById("subContact");
  var contact = null; // {channel, qq, hint} —— 由 /status 下发

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

  // 人工开通说明：常驻展示，不用等下单完才知道怎么开通
  function renderContact(c) {
    if (!contactEl) return;
    if (!c || !c.qq) { contactEl.innerHTML = ""; return; }
    contactEl.innerHTML =
      '<div class="sub-contact-title">如何开通</div>' +
      "<p>目前暂未开放在线支付，采用人工开通：选择套餐生成订单号后，" +
      "加 QQ <strong class=\"sub-contact-qq\">" + esc(c.qq) + "</strong> " +
      "并发送订单号，核对后即为你开通对应时长。</p>";
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
          return;
        }
        var qq = (o.contact && o.contact.qq) || (contact && contact.qq) || "";
        msgEl.innerHTML =
          '<div class="sub-order-ok">' +
          "<p>订单已创建：<strong>" + esc(o.order_no) + "</strong>" +
          "（" + esc(o.plan_label || "") + " ¥" + esc(o.amount_yuan) + "）</p>" +
          (qq
            ? "<p>请加 QQ <strong class=\"sub-contact-qq\">" + esc(qq) +
              "</strong>，把上面的订单号发给我，核对后为你开通。</p>" +
              '<button class="nav-btn" id="subCopyBtn" type="button">复制订单号</button>'
            : "<p>请联系主理人开通。</p>") +
          "</div>";
        var copyBtn = document.getElementById("subCopyBtn");
        if (copyBtn) copyBtn.addEventListener("click", function () {
          copyText(o.order_no, copyBtn);
        });
      })
      .catch(function (e) { msgEl.textContent = "下单失败：" + e.message; });
  }

  function load() {
    getJson("/api/subscription/status")
      .then(function (st) {
        contact = st.contact || null;
        renderStatus(st);
        renderPlans(st.plans);
        renderContact(contact);
      })
      .catch(function () {
        statusEl.textContent = "加载失败，请刷新重试";
      });
  }

  load();
})();
