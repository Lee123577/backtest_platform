"""
数据看板布局 —— 每个访客一份,身份按"登录用户 > sp_sid > IP"降级。

三种归属(scope),读写用的是同一套判定,所见即所存:

  user          已登录     → user_board_layout 里自己那一行
  site_default  白名单 IP   → user_board_layout 的 user_id=0 那一行,也就是
                             全站默认布局。维护者摆好的样子 = 新访客看到的
                             样子,这是既有的产品行为,保持不变。
  ip            普通访客    → board_layout_by_ip 里 visitor_key 那一行。第一次
                             来没有自己的行,读到的是全站默认布局(当模板用);
                             一旦拖动保存,就落到自己那一行,与别人隔离。
                             (scope 值仍叫 "ip" 是历史原因 —— 它表示的是
                             "匿名访客私有的那一档",与用什么键无关。)

访客那一档的键由 visitor_key() 定,两级降级:

  sid:<32hex>   请求带得到 sp_sid cookie 时用它。这个 cookie 由 visit_log
                中间件对每个非静态响应补发(见 analytics/attribution),
                浏览器级稳定、一年有效,HttpOnly 前端读不到。
  <ip 归一值>   拿不到 sp_sid 时(首次进站那一个响应、禁用 cookie、脚本
                直连)退回 IP,行为与切换前完全一致。

为什么要从 IP 换成 sp_sid:IP 当身份两头都不准 —— 同一出口 IP(公司/学校
NAT)的多个人共用一份看板,而换 WiFi/切流量的同一个人却找不回自己那份。
sp_sid 一人一份、跨网络不变,正好把这两个方向都修掉。

存量数据不丢:一个浏览器第一次带 sid 来时,它的 sid 行还不存在,这时会读
它所在 IP 的那一行当模板(见 get_layout),下次保存才落到自己的 sid 行 ——
旧的 IP 行不动,由保留策略在 180 天后自然清掉。唯一会顺手删掉 IP 行的是
"重置为默认":不删的话下次打开又从 IP 行继承回来,等于重置没生效。

sid 来自客户端 cookie,能伪造 —— 但 IP(经 XFF)同样能,而 sid 是 128bit
随机、不可枚举,这一档的抗冒充能力只增不减。布局本身也不是敏感数据。

存储的布局是一份 {"cards": [...], "positions": {...}} 文档:
- cards:    当前画布上有哪些卡片、什么类型(stock/rank/compare/review/hotsector/indices),
            决定了增删卡片后下次打开时展示哪些卡片、以及它们的先后顺序。
- positions:每张卡片的坐标/尺寸,行情卡片(kind=stock)还可以带 code/type,
            记录用户把这张卡片切换成了哪只股票/指数,以及 chart(走势线/K线,
            缺省按走势线);对比卡片(kind=compare)
            带 codes(数组,最多 MAX_COMPARE_CODES 支),记录这张卡片同时对比
            哪几只股票/指数 —— 卡片是"槽位",股票只是槽位里当前展示的内容,
            槽位 id 不随切换/增删其它卡片而变。

兼容旧数据:更早版本存的是"卡片 id -> 坐标"的扁平映射(没有 cards/positions
两个字段),前端加载时会把这种旧文档整体当作 positions、cards 置空从而回退
到默认卡片集合,所以这里也按同样的规则原样存/验(不强制升级旧数据)。
"""
from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from ..analytics import attribution
from ..ratelimit import SlidingWindowLimiter
from . import db

logger = logging.getLogger(__name__)

SCOPE_USER = "user"
SCOPE_SITE = "site_default"
# 匿名访客私有的那一档。值沿用 "ip" 不改 —— 前端按它决定保存失败时的说法,
# 换个值只会让还缓存着旧 my_board.js 的浏览器认不出来,而这一档的含义并没变。
SCOPE_IP = "ip"
# 连 IP 都拿不到(request.client 缺失/伪造成垃圾值):没有任何可持久化的身份。
# 读全站默认布局的只读副本,写一律拒绝 —— 绝不能让它退回去写全站默认那一行,
# 那正是本模块一直在防的内容投毒面。
SCOPE_EPHEMERAL = "ephemeral"
# 维护者正在看别人那份。resolve_scope 永远不会返回它 —— 它不是一种身份,
# 而是"这次读的不是自己的布局"这个状态,只由 API 层在预览请求上贴出来。
SCOPE_PREVIEW = "preview"

