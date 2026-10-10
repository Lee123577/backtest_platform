/**
 * 讨论区 /discuss
 * ================
 * 列表 / 发帖 / 回复 / 点赞 / 删除 / 管理员隐藏,外加首屏弹幕。
 *
 * 弹幕规则:每次打开页面,把素材从头到尾**播一遍就停,不循环**。播完舞台收起,
 * 首屏回到普通高度。自己新发的短帖(≤40 字)会单独再飞一次。可以「关闭弹幕」
 * (记在本地,下次打开也不播);系统开了"减少动态效果"的不播。
 *
 * 所有用户内容一律走 esc() 再进 innerHTML。
 */
(function () {
  "use strict";

  var DM_MAX_LEN = 40;
  var DM_OFF_KEY = "dc_dm_off";

  // ── 小工具 ──────────────────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }

  function api(method, url, body) {
    var opt = { method: method, headers: {} };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    return fetch(url, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) {
          var err = new Error(j.detail || ("请求失败(" + r.status + ")"));
          err.status = r.status;
          throw err;
        }
        return j;
      });
    });
  }

  function timeAgo(iso) {
    if (!iso) return "";
    var t = new Date(iso.replace(" ", "T")).getTime();
    var s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 60) return "刚刚";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    if (s < 86400 * 7) return Math.floor(s / 86400) + " 天前";
    return iso.slice(0, 10);
  }

  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 隐私模式 */ } }

  // ── 弹幕 ────────────────────────────────────────────────────────────────
  var Danmaku = (function () {
    var stage = $("dcStage");
    var toggle = $("dcDmToggle");
    var LANE_H = 34, TOP = 8, SPEED = 110;      // px/s
    var lanes = [], queue = [], active = 0, timer = null;
    var running = false, off = lsGet(DM_OFF_KEY) === "1";
    var reduced = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
    var canAnimate = !!(stage && stage.animate) && !reduced;

    function laneCount() {
      return Math.max(1, Math.floor((stage.clientHeight - TOP) / LANE_H));
    }

    function freeLane(now) {
      var n = laneCount();
      while (lanes.length < n) lanes.push(0);
      for (var i = 0; i < n; i++) if (lanes[i] <= now) return i;
      return -1;
    }

    function launch(item, lane) {
      var el = document.createElement("span");
      el.className = "dc-dm" + (item.official ? " dc-dm--official" : "") +
        (item.mine ? " dc-dm--mine" : "");
      el.textContent = item.text;
      el.style.top = (TOP + lane * LANE_H) + "px";
      el.style.transform = "translateX(-9999px)";       // 量宽度前先放到画外
      stage.appendChild(el);
      var w = el.offsetWidth, W = stage.clientWidth;
      var dur = (W + w) / SPEED * 1000;
      // 这条弹道要等这条弹幕的尾巴进场、再空出 48px 才能放下一条,避免叠在一起
      lanes[lane] = performance.now() + (w + 48) / SPEED * 1000;
      active++;
      var anim = el.animate(
        [{ transform: "translateX(" + W + "px)" }, { transform: "translateX(" + (-w - 20) + "px)" }],
        { duration: dur, easing: "linear", fill: "forwards" }
      );
      anim.onfinish = function () {
        el.remove();
        active--;
        maybeFinish();
      };
    }

    function tick() {
      timer = null;
      if (off) return;
      var now = performance.now();
      // 每次最多放一条,错开出场;有空弹道就放,没有就稍后再看
      if (queue.length) {
        var lane = freeLane(now);
        if (lane >= 0) launch(queue.shift(), lane);
      }
      if (queue.length) timer = setTimeout(tick, 450 + Math.random() * 450);
      else maybeFinish();
    }

    function maybeFinish() {
      if (queue.length || active > 0) return;
      running = false;
      stage.classList.add("is-done");             // 播完一遍就收起,不循环
      if (!off) toggle.hidden = true;
    }

    function start(items) {
      if (!canAnimate || !items.length) { stage && stage.classList.add("is-done"); return; }
      toggle.hidden = false;
      if (off) {
        stage.classList.add("is-off");
        toggle.textContent = "开启弹幕";
        queue = items.slice();                    // 留着,用户点「开启」时播这一遍
        return;
      }
      queue = items.slice();
      running = true;
      stage.classList.remove("is-done", "is-off");
      tick();
    }

    // 自己刚发的短帖:单独飞一次(舞台已收起就临时展开,飞完再收)
    function fly(text) {
      if (!canAnimate || off || !text || text.length > DM_MAX_LEN) return;
      queue.push({ text: text, mine: true });
      stage.classList.remove("is-done");
      if (!running) { running = true; setTimeout(tick, 650); }   // 等舞台展开
    }

    function setOff(v) {
      off = v;
      lsSet(DM_OFF_KEY, v ? "1" : "0");
      if (v) {
        queue = [];
        if (timer) clearTimeout(timer);
        timer = null;
        stage.querySelectorAll(".dc-dm").forEach(function (el) { el.remove(); });
        active = 0;
        running = false;
        stage.classList.add("is-off");
        toggle.textContent = "开启弹幕";
      } else {
        stage.classList.remove("is-off");
        toggle.textContent = "关闭弹幕";
        if (queue.length) { running = true; setTimeout(tick, 650); }
        else maybeFinish();
      }
    }

    if (toggle) toggle.addEventListener("click", function () { setOff(!off); });

    return { start: start, fly: fly };
  })();

  // ── 列表 ────────────────────────────────────────────────────────────────
  var state = { sort: "new", page: 1, isAdmin: false, loggedIn: false, loading: false };
  var list = $("dcList");
  var moreBtn = $("dcMore");

  function avatar(a, small) {
    var cls = "dc-avatar" + (small ? " dc-avatar--sm" : "") + (a.official ? " dc-avatar--official" : "");
    if (a.avatar_url) {
      return '<img class="' + cls + '" src="' + esc(a.avatar_url) + '" alt="" loading="lazy">';
    }
    var chars = Array.from ? Array.from(a.name || "?") : String(a.name || "?").split("");
    var c = a.official ? "收" : chars[0];
    return '<span class="' + cls + '" aria-hidden="true">' + esc(/[a-z]/.test(c) ? c.toUpperCase() : c) + "</span>";
  }

  function postHtml(p, isReply) {
    var a = p.author || {};
    var acts = [
      '<button type="button" class="dc-act' + (p.liked ? " is-on" : "") + '" data-act="like" data-id="' + p.id + '">' +
        (p.liked ? "♥" : "♡") + " " + (p.like_count || "赞") + "</button>",
    ];
    if (!isReply) {
      acts.push('<button type="button" class="dc-act" data-act="reply" data-id="' + p.id + '">💬 ' +
        (p.reply_count ? p.reply_count + " 条回复" : "回复") + "</button>");
    }
    if (p.mine || state.isAdmin) {
      acts.push('<button type="button" class="dc-act dc-act--danger" data-act="delete" data-id="' + p.id + '">删除</button>');
    }
    if (state.isAdmin) {
      acts.push('<button type="button" class="dc-act" data-act="hide" data-id="' + p.id + '" data-hidden="' +
        (p.hidden ? "1" : "0") + '">' + (p.hidden ? "恢复显示" : "隐藏") + "</button>");
    }
    var replies = "";
    if (!isReply) {
      replies =
        '<div class="dc-replies" id="dcReplies' + p.id + '">' +
        (p.replies || []).map(function (r) { return postHtml(r, true); }).join("") + "</div>" +
        '<form class="dc-reply-form" data-parent="' + p.id + '" hidden>' +
        '<input class="dc-input" maxlength="500" placeholder="回复 ' + esc(a.name) + '…">' +
        '<button type="submit" class="dc-btn dc-btn--primary">回复</button></form>';
    }
    return (
      '<article class="dc-post' + (p.hidden ? " dc-post--hidden" : "") + '" data-post="' + p.id + '">' +
      avatar(a, isReply) +
      '<div class="dc-body">' +
      '<div class="dc-head"><span class="dc-name">' + esc(a.name) + "</span>" +
      (a.official ? '<span class="dc-badge dc-badge--official">官方</span>' : "") +
      (p.hidden ? '<span class="dc-badge dc-badge--hidden">已隐藏</span>' : "") +
      '<span class="dc-time" title="' + esc(p.created_at || "") + '">' + esc(timeAgo(p.created_at)) + "</span></div>" +
      '<div class="dc-content">' + esc(p.content) + "</div>" +
      '<div class="dc-actions">' + acts.join("") + "</div>" +
      replies +
      "</div></article>"
    );
  }

  function load(reset) {
    if (state.loading) return;
    state.loading = true;
    if (reset) { state.page = 1; list.innerHTML = '<div class="dc-empty">加载中…</div>'; }
    api("GET", "/api/discuss/posts?sort=" + state.sort + "&page=" + state.page)
      .then(function (d) {
        state.isAdmin = !!d.is_admin;
        state.loggedIn = !!d.logged_in;
        var html = (d.posts || []).map(function (p) { return postHtml(p, false); }).join("");
        if (reset) list.innerHTML = html || '<div class="dc-empty">还没有人发帖，来写第一条吧。</div>';
        else list.insertAdjacentHTML("beforeend", html);
        moreBtn.hidden = !d.has_more;
      })
      .catch(function (e) {
        if (reset) list.innerHTML = '<div class="dc-empty">' + esc(e.message) + "</div>";
      })
      .then(function () { state.loading = false; });
  }

  moreBtn.addEventListener("click", function () { state.page += 1; load(false); });

  document.querySelectorAll(".dc-tab").forEach(function (t) {
    t.addEventListener("click", function () {
      if (t.classList.contains("active")) return;
      document.querySelectorAll(".dc-tab").forEach(function (x) { x.classList.remove("active"); });
      t.classList.add("active");
      state.sort = t.getAttribute("data-sort");
      load(true);
    });
  });

  // 需要登录的动作:没登录先弹登录框,登录成功再接着做
  function withLogin(fn) {
    if (state.loggedIn) return fn();
    if (window.SPAuth) window.SPAuth.requireLogin().then(function () { state.loggedIn = true; fn(); });
  }

  list.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-act]");
    if (!btn) return;
    var id = btn.getAttribute("data-id");
    var act = btn.getAttribute("data-act");
    if (act === "reply") {
      withLogin(function () {
        var form = list.querySelector('.dc-reply-form[data-parent="' + id + '"]');
        if (!form) return;
        form.hidden = !form.hidden;
        if (!form.hidden) form.querySelector("input").focus();
      });
    } else if (act === "like") {
      withLogin(function () {
        btn.disabled = true;
        api("POST", "/api/discuss/posts/" + id + "/like")
          .then(function (r) {
            btn.classList.toggle("is-on", r.liked);
            btn.textContent = (r.liked ? "♥" : "♡") + " " + (r.like_count || "赞");
          })
          .catch(function (err) { alert(err.message); })
          .then(function () { btn.disabled = false; });
      });
    } else if (act === "delete") {
      if (!confirm("确定删除这条？")) return;
      api("DELETE", "/api/discuss/posts/" + id)
        .then(function () {
          var el = list.querySelector('[data-post="' + id + '"]');
          if (el) el.remove();
        })
        .catch(function (err) { alert(err.message); });
    } else if (act === "hide") {
      var hidden = btn.getAttribute("data-hidden") !== "1";
      api("POST", "/api/discuss/posts/" + id + "/hide", { hidden: hidden })
        .then(function () { load(true); })
        .catch(function (err) { alert(err.message); });
    }
  });

  list.addEventListener("submit", function (e) {
    var form = e.target.closest(".dc-reply-form");
    if (!form) return;
    e.preventDefault();
    var input = form.querySelector("input");
    var text = input.value.trim();
    if (!text) return;
    var btn = form.querySelector("button");
    btn.disabled = true;
    api("POST", "/api/discuss/posts", { content: text, parent_id: Number(form.getAttribute("data-parent")) })
      .then(function (r) {
        $("dcReplies" + form.getAttribute("data-parent")).insertAdjacentHTML("beforeend", postHtml(r, true));
        input.value = "";
        form.hidden = true;
        Danmaku.fly(r.content);
      })
      .catch(function (err) { alert(err.message); })
      .then(function () { btn.disabled = false; });
  });

  // ── 发帖框 ──────────────────────────────────────────────────────────────
  var form = $("dcForm"), input = $("dcInput"), count = $("dcCount"), msg = $("dcMsg");

  function setMsg(t, ok) { msg.textContent = t || ""; msg.className = "dc-msg" + (ok ? " ok" : ""); }

  input.addEventListener("input", function () {
    count.textContent = input.value.length + " / 500";
    setMsg("");
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = input.value.trim();
    if (text.length < 2) { setMsg("至少写 2 个字"); return; }
    var btn = $("dcSubmit");
    btn.disabled = true;
    api("POST", "/api/discuss/posts", { content: text })
      .then(function (p) {
        input.value = "";
        count.textContent = "0 / 500";
        setMsg("发布成功", true);
        if (state.sort === "new") {
          var empty = list.querySelector(".dc-empty");
          if (empty) empty.remove();
          list.insertAdjacentHTML("afterbegin", postHtml(p, false));
        }
        Danmaku.fly(p.content);
      })
      .catch(function (err) { setMsg(err.message); })
      .then(function () { btn.disabled = false; });
  });

  $("dcLoginBtn").addEventListener("click", function () {
    if (window.SPAuth) window.SPAuth.requireLogin();
  });

  function renderComposer(user) {
    state.loggedIn = !!user;
    $("dcLoginGate").hidden = !!user;
    form.hidden = !user;
    $("dcMe").innerHTML = user && window.SPAuth ? window.SPAuth.avatarHtml(user, 36) : "";
  }

  // ── 启动 ────────────────────────────────────────────────────────────────
  var firstAuth = true;
  if (window.SPAuth) {
    window.SPAuth.onChange(function (user) {
      renderComposer(user);
      // 登录态变了(登录/登出),"我赞过""我的"要跟着变 —— 重拉一次;首次不重复拉
      if (!firstAuth) load(true);
      firstAuth = false;
    });
  }
  load(true);

  api("GET", "/api/discuss/danmaku")
    .then(function (d) { Danmaku.start(d.items || []); })
    .catch(function () { Danmaku.start([]); });
})();
