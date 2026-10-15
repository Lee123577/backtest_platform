"""
邮件通知测试
============
锁住四组行为，全程内存假 DB + 假发信，不碰 MySQL、不碰 SMTP：

  1. 偏好读写 —— 懒建行、只认白名单里的开关键
  2. 退订 —— 分组语义(退"每日推送"要把复盘和热板一起关)、无效令牌不报错
  3. 正文渲染 —— HTML 转义、退订链接与免责声明必须在、摘要不等于全文
  4. 发送 —— 一天一封的幂等、失败要释放占位(否则重跑时这个人被永久跳过)

第 4 组是这次改动里最容易出错的地方：占位与发信的顺序一旦写反，
用户会在任务重跑时收到重复的信。
"""
from datetime import date

import pytest

from app.notify import db as notify_db, render, service


UID = 7
TOKEN = "tok-" + "x" * 39


class FakeDB:
    """按 app/notify/db.py 的契约做的内存实现。"""

    KINDS = notify_db.KINDS

    def __init__(self):
        self.prefs = {}      # user_id -> row
        self.sent = set()    # (user_id, kind, ref_key)
        self.purged = 0

    def ensure_tables(self):
        pass

    def new_token(self):
        return TOKEN

    def get_pref(self, user_id):
        row = self.prefs.get(user_id)
        return dict(row) if row else None

    def get_pref_by_token(self, token):
        for row in self.prefs.values():
            if row["unsub_token"] == token:
                return dict(row)
        return None

    def create_pref(self, user_id, token):
        self.prefs.setdefault(user_id, {
            "user_id": user_id, "unsub_token": token,
            # 与 DDL 默认值一致：事务性的默认开，内容推送默认关，
            # 新手引导(有终点的 4 封)默认开
            "watchlist_alert": 1, "daily_review": 0, "ai_hotsector": 0,
            "onboarding": 1,
            "email": f"u{user_id}@example.com", "display_name": None,
        })

    # 新手引导名单:[(user_id, created_at)];测试里直接往这里塞
    users = ()

    def new_users(self, since):
        return [{"user_id": uid, "email": f"u{uid}@example.com",
                 "display_name": None, "created_at": created}
                for uid, created in self.users if created >= since]

    def set_flags(self, user_id, flags):
        row = self.prefs.get(user_id)
        if row is None:
            return
        for k, v in flags.items():
            if k in notify_db.KINDS:
                row[k] = int(bool(v))

    def recipients(self, kind):
        return [dict(r) for r in self.prefs.values() if r.get(kind)]

    def pref_for_users(self, user_ids):
        return {u: dict(self.prefs[u]) for u in user_ids if u in self.prefs}

    def claim_send(self, user_id, kind, ref_key, subject):
        key = (user_id, kind, ref_key)
        if key in self.sent:
            return False
        self.sent.add(key)
        return True

    def release_send(self, user_id, kind, ref_key):
        self.sent.discard((user_id, kind, ref_key))

    def purge_send_log(self, before):
        self.purged += 1
        return 0


class FakeMailer:
    def __init__(self, fail=False):
        self.fail = fail
        self.outbox = []

    def send_mail(self, to_addrs, subject, text, html=None, headers=None):
        if self.fail:
            raise RuntimeError("SMTP 拒绝")
        self.outbox.append({"to": to_addrs[0], "subject": subject, "text": text,
                            "html": html, "headers": headers or {}})