MAX_CARDS = 10   # 与前端 my_board.js 的 MAX_BOARD_CARDS 保持一致
MAX_COMPARE_CODES = 6
COORD_MIN, COORD_MAX = -5000, 20000
SIZE_MIN, SIZE_MAX = 200, 1200
_CODE_RE = re.compile(r"^[0-9A-Za-z]{1,10}$")
_CARD_ID_RE = re.compile(r"^[0-9A-Za-z_]{1,40}$")
_KINDS = ("stock", "rank", "compare", "review", "hotsector", "indices")
# 行情卡片的图表样式(与前端 my_board.js 的 CHART_MODES 保持一致)
_CHART_MODES = ("line", "kline")
# 排行卡片类目是有限、写死的(与前端 my_board.js 的 RANK_INFO 保持一致);
# 不校验的话,直接调 API 能往访客共享布局塞前端渲染不了的"僵尸"排行卡。
_RANK_IDS = ("rk_groups", "rk_industry", "rk_concept", "rk_special")
# 单例卡片(每种卡片全站只有一个固定 id,没有可配置内容,数据来自各自模块
# 现成的接口):每日复盘摘要 / AI热门板块战绩 / 指数速览。跟排行卡片一样,
# id 与 kind 是绑定死的,不校验的话能塞进前端渲染不了的 id。
_SINGLETON_IDS = {
    "review": "dr_summary",
    "hotsector": "hs_today",
    "indices": "idx_overview",
}


class LayoutError(Exception):
    pass


class LayoutForbidden(Exception):
    """没有可持久化的身份,这次保存不该落库(由 API 层翻成 403)。"""
    pass


class LayoutRateLimited(Exception):
    """同一 IP 短时间内建了太多访客行,像在灌库(由 API 层翻成 429)。"""

    def __init__(self, retry_after: int = 60) -> None:
        super().__init__("保存过于频繁，请稍后再试")
        self.retry_after = retry_after


# 灌库防护:只数"新建了一行"的次数,按请求 IP 算。
#
# 按 IP 存的年代,一个 IP 最多占一行,表的行数天然被公网 IP 数封住;换成 sp_sid
# 之后这个上限没了 —— 一个脚本每次带个新的随机 sid 来 POST,就能一直往表里加行,
# 把保留策略的 20000 上限顶满,再由 LRU 把真实访客的布局挤掉。
#
# 正常访客几乎不会连着新建行(新建 = 第一次保存/换浏览器/清了 cookie),
# 10 分钟 20 次给公司或校园 NAT 出口留足了余量;更新已有行完全不受限,
# 拖多少次都行。窗口取 10 分钟而不是 1 小时,是为了让误伤的人等得起 ——
# Retry-After 最多 600 秒,而不是让人干等一小时。
_new_row_limiter = SlidingWindowLimiter(limit=20, window_sec=600.0,
                                        name="my_board_new_visitor")


def ip_key(ip: Optional[str]) -> Optional[str]:
    """把请求 IP 归一成看板的存储键;拿不到可用身份时返回 None。

    - IPv4 → 原样(含内网地址:局域网部署/本地开发也得能存)
    - IPv6 → 截到 /64 前缀。运营商给手机的 IPv6 后 64 位是接口标识,会随机
      轮换,用完整地址存等于每刷新一次就换个人,看板永远读不回来。
    - 解析不了(空串 / "unknown" / 伪造的垃圾值)→ None,不落库
    """
    ip = (ip or "").strip()
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.version == 6:
        net = ipaddress.ip_network(f"{addr}/64", strict=False)
        return str(net)          # 形如 "2408:8207:xxxx:yyyy::/64",≤45 字符
    return str(addr)


# 访客键的 sid 前缀。带前缀是为了让 sid 行和 IP 行能共用一张表(和同一套保留
# 策略、同一个预览面板),同时两种键永远不会撞车 —— IP 里不可能出现 ":" 开头
# 的这种前缀,而 "sid:" + 32hex = 36 字符,也没超过 ip_key 的 45 列宽。
SID_PREFIX = "sid:"


def sid_key(sid: Optional[str]) -> Optional[str]:
    """sp_sid cookie → 存储键;不是自己签发的格式一律当没有。

    valid_sid 的白名单校验不能省:这个值直接进 SQL 参数和维护者面板,
    放任意字符串进来等于让客户端自选主键。"""
    sid = (sid or "").strip()
    if not attribution.valid_sid(sid):
        return None
    return SID_PREFIX + sid


