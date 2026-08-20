"""看板布局的身份归属(scope)、访客键(sp_sid > IP)与 IP 归一规则。

这块的风险不在功能而在越权:判错一档,普通访客就能改掉全站默认布局
(所有新访客第一眼看到的东西)。所以每一档的读/写落点都要钉死。

不连库 —— 把 my_board.db 的读写换成内存字典。
"""
import pytest

from app.my_board import db, service


# ── IP 归一 ──────────────────────────────────────────────────────────────────

def test_ipv4_原样做键():
    assert service.ip_key("223.65.129.69") == "223.65.129.69"


def test_内网地址也能存():
    """局域网部署 / 本地开发的访客同样要能留住看板。"""
    assert service.ip_key("192.168.1.7") == "192.168.1.7"


def test_ipv6_截到64位前缀():
    """手机 IPv6 的后 64 位会轮换,不截前缀等于每次刷新都换个人。"""
    a = service.ip_key("2408:8207:1234:5678:aaaa:bbbb:cccc:dddd")
    b = service.ip_key("2408:8207:1234:5678:1111:2222:3333:4444")
    assert a == b == "2408:8207:1234:5678::/64"
    assert len(a) <= 45          # 不能超过 ip_key 列宽


def test_不同64段是不同的人():
    assert service.ip_key("2408:8207:1234:5678::1") != \
           service.ip_key("2408:8207:1234:9999::1")


@pytest.mark.parametrize("bad", ["", "   ", None, "unknown", "1.2.3", "not-an-ip"])
def test_取不到有效ip就没有身份(bad):
    assert service.ip_key(bad) is None


# ── scope 判定 ───────────────────────────────────────────────────────────────

def test_登录用户走自己那一行():
    scope, key = service.resolve_scope({"id": 7}, False, "1.2.3.4")
    assert scope == service.SCOPE_USER and key is None


def test_登录态优先于白名单():
    """维护者登录之后改的是自己的看板,不该再顺手覆盖全站默认。"""
    scope, _ = service.resolve_scope({"id": 7}, True, "1.2.3.4")
    assert scope == service.SCOPE_USER


def test_白名单ip写全站默认():
    scope, key = service.resolve_scope(None, True, "1.2.3.4")
    assert scope == service.SCOPE_SITE and key is None


def test_普通访客按ip分行():
    scope, key = service.resolve_scope(None, False, "1.2.3.4")
    assert scope == service.SCOPE_IP and key == "1.2.3.4"


def test_没有可用ip时不落到全站默认():
    """这一条是防投毒的关键:降级不能降到"写全站默认"上去。"""
    scope, key = service.resolve_scope(None, False, "unknown")
    assert scope == service.SCOPE_EPHEMERAL and key is None


# ── 读写落点 ─────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_store(monkeypatch):
    """把 db 的读写换成内存字典,顺便记录调用落在哪张表。"""
    users, ips = {}, {}

    def _save_ip(k, lay):
        created = k not in ips          # 真库靠 upsert 的 rowcount 得到同一件事
        ips[k] = lay
        return created

    monkeypatch.setattr(db, "ensure_tables", lambda: None)
    monkeypatch.setattr(db, "load_layout", lambda uid: users.get(uid, {}))
    monkeypatch.setattr(db, "save_layout", lambda uid, lay: users.__setitem__(uid, lay))
    monkeypatch.setattr(db, "load_ip_layout", lambda k: ips.get(k))
    monkeypatch.setattr(db, "save_ip_layout", _save_ip)
    monkeypatch.setattr(db, "delete_ip_layout", lambda k: ips.pop(k, None))
    service._new_row_limiter.reset()    # 限流器是模块级的,逐条用例隔离
    return users, ips


SITE_DEFAULT = {"cards": [{"id": "idx_overview", "kind": "indices"}]}
ONE_CARD = {"cards": [{"id": "dr_summary", "kind": "review"}], "positions": {}}


def test_访客首次进来看到全站默认(fake_store):
    users, _ = fake_store
    users[db.GUEST_USER_ID] = SITE_DEFAULT
    assert service.get_layout(None, False, "8.8.8.8") == SITE_DEFAULT


def test_访客保存后读回自己那一份(fake_store):
    users, ips = fake_store
    users[db.GUEST_USER_ID] = SITE_DEFAULT
    service.save_layout(None, ONE_CARD, False, "8.8.8.8")
    assert ips["8.8.8.8"] == ONE_CARD
    assert service.get_layout(None, False, "8.8.8.8") == ONE_CARD
    # 全站默认没被动过,别的访客还是看到原来的
    assert users[db.GUEST_USER_ID] == SITE_DEFAULT
    assert service.get_layout(None, False, "9.9.9.9") == SITE_DEFAULT