@pytest.fixture
def fake(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(service, "db", db)
    monkeypatch.setattr(service, "SEND_INTERVAL_SEC", 0)   # 测试不真等
    return db


@pytest.fixture
def mail(monkeypatch):
    m = FakeMailer()
    monkeypatch.setattr(service, "mailer", m)
    return m


# ── 1. 偏好 ───────────────────────────────────────────────────────────────────

def test_prefs_lazy_created_with_defaults(fake):
    out = service.get_prefs(UID)
    # 盯盘提醒是用户自己配策略换来的结果，默认开；两个内容推送必须自己勾；
    # 新手引导只有 4 封、发完即止，默认开
    assert out["prefs"] == {"watchlist_alert": True,
                            "daily_review": False, "ai_hotsector": False,
                            "onboarding": True}


def test_update_prefs_only_known_keys(fake):
    service.get_prefs(UID)
    out = service.update_prefs(UID, {"daily_review": True, "drop_table": 1})
    assert out["prefs"]["daily_review"] is True
    assert "drop_table" not in fake.prefs[UID]


def test_update_prefs_partial_keeps_others(fake):
    service.get_prefs(UID)
    service.update_prefs(UID, {"daily_review": True})
    service.update_prefs(UID, {"ai_hotsector": True})
    p = service.get_prefs(UID)["prefs"]
    assert p["daily_review"] is True and p["ai_hotsector"] is True


# ── 2. 退订 ───────────────────────────────────────────────────────────────────

def test_unsubscribe_daily_group_turns_off_both(fake):
    service.update_prefs(UID, {"daily_review": True, "ai_hotsector": True})
    out = service.unsubscribe(TOKEN, "daily_review")
    # 那封信里两块内容是拼在一起的，只关一个等于"退了还在收"
    assert out["prefs"]["daily_review"] is False
    assert out["prefs"]["ai_hotsector"] is False
    # 盯盘提醒是另一回事，不能被连坐
    assert out["prefs"]["watchlist_alert"] is True


def test_unsubscribe_watchlist_only(fake):
    service.update_prefs(UID, {"daily_review": True})
    out = service.unsubscribe(TOKEN, "watchlist_alert")
    assert out["prefs"]["watchlist_alert"] is False
    assert out["prefs"]["daily_review"] is True


def test_unsubscribe_all_when_kind_missing(fake):
    service.update_prefs(UID, {"daily_review": True, "ai_hotsector": True})
    out = service.unsubscribe(TOKEN, None)
    assert not any(out["prefs"].values())


def test_unsubscribe_bad_token_returns_none(fake):
    # 无效令牌只回 None，由页面提示"链接失效"——不能抛异常变成 500
    assert service.unsubscribe("nope") is None
    assert service.describe_token("nope") is None


# ── 3. 渲染 ───────────────────────────────────────────────────────────────────

def _person(**kw):
    base = {"email": "abcdef@example.com", "display_name": None,
            "unsub_token": TOKEN}
    base.update(kw)
    return base


def test_greeting_masks_email_local_part():
    # 邮件可能被转发，没必要把完整地址再写一遍
    assert render.greeting(_person()) == "abc***"
    assert render.greeting(_person(display_name="老李")) == "老李"


def test_watchlist_mail_escapes_html():
    alerts = [{"code": "000001", "name": "<script>x</script>",
               "strategy_name": "均线交叉", "signal": "buy"}]
    _, text, html = render.watchlist_alert_mail(
        _person(), alerts, "2026-09-08", TOKEN)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "买入" in text


def test_watchlist_mail_has_unsub_and_disclaimer():
    alerts = [{"code": "000001", "name": "平安银行",
               "strategy_name": "均线交叉", "signal": "sell"}]
    _, text, html = render.watchlist_alert_mail(
        _person(), alerts, "2026-09-08", TOKEN)
    for body in (text, html):
        assert TOKEN in body                      # 退订链接
        assert "不构成投资建议" in body            # 合规声明必须自带
    assert "/unsubscribe?token=" in html


def test_digest_returns_none_when_nothing_to_send():
    # 两块内容都没有就不该发空信
    assert render.daily_digest_mail(_person(), "2026-09-08", TOKEN) is None


def test_digest_only_carries_summary_not_full_text():
    review = {"title": "标题", "summary": "只有摘要"}
    subject, text, html = render.daily_digest_mail(
        _person(), "2026-09-08", TOKEN, review=review)
    assert "只有摘要" in text and "标题" in subject
    # 正文里必须有回站里看全文的链接
    assert "/daily_review/2026-09-08" in html


def test_digest_html_carries_ai_disclaimer():
    """每日推送承载 AI 生成内容(复盘/板块观察) → HTML 外壳页脚必须带 AI 标识全文。"""
    review = {"title": "标题", "summary": "摘要"}
    _, _, html = render.daily_digest_mail(_person(), "2026-09-08", TOKEN, review=review)
    assert render.DISCLAIMER in html
    assert "不构成投资建议" in html and "AI" in html


def test_watchlist_alert_html_keeps_transactional_footer():
    """自选提醒是规则触发的事务类邮件 → 外壳页脚保持通用旧文案,不带 AI 标识。"""
    alerts = [{"code": "000001", "name": "平安银行",
               "strategy_name": "均线交叉", "signal": "buy"}]
    _, _, html = render.watchlist_alert_mail(_person(), alerts, "2026-09-08", TOKEN)
    assert "本站内容仅供研究，不构成投资建议。" in html
    assert render.DISCLAIMER not in html


# ── 4. 发送 ───────────────────────────────────────────────────────────────────

@pytest.fixture
def wl(monkeypatch):
    """假的 watchlist 数据源(service 是在函数里 import 它的)。"""
    import app.watchlist.db as wl_db

    rows = [
        {"user_id": UID, "code": "000001", "name": "平安银行",
         "strategy_id": "ma_cross", "strategy_name": "均线交叉",
         "signal": "buy", "trade_date": date(2026, 9, 8)},
    ]
    monkeypatch.setattr(wl_db, "ensure_tables", lambda: None)
    monkeypatch.setattr(wl_db, "latest_market_date", lambda: date(2026, 9, 8))
    monkeypatch.setattr(wl_db, "alerts_by_date", lambda d: list(rows))
    return rows


def test_watchlist_send_once_then_skips(fake, mail, wl):
    first = service.send_watchlist_alerts()
    assert first["sent"] == 1 and first["failed"] == 0
    assert mail.outbox[0]["to"] == f"u{UID}@example.com"

    # 同一天重跑：账本已有记录，这个人被跳过，不会收到第二封
    second = service.send_watchlist_alerts()
    assert second["sent"] == 0 and second["skipped"] == 1
    assert len(mail.outbox) == 1


def test_watchlist_send_respects_switch(fake, mail, wl):
    service.get_prefs(UID)
    service.update_prefs(UID, {"watchlist_alert": False})
    out = service.send_watchlist_alerts()
    assert out["sent"] == 0 and out["skipped"] == 1
    assert mail.outbox == []


def test_failed_send_releases_slot_for_retry(fake, monkeypatch, wl):
    boom = FakeMailer(fail=True)
    monkeypatch.setattr(service, "mailer", boom)
    out = service.send_watchlist_alerts()
    assert out["failed"] == 1
    # 占位必须被释放，否则调度器重跑时这个人会被当成"已发过"永久跳过
    assert (UID, "watchlist_alert", "2026-09-08") not in fake.sent

    ok = FakeMailer()
    monkeypatch.setattr(service, "mailer", ok)
    assert service.send_watchlist_alerts()["sent"] == 1


def test_watchlist_send_carries_one_click_unsubscribe(fake, mail, wl):
    service.send_watchlist_alerts()
    h = mail.outbox[0]["headers"]
    assert h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    # 一键退订退的必须是这封信那一类，不能顺手把别的通知也关了
    assert "kind=watchlist_alert" in h["List-Unsubscribe"]


def test_digest_merges_two_blocks_into_one_mail(fake, mail, monkeypatch):
    import app.watchlist.db as wl_db
    monkeypatch.setattr(wl_db, "latest_market_date", lambda: date(2026, 9, 8))
    monkeypatch.setattr(service, "_review_block",
                        lambda d: {"title": "今日复盘", "summary": "摘要"})
    monkeypatch.setattr(service, "_hotsector_block",
                        lambda d: {"sectors": ["半导体"],
                                   "stocks": [{"code": "000001", "name": "平安银行",
                                               "sector_name": "半导体"}]})
    service.get_prefs(UID)
    service.update_prefs(UID, {"daily_review": True, "ai_hotsector": True})

    out = service.send_daily_digest()
    assert out["sent"] == 1
    assert len(mail.outbox) == 1          # 两块内容合成一封，不是两封
    body = mail.outbox[0]["text"]
    assert "今日复盘" in body and "半导体" in body


def test_digest_skips_when_no_content(fake, mail, monkeypatch):
    import app.watchlist.db as wl_db
    monkeypatch.setattr(wl_db, "latest_market_date", lambda: date(2026, 9, 8))
    monkeypatch.setattr(service, "_review_block", lambda d: None)
    monkeypatch.setattr(service, "_hotsector_block", lambda d: None)
    service.get_prefs(UID)
    service.update_prefs(UID, {"daily_review": True})
    out = service.send_daily_digest()
    assert out["sent"] == 0 and mail.outbox == []


# ── 5. 退订页 ─────────────────────────────────────────────────────────────────
# 直接调处理函数,不走 TestClient:那会经过访问日志中间件,往真库里写一行探针数据。

def _html(resp):
    return resp.body.decode("utf-8")


def test_unsub_page_shows_confirm_button(fake):
    from app.notify import api as notify_api

    service.get_prefs(UID)
    resp = notify_api.unsubscribe_page(token=TOKEN, kind="daily_review")
    body = _html(resp)
    assert resp.status_code == 200
    assert "确认退订" in body
    # 真正的退订动作在 POST 上(见下一个用例)
    assert '<form method="post"' in body


def test_unsub_get_does_not_unsubscribe(fake):
    from app.notify import api as notify_api

    service.update_prefs(UID, {"daily_review": True})
    notify_api.unsubscribe_page(token=TOKEN, kind="daily_review")
    assert service.get_prefs(UID)["prefs"]["daily_review"] is True


def test_unsub_post_actually_unsubscribes(fake):
    from app.notify import api as notify_api

    service.update_prefs(UID, {"daily_review": True, "ai_hotsector": True})
    resp = notify_api.unsubscribe_submit(token=TOKEN, kind="daily_review")
    assert "已退订" in _html(resp)
    p = service.get_prefs(UID)["prefs"]
    assert p["daily_review"] is False and p["ai_hotsector"] is False


def test_unsub_bad_token_page_is_404(fake):
    from app.notify import api as notify_api

    resp = notify_api.unsubscribe_page(token="nope")
    assert resp.status_code == 404 and "已失效" in _html(resp)


def test_unsub_page_escapes_query_params(fake):
    """token/kind 是 URL 里带进来的,直接回显进 value= 就是反射型 XSS。"""
    from app.notify import api as notify_api

    service.get_prefs(UID)
    resp = notify_api.unsubscribe_page(token=TOKEN, kind='"><script>x</script>')
    body = _html(resp)
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


# ── 6. 新手引导(注册后第 0/1/3/7 天各一封) ───────────────────────────────────
# 最容易出错的三件事:同一封发两遍、任务停几天后一口气补发好几封、
# 老用户被拉进来收"欢迎"信。下面每条都锁一件。

from datetime import datetime, timedelta   # noqa: E402

TODAY = date(2026, 9, 29)


def _registered(days_ago, uid=UID):
    return (uid, datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time())
            + timedelta(hours=9))


