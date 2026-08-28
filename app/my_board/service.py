"""
数据看板布局 —— 按登录账号存,未登录的所有人共用同一份只读配置。

两档身份,读写用的是同一套判定,所见即所存:

  user   已登录  → user_board_layout 里自己那一行,拖完自动保存
  guest  未登录  → 统一读同一份"初始配置"。拖动/增删在本次浏览里照常生效
                  (不然连试都试不了),但一律不落库,刷新即回到初始状态。
                  前端常驻一条提示把这件事说在前头,别让人拖了半天才发现。

那份"初始配置"是**管理员账号自己的看板**(ADMIN_EMAILS,见 app/auth/admin.py)。
管理员摆好自己的看板,新访客第一眼看到的就是这一份 —— 不再有独立的"全站默认"
概念要单独维护。管理员还没注册/还没存过,就回退到 user_id=0 那一行(历史遗留的
全站默认),所以换过来的过程中访客看到的东西不会突然变空。

**为什么不再有"维护者"这一档**:以前维护者是**白名单 IP + 未登录**才成立的身份,
写的是 user_id=0 那一行。管理身份改成登录账号之后这个组合不可能再出现(管理员
必然是登录态),这一档就只剩下"谁也进不来的死代码"。索性合并:管理员的看板就是
访客看到的那份。

**为什么去掉按访客存的那一档**:此前未登录访客各自按 sp_sid cookie(更早是 IP)
在 board_layout_by_ip 里占一行。那是"平台一个登录用户都没有"时的权宜之计 ——
账号体系当时因为短信通道没接通而完全不可用,不给访客留一份就等于所有人拖完
刷新就没了。邮箱登录上线后这个前提没了,继续按浏览器存反而有三个坏处:

  - 匿名行认不回人:换浏览器/清 cookie 就丢,用户以为看板"没了"
  - 一张只增不减的匿名表要配保留策略、灌库限流、维护者预览面板三套机制,
    全是为了伺候一份谁也认领不了的数据
  - 看板是这个站少数几个"值得为它注册"的功能,免登录就能存等于把注册理由
    白送掉

board_layout_by_ip 这张表不再读写,也不自动删 —— 里面是真实访客摆过的布局,
留着不占什么地方。确认不需要了再手动 `DROP TABLE board_layout_by_ip`。

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

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from . import db

logger = logging.getLogger(__name__)

SCOPE_USER = "user"
# 未登录:读初始配置的只读副本,写一律拒绝。绝不能让它退回去写任何共享的那一行 ——
# 那是本模块一直在防的内容投毒面(任何路人都能改掉所有人看到的初始画布)。
SCOPE_GUEST = "guest"

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
    """未登录,这次保存不该落库(由 API 层翻成 403)。"""
    pass


def resolve_scope(user: Optional[Dict[str, Any]]) -> str:
    """定位这次请求该读写哪一份布局。登录即自己那一份,否则只读。"""
    return SCOPE_USER if user is not None else SCOPE_GUEST


def _initial_layout() -> Dict[str, Any]:
    """未登录访客统一看到的那一份,也是新用户的开局模板。

    优先取管理员账号自己的看板 —— 管理员摆好的样子就是新人第一眼看到的样子。
    取不到(没配 ADMIN_EMAILS / 管理员还没注册 / 他还没存过看板 / DB 抖)就回退到
    user_id=0 那一行,即换成账号制之前的全站默认,不会突然变成空画布。
    """
    try:
        from ..auth.admin import admin_user_ids
        for uid in sorted(admin_user_ids()):
            saved = db.load_layout(uid)
            if saved:
                return saved
    except Exception as e:
        logger.info("取管理员看板作为初始配置失败,回退全站默认: %s", e)
    return db.load_layout(db.GUEST_USER_ID) or {}


def get_layout(user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """取这次请求该看到的布局。"""
    db.ensure_tables()
    if user is not None:
        saved = db.load_layout(int(user["id"]))
        # 没存过 → 拿初始配置当开局模板。新注册的人第一次打开看板,看到的
        # 应该还是他刚才作为访客看到的那份画布,而不是突然变成另一个样子;
        # 存过一份空的是另一回事(主动重置),要尊重。
        if saved is not None:
            return saved
    return _initial_layout()


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
    if user is None:
        raise LayoutForbidden("登录后才能保存看板布局")
    # 走到这里只剩登录用户一种身份,写的永远是他自己那一行 —— 没有任何路径
    # 能让一个请求写到别人或共享的行上。
    key = int(user["id"])
    if not clean:
        # 前端"重置布局"发的就是一份空 layout({})。删掉这一行,下次打开回退到
        # 全站默认 —— 这才是"重置为默认"该有的意思。存一份空的会让人重置完
        # 刷新还是空画布。
        db.delete_layout(key)
        return
    db.save_layout(key, clean)


def search(q: str) -> List[Dict[str, str]]:
    return db.search_stocks(q, limit=10)
