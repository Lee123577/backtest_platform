"""
数据看板布局 —— 每个访客一份,身份按"登录用户 > IP"降级。

三种归属(scope),读写用的是同一套判定,所见即所存:

  user          已登录     → user_board_layout 里自己那一行
  site_default  白名单 IP   → user_board_layout 的 user_id=0 那一行,也就是
                             全站默认布局。维护者摆好的样子 = 新访客看到的
                             样子,这是既有的产品行为,保持不变。
  ip            普通访客    → board_layout_by_ip 里 ip_key 那一行。第一次来
                             没有自己的行,读到的是全站默认布局(当模板用);
                             一旦拖动保存,就落到自己那一行,与别人隔离。

IP 当身份是有损的,这点必须清楚:同一出口 IP(公司/学校 NAT)的多个人会共用
一份看板;换 WiFi/切流量则等于换了个人,原来的看板找不回来。之所以还这么做,
是因为平台目前 user_session 表为空、没有任何登录态可用,按 IP 存至少让绝大
多数家庭宽带/固定办公网的访客能把看板留住 —— 比现在"谁都存不下"强。
真要稳,后续应改用已经在下发的 sp_sid cookie(见 analytics/attribution)。

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
import re
from typing import Any, Dict, List, Optional, Tuple

from . import db

SCOPE_USER = "user"
SCOPE_SITE = "site_default"
SCOPE_IP = "ip"
# 连 IP 都拿不到(request.client 缺失/伪造成垃圾值):没有任何可持久化的身份。
# 读全站默认布局的只读副本,写一律拒绝 —— 绝不能让它退回去写全站默认那一行,
# 那正是本模块一直在防的内容投毒面。
SCOPE_EPHEMERAL = "ephemeral"

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


def _key_for(user: Optional[Dict[str, Any]]) -> int:
    return int(user["id"]) if user else db.GUEST_USER_ID


def resolve_scope(
    user: Optional[Dict[str, Any]], is_site_editor: bool, ip: Optional[str]
) -> Tuple[str, Optional[str]]:
    """定位这次请求该读写哪一份布局,返回 (scope, ip_key)。

    ip_key 只在 scope == SCOPE_IP 时有意义。判定顺序是固定的:
    登录态最可信,其次是白名单(维护者改的是全站默认),最后才落到 IP。
    """
    if user is not None:
        return SCOPE_USER, None
    if is_site_editor:
        return SCOPE_SITE, None
    key = ip_key(ip)
    if key is None:
        return SCOPE_EPHEMERAL, None
    return SCOPE_IP, key


def get_layout(
    user: Optional[Dict[str, Any]] = None,
    is_site_editor: bool = False,
    ip: Optional[str] = None,
) -> Dict[str, Any]:
    db.ensure_tables()
    scope, key = resolve_scope(user, is_site_editor, ip)
    if scope == SCOPE_IP:
        saved = db.load_ip_layout(key)
        # 这个 IP 从没存过 → 拿全站默认布局当开局模板(区别于存过一份空的,
        # 那是用户主动"重置为默认",不该再把默认卡片塞回去)
        return saved if saved is not None else db.load_layout(db.GUEST_USER_ID)
    if scope == SCOPE_EPHEMERAL:
        return db.load_layout(db.GUEST_USER_ID)
    return db.load_layout(_key_for(user))


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
    scope, key = resolve_scope(user, is_site_editor, ip)
    if scope == SCOPE_EPHEMERAL:
        raise LayoutForbidden("识别不到你的网络地址,布局无法保存")
    if scope == SCOPE_IP:
        if not clean:
            # 前端"重置为默认"发的就是空布局。对访客而言删掉自己那一行,
            # 下次打开重新回退到全站默认 —— 这才是"默认"该有的意思,
            # 顺带把不再需要的行清掉,不留空壳。
            db.delete_ip_layout(key)
        else:
            db.save_ip_layout(key, clean)
        return
    db.save_layout(_key_for(user), clean)


def search(q: str) -> List[Dict[str, str]]:
    return db.search_stocks(q, limit=10)