@pytest.fixture
def no_board(monkeypatch):
    """第 1 天那封的实测前三要查库,测试里不查。"""
    monkeypatch.setattr(service, "_onboarding_board", lambda: None)


@pytest.mark.parametrize("age,step", [
    (0, "d0"), (1, "d1"), (2, "d1"), (3, "d3"), (4, "d3"),
    (5, None), (6, None), (7, "d7"), (8, "d7"), (9, None), (30, None),
])
def test_onboarding_step_schedule(age, step):
    assert service.onboarding_step_due(age) == step


def test_onboarding_sends_due_step_once(fake, mail, no_board):
    fake.users = (_registered(0),)
    first = service.send_onboarding(TODAY)
    again = service.send_onboarding(TODAY)
    assert first["sent"] == 1 and first["by_step"] == {"d0": 1}
    assert again["sent"] == 0                       # 同一封不发第二遍
    assert len(mail.outbox) == 1
    assert "欢迎" in mail.outbox[0]["subject"]


def test_onboarding_creates_pref_row_for_new_user(fake, mail, no_board):
    """新账号多半还没有偏好行 —— 引导默认开,得替他补上再发。"""
    fake.users = (_registered(1),)
    assert UID not in fake.prefs
    service.send_onboarding(TODAY)
    assert UID in fake.prefs and len(mail.outbox) == 1