def visitor_key(sid: Optional[str], ip: Optional[str]) -> Optional[str]:
    """匿名访客这一档到底存哪一行:sp_sid 优先,拿不到才退回 IP。

    两个都没有 → None,调用方按 SCOPE_EPHEMERAL 处理(只读全站默认,不落库)。"""
    return sid_key(sid) or ip_key(ip)


def _key_for(user: Optional[Dict[str, Any]]) -> int:
    return int(user["id"]) if user else db.GUEST_USER_ID


def resolve_scope(
    user: Optional[Dict[str, Any]],
    is_site_editor: bool,
    ip: Optional[str],
    sid: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """定位这次请求该读写哪一份布局,返回 (scope, visitor_key)。

    visitor_key 只在 scope == SCOPE_IP 时有意义。判定顺序是固定的:
    登录态最可信,其次是白名单(维护者改的是全站默认),最后才落到访客那一档
    —— 那一档内部再按 sp_sid > IP 降级(见 visitor_key)。
    """
    if user is not None:
        return SCOPE_USER, None
    if is_site_editor:
        return SCOPE_SITE, None
    key = visitor_key(sid, ip)
    if key is None:
        return SCOPE_EPHEMERAL, None
    return SCOPE_IP, key


def get_layout(
    user: Optional[Dict[str, Any]] = None,
    is_site_editor: bool = False,
    ip: Optional[str] = None,
    sid: Optional[str] = None,
) -> Dict[str, Any]:
    db.ensure_tables()
    scope, key = resolve_scope(user, is_site_editor, ip, sid)
    if scope == SCOPE_IP:
        saved = db.load_ip_layout(key)
        if saved is None and key.startswith(SID_PREFIX):
            # 这个浏览器还没有自己的行 —— 可能是按 IP 存的那个年代留下来的
            # 老访客。读他所在 IP 的那一行当模板,人就感觉不到身份换过。
            # 只读不搬:等他下次保存自然落到 sid 行,IP 行留给同 IP 的其他
            # 设备继续用,到期由保留策略清掉。
            legacy = ip_key(ip)
            if legacy:
                saved = db.load_ip_layout(legacy)
        # 从没存过 → 拿全站默认布局当开局模板(区别于存过一份空的,
        # 那是用户主动"重置为默认",不该再把默认卡片塞回去)
        return saved if saved is not None else db.load_layout(db.GUEST_USER_ID)
    if scope == SCOPE_EPHEMERAL:
        return db.load_layout(db.GUEST_USER_ID)
    return db.load_layout(_key_for(user))


# ── 维护者只读预览访客看板 ───────────────────────────────────────────────────
#
# 用来看"访客把看板摆成了什么样",纯读。这里刻意不提供对应的写入路径:看板每拖
# 一下就 POST 一次,给维护者开个"写别人那一行"的口子,等于给误操作发通行证 ——
# 想改的是自己的布局,退出预览就是了。

PREVIEW_LIST_LIMIT = 200
IP_KEY_MAX_LEN = 45          # board_layout_by_ip.ip_key 的列宽
# 面板上 sid 只露前几位:32 位十六进制看不出任何信息,而完整值等于一枚能冒充
# 该访客的令牌 —— 预览用的是清单里回传的原文,展示层没必要把它印在屏幕上。
SID_LABEL_KEEP = 8


def key_label(key: str) -> str:
    """存储键 → 面板上显示的样子。IP 原样,sid 截断成 "浏览器 9f3a1b2c…"。"""
    key = key or ""
    if key.startswith(SID_PREFIX):
        return "浏览器 " + key[len(SID_PREFIX):len(SID_PREFIX) + SID_LABEL_KEEP] + "…"
    return key


def list_ip_layouts(limit: int = PREVIEW_LIST_LIMIT) -> Dict[str, Any]:
    """访客布局清单(最近更新在前)+ 总数。

    键有两种(sid: 开头的浏览器标识 / IP),kind 告诉前端这是哪种,label 是
    已经处理好的展示文案;ip_key 仍回原文,预览要拿它当参数。"""
    db.ensure_tables()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = PREVIEW_LIST_LIMIT
    limit = max(1, min(limit, PREVIEW_LIST_LIMIT))
    items = []
    for row in db.list_ip_layouts(limit):
        ts = row.get("updated_at")
        key = row.get("ip_key") or ""
        items.append({
            "ip_key": key,
            "kind": "sid" if key.startswith(SID_PREFIX) else "ip",
            "label": key_label(key),
            "cards": int(row.get("cards") or 0),
            # 面板上只显示到分钟就够了,秒和时区在这没有信息量
            "updated_at": ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else (ts or ""),
        })
    return {"items": items, "total": db.count_ip_layouts(), "limit": limit}


def get_ip_layout(ip_or_key: str) -> Optional[Dict[str, Any]]:
    """按存储键取一份访客布局;这个键从没存过则返回 None(由 API 层翻成 404)。

    先按原样查:面板回传的就是库里的键原文,"sid:xxxx" 和 IPv6 的
    "2408:xxxx::/64" 都只有原样才查得到 —— 再过一次 ip_key() 只会失败
    (一个不是地址,一个是网段)。查不到再当普通 IP 归一一次,这样手输一个
    完整 IPv6 地址也能对上它的 /64 行。
    """
    key = (ip_or_key or "").strip()
    if not key or len(key) > IP_KEY_MAX_LEN:
        return None
    db.ensure_tables()
    found = db.load_ip_layout(key)
    if found is not None:
        return found
    norm = ip_key(key)
    if norm and norm != key:
        return db.load_ip_layout(norm)
    return None


def _clean_positions(positions: Any) -> Dict[str, Any]:
    if not isinstance(positions, dict):
        raise LayoutError("坐标格式不对")
    if len(positions) > MAX_CARDS:
        raise LayoutError("卡片太多")

    clean: Dict[str, Any] = {}
    for card_id, pos in positions.items():
        if not isinstance(card_id, str) or not _CARD_ID_RE.match(card_id):
            raise LayoutError("卡片 id 不合法")
        if not isinstance(pos, dict):
            raise LayoutError("坐标格式不对")
        try:
            left = float(pos.get("left"))
            top = float(pos.get("top"))
        except (TypeError, ValueError):
            raise LayoutError("坐标不是数字")
        if not (COORD_MIN <= left <= COORD_MAX and COORD_MIN <= top <= COORD_MAX):
            raise LayoutError("坐标超出范围")
        entry: Dict[str, Any] = {"left": left, "top": top}

        for dim in ("width", "height"):
            val = pos.get(dim)
            if val is None:
                continue
            try:
                val = float(val)
            except (TypeError, ValueError):
                raise LayoutError(f"{dim} 不是数字")
            if not (SIZE_MIN <= val <= SIZE_MAX):
                raise LayoutError(f"{dim} 超出范围")
            entry[dim] = val

        code, typ = pos.get("code"), pos.get("type")
        if code is not None or typ is not None:
            if typ not in ("stock", "index"):
                raise LayoutError("type 不合法")
            if not isinstance(code, str) or not _CODE_RE.match(code):
                raise LayoutError("code 不合法")
            entry["code"] = code
            entry["type"] = typ
            if isinstance(pos.get("name"), str) and len(pos["name"]) <= 20:
                entry["name"] = pos["name"]

        # 行情卡片的图表样式(走势线/K线)。白名单校验:布局是原样存原样发回
        # 前端的,不限值域就能往这里塞任意字符串,而前端会拿它做样式判断。
        chart = pos.get("chart")
        if chart is not None:
            if chart not in _CHART_MODES:
                raise LayoutError("chart 不合法")
            entry["chart"] = chart

        codes = pos.get("codes")
        if codes is not None:
            if not isinstance(codes, list) or len(codes) > MAX_COMPARE_CODES:
                raise LayoutError("对比卡片股票数量不合法")
            clean_codes: List[Dict[str, str]] = []
            seen_codes = set()
            for c in codes:
                if not isinstance(c, dict):
                    raise LayoutError("对比卡片股票格式不对")
                ccode, ctyp = c.get("code"), c.get("type")
                if ctyp not in ("stock", "index"):
                    raise LayoutError("对比卡片股票 type 不合法")
                if not isinstance(ccode, str) or not _CODE_RE.match(ccode):
                    raise LayoutError("对比卡片股票 code 不合法")
                key = (ccode, ctyp)
                if key in seen_codes:
                    continue
                seen_codes.add(key)
                citem: Dict[str, str] = {"code": ccode, "type": ctyp}
                if isinstance(c.get("name"), str) and len(c["name"]) <= 20:
                    citem["name"] = c["name"]
                clean_codes.append(citem)
            entry["codes"] = clean_codes

        clean[card_id] = entry
    return clean


def _clean_cards(cards: Any) -> List[Dict[str, str]]:
    if not isinstance(cards, list):
        raise LayoutError("卡片列表格式不对")
    if len(cards) > MAX_CARDS:
        raise LayoutError("卡片太多")

    clean: List[Dict[str, str]] = []
    seen = set()
    for item in cards:
        if not isinstance(item, dict):
            raise LayoutError("卡片格式不对")
        card_id, kind = item.get("id"), item.get("kind")
        if not isinstance(card_id, str) or not _CARD_ID_RE.match(card_id):
            raise LayoutError("卡片 id 不合法")
        if kind not in _KINDS:
            raise LayoutError("卡片类型不合法")
        if kind == "rank" and card_id not in _RANK_IDS:
            raise LayoutError("排行卡片 id 不合法")
        if kind in _SINGLETON_IDS and card_id != _SINGLETON_IDS[kind]:
            raise LayoutError("卡片 id 不合法")
        # 反向也拦:保留 id(排行/单例)不允许挂在别的 kind 下 —— 否则伪造一张
        # id=dr_summary 的行情卡就能把"添加卡片"菜单里的对应项顶掉
        if card_id in _RANK_IDS and kind != "rank":
            raise LayoutError("卡片 id 不合法")
        if card_id in _SINGLETON_IDS.values() and _SINGLETON_IDS.get(kind) != card_id:
            raise LayoutError("卡片 id 不合法")
        if card_id in seen:
            continue
        seen.add(card_id)
        clean.append({"id": card_id, "kind": kind})
    return clean


def save_layout(
    user: Optional[Dict[str, Any]],
    layout: Dict[str, Any],
    is_site_editor: bool = False,
    ip: Optional[str] = None,
    sid: Optional[str] = None,
) -> None:
    if not isinstance(layout, dict):
        raise LayoutError("布局格式不对")

    clean: Dict[str, Any] = {}
    if "cards" in layout:
        clean["cards"] = _clean_cards(layout.get("cards"))
    if "positions" in layout:
        clean["positions"] = _clean_positions(layout.get("positions"))
    elif not clean:
        # 兼容旧版数据结构:整份就是"卡片 id -> 坐标"的扁平映射
        clean = _clean_positions(layout)

    db.ensure_tables()
    scope, key = resolve_scope(user, is_site_editor, ip, sid)
    if scope == SCOPE_EPHEMERAL:
        raise LayoutForbidden("识别不到你的身份,布局无法保存")
    if scope == SCOPE_IP:
        if not clean:
            # 前端"重置为默认"发的就是空布局。对访客而言删掉自己那一行,
            # 下次打开重新回退到全站默认 —— 这才是"默认"该有的意思,
            # 顺带把不再需要的行清掉,不留空壳。
            db.delete_ip_layout(key)
            # 连同他所在 IP 的老行一起删。get_layout 在 sid 行缺失时会去
            # 继承 IP 行,只删 sid 行的话下次打开又把老布局继承回来,
            # 用户会看到"重置了个寂寞"。
            legacy = ip_key(ip) if key.startswith(SID_PREFIX) else None
            if legacy and legacy != key:
                db.delete_ip_layout(legacy)
        else:
            created = db.save_ip_layout(key, clean)
            if created:
                _guard_new_row(key, ip)
        return
    db.save_layout(_key_for(user), clean)


def _guard_new_row(key: str, ip: Optional[str]) -> None:
    """刚新建的那一行是不是灌进来的?是就立刻删掉并拒绝。

    先写后判,不是先判后写:正常保存的绝大多数是更新已有行,没必要为了
    识别"新建"在每次保存前多打一次库 —— upsert 的 rowcount 已经把这件事
    白送了。误删风险也不存在:删的就是这次刚建的那一行,老数据碰不到。
    """
    # 按 IP 分桶(而不是按 sid):sid 正是攻击者能随意换的那个东西。
    # 连 IP 都拿不到时归到同一个桶里,宁可严一点。
    bucket = ip_key(ip) or "_noip"
    if _new_row_limiter.allow(bucket):
        return
    try:
        db.delete_ip_layout(key)
    except Exception as e:      # 删不掉就留着,保留策略最终会收走
        logger.warning("回滚超限的访客布局失败(忽略) key=%s: %s", key, e)
    raise LayoutRateLimited(_new_row_limiter.retry_after(bucket))


def search(q: str) -> List[Dict[str, str]]:
    return db.search_stocks(q, limit=10)