def test_两个访客互不覆盖(fake_store):
    _, ips = fake_store
    service.save_layout(None, ONE_CARD, False, "8.8.8.8")
    service.save_layout(None, {"cards": []}, False, "9.9.9.9")
    assert ips["8.8.8.8"] == ONE_CARD
    assert ips["9.9.9.9"] == {"cards": []}


def test_访客重置删掉自己那一行并回退默认(fake_store):
    users, ips = fake_store
    users[db.GUEST_USER_ID] = SITE_DEFAULT
    service.save_layout(None, ONE_CARD, False, "8.8.8.8")
    service.save_layout(None, {}, False, "8.8.8.8")      # 前端"重置为默认"发的就是空布局
    assert "8.8.8.8" not in ips
    assert service.get_layout(None, False, "8.8.8.8") == SITE_DEFAULT


def test_白名单ip改的是全站默认(fake_store):
    users, ips = fake_store
    service.save_layout(None, ONE_CARD, True, "1.2.3.4")
    assert users[db.GUEST_USER_ID] == ONE_CARD
    assert ips == {}


def test_没有身份的写请求被拒(fake_store):
    users, ips = fake_store
    users[db.GUEST_USER_ID] = SITE_DEFAULT
    with pytest.raises(service.LayoutForbidden):
        service.save_layout(None, ONE_CARD, False, "unknown")
    assert users[db.GUEST_USER_ID] == SITE_DEFAULT and ips == {}


def test_写请求仍然过一遍校验(fake_store):
    """按 IP 存不等于放松校验 —— 非法卡片类型照样拒。"""
    _, ips = fake_store
    with pytest.raises(service.LayoutError):
        service.save_layout(None, {"cards": [{"id": "x", "kind": "邪门卡片"}]},
                            False, "8.8.8.8")
    assert ips == {}


# ── 访客键:sp_sid 优先,IP 兜底 ──────────────────────────────────────────────

SID_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SID_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_合法sid带前缀做键():
    assert service.sid_key(SID_A) == "sid:" + SID_A
    assert len(service.sid_key(SID_A)) <= service.IP_KEY_MAX_LEN   # 不能超过列宽


@pytest.mark.parametrize("bad", [
    "", "   ", None, "xyz", "A" * 32,          # 大写不是 token_hex 的输出
    "a" * 31, "a" * 33, "a" * 31 + "?",        # 长度/字符不对
])
def test_不是自己签发的sid一律不认(bad):
    """这个值直接进 SQL 参数和维护者面板,放任意字符串进来等于让客户端自选主键。"""
    assert service.sid_key(bad) is None


def test_有sid就不用ip():
    assert service.visitor_key(SID_A, "8.8.8.8") == "sid:" + SID_A


def test_sid拿不到才退回ip():
    """进站第一个响应才刚下发 cookie,那次请求读不到 sid —— 不能因此没身份。"""
    assert service.visitor_key(None, "8.8.8.8") == "8.8.8.8"
    assert service.visitor_key("垃圾值", "8.8.8.8") == "8.8.8.8"


def test_两个都没有就没有身份():
    assert service.visitor_key(None, "unknown") is None


def test_scope判定带上sid():
    scope, key = service.resolve_scope(None, False, "8.8.8.8", SID_A)
    assert scope == service.SCOPE_IP and key == "sid:" + SID_A


def test_登录和白名单仍然优先于sid():
    assert service.resolve_scope({"id": 7}, False, "8.8.8.8", SID_A)[0] == service.SCOPE_USER
    assert service.resolve_scope(None, True, "1.2.3.4", SID_A)[0] == service.SCOPE_SITE


# ── 换成 sid 之后真正解决的两件事 ────────────────────────────────────────────

def test_同一出口ip的两个浏览器互不覆盖(fake_store):
    """公司/学校 NAT:按 IP 存的时候这两个人共用一份,现在各存各的。"""
    _, ips = fake_store
    service.save_layout(None, ONE_CARD, False, "8.8.8.8", SID_A)
    service.save_layout(None, {"cards": []}, False, "8.8.8.8", SID_B)
    assert ips["sid:" + SID_A] == ONE_CARD
    assert ips["sid:" + SID_B] == {"cards": []}
    assert service.get_layout(None, False, "8.8.8.8", SID_A) == ONE_CARD