def test_onboarding_respects_unsubscribe(fake, mail, no_board):
    fake.users = (_registered(3),)
    service.get_prefs(UID)
    service.unsubscribe(TOKEN, "onboarding")
    out = service.send_onboarding(TODAY)
    assert out["sent"] == 0 and mail.outbox == []


def test_onboarding_unsubscribe_does_not_touch_other_switches(fake):
    service.update_prefs(UID, {"daily_review": True})
    service.unsubscribe(TOKEN, "onboarding")
    prefs = service.get_prefs(UID)["prefs"]
    assert prefs["onboarding"] is False
    assert prefs["daily_review"] is True and prefs["watchlist_alert"] is True


def test_onboarding_skips_old_users(fake, mail, no_board):
    """老用户不在窗口里,一封都不会收到。"""
    fake.users = (_registered(40),)
    out = service.send_onboarding(TODAY)
    assert out["users"] == 0 and mail.outbox == []


def test_onboarding_one_mail_per_day_even_after_outage(fake, mail, no_board):
    """任务停了一天:第 1 天时 d0、d1 都还在宽限期里,只发最新那封。"""
    fake.users = (_registered(1),)
    service.send_onboarding(TODAY)
    assert len(mail.outbox) == 1
    assert "策略" in mail.outbox[0]["subject"]      # d1,不是 d0 的欢迎信


