/**
 * 订阅页
 * ======
 * 展示会员状态 + 套餐；点击套餐下单。当前无在线支付(pay_ready=false)：下单
 * 生成订单号，引导用户拿订单号加 QQ 人工开通。接入支付宝后 pay_ready 置 true，
 * 走 else 分支拉起二维码。
 *
 * 另有一块限量福利：前 N 名免费领终生会员(lifetime_offer)。领取要登录 ——
 * 名额得挂在一个能找回的身份上，所以未登录时按钮先唤起邮箱登录弹窗，
 * 登录成功再自动接着领，不让用户点两次。
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
  var ltPanel = document.getElementById("ltPanel");
  var ltBody = document.getElementById("ltBody");
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
    if (st.subscribed && st.lifetime) {
      // 终生会员的 expires_at 是 2099 年,直接摆出来像 bug,只说"无到期时间"
      statusEl.innerHTML =
        '<span class="sub-active">终生会员</span>，全部会员功能已解锁，无到期时间。';
    } else if (st.subscribed) {
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

  // ── 终生会员免费名额 ──────────────────────────────────────────────────────
  function renderLifetime(st) {
    var offer = st.lifetime_offer;
    // 后端在"表还没建/库连不上/活动关闭"时回 null —— 整块藏掉，不显示半截福利
    if (!offer || !offer.total) { ltPanel.hidden = true; return; }
    ltPanel.hidden = false;

    var total = offer.total;
    var claimed = offer.claimed;
    var left = offer.remaining;
    var pct = total ? Math.round((claimed / total) * 100) : 0;
    var meter =
      '<div class="lt-bar"><div class="lt-bar-fill" style="width:' + pct + '%"></div></div>' +
      '<div class="lt-count">已领取 ' + esc(claimed) + " / " + esc(total) +
      '，剩余 <span class="lt-left">' + esc(left) + "</span> 个</div>";

    // 已经领到手：只报座位号，不再显示按钮
    if (st.lifetime || offer.mine) {
      ltBody.innerHTML =
        '<div class="lt-title">🎁 你已领取终生会员</div>' + meter +
        '<div class="lt-ok">第 <span class="lt-seat">' + esc(offer.mine || "-") +
        "</span> 号名额已归你，会员权益永久有效。</div>";
      return;
    }

    if (left <= 0) {
      ltBody.innerHTML =
        '<div class="lt-title">🎁 前 ' + esc(total) + " 名免费领终生会员</div>" + meter +
        '<div class="lt-soldout">名额已经领完了。后续可以选下面的套餐开通，' +
        "或者关注站内公告等下一轮活动。</div>";
      return;
    }

    var loggedIn = !!st.logged_in;
    ltBody.innerHTML =
      '<div class="lt-title">🎁 前 ' + esc(total) + " 名免费领终生会员</div>" +
      '<p class="lt-desc">开站早期福利：<strong>前 ' + esc(total) +
      " 名用户可免费领取终生会员</strong>，解锁历史每日复盘全文、AI 热门板块战绩、" +
      "自选盯盘信号提醒等全部会员功能，永久有效、不需要付费。</p>" +
      meter +
      '<button class="lt-btn" id="ltClaimBtn" type="button">' +
      (loggedIn ? "立即免费领取" : "邮箱登录后领取") + "</button>" +
      '<p class="lt-note">' +
      (loggedIn
        ? "一个账号限领一个名额，领完即止。"
        : "领取需要邮箱登录（收一封验证码邮件即可，无需密码）—— 名额要记在账号上，" +
          "换设备才找得回。") +
      "</p>" +
      '<div class="lt-err" id="ltErr"></div>';

    var btn = document.getElementById("ltClaimBtn");
    if (btn) btn.addEventListener("click", onClaimClick);
  }

  function onClaimClick() {
    // 未登录：先弹登录框，登录成功后接着把这一次领取做完
    if (!window.SPAuth.me()) {
      window.SPAuth.requireLogin().then(function (u) { if (u) doClaim(); });
      return;
    }
    doClaim();
  }

  function doClaim() {
    var btn = document.getElementById("ltClaimBtn");
    var errEl = document.getElementById("ltErr");
    if (btn) { btn.disabled = true; btn.textContent = "领取中…"; }
    if (errEl) errEl.textContent = "";
    postJson("/api/subscription/claim_lifetime", {})
      .then(function () {
        load();   // 重新拉状态：会员态、名额进度、套餐区都跟着刷新
      })
      .catch(function (e) {
        // 名额被别人抢完(409)也走这里 —— 重拉一次，让页面切到"已领完"的样子
        if (errEl) errEl.textContent = "领取失败：" + e.message;
        if (btn) { btn.disabled = false; btn.textContent = "立即免费领取"; }
        load();
      });
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
        renderLifetime(st);
        // 终生会员就别再摆套餐了:他买了也只是把钱扔进来,时长对他毫无意义
        // (后端 fulfill_order 也不会因此把他降级成月卡,但先别让人误买)
        var planPanel = document.getElementById("planPanel");
        if (st.lifetime) {
          if (planPanel) planPanel.hidden = true;
          return;
        }
        if (planPanel) planPanel.hidden = false;
        renderPlans(st.plans);
        renderContact(contact);
      })
      .catch(function () {
        statusEl.textContent = "加载失败，请刷新重试";
      });
  }

  load();
})();