def test_换网络也能读回自己那份(fake_store):
    """手机切流量/换 WiFi 就换了 IP,sid 不变,看板要跟着人走。"""
    service.save_layout(None, ONE_CARD, False, "8.8.8.8", SID_A)
    assert service.get_layout(None, False, "223.65.129.69", SID_A) == ONE_CARD


# ── 存量数据:按 IP 存的老访客不能丢看板 ──────────────────────────────────────

def test_第一次带sid来继承ip那一行(fake_store):
    """切换前存的布局挂在 IP 上,sid 行还不存在 —— 读它当模板,人感觉不到换过身份。"""
    users, ips = fake_store
    users[db.GUEST_USER_ID] = SITE_DEFAULT
    ips["8.8.8.8"] = ONE_CARD
    assert service.get_layout(None, False, "8.8.8.8", SID_A) == ONE_CARD


def test_继承只读不搬_保存才落到sid行(fake_store):
    """IP 行留给同 IP 的其他设备继续用,到期由保留策略清掉。"""
    _, ips = fake_store
    ips["8.8.8.8"] = ONE_CARD
    service.get_layout(None, False, "8.8.8.8", SID_A)
    assert "sid:" + SID_A not in ips and ips["8.8.8.8"] == ONE_CARD
    service.save_layout(None, {"cards": []}, False, "8.8.8.8", SID_A)
    assert ips["sid:" + SID_A] == {"cards": []}
    assert ips["8.8.8.8"] == ONE_CARD


def test_有自己那行就不再看ip行(fake_store):
    _, ips = fake_store
    ips["8.8.8.8"] = ONE_CARD
    ips["sid:" + SID_A] = {"cards": []}
    assert service.get_layout(None, False, "8.8.8.8", SID_A) == {"cards": []}


def test_重置时连ip老行一起删(fake_store):
    """只删 sid 行的话,下次打开又从 IP 行继承回来,等于重置没生效。"""
    users, ips = fake_store
    users[db.GUEST_USER_ID] = SITE_DEFAULT
    ips["8.8.8.8"] = ONE_CARD
    service.save_layout(None, ONE_CARD, False, "8.8.8.8", SID_A)
    service.save_layout(None, {}, False, "8.8.8.8", SID_A)
    assert ips == {}
    assert service.get_layout(None, False, "8.8.8.8", SID_A) == SITE_DEFAULT


def test_没有sid的重置只删自己那行(fake_store):
    """退回 IP 那条路上,键本身就是 IP 行,别再多删一次。"""
    _, ips = fake_store
    ips["9.9.9.9"] = ONE_CARD
    service.save_layout(None, {}, False, "8.8.8.8", None)
    assert ips == {"9.9.9.9": ONE_CARD}


# ── 灌库防护:换成 sid 之后"一个 IP 一行"的天然上限没了 ──────────────────────

def _sid(i):
    return "%032x" % i


def test_同一ip狂建新行会被拦下(fake_store):
    """一个脚本每次带个新的随机 sid 来,就能一直往表里加行 —— 这条是唯一的闸。"""
    _, ips = fake_store
    for i in range(service._new_row_limiter.limit):
        service.save_layout(None, ONE_CARD, False, "8.8.8.8", _sid(i))
    assert len(ips) == service._new_row_limiter.limit

    with pytest.raises(service.LayoutRateLimited) as e:
        service.save_layout(None, ONE_CARD, False, "8.8.8.8", _sid(999))
    # 超限那一行不能留在库里,不然拦了也白拦
    assert "sid:" + _sid(999) not in ips
    assert len(ips) == service._new_row_limiter.limit
    assert e.value.retry_after >= 1


def test_更新已有行不受限流影响(fake_store):
    """正常访客每拖一下就保存一次,一小时几百次很常见,不能被当成灌库。"""
    _, ips = fake_store
    for _ in range(service._new_row_limiter.limit * 3):
        service.save_layout(None, ONE_CARD, False, "8.8.8.8", SID_A)
    assert ips == {"sid:" + SID_A: ONE_CARD}


def test_限流按ip分桶(fake_store):
    """一个 IP 打满了,不该连累别人。"""
    _, ips = fake_store
    for i in range(service._new_row_limiter.limit):
        service.save_layout(None, ONE_CARD, False, "8.8.8.8", _sid(i))
    service.save_layout(None, ONE_CARD, False, "9.9.9.9", _sid(1000))
    assert "sid:" + _sid(1000) in ips