def test_onboarding_failed_send_is_retried(fake, monkeypatch, no_board):
    fake.users = (_registered(0),)
    monkeypatch.setattr(service, "mailer", FakeMailer(fail=True))
    out = service.send_onboarding(TODAY)
    assert out["failed"] == 1
    ok = FakeMailer()
    monkeypatch.setattr(service, "mailer", ok)
    assert service.send_onboarding(TODAY)["sent"] == 1


def test_onboarding_mail_has_one_click_unsubscribe(fake, mail, no_board):
    fake.users = (_registered(7),)
    service.send_onboarding(TODAY)
    m = mail.outbox[0]
    assert "kind=onboarding" in m["headers"]["List-Unsubscribe"]
    assert "kind=onboarding" in m["text"] and "kind=onboarding" in m["html"]


@pytest.mark.parametrize("step", render.ONBOARDING_STEPS)
def test_onboarding_mail_basics(step):
    user = {"email": "abcdef@example.com", "display_name": "<b>老王</b>"}
    subject, text, html = render.onboarding_mail(user, step, TOKEN)
    assert render.SITE_NAME in subject
    # 新手引导是**非 AI 内容**邮件 → 外壳页脚保持通用旧文案不变(事务类文案零变化)
    assert "本站内容仅供研究，不构成投资建议。" in html
    assert render.DISCLAIMER not in html            # 不带 AI 标识
    assert "<b>老王</b>" not in html                # 昵称转义
    assert "&lt;b&gt;老王&lt;/b&gt;" in html
    assert render.site_url("/") in text            # 纯文本也带可点的绝对地址


def test_onboarding_day1_includes_board_when_available():
    board = [{"id": "donchian", "name": "唐奇安通道", "median_return": 5.6}]
    _, text, html = render.onboarding_mail({"email": "a@b.c"}, "d1", TOKEN, board=board)
    assert "唐奇安通道" in text and "+5.6%" in html
    assert render.site_url("/strategy/donchian") in html


def test_onboarding_last_mail_invites_feedback():
    _, text, _ = render.onboarding_mail({"email": "a@b.c"}, "d7", TOKEN)
    assert "/?feedback=praise" in text
    assert "最后一封" in text


def test_onboarding_scheduled_task_registered():
    from app.scheduler import registry
    t = registry.get_task("onboarding_mail")
    assert t and t["cmd"][-1] == "scripts/notify_onboarding.py"
    assert t["schedule"].startswith("daily:")      # 周末也有人注册

