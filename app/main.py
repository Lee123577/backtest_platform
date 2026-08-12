import asyncio
import hashlib
import html as _htmlmod
import json
import logging
import re
import time
from datetime import date as _date
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field
from starlette.requests import Request

from .config import settings
from .ratelimit import SlidingWindowLimiter

logger = logging.getLogger(__name__)


_MAX_RANGE_DAYS = 15 * 366  # 15 年(含闰年余量),防 DoS:全市场×长跨度抓崩后端


def _validate_date_range(start: str, end: str) -> None:
    """Raise HTTPException(400) on bad date input — keep error UX consistent."""
    try:
        s = _date.fromisoformat(start)
        e = _date.fromisoformat(end)
    except (TypeError, ValueError):
        raise HTTPException(400, "日期格式须为 YYYY-MM-DD")
    if s >= e:
        raise HTTPException(400, "开始日期必须早于结束日期")
    if s.year < 1990:
        raise HTTPException(400, "开始日期不能早于 1990 年")
    if (e - s).days > _MAX_RANGE_DAYS:
        raise HTTPException(400, f"回测时间跨度最大 15 年,请缩短日期范围")

from .auth import deps as _auth_deps
from .daily_review import db as _dr_db
from .daily_review import render as _dr_render
from .stock_report import db as _sr_db
from .stock_report import service as _sr_service
from .data.calendar import count_trading_days, next_n_trading_days
from .data.data_loader import get_kline_data, get_stock_name, normalize_code
from .data.market_data import (
    build_hist_market_caps,
    build_universe_hint,
    download_universe_history,
    get_historical_universe,
    get_index_history,
    get_index_latest_quotes,
    get_universe_stats,
    get_universe_stocks,
)
from .data.realtime import get_realtime_prices
from .data import stock_search
from .engine.backtest import calc_benchmark, run_backtest
from .engine.portfolio_backtest import run_portfolio_backtest
from .engine.robustness import compute_oos_split, compute_param_sensitivity
from .paper_trading import admin_ip as paper_admin_ip
from .paper_trading import db as paper_db
from .scheduler import db as scheduler_db
from .scheduler import registry as scheduler_registry
from .scheduler import runner as scheduler_runner
from .strategies.registry import (
    get_portfolio_strategy,
    get_strategy,
    get_strategy_class,
    list_strategies,
)

app = FastAPI(title="A股量化回测平台", version="1.2.0")

# 访问日志中间件 —— 异步写入 back_test.user_visit_log
from .visit_log import VisitLogMiddleware  # noqa: E402
app.add_middleware(VisitLogMiddleware)

# 安全辅助:取真实客户端 IP、判定可信反代
from .visit_log import _client_ip, _is_from_trusted_proxy  # noqa: E402

# GZip —— K线/回测等大 JSON 可压到 ~1/5,移动端收益最明显。
# starlette 内置排除 text/event-stream,portfolio_backtest 的 SSE 不受影响;
# level=6 而非默认 9:小内存生产机上压缩比接近但 CPU 省一半
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


# ── 安全响应头(防御性,不影响 SSE/业务)──────────────────────────────────────
class SecurityHeadersMiddleware:
    _SEC_HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Content-Security-Policy": (
            "default-src 'self'; "
            "img-src 'self' data: https:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        ),
    }

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                existing = {k.decode().lower(): k for k, _ in message.get("headers", [])}
                new_headers = list(message.get("headers", []))
                for h, v in self._SEC_HEADERS.items():
                    if h.lower() not in existing:
                        new_headers.append((h.encode(), v.encode()))
                if _is_https_request(scope) and "strict-transport-security" not in existing:
                    new_headers.append(
                        (b"strict-transport-security",
                         b"max-age=31536000; includeSubDomains")
                    )
                message["headers"] = new_headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _is_https_request(scope) -> bool:
    """请求经 https 才下发 HSTS:直连 https,或受信反代下 X-Forwarded-Proto=https。"""
    if scope.get("scheme") == "https":
        return True
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    peer = scope.get("client")
    peer_ip = peer[0] if peer else None
    if peer_ip and _is_from_trusted_proxy(peer_ip):
        return headers.get("x-forwarded-proto", "").lower() == "https"
    return False


def _safe_detail(msg: str, exc: Exception | None = None) -> str:
    """对外错误信息:默认不泄露内部异常细节,DEBUG=1 才带原始错误(仅本地排障)。"""
    if settings.DEBUG and exc is not None:
        return f"{msg}：{exc}"
    return msg


# ── 轻量速率限制(防重型计算接口被滥用/DoS)──────────────────────────────────
# 滑动窗口本体在 app/ratelimit.py(五处调用点共用一份实现)。
_BACKTEST_LIMIT = 6        # 每窗口最多请求数
_BACKTEST_WINDOW = 60.0    # 秒
_backtest_limiter = SlidingWindowLimiter(
    limit=_BACKTEST_LIMIT, window_sec=_BACKTEST_WINDOW, name="backtest",
)


def _rate_limit_backtest(request: Request) -> None:
    ip = _client_ip(request)
    if not _backtest_limiter.allow(ip):
        raise HTTPException(
            429,
            detail="请求过于频繁,请稍后再试(回测接口每分钟最多 %d 次)" % _BACKTEST_LIMIT,
            headers={"Retry-After": str(_backtest_limiter.retry_after(ip))},
        )


app.add_middleware(SecurityHeadersMiddleware)

# 今日数据入库状态 API
from .data_status.api import router as data_status_router  # noqa: E402
app.include_router(data_status_router)

# 大盘云图 API（前端 ECharts treemap 消费）
from .cloudmap.api import router as cloudmap_router  # noqa: E402
app.include_router(cloudmap_router)

# AI 热门板块(DeepSeek 每日选板块+选股，T 日收盘买入/T+1 收盘卖出模拟)
from .ai_hotsector.api import router as ai_hotsector_router  # noqa: E402
app.include_router(ai_hotsector_router)

# AI 每日复盘(DeepSeek 收盘后基于当日真实数据生成市场复盘)
from .daily_review.api import router as daily_review_router  # noqa: E402
app.include_router(daily_review_router)

# 账号/登录(手机号验证码，付费墙前置)
from .auth.api import router as auth_router  # noqa: E402
app.include_router(auth_router)

# 订阅/订单(内容付费墙)
from .subscription.api import router as subscription_router  # noqa: E402
app.include_router(subscription_router)

# 用户反馈 / 功能建议
from .feedback.api import router as feedback_router  # noqa: E402
app.include_router(feedback_router)

# 自选盯盘(收盘信号提醒,会员权益)
from .watchlist.api import router as watchlist_router  # noqa: E402
app.include_router(watchlist_router)

# 板块涨幅排行榜(读每日快照表)
from .sectors.api import router as sectors_router  # noqa: E402
app.include_router(sectors_router)

# 数据看板卡片拖拽布局(登录用户各自一份,访客共享默认布局)
from .my_board.api import router as my_board_router  # noqa: E402
app.include_router(my_board_router)

# 个股 AI 分析报告(DeepSeek 解读本站行情库快照 + 9 种策略的回测实证)
from .stock_report.api import router as stock_report_router  # noqa: E402
app.include_router(stock_report_router)

# 转化埋点与漏斗(自建,不引入 GA4/神策 —— 访问日志表本来就在跑)
from .analytics.api import router as analytics_router  # noqa: E402
app.include_router(analytics_router)

# 回测结果分享快照(公开链接)
from .share.api import router as share_router  # noqa: E402
app.include_router(share_router)

STATIC_DIR = Path(__file__).parent / "static"


# ── 缓存策略 ────────────────────────────────────────────────────────────────
# 起因:Starlette 的 StaticFiles / FileResponse 只发 etag+last-modified,不发
# Cache-Control,浏览器于是走"启发式新鲜期"((now - Last-Modified) * 10%)——
# 一个两周没改的 JS 能被缓存一天多且期间完全不回源。曾因此翻车:HTML 里的
# 内联 onclick 改成事件委托后忘了 bump ?v=,老用户拿到"新 HTML + 旧 JS",
# 整页按钮失灵。
#
# 光把 Cache-Control 定死还不够 —— 那只是把"漏 bump ?v=" 的惩罚从缓存一两天
# 放大成缓存一年,失误本身还得靠人不犯。所以版本号改成**服务端按文件内容
# 自动注入**:HTML 里写什么 ?v= 都无所谓,响应前统一换成该文件当前内容的
# sha256 前 12 位。内容变 → URL 变 → 浏览器必然重新拉;内容没变 → URL 不变 →
# 继续吃 immutable 缓存。人不需要再维护 26 个版本号,这类 bug 从根上消失。
#
#   /static?v=<hash> → immutable 一年
#   /static 无版本    → 5 分钟 + must-revalidate
#   HTML 文档         → no-cache + ETag(内容变才重传)
_STATIC_IMMUTABLE = "public, max-age=31536000, immutable"
_STATIC_SHORT = "public, max-age=300, must-revalidate"
_HTML_CACHE = "no-cache"

# 匹配 HTML 里对本站静态资源的引用,?v=... 可有可无
_ASSET_REF_RE = re.compile(r'(/static/[A-Za-z0-9_\-./]+\.(?:js|css))(\?v=[^"\']*)?')
_asset_hash_cache: Dict[str, tuple] = {}   # rel_path  -> ((mtime,size), hash)
_html_raw_cache: Dict[str, tuple] = {}     # abs_path  -> ((mtime,size), 原始文本)


def _asset_hash(rel_path: str) -> Optional[str]:
    """/static/js/app.js → 该文件内容 sha256 的前 12 位。按 (mtime,size) 缓存。"""
    fp = STATIC_DIR / rel_path[len("/static/"):]
    try:
        st = fp.stat()
    except OSError:
        return None            # 引用了不存在的文件:原样放过,别把页面搞挂
    key = (st.st_mtime_ns, st.st_size)
    hit = _asset_hash_cache.get(rel_path)
    if hit and hit[0] == key:
        return hit[1]
    digest = hashlib.sha256(fp.read_bytes()).hexdigest()[:12]
    _asset_hash_cache[rel_path] = (key, digest)
    return digest


def _stamp_asset_versions(html: str) -> str:
    def sub(m: "re.Match") -> str:
        ref = m.group(1)
        h = _asset_hash(ref)
        return f"{ref}?v={h}" if h else m.group(0)
    return _ASSET_REF_RE.sub(sub, html)


class VersionedStaticFiles(StaticFiles):
    """带版本号(?v=)的静态资源长缓存,没带的只给短缓存。

    判定要解析查询串取 v 这个 key —— 早先写成 `b"v=" in query_string` 的子串
    匹配,`?rev=1` 也会被当成带版本号而缓存一年。
    """

    async def get_response(self, path: str, scope):
        # 页面模板不从 /static 裸发。它们必须经页面路由的 _html() 出去 ——
        # 那一层才会注入公共页脚(含法定 ICP 备案号)、戳静态资源版本号、按
        # 登录态决定复盘正文直出多少。裸文件绕过这些,吐出来的是缺页脚、
        # 占位注释没替换的半成品,还跟正式路由重复收录(robots 早已 Disallow,
        # 但那只挡爬虫,挡不住直接访问)。
        if path.lower().endswith(".html"):
            return Response(status_code=404)

        resp = await super().get_response(path, scope)
        # 只给真正命中的文件打缓存头;404/405 之类不缓存
        if resp.status_code in (200, 304):
            qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            versioned = bool(qs.get("v", [""])[0])
            resp.headers["Cache-Control"] = (
                _STATIC_IMMUTABLE if versioned else _STATIC_SHORT
            )
        return resp


app.mount("/static", VersionedStaticFiles(directory=str(STATIC_DIR)), name="static")


# ── 公共页脚(单一事实来源)────────────────────────────────────────────────────
# 此前这段在 12 个 HTML 里逐字复制。改一次联系邮箱或备案号要动 12 个文件,
# 且已经漂出了三种不一致的版本。收拢到这里,各页只留 <!--FOOTER--> 占位。
#
# 备案号是法定必须展示的 —— 任何新页面漏了占位注释就会缺它。因此
# _html() 里对"整页无页脚"的情况会记 warning,别静默放过。
_FOOTER_MARKER = "<!--FOOTER-->"
_CONTACT_EMAIL = "lihaichuan393@gmail.com"
_ICP_NUMBER = "苏ICP备2026049395号-1"


def _footer(*, legal_links: bool = True, contact: bool = True,
            extra_class: str = "") -> str:
    """站点页脚 HTML。

    三处开关对应收拢前就存在的、有意为之的差异:
      legal_links=False —— /legal 自己不再链回自己
      contact=False     —— /s/{token} 分享快照页不放"定制策略"的销售话术
                           (页面会被分享给陌生人,带招揽内容不合适)
      extra_class       —— /my_board 需要额外的 mb-footer 参与卡片布局
    """
    cls = "site-footer" + (f" {extra_class}" if extra_class else "")
    parts = [
        f'<footer class="{cls}">',
        '  <div class="site-footer-brand">🕐 收盘 shoupan · 用数据验证策略，让判断有据可依</div>',
    ]
    if contact:
        parts.append(
            '  <div class="site-footer-contact">\n'
            '  💡 需要定制策略 / 接入实盘？只提供代码，服务器需自行购买 · '
            '第一单免费（目前限时全免）·\n'
            f'  <a href="mailto:{_CONTACT_EMAIL}">{_CONTACT_EMAIL}</a>\n'
            '  </div>'
        )
    if legal_links:
        parts.append(
            '  <div class="site-footer-legal">'
            '<a href="/legal">隐私政策</a> · <a href="/legal#terms">用户协议</a></div>'
        )
    parts.append(
        '  <div class="site-footer-beian">'
        '<a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener">'
        f'{_ICP_NUMBER}</a></div>'
    )
    parts.append("</footer>")
    return "\n".join(parts)


def _html(request: Request, *parts: str,
          replacements: Optional[Dict[str, str]] = None,
          footer: Optional[str] = None) -> Response:
    """返回页面 HTML:注入公共页脚,给静态资源引用打上内容 hash 版本号,配 no-cache + ETag。

    no-cache 不是"不缓存",是"每次都回源校验" —— 配 ETag 后内容没变就 304,
    只有几十字节的开销,而页面和它引用的 JS 永远不会出现版本错配。

    replacements: 服务端直出用 —— {占位注释: 替换成的 HTML}。只允许放**公开
    内容**:响应带 ETag 且可被共享缓存复用,任何按登录态变化的内容进了 HTML
    就可能串给其他人(付费正文永远走 API,不走这里)。

    footer: 覆盖默认页脚(见 _footer 的三处开关);None = 标准页脚。
    """
    fp = STATIC_DIR.joinpath(*parts)
    st = fp.stat()
    key = (st.st_mtime_ns, st.st_size)

    # 只缓存**原始文本**(省一次磁盘读),戳版本号每次都重算。
    # 别把戳好的成品也缓存起来按 HTML 的 mtime 失效 —— "只改了 JS、没动 HTML"
    # 恰恰是最常见的情况,那样戳出来的还是旧 hash,等于把这个 bug 换层再造一遍。
    # 重算的实际开销是每个被引用文件一次 stat(hash 本身有 _asset_hash_cache)。
    cached = _html_raw_cache.get(str(fp))
    if cached and cached[0] == key:
        text = cached[1]
    else:
        text = fp.read_text(encoding="utf-8")
        _html_raw_cache[str(fp)] = (key, text)

    if replacements:
        for marker, value in replacements.items():
            text = text.replace(marker, value)

    if _FOOTER_MARKER in text:
        text = text.replace(_FOOTER_MARKER, footer if footer is not None else _footer())
    elif "<footer" not in text:
        # 备案号是法定要展示的,漏了得有人知道 —— 新页面忘了写占位注释时会走到这。
        logger.warning("页面 %s 既无 %s 占位也无 <footer>，页脚与备案号缺失",
                       "/".join(parts), _FOOTER_MARKER)

    body = _stamp_asset_versions(text).encode("utf-8")
    etag = '"%s"' % hashlib.sha256(body).hexdigest()[:16]

    headers = {"Cache-Control": _HTML_CACHE, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="text/html; charset=utf-8",
                    headers=headers)


def _kline_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """DataFrame → 前端 K 线数组用的记录列表(date/open/high/low/close/volume)。

    NaN 一律转 None:json.dumps 会把 NaN 输出成裸 `NaN`(非法 JSON),
    浏览器 fetch().json() 直接抛异常(DB 中 NULL 的 open/high/low 会触发)。
    """
    def _f(v):
        return None if v is None or (isinstance(v, float) and pd.isna(v)) else v

    return [
        {
            "date": str(r["date"].date()),
            "open": _f(r["open"]), "high": _f(r["high"]),
            "low": _f(r["low"]),  "close": _f(r["close"]),
            "volume": _f(r["volume"]),
        }
        for r in df.to_dict("records")
    ]


@app.get("/")
async def root(request: Request):
    return _html(request, "index.html")


@app.get("/paper_trading")
async def paper_trading_page(request: Request):
    return _html(request, "paper_trading.html")


@app.get("/cloudmap", include_in_schema=False)
async def page_cloudmap(request: Request):
    """大盘云图（D3 Treemap）。资源在 app/static/cloudmap/ 下。"""
    return _html(request, "cloudmap", "index.html")


@app.get("/ai_hotsector", include_in_schema=False)
async def page_ai_hotsector(request: Request):
    return _html(request, "ai_hotsector.html")


@app.get("/daily_review", include_in_schema=False)
def page_daily_review(request: Request):
    """复盘首页 —— 展示最新一篇(对所有人免费)。"""
    return _daily_review_page(request, None)


@app.get("/daily_review/{review_date}", include_in_schema=False)
def page_daily_review_dated(review_date: str, request: Request):
    """往期复盘的独立可索引 URL。

    改这条路由的起因:原先往期靠 `/daily_review#2026-07-08` 的 hash 区分,而
    `#` 后面根本不会发给服务器 —— 几百篇复盘在搜索引擎眼里只有 1 个 URL,
    正文又是 JS 拉的,抓到的还是空壳。真实路径 + 正文直出才可能被收录。
    """
    try:
        d = _date.fromisoformat(review_date)
    except ValueError:
        raise HTTPException(404, "页面不存在")
    return _daily_review_page(request, d)


@app.get("/subscribe", include_in_schema=False)
async def page_subscribe(request: Request):
    return _html(request, "subscribe.html")


@app.get("/watchlist", include_in_schema=False)
async def page_watchlist(request: Request):
    return _html(request, "watchlist.html")


@app.get("/dashboard", include_in_schema=False)
async def page_dashboard(request: Request):
    return _html(request, "dashboard.html")


@app.get("/my_board", include_in_schema=False)
async def page_my_board(request: Request):
    # mb-footer 参与看板的卡片布局(页脚不能被浮动卡片顶掉)
    return _html(request, "my_board.html", footer=_footer(extra_class="mb-footer"))


@app.get("/legal", include_in_schema=False)
async def page_legal(request: Request):
    # 已经在法务页上了,页脚不必再链回自己
    return _html(request, "legal.html", footer=_footer(legal_links=False))


@app.get("/s/{token}", include_in_schema=False)
async def page_share(token: str, request: Request):
    """回测结果分享页。内容由 /api/backtest/share/{token} 前端拉取。

    这页**不进 sitemap、页面自带 noindex**:快照是用户生成的,成千上万份
    结构雷同,放进索引既是薄内容,又会变成一堆"晒收益"页面,合规上不划算。
    分享靠链接传播,不靠搜索。
    """
    if not _SHARE_TOKEN_RE.match(token):
        raise HTTPException(404, "页面不存在")
    # 快照会被分享给陌生人,页脚不带"定制策略"的招揽内容
    return _html(request, "share.html", footer=_footer(contact=False))


_SHARE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


_SITE_ORIGIN = "https://shoupan.asia"


# ── 每日复盘：服务端直出(SEO)──────────────────────────────────────────────
# 只直出**公开内容**：最新一篇全文免费，往期正文是会员内容，HTML 里只放一段
# 免费预览 + 付费墙占位，完整正文照旧走 /api/daily_review/{date} 按登录态发。
# 给爬虫和给访客的 HTML 完全一致 —— 按 UA 区分就是 cloaking,会被判罚。
_DR_LATEST_CACHE: Dict[str, Any] = {}
_DR_HISTORY_CACHE: Dict[str, Any] = {}
_DR_LATEST_TTL = 60.0


def _dr_latest_cached():
    """最新一篇复盘(60s 进程缓存)。DB 不可用/表未建时返回 None,页面照常出壳。"""
    now = time.time()
    if _DR_LATEST_CACHE and now - _DR_LATEST_CACHE["ts"] < _DR_LATEST_TTL:
        return _DR_LATEST_CACHE["row"]
    try:
        row = _dr_db.get_latest_review()
    except Exception as e:
        logger.info("复盘页取最新篇失败(按无数据渲染): %s", e)
        return None
    _DR_LATEST_CACHE.update(ts=now, row=row)
    return row


def _json_ld(obj: Dict[str, Any]) -> str:
    """内联 JSON-LD。`<` 转义成 \\u003c —— 正文里出现 `</script>` 会提前闭合脚本块。"""
    raw = json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c")
    return f'<script type="application/ld+json">{raw}</script>'


def _dr_head(row: Optional[Dict[str, Any]], canonical: str,
             locked: bool) -> str:
    """复盘页 <head> 的 title / description / canonical / OG / 结构化数据。"""
    if row is None:
        title = "AI 每日复盘 · A股每日行情复盘 | shoupan"
        desc = ("每个交易日收盘后自动生成的 A 股市场复盘：主要指数、涨跌家数、"
                "成交额放缩量、涨停梯队与全市场涨跌幅榜，数据全部来自当日真实行情。")
        head = [f"<title>{_htmlmod.escape(title)}</title>"]
    else:
        d = str(row.get("review_date"))
        art_title = (row.get("title") or f"{d} A股复盘").strip()
        title = f"{art_title}（{d}） | shoupan"
        desc = _dr_render.plain_text(row.get("content_md"), limit=140) or (
            f"{d} A 股市场复盘：主要指数、涨跌家数、成交额与涨停梯队，"
            "基于当日真实行情数据自动生成。")
        head = [f"<title>{_htmlmod.escape(title)}</title>"]

    esc_desc = _htmlmod.escape(desc, quote=True)
    esc_title = _htmlmod.escape(title, quote=True)
    head += [
        f'<meta name="description" content="{esc_desc}">',
        f'<link rel="canonical" href="{canonical}">',
        '<meta property="og:type" content="article">',
        '<meta property="og:site_name" content="收盘 shoupan">',
        f'<meta property="og:title" content="{esc_title}">',
        f'<meta property="og:description" content="{esc_desc}">',
        f'<meta property="og:url" content="{canonical}">',
        '<meta name="twitter:card" content="summary">',
    ]

    if row is not None:
        d = str(row.get("review_date"))
        article: Dict[str, Any] = {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": (row.get("title") or f"{d} A股复盘").strip()[:110],
            "description": desc,
            "datePublished": d,
            "dateModified": d,
            "inLanguage": "zh-CN",
            "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
            "publisher": {"@type": "Organization", "name": "收盘 shoupan",
                          "url": _SITE_ORIGIN},
            "isAccessibleForFree": not locked,
        }
        if locked:
            # 付费墙的正规声明:告诉搜索引擎"这段确实是会员内容",
            # 不声明才会被当成对爬虫和用户发不同内容。
            article["hasPart"] = {
                "@type": "WebPageElement",
                "isAccessibleForFree": False,
                "cssSelector": ".dr-paid",
            }
        head.append(_json_ld(article))
        head.append(_json_ld({
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "首页",
                 "item": _SITE_ORIGIN + "/"},
                {"@type": "ListItem", "position": 2, "name": "AI 每日复盘",
                 "item": _SITE_ORIGIN + "/daily_review"},
                {"@type": "ListItem", "position": 3, "name": d},
            ],
        }))
    return "\n  ".join(head)


def _dr_body(row: Optional[Dict[str, Any]], locked: bool) -> str:
    """复盘正文区。locked=True 时只出免费预览，完整正文由 JS 按登录态补。"""
    if row is None:
        return ('<div class="no-data">还没有生成过复盘 —— 每个交易日 17:45 '
                "数据入库后自动生成。</div>")
    if row.get("status") == "failed":
        return '<div class="no-data">该日复盘生成失败</div>'
    if locked:
        # 免费预览 + 付费墙。付费墙整段包在 .dr-paid 里 —— 与 _dr_head() 里
        # hasPart.cssSelector 对应,是向搜索引擎声明"这里确实是会员内容"的锚点。
        return (
            _dr_render.preview_html(row.get("content_md")) +
            '<div class="dr-paid">'
            '<div class="dr-paywall">'
            '<div class="dr-paywall-title">🔒 订阅解锁完整复盘</div>'
            "<p>往期复盘的完整正文与当日数据快照为会员内容，最新一篇可免费查看。</p>"
            '<button class="dr-paywall-btn" id="drSubBtn" type="button">'
            "开通会员 · 查看套餐</button>"
            "</div></div>"
        )
    return _dr_render.md_to_html(row.get("content_md"))


def _dr_history(limit: int = 30) -> str:
    """历史列表 —— 直出成真链接。

    原先是 JS 生成的 div + click 事件,爬虫顺不到任何一篇往期;换成 <a href>
    后每篇复盘都有入口,不必只靠 sitemap 发现(前端仍会拦截点击做局部刷新)。

    列表对所有页面都一样、一天只变一次 —— 缓存 60s,免得每篇复盘被爬一次就查一次库。
    """
    now = time.time()
    if _DR_HISTORY_CACHE and now - _DR_HISTORY_CACHE["ts"] < _DR_LATEST_TTL:
        return _DR_HISTORY_CACHE["html"]
    try:
        rows = _dr_db.list_reviews(limit)
    except Exception as e:
        logger.info("复盘页取历史列表失败(按空列表渲染): %s", e)
        return ""
    if not rows:
        return ""
    items = []
    for r in rows:
        d = str(r.get("review_date"))
        t = (r.get("title") or "").strip()
        items.append(
            f'<a class="dr-history-item" href="/daily_review/{d}" data-date="{d}">'
            f'<span class="dr-history-date">{d}</span>'
            f'<span class="dr-history-title">{_htmlmod.escape(t)}</span>'
            + ('<span class="dr-history-failed">生成失败</span>'
               if r.get("status") == "failed" else "")
            + "</a>"
        )
    html = "".join(items)
    _DR_HISTORY_CACHE.update(ts=now, html=html)
    return html


def _daily_review_page(request: Request, d: Optional[_date]) -> Response:
    """渲染复盘页。d=None 表示复盘首页(展示最新一篇)。"""
    latest = _dr_latest_cached()
    if d is None:
        row = latest
        canonical = f"{_SITE_ORIGIN}/daily_review"
    else:
        try:
            row = _dr_db.get_review(d)
        except Exception as e:
            logger.warning("复盘页按日期取失败: %s", e)
            raise HTTPException(503, "数据服务暂时不可用，请稍后再试")
        if row is None:
            # 连不上库时 get_review 也返回 None —— 这里必须分清楚:
            # 这些 URL 会进搜索引擎索引,DB 抖一下就回 404 等于告诉爬虫"页面没了",
            # 反复几次就会被踢出索引。连不上就回 503(爬虫会择期重试)。
            if not _dr_db.db_available():
                raise HTTPException(503, "数据服务暂时不可用，请稍后再试")
            raise HTTPException(404, f"{d} 无复盘记录")
        canonical = f"{_SITE_ORIGIN}/daily_review/{d}"

    # 最新一篇对所有人免费;往期是会员内容 —— 服务端一律按"未订阅"直出,
    # 订阅用户的完整正文由前端拿 API 覆盖(HTML 带 ETag,不能塞按人变化的内容)
    is_latest = (
        row is not None and latest is not None
        and row.get("review_date") == latest.get("review_date")
    )
    locked = row is not None and not is_latest and row.get("status") != "failed"

    date_txt = str(row.get("review_date")) if row else ""
    art_title = ""
    if row is not None:
        art_title = (row.get("title") or f"{date_txt} A股复盘").strip()

    return _html(
        request, "daily_review.html",
        replacements={
            "<!--DR_HEAD-->": _dr_head(row, canonical, locked),
            "<!--DR_TITLE-->": _htmlmod.escape(art_title) or "加载中…",
            "<!--DR_DATE-->": _htmlmod.escape(date_txt),
            "<!--DR_BODY-->": _dr_body(row, locked),
            "<!--DR_HISTORY-->": _dr_history(),
            # 前端据此判断服务端已经直出了哪一篇,避免二次渲染时白闪一下
            "<!--DR_INIT-->": (
                f'<span id="drInit" hidden data-review-date="{date_txt}" '
                f'data-locked="{"1" if locked else "0"}" '
                f'data-ssr="{"1" if row is not None else "0"}"></span>'
            ),
        },
    )


# ── 个股 AI 分析报告：服务端直出(SEO)──────────────────────────────────────
# 全站 5000+ 只股票各一个 URL,是目前最大的可收录内容源。三条约束:
#   1. 正文必须直出 —— 靠 JS 拉的话爬虫抓到的是空壳,等于白做;
#   2. 页面渲染**绝不触发生成** —— 生成一次就是一次 DeepSeek 调用,
#      让 GET 顺手生成,爬虫爬一遍就能把额度烧穿(见 stock_report/service.py);
#   3. 还没有报告的股票出 noindex —— 一个只有"生成"按钮的空页面是薄内容,
#      放进索引只会拉低整站质量评分。有报告了自然会被重新抓到。
_SR_CODE_RE = re.compile(r"^\d{6}$")

_TREND_CLASS = {"看多": "sr-trend-up", "震荡": "sr-trend-flat", "看空": "sr-trend-down"}


def _sr_desc_fallback(label: str) -> str:
    return f"{label} 的技术面、估值与量化策略回测实证，AI 基于本站行情库自动解读。"


def _sr_head(row: Optional[Dict[str, Any]], code: str, name: str, canonical: str) -> str:
    """个股报告页 <head>。没有报告时带 noindex。"""
    label = f"{name}({code})" if name else code
    if row is None:
        title = f"{label} AI 数据分析 | shoupan"
        desc = (f"{label} 的技术面、估值与 9 种量化策略历史回测实证，"
                "数据来自本站行情库，AI 自动解读。")
        head = [
            f"<title>{_htmlmod.escape(title)}</title>",
            '<meta name="robots" content="noindex,follow">',
        ]
    else:
        art_title = (row.get("title") or f"{label} 数据解读").strip()
        title = f"{art_title} | shoupan"
        desc = (_dr_render.plain_text(row.get("content_md"), limit=140)
                or _sr_desc_fallback(label))
        head = [f"<title>{_htmlmod.escape(title)}</title>"]

    esc_desc = _htmlmod.escape(desc, quote=True)
    esc_title = _htmlmod.escape(title, quote=True)
    head += [
        f'<meta name="description" content="{esc_desc}">',
        f'<link rel="canonical" href="{canonical}">',
        '<meta property="og:type" content="article">',
        '<meta property="og:site_name" content="收盘 shoupan">',
        f'<meta property="og:title" content="{esc_title}">',
        f'<meta property="og:description" content="{esc_desc}">',
        f'<meta property="og:url" content="{canonical}">',
        '<meta name="twitter:card" content="summary">',
    ]
    if row is not None:
        d = str(row.get("report_date"))
        article = {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": (row.get("title") or f"{label} 数据解读").strip()[:110],
            "description": desc,
            "datePublished": d,
            "dateModified": d,
            "inLanguage": "zh-CN",
            "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
            "publisher": {"@type": "Organization", "name": "收盘 shoupan",
                          "url": _SITE_ORIGIN},
        }
        head.append(
            '<script type="application/ld+json">'
            + json.dumps(article, ensure_ascii=False)
            + "</script>"
        )
    return "\n  ".join(head)


def _sr_score(row: Optional[Dict[str, Any]]) -> str:
    if row is None:
        return ""
    score, trend, reason = row.get("score"), row.get("trend"), row.get("score_reason")
    if score is None and not trend:
        return ""
    parts = ['<div class="sr-score">']
    if score is not None:
        parts.append(f'<div class="sr-score-num">{int(score)}<small>/100</small></div>')
    if trend:
        cls = _TREND_CLASS.get(trend, "sr-trend-flat")
        parts.append(f'<span class="sr-trend {cls}">{_htmlmod.escape(trend)}</span>')
    if reason:
        parts.append(f'<div class="sr-score-reason">{_htmlmod.escape(reason)}</div>')
    parts.append("</div>")
    return "".join(parts)


def _sr_body(row: Optional[Dict[str, Any]], label: str) -> str:
    if row is None:
        return (
            '<div class="sr-empty">'
            f"还没有 {_htmlmod.escape(label)} 的 AI 分析报告。<br>"
            "点下面的按钮生成一份：读取本站行情库的最新快照，"
            "跑一遍 9 种内置策略的历史回测，再交给 AI 解读。"
            "</div>"
        )
    return _dr_render.md_to_html(row.get("content_md"))


def _sr_actions(row: Optional[Dict[str, Any]]) -> str:
    if row is not None:
        return ""
    return (
        '<button type="button" class="sr-gen-btn" id="srGenBtn">生成 AI 分析</button>'
        '<span class="sr-hint" id="srGenHint">约需 30 秒</span>'
    )


@app.get("/stock/{code}", include_in_schema=False)
def page_stock_report(code: str, request: Request):
    """个股 AI 分析报告页。已有报告则正文直出,没有则出生成入口 + noindex。"""
    try:
        norm = normalize_code(code)
    except ValueError:
        raise HTTPException(404, "页面不存在")
    if not _SR_CODE_RE.match(norm):
        raise HTTPException(404, "页面不存在")

    try:
        row = _sr_service.get_report(norm)
    except Exception as e:
        # 这些 URL 会进索引,DB 抖一下就回 404 等于告诉爬虫"页面没了"。
        # 连不上就 503,爬虫会择期重试(与复盘页同一套考量)。
        logger.warning("个股报告页取数失败(%s): %s", norm, e)
        raise HTTPException(503, "数据服务暂时不可用，请稍后再试")

    name = ""
    try:
        name = get_stock_name(norm) or ""
    except Exception:
        pass          # 名字只影响标题好看程度,取不到就用代码顶上
    label = f"{name}({norm})" if name else norm
    canonical = f"{_SITE_ORIGIN}/stock/{norm}"

    art_title = (row.get("title") if row else "") or f"{label} 数据解读"
    date_txt = str(row.get("report_date")) if row else ""
    return _html(
        request, "stock_report.html",
        replacements={
            "<!--SR_HEAD-->": _sr_head(row, norm, name, canonical),
            "<!--SR_TITLE-->": _htmlmod.escape(art_title),
            "<!--SR_META-->": (f"数据截至 {_htmlmod.escape(date_txt)}" if date_txt else ""),
            "<!--SR_SCORE-->": _sr_score(row),
            "<!--SR_BODY-->": _sr_body(row, label),
            "<!--SR_ACTIONS-->": _sr_actions(row),
            "<!--SR_INIT-->": (
                f'<span id="srInit" hidden data-code="{_htmlmod.escape(norm, quote=True)}" '
                f'data-ssr="{"1" if row is not None else "0"}"></span>'
            ),
        },
    )


# (路径, 更新频率, 优先级) —— 仅收录面向用户的功能页；/admin/tasks、/tasks 是运维监控页不收录
# /subscribe 在这里是**有意为之**:它不在侧边栏(入口走场景内引导,见 shell.js 的
# NAV 注释),但页面正常收单,该吃的搜索流量要吃。别因为"侧边栏里没有"就摘掉。
_SITEMAP_PAGES = [
    ("/", "daily", "1.0"),
    ("/paper_trading", "daily", "0.8"),
    ("/cloudmap", "daily", "0.7"),
    ("/ai_hotsector", "daily", "0.7"),
    ("/daily_review", "daily", "0.7"),
    ("/watchlist", "daily", "0.6"),
    ("/my_board", "weekly", "0.5"),
    ("/subscribe", "weekly", "0.5"),
    ("/legal", "yearly", "0.2"),
]

# 每篇复盘一条 URL,数量随天数增长 —— 每次请求都查库会被爬虫拖累,缓存 10 分钟。
# 复盘一天只新增一篇,这个新鲜度绰绰有余。
_SITEMAP_DR_CACHE: Dict[str, Any] = {}
_SITEMAP_DR_TTL = 600.0


def _sitemap_review_urls() -> str:
    now = time.time()
    if _SITEMAP_DR_CACHE and now - _SITEMAP_DR_CACHE["ts"] < _SITEMAP_DR_TTL:
        return _SITEMAP_DR_CACHE["xml"]
    try:
        dates = _dr_db.list_review_dates(365)
    except Exception as e:
        # 表未建/DB 抖动:少收几条 URL 无所谓,但 sitemap 本身不能 500 ——
        # 500 会让搜索引擎把整份 sitemap 判为失效
        logger.info("sitemap 取复盘日期失败(本次跳过复盘条目): %s", e)
        return ""
    xml = "".join(
        f"  <url><loc>{_SITE_ORIGIN}/daily_review/{d}</loc>"
        f"<lastmod>{d}</lastmod>"
        f"<changefreq>monthly</changefreq><priority>0.6</priority></url>\n"
        for d in dates
    )
    _SITEMAP_DR_CACHE.update(ts=now, xml=xml)
    return xml


# 个股报告的 URL 数量随生成量增长(上限 5000+),每次请求都查库会被爬虫拖累。
# 缓存 10 分钟 —— 报告一天更新一轮,这个新鲜度绰绰有余。
_SITEMAP_SR_CACHE: Dict[str, Any] = {}
_SITEMAP_SR_TTL = 600.0


def _sitemap_stock_urls() -> str:
    """只收录**已经生成过报告**的股票。

    没有报告的页面是一个只有生成按钮的空壳(而且自带 noindex),
    把 5000 个空壳塞进 sitemap 只会稀释抓取预算、拉低整站质量评分。
    """
    now = time.time()
    if _SITEMAP_SR_CACHE and now - _SITEMAP_SR_CACHE["ts"] < _SITEMAP_SR_TTL:
        return _SITEMAP_SR_CACHE["xml"]
    try:
        rows = _sr_db.list_codes_with_report(5000)
    except Exception as e:
        logger.info("sitemap 取个股报告列表失败(本次跳过个股条目): %s", e)
        return ""
    xml = "".join(
        f"  <url><loc>{_SITE_ORIGIN}/stock/{r['code']}</loc>"
        f"<lastmod>{r['report_date']}</lastmod>"
        f"<changefreq>weekly</changefreq><priority>0.6</priority></url>\n"
        for r in rows if r.get("code")
    )
    _SITEMAP_SR_CACHE.update(ts=now, xml=xml)
    return xml


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "Disallow: /api/\n"
        # 页面模板的裸文件(/static/daily_review.html 等)与正式路由内容重复,
        # 且没有 canonical —— 挡掉避免重复收录。CSS/JS 仍允许抓取(渲染需要)。
        "Disallow: /static/*.html$\n"
        f"Sitemap: {_SITE_ORIGIN}/sitemap.xml\n"
    )
    return Response(body, media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml():
    urls = "".join(
        f"  <url><loc>{_SITE_ORIGIN}{path}</loc>"
        f"<changefreq>{freq}</changefreq><priority>{prio}</priority></url>\n"
        for path, freq, prio in _SITEMAP_PAGES
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}"
        f"{_sitemap_review_urls()}"
        f"{_sitemap_stock_urls()}"
        "</urlset>\n"
    )
    return Response(body, media_type="application/xml")


# ── Paper trading (实盘信号观察) ──────────────────────────────────────────────

from .json_safe import json_safe as _json_safe  # noqa: E402


@app.get("/api/paper_trading/account")
def api_paper_account():
    """
    返回账户摘要 + 当前持仓（含浮盈）+ 最近一次运行概览。

    优先用实时行情（xuangu API）作为"最新价"：
      实时价 > 数据库最新收盘价 > 买入价
    顶部"总值/持仓市值/累计收益"也按实时价重算 —— 而不是用上次 daily_signal
    跑完时落库的快照，避免开盘后 / 当天涨跌后页面不更新。

    普通 def 路由:FastAPI 自动丢 anyio 线程池,同步 DB / requests 调用
    不会阻塞事件循环(此前是 async def + 直接同步查 DB,DB 抖动会卡住全部请求)。
    """
    try:
        paper_db.ensure_tables()
    except Exception as e:
        return {"account": None, "holdings": [], "latest_run": None,
                "error": f"数据库未就绪：{e}"}

    account = paper_db.get_account()
    holdings = paper_db.get_latest_holdings_with_prices()
    runs = paper_db.list_runs(limit=1)
    latest = runs[0] if runs else None

    # ── 实时价(本函数已在线程池,直接同步调)────────────────────────────────
    codes = [h["code"] for h in holdings if h.get("code")]
    realtime: Dict[str, float] = {}
    if codes:
        try:
            realtime = get_realtime_prices(codes)
        except Exception:
            realtime = {}

    # 持仓加浮盈字段
    holdings_out = []
    total_pos_value = 0.0
    cash_from_account = float(account["cash"]) if account and account.get("cash") is not None else 0.0
    initial_capital = (float(account["initial_capital"])
                       if account and account.get("initial_capital") is not None else 0.0)

    for h in holdings:
        code = h["code"]
        buy_px = float(h["buy_price"]) if h.get("buy_price") else 0.0
        db_close = float(h["last_close"]) if h.get("last_close") else 0.0
        # 实时优先，没有实时就用数据库收盘价，再没有就退到买入价
        last = realtime.get(code) or db_close or buy_px
        shares = int(h["shares"]) if h.get("shares") else 0
        market_value = last * shares
        cost = float(h["cost"]) if h.get("cost") else 0.0
        pnl = market_value - cost
        pnl_pct = (pnl / cost) if cost > 0 else 0.0
        total_pos_value += market_value
        holdings_out.append({
            "code": code,
            "name": h.get("name") or "",
            "shares": shares,
            "buy_price": buy_px,
            "buy_date": str(h["buy_date"]) if h.get("buy_date") else None,
            "cost": round(cost, 2),
            "last_close": round(last, 3),
            "is_realtime": code in realtime,
            "market_value": round(market_value, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct * 100, 2),
        })

    # ── 顶部摘要：用实时价重算总值/收益（覆盖最近一次 run 的 DB 快照）────
    # 空仓(全部卖出)时也要重算:total = cash + 0,否则总值停留在旧快照
    latest_out = _json_safe([latest])[0] if latest else None
    if latest_out is not None:
        total_value = cash_from_account + total_pos_value
        latest_out["total_value"] = round(total_value, 2)
        latest_out["position_value"] = round(total_pos_value, 2)
        latest_out["cash"] = round(cash_from_account, 2)
        if initial_capital > 0:
            latest_out["cum_return"] = round(
                (total_value - initial_capital) / initial_capital, 6
            )

    # ── 下次调仓日 ───────────────────────────────────────────────────────────
    next_rb_date: str | None = None
    days_until_rb: int | None = None
    rb_overdue_days: int = 0   # 已逾期天数（daily_signal 没跑、休假等情形）
    if account and account.get("last_rebalance_date"):
        # 复用 paper_db.get_strategy_params()，避免在两处重复处理 bytes/str/dict 三态
        sp = paper_db.get_strategy_params() or {}
        hold_days_val = int(sp.get("hold_days") or 5)
        last_rb_raw = account["last_rebalance_date"]
        last_rb_date = (
            last_rb_raw if isinstance(last_rb_raw, _date)
            else _date.fromisoformat(str(last_rb_raw))
        )
        nrd = next_n_trading_days(last_rb_date, hold_days_val)
        if nrd:
            next_rb_date = str(nrd)
            today = _date.today()
            if nrd >= today:
                # 还没到/正好今天到
                days_until_rb = count_trading_days(today, nrd)
            else:
                # 已经过了——daily_signal 没跑、或上次跑完恰好碰上长假
                # 用 nrd 在前、today 在后的方向数交易日，给出逾期天数
                days_until_rb = 0
                rb_overdue_days = count_trading_days(nrd, today)

    # 待执行挂单(T+1 成交模型:今晚生成,次日开盘成交)
    try:
        pending_actions = paper_db.get_pending_actions()
    except Exception:
        pending_actions = None

    account_out = _json_safe([account])[0] if account else None
    if account_out is not None:
        # 原始 JSON 字符串列不直接透出,统一走解析后的 pending_actions 字段
        account_out.pop("pending_actions", None)

    return {
        "account": account_out,
        "holdings": holdings_out,
        "latest_run": latest_out,
        "realtime_count": len(realtime),
        "next_rebalance_date": next_rb_date,
        "days_until_rebalance": days_until_rb,
        "rebalance_overdue_days": rb_overdue_days,
        "pending_actions": pending_actions,
    }


@app.get("/api/paper_trading/runs")
def api_paper_runs(limit: int = 30):
    try:
        paper_db.ensure_tables()
    except Exception:
        return []
    runs = paper_db.list_runs(limit=max(1, min(limit, 200)))
    return _json_safe(runs)


@app.get("/api/paper_trading/universe_preview")
def api_paper_universe_preview(cap_min: float, cap_max: float):
    """
    实时预览：给定市值范围，返回今日全市场命中数 + 整体分位数 + 头部样本。
    供策略参数编辑器在用户输入时显示「此范围内 X 只」，避免"未找到股票"的死胡同。
    """
    if cap_min < 0 or cap_max < 0 or cap_min >= cap_max:
        raise HTTPException(400, "需满足 0 ≤ cap_min < cap_max")
    return get_universe_stats(cap_min, cap_max)


@app.get("/api/paper_trading/strategy_params")
def api_paper_get_strategy_params():
    """
    返回当前策略参数 + 默认值（首次未初始化时空表用得上）。
    前端编辑后调 POST 写回，下次 daily_signal 跑就用新参数。
    """
    DEFAULTS = {
        "capital": 90000.0,
        "cap_min": 20.0, "cap_max": 30.0,
        "stock_num": 3, "hold_days": 5,
        "stop_loss_pct": 5.0,
        "allow_boards": ["main"],
    }
    try:
        paper_db.ensure_tables()
    except Exception as e:
        return {"params": DEFAULTS, "defaults": DEFAULTS, "initialized": False,
                "error": f"数据库未就绪：{e}"}

    current = paper_db.get_strategy_params() or {}
    # 缺字段用默认值补全（让前端不用做兜底）
    merged = {**DEFAULTS, **{k: v for k, v in current.items() if v is not None}}
    return {
        "params": merged,
        "defaults": DEFAULTS,
        "initialized": bool(current),
    }


class StrategyParamsPatch(BaseModel):
    cap_min: Optional[float] = None
    cap_max: Optional[float] = None
    stock_num: Optional[int] = None
    hold_days: Optional[int] = None
    stop_loss_pct: Optional[float] = None
    allow_boards: Optional[List[str]] = None
    # capital 只读：账户已开后改这个会让累计收益失真


@app.post("/api/paper_trading/strategy_params")
def api_paper_update_strategy_params(
    patch: StrategyParamsPatch,
    admin: str = Depends(paper_admin_ip.require_admin_ip),
):
    """
    保存策略参数。校验范围后 merge 到 paper_account.strategy_params。
    不直接动持仓/账户 —— 真正生效在下次 daily_signal 跑时。
    """
    # ── 范围校验 ─────────────────────────────────────────────────────────
    if patch.cap_min is not None and not (0.1 <= patch.cap_min <= 10000):
        raise HTTPException(400, "市值下限需在 0.1 ~ 10000 亿之间")
    if patch.cap_max is not None and not (0.1 <= patch.cap_max <= 10000):
        raise HTTPException(400, "市值上限需在 0.1 ~ 10000 亿之间")
    if (patch.cap_min is not None and patch.cap_max is not None
            and patch.cap_min >= patch.cap_max):
        raise HTTPException(400, "市值下限必须小于上限")
    if patch.stock_num is not None and not (1 <= patch.stock_num <= 20):
        raise HTTPException(400, "持仓数量需在 1 ~ 20 之间")
    if patch.hold_days is not None and not (1 <= patch.hold_days <= 120):
        raise HTTPException(400, "调仓周期需在 1 ~ 120 天之间")
    if patch.stop_loss_pct is not None and not (0 <= patch.stop_loss_pct <= 50):
        raise HTTPException(400, "止损百分比需在 0 ~ 50 之间（0=关闭）")
    if patch.allow_boards is not None:
        valid = {"main", "gem", "star", "bj"}
        bad = set(patch.allow_boards) - valid
        if bad:
            raise HTTPException(400, f"未知板块：{bad}，可选 {valid}")
        if not patch.allow_boards:
            raise HTTPException(400, "至少选择一个板块")

    try:
        paper_db.ensure_tables()
    except Exception as e:
        raise HTTPException(500, f"数据库未就绪：{e}")

    # 账户首次未初始化 → 自动建一个空账户，capital 用默认 9 万（之后用户也改不了）
    if paper_db.get_account() is None:
        paper_db.init_account(90000.0, patch.model_dump(exclude_none=True))

    merged = paper_db.update_strategy_params(patch.model_dump(exclude_none=True))
    return {"ok": True, "params": merged,
            "message": "已保存，下次 daily_signal 运行时生效。可点「立即扫描」即时生效。"}


@app.get("/api/paper_trading/trades")
def api_paper_trades(limit: int = 200):
    """
    成交流水（只含真买卖，已 FIFO 配对算出每笔卖出的实现盈亏）。
    用于前端"成交流水"面板，比"历史运行记录"更聚焦。
    """
    try:
        paper_db.ensure_tables()
    except Exception:
        return {"trades": []}
    trades = paper_db.list_trades(limit=limit)
    return {"trades": _json_safe(trades)}


@app.get("/api/paper_trading/run/{run_id}")
def api_paper_run_detail(run_id: int):
    try:
        paper_db.ensure_tables()
    except Exception as e:
        raise HTTPException(500, f"数据库未就绪：{e}")
    run = paper_db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "运行记录不存在")
    positions = run.pop("positions", [])
    run_json = _json_safe([run])[0]
    run_json["positions"] = _json_safe(positions)
    return run_json


# ── 定时任务监控 ──────────────────────────────────────────────────────────────

# 手动触发任务的常驻执行池(线程复用 → _get_pool 的 thread-local 连接也复用)
from concurrent.futures import ThreadPoolExecutor as _TPE  # noqa: E402
_task_runner_pool = _TPE(max_workers=2, thread_name_prefix="task-run")


@app.get("/api/tasks/summary")
def api_tasks_summary():
    """每个任务的：注册信息 + 最近一次状态 + 近 30 天成功率 + 今天是否已成功。"""
    try:
        scheduler_db.ensure_table()
    except Exception as e:
        return {"tasks": [], "error": f"task_run_log 表未就绪：{e}"}

    agg_map = {a["task_name"]: a for a in scheduler_db.summarize_by_task(30)}
    out = []
    for name, spec in scheduler_registry.TASKS.items():
        a = agg_map.get(name, {})
        total = int(a.get("recent_total", 0))
        success = int(a.get("recent_success", 0))
        out.append({
            "task_name": name,
            "description": spec.get("description", ""),
            "schedule": spec.get("schedule", ""),
            "depends_on": spec.get("depends_on"),
            "timeout_sec": spec.get("timeout_sec"),
            "last_started_at": _iso(a.get("last_started_at")),
            "last_status": a.get("last_status"),
            "last_duration_ms": a.get("last_duration_ms"),
            "last_exit_code": a.get("last_exit_code"),
            "last_error_msg": (a.get("last_error_msg") or "")[:500],
            "recent_total": total,
            "recent_success": success,
            "recent_failed": int(a.get("recent_failed", 0)),
            "success_rate": round(success / total, 3) if total > 0 else None,
            # 并入 summarize_by_task 的同一次聚合查询里取,不再逐任务单独查
            # already_ran_today —— 8 个任务就是 8 次多余的 DB 往返
            "ran_today_success": bool(a.get("ran_today_success")),
        })
    return {"tasks": out}


@app.get("/api/tasks/runs")
def api_tasks_runs(
    task: Optional[str] = None, status: Optional[str] = None,
    limit: int = 30, offset: int = 0,
    _admin: str = Depends(paper_admin_ip.require_admin_ip),
):
    """运行明细仅管理员可读:内含 stdout/stderr 尾巴与报错细节，不对外。
    (/api/tasks/summary 保持公开 —— 实盘观察页要用它显示扫描状态徽章)"""
    try:
        scheduler_db.ensure_table()
    except Exception:
        return {"runs": [], "has_more": False}
    page = max(1, min(limit, 100))
    offset = max(0, offset)
    # 多取 1 条判断是否还有下一页,避免额外 COUNT 查询
    rows = scheduler_db.list_recent_runs(
        task=task, status=status, limit=page + 1, offset=offset
    )
    has_more = len(rows) > page
    rows = rows[:page]
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "task_name": r["task_name"],
            "scheduled_at": _iso(r["scheduled_at"]),
            "started_at": _iso(r["started_at"]),
            "finished_at": _iso(r["finished_at"]),
            "duration_ms": r["duration_ms"],
            "status": r["status"],
            "exit_code": r["exit_code"],
            "trigger_type": r["trigger_type"],
            "stdout_tail": r.get("stdout_tail") or "",
            "stderr_tail": r.get("stderr_tail") or "",
            "error_msg": r.get("error_msg") or "",
            "host": r.get("host"),
        })
    return {"runs": out, "has_more": has_more}


@app.post("/api/tasks/{name}/run")
def api_tasks_run(
    name: str,
    force: bool = False,
    admin: str = Depends(paper_admin_ip.require_admin_ip),
):
    """
    手动触发一个任务。
    流程：
      1. precheck（同步）：依赖未满足 → 立刻返回 skipped + reason，并往
         task_run_log 写一条 status='skipped' 的记录方便溯源
      2. 通过检查 → 丢线程池跑 subprocess，HTTP 立即返回 queued
         （前端会在几秒后刷新 /api/tasks/runs 拿到最新结果）

    ?force=1 跳过依赖检查直接执行。**紧急用**——例如 daily_signal
    依赖 daily_update 但 daily_update 跑挂了，用户又必须立即扫描调仓。
    跑的子进程拿到自带的 env，不会重新触发 daily_update。
    """
    if scheduler_registry.get_task(name) is None:
        raise HTTPException(404, f"未知任务: {name}")

    # 防并发：同任务还有 running 记录（120 分钟内）拒绝重复触发
    # force=1 仍可绕过（紧急场景）。这是 daily_update 跑长时间时
    # 用户连点"立即刷新"导致 N 个 subprocess 并发抓 sina 的修复
    if not force:
        running = scheduler_db.has_running(name, within_minutes=120)
        if running:
            from datetime import datetime as _DT
            started = running.get("started_at")
            ago_sec = int((_DT.now() - started).total_seconds()) if started else None
            return {
                "task": name,
                "status": "skipped",
                "reason": (
                    f"同任务还在运行中 (run_id={running.get('run_id')}, "
                    f"已跑 {ago_sec or '?'} 秒)。再等等，或加 ?force=1 强制再启"
                ),
                "running": {
                    "run_id": running.get("run_id"),
                    "started_at": str(started) if started else None,
                    "host": running.get("host"),
                },
            }

        check = scheduler_runner.precheck(name)
        if not check["ok"]:
            # 注意：HTTP 200，不是 4xx —— 这是业务结果不是 API 错误
            return {"task": name, "status": "skipped", "reason": check["reason"],
                    "hint": "可调用 POST /api/tasks/{name}/run?force=1 跳过依赖检查"}

    trigger = "manual-force" if force else "manual"
    # subprocess 阻塞调用,丢到常驻小线程池跑,HTTP 立即返回。
    # 用常驻池而非每次新起线程:run_one 内部经 _get_pool() 借 thread-local
    # 连接且线程死亡不归还,一次性线程会逐次泄漏连接池计数
    _task_runner_pool.submit(scheduler_runner.run_one, name, trigger)
    from datetime import datetime as _DT
    return {"task": name, "status": "queued", "force": force,
            "queued_at": _DT.now().isoformat()}


def _iso(v):
    """datetime → ISO 字符串，None → None。"""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


@app.get("/admin/tasks", include_in_schema=False)
async def page_tasks(request: Request):
    return _html(request, "tasks.html")


@app.get("/tasks", include_in_schema=False)
async def page_tasks_legacy_redirect():
    """旧路径重定向：运维监控页不该跟用户功能挂在同一层级，迁到 /admin/tasks。
    保留 307 跳转只是防旧书签失效，新链接不应再指向这里。"""
    return RedirectResponse("/admin/tasks", status_code=307)


# ── 管理员 IP 白名单 ──────────────────────────────────────────────────────────
# 实盘观察 / 定时任务页的所有"写"操作（改策略参数、手动触发任务、增删 IP）
# 都受白名单保护。读操作（GET）全部公开。

class AdminIpAddRequest(BaseModel):
    ip: str
    note: Optional[str] = None


@app.get("/api/admin/ip/me")
def api_admin_ip_me(request: Request):
    """
    公开端点：返回当前请求 IP 和它是否在白名单。前端用它决定按钮禁不禁用。
    DB 未就绪时降级到只返回 IP，is_admin=False。
    """
    ip = paper_admin_ip.get_request_ip(request)
    try:
        paper_admin_ip.ensure_table()
    except Exception as e:
        return {"ip": ip, "is_admin": False, "whitelist_empty": True,
                "error": f"DB 未就绪：{e}"}
    # 这是全站访问量最高的接口之一（几乎每次翻页都会调），改用 60s TTL 的
    # 进程内缓存，同一份缓存顺带算出 whitelist_empty，不再各查一次 DB
    return {
        "ip": ip,
        "is_admin": paper_admin_ip.is_admin_cached(ip),
        "whitelist_empty": paper_admin_ip.whitelist_empty_cached(),
    }


@app.get("/api/admin/ip")
def api_admin_ip_list(admin: str = Depends(paper_admin_ip.require_admin_ip)):
    """列出全部白名单 IP（仅 admin 可见）。"""
    return {
        "current_ip": admin,
        "ips": _json_safe(paper_admin_ip.list_ips()),
    }


@app.post("/api/admin/ip")
def api_admin_ip_add(
    payload: AdminIpAddRequest,
    admin: str = Depends(paper_admin_ip.require_admin_ip),
):
    """添加 IP 到白名单（仅 admin）。"""
    try:
        paper_admin_ip.add_ip(payload.ip, payload.note or "", admin)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "ip": payload.ip}


@app.delete("/api/admin/ip/{ip}")
def api_admin_ip_delete(
    ip: str,
    admin: str = Depends(paper_admin_ip.require_admin_ip),
):
    """
    从白名单删除 IP（仅 admin）。
    安全保护：
      - 不允许在只剩自己的情况下删除自己 → 会把自己锁在外面
      - 允许 admin 删除其他 IP（包括同事的 IP，由 admin 自行负责）
    """
    if ip == admin and paper_admin_ip.count_ips() <= 1:
        raise HTTPException(
            400, "不能删除最后一个白名单 IP（会把自己锁在外面）。"
                 "请先添加另一个 IP 再删除当前 IP。"
        )
    removed = paper_admin_ip.remove_ip(ip)
    if not removed:
        raise HTTPException(404, f"IP {ip} 不在白名单中")
    return {"ok": True, "ip": ip}


# ── 旧分组：paper trading equity ──────────────────────────────────────────────

@app.get("/api/paper_trading/equity")
def api_paper_equity(start: Optional[str] = None, end: Optional[str] = None):
    for v in (start, end):
        if v is not None:
            try:
                _date.fromisoformat(v)
            except ValueError:
                raise HTTPException(400, "日期格式须为 YYYY-MM-DD")
    try:
        paper_db.ensure_tables()
    except Exception:
        return {"data": []}
    rows = paper_db.get_equity_curve(start=start, end=end)
    return {"data": _json_safe(rows)}


# ── Strategy list ─────────────────────────────────────────────────────────────

@app.get("/api/strategies")
async def api_list_strategies(response: Response):
    # 纯内存静态列表，进程存活期间不变 —— 允许浏览器短时间内直接复用，
    # 省掉每次翻页都重新拉一遍的往返（数据本身不查库，收益不大但几乎零成本）
    response.headers["Cache-Control"] = "public, max-age=300"
    return list_strategies()


# ── Single-stock endpoints ────────────────────────────────────────────────────

# 放在 /api/stock/{code}/... 之前:虽然 "search" 匹配不到需要后缀的那几条路由,
# 但把字面量路径写在参数化路径前面是更稳的习惯,以后加 /api/stock/{code} 时
# 不会突然被吃掉。
@app.get("/api/stock/search")
def api_stock_search(response: Response, q: str = "", limit: int = 10):
    """按代码或名称搜个股。给回测/自选盯盘的输入框做联想用。

    只出个股不出指数 —— 这两处拿到的每一条都要能直接回测/加自选。
    结果来自进程内缓存(见 data/stock_search.py),正常情况下不碰 DB。
    """
    results = stock_search.search(q, limit)
    # 股票名录一天最多动几只,允许浏览器短时间复用:用户回删一个字符再敲回来
    # 是最常见的操作,没必要再打一次请求
    response.headers["Cache-Control"] = "public, max-age=300"
    return {"results": results}


@app.get("/api/stock/{code}/info")
def api_stock_info(code: str):
    try:
        code = normalize_code(code)
    except ValueError as e:
        raise HTTPException(400, _safe_detail("股票代码格式不合法", e))
    name = get_stock_name(code)
    return {"code": code, "name": name}


@app.get("/api/stock/{code}/kline")
def api_kline(response: Response, code: str, start_date: str,
              end_date: str, adjust: str = "qfq"):
    _validate_date_range(start_date, end_date)
    try:
        code = normalize_code(code)
    except ValueError as e:
        raise HTTPException(400, _safe_detail("股票代码格式不合法", e))
    try:
        df = get_kline_data(code, start_date, end_date, adjust)
    except Exception as e:
        raise HTTPException(status_code=400, detail=_safe_detail("获取行情数据失败，请检查股票代码与日期范围", e))

    # 纯历史区间允许浏览器缓存 1 小时:同一股票反复调参回测时省掉重复请求。
    # 不敢给更长 —— qfq 在除权日会整体重算,历史值并非严格不可变;
    # 含当日的区间只给 60s,盘中数据可能更新
    if _date.fromisoformat(end_date) < _date.today():
        response.headers["Cache-Control"] = "public, max-age=3600"
    else:
        response.headers["Cache-Control"] = "public, max-age=60"

    records = _kline_records(df)
    return {"code": code, "total": len(records), "data": records}


@app.get("/api/index/{index_code}/kline")
def api_index_kline(response: Response, index_code: str,
                    start_date: str, end_date: str):
    """指数日线(index_daily 表)。

    注意不能拿 /api/stock/{code}/kline 取指数：000001 在个股表里是平安银行，
    在指数表里才是上证综指，两张表同码不同物。
    """
    _validate_date_range(start_date, end_date)
    index_code = (index_code or "").strip()
    df = get_index_history(index_code, start_date, end_date)
    if df is None or df.empty:
        raise HTTPException(404, f"指数 {index_code} 无数据")

    if _date.fromisoformat(end_date) < _date.today():
        response.headers["Cache-Control"] = "public, max-age=3600"
    else:
        response.headers["Cache-Control"] = "public, max-age=60"

    records = _kline_records(df)
    return {"index_code": index_code, "total": len(records), "data": records}


@app.get("/api/index/quotes")
def api_index_quotes(response: Response, codes: str):
    """批量指数最新报价(收盘 + 涨跌幅)。codes 为逗号分隔的指数代码。

    指数速览卡用一次请求替代过去"每指数一条 /kline",且直接用 index_daily 里
    存好的 pct_change,不在客户端拿两根收盘价自算。
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"data": []}
    rows = get_index_latest_quotes(code_list)
    response.headers["Cache-Control"] = "public, max-age=60"
    return {"data": _json_safe(rows)}


class StrategyConfig(BaseModel):
    strategy_id: str
    params: Optional[Dict[str, Any]] = None
    label: Optional[str] = None


class BacktestRequest(BaseModel):
    stock_code: str
    start_date: str
    end_date: str
    initial_capital: float = 100_000
    adjust: str = "qfq"
    # 有上限:每个 cfg 都要跑一次全量回测,robustness_check 还会再放大数倍。
    # 频控只限请求次数、不限单次成本,不设上限的话一个请求就能顶满线程池。
    # 20 对正常用法足够宽松(共 9 个信号策略,允许同一策略挂多组参数对比),
    # 前端在 MAX_INSTANCES 处做了同值的拦截,正常操作碰不到这里。
    strategies: List[StrategyConfig] = Field(..., min_length=1, max_length=20)
    slippage_rate: float = 0.0001
    stop_loss: Optional[float] = None    # e.g. 0.05 for 5% stop-loss
    take_profit: Optional[float] = None  # e.g. 0.20 for 20% take-profit
    # 防过拟合检查(参数敏感性 + 样本内/外拆分)——默认关,勾选才多跑几组回测
    robustness_check: bool = False


@app.post("/api/backtest")
def api_backtest(req: BacktestRequest,
                 request: Request,
                 _rl: None = Depends(_rate_limit_backtest)):
    # 普通 def:数据加载(DB/akshare) + 回测计算都是同步重活,
    # 让 FastAPI 丢 anyio 线程池,不阻塞事件循环
    _validate_date_range(req.start_date, req.end_date)
    if req.initial_capital <= 0:
        raise HTTPException(400, "初始资金必须大于 0")

    try:
        code = normalize_code(req.stock_code)
    except ValueError as e:
        raise HTTPException(400, _safe_detail("股票代码格式不合法", e))

    try:
        df = get_kline_data(code, req.start_date, req.end_date, req.adjust)
    except Exception as e:
        raise HTTPException(status_code=400, detail=_safe_detail("获取行情数据失败，请检查股票代码与日期范围", e))

    if df.empty:
        raise HTTPException(status_code=400, detail="数据为空，请检查股票代码和日期范围")

    stock_name = get_stock_name(code)   # 预检警告和响应体共用,只查一次

    # ── 资金充足性预检：A 股最小买入 100 股，资金低于"区间最低价×100"则无法成交 ──
    # 不直接拦截（用户可能仍想看价格曲线），但把警告塞进结果里，避免"0% 收益 0 笔交易"
    # 让人误以为是策略本身不出信号
    capital_warning: str | None = None
    if "low" in df.columns and len(df) > 0:
        min_low = float(df["low"].min())
        min_buy_cost = min_low * 100 * 1.0005  # 留一点手续费余量
        if min_buy_cost > req.initial_capital:
            recommended = int((min_low * 100 * 1.01 // 1000 + 1) * 1000)  # 上取整到 1000
            capital_warning = (
                f"初始资金 ¥{req.initial_capital:,.0f} 不足以买入 1 手 {stock_name}（{code}）。"
                f"区间内最低价 ¥{min_low:.2f}，单手 100 股最少需要 ¥{min_buy_cost:,.0f}。"
                f"建议把初始资金调到 ¥{recommended:,} 以上再回测。"
            )

    bt_kwargs = dict(
        slippage_rate=req.slippage_rate,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
    )

    results = []
    for cfg in req.strategies:
        try:
            strategy = get_strategy(cfg.strategy_id, cfg.params)
            result = run_backtest(df, strategy, req.initial_capital, **bt_kwargs)
            result["strategy_id"] = cfg.strategy_id
            result["strategy_name"] = cfg.label or strategy.name

            if req.robustness_check:
                strategy_cls = get_strategy_class(cfg.strategy_id)
                result["sensitivity"] = compute_param_sensitivity(
                    df, strategy_cls, strategy.params, req.initial_capital, **bt_kwargs
                )
                result["oos_split"] = compute_oos_split(
                    df, strategy, req.initial_capital, **bt_kwargs
                )

            results.append(result)
        except Exception as e:
            results.append({
                "strategy_id": cfg.strategy_id,
                "strategy_name": cfg.label or cfg.strategy_id,
                "error": _safe_detail("回测执行出错，请检查参数后重试", e),
            })

    benchmark = calc_benchmark(df, req.initial_capital, slippage_rate=req.slippage_rate)
    kline = _kline_records(df)

    # 漏斗第二层「跑过回测」。在服务端记而不是让前端上报 —— 前端 beacon
    # 会被拦截器/关页面吃掉,拿它算激活率会系统性偏低。埋点失败不影响返回。
    _record_backtest_event(request, code, req.robustness_check, len(results))

    return {
        "stock_code": code,
        "stock_name": stock_name,
        "results": results,
        "benchmark": benchmark,
        "kline": kline,
        "capital_warning": capital_warning,
    }


def _record_backtest_event(request: Request, code: str,
                           robustness: bool, n: int) -> None:
    try:
        from .analytics.api import request_context
        from .analytics import service as an_service
        user = _auth_deps.get_current_user(request)
        an_service.record(
            "backtest_run",
            user_id=int(user["id"]) if user else None,
            meta={"code": code, "robustness": bool(robustness), "strategies": n},
            **request_context(request),
        )
    except Exception as e:
        logger.info("回测事件埋点失败(忽略): %s", e)


# ── Portfolio backtest ────────────────────────────────────────────────────────

# 指数代码 → 展示名(基准下拉用)
_BENCHMARK_NAMES = {
    "000300": "沪深300", "000905": "中证500", "000852": "中证1000",
    "000016": "上证50", "000001": "上证指数", "399006": "创业板指",
    "399303": "国证2000",
}
# 策略 → 默认基准(选对标的:小市值/低波/动量 这类全市场选股偏小盘,
# 对标中证1000 比中证500 更贴切;否则默认中证500)
_STRATEGY_DEFAULT_BENCHMARK = {
    "small_cap": "000852",
    "low_vol":   "000852",
    "momentum":  "000852",
}


def _resolve_benchmark(strategy_id: str, override: Optional[str]) -> tuple[str, str]:
    """返回 (指数代码, 展示名)。override 优先,否则按策略默认,再否则中证500。"""
    code = override or _STRATEGY_DEFAULT_BENCHMARK.get(strategy_id, "000905")
    name = _BENCHMARK_NAMES.get(code, code)
    return code, f"{name}（基准）"


class PortfolioBacktestRequest(BaseModel):
    strategy_id: str
    params: Optional[Dict[str, Any]] = None
    label: Optional[str] = None
    start_date: str
    end_date: str
    initial_capital: float = 100_000
    slippage_rate: float = 0.0001
    benchmark_code: Optional[str] = None  # 不传则按策略选默认(小市值→中证1000)
    # 无偏回测:universe 用"期内任意日真实在区间"的历史池(消除幸存者偏差),
    # 而非今日快照池。默认开。代价:候选股更多 → 下载/回测更慢。
    point_in_time: bool = True
    allow_boards: Optional[List[str]] = None  # 如 ["main"];None=全部板块
    exclude_st: bool = True                   # 排除当前 ST(近似,无历史 ST 数据)


@app.post("/api/portfolio_backtest")
async def api_portfolio_backtest(
    req: PortfolioBacktestRequest,
    request: Request,
    _rl: None = Depends(_rate_limit_backtest),
):
    """
    Streams SSE progress events then a final result event.
    Event format:  data: <json>\n\n
    Types: "progress" {msg, pct?}  |  "result" {…}  |  "error" {msg}
    """
    # Pre-flight validation — fail fast with 400 before opening the stream
    _validate_date_range(req.start_date, req.end_date)
    if req.initial_capital <= 0:
        raise HTTPException(400, "初始资金必须大于 0")

    def _sse(payload: dict) -> str:
        # Strip any control chars from msg to prevent SSE-stream injection
        if isinstance(payload.get("msg"), str):
            payload["msg"] = payload["msg"].replace("\r", " ").replace("\n", " ")
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def generate():
        loop = asyncio.get_running_loop()

        # ── Validate strategy ────────────────────────────────────────────────
        try:
            strategy = get_portfolio_strategy(req.strategy_id, req.params)
        except ValueError as e:
            yield _sse({"type": "error", "msg": str(e)})
            return

        if req.label:
            strategy.name = req.label

        cap_min = float(strategy.params.get("cap_min", 20))
        cap_max = float(strategy.params.get("cap_max", 30))

        # ── Step 1: Get universe ─────────────────────────────────────────────
        if req.point_in_time:
            uni_msg = "正在构建历史股票池(无偏·期内真实市值)…"
        else:
            uni_msg = "正在获取全市场股票池(今日快照)…"
        yield _sse({"type": "progress", "msg": uni_msg, "indeterminate": True})
        try:
            if req.point_in_time:
                universe_df = await loop.run_in_executor(
                    None,
                    lambda: get_historical_universe(
                        cap_min, cap_max, req.start_date, req.end_date,
                        boards=req.allow_boards, exclude_st=req.exclude_st,
                    ),
                )
            else:
                universe_df = await loop.run_in_executor(
                    None, lambda: get_universe_stocks(cap_min, cap_max)
                )
        except Exception as e:
            yield _sse({"type": "error", "msg": _safe_detail("获取股票池失败，请稍后重试", e)})
            return

        if universe_df.empty:
            # 把空池转化为可操作的诊断（分布 + 建议范围 / 数据缺失提示）
            hint = await loop.run_in_executor(
                None, lambda: build_universe_hint(cap_min, cap_max)
            )
            yield _sse({"type": "error", "msg": hint})
            return

        codes = universe_df["code"].tolist()

        # ── Step 2: 批量加载行情 ──────────────────────────────────────────────
        # 行情已改"分批 IN 一次性查"(不再逐只返回),没有逐只进度可上报 →
        # 用不定量进度条(流动条纹),不再假装精确百分比卡在某个数字上。
        yield _sse({"type": "progress",
                    "msg": f"股票池共 {len(codes)} 只,批量加载历史行情中…",
                    "indeterminate": True})
        try:
            price_data: Dict[str, Any] = await loop.run_in_executor(
                None,
                lambda: download_universe_history(
                    codes, req.start_date, req.end_date),
            )
        except Exception as e:
            # 加载异常也要回一条 error,前端不会无限转圈(SSE 挂死防护)
            yield _sse({"type": "error", "msg": _safe_detail("加载历史行情失败，请稍后重试", e)})
            return

        if not price_data:
            yield _sse({"type": "error", "msg": "历史数据为空，请检查日期范围"})
            return

        # 回测计算本身也无法分步上报 → 同样走不定量进度
        yield _sse({"type": "progress", "msg": "正在运行回测策略…",
                    "indeterminate": True})

        # ── Step 3: 历史真实市值 —— 直接复用 price_data 已带回的 market_cap 列 ──
        # (不再对 stock_kline 二次全扫;纯内存转换,放 executor 避免阻塞事件循环)
        hist_market_caps = await loop.run_in_executor(
            None, lambda: build_hist_market_caps(price_data)
        )

        # ── Step 4: Run backtest ─────────────────────────────────────────────
        try:
            result = await loop.run_in_executor(
                None,
                lambda: run_portfolio_backtest(
                    ref_data=universe_df,
                    price_data=price_data,
                    strategy=strategy,
                    initial_capital=req.initial_capital,
                    slippage_rate=req.slippage_rate,
                    hist_market_caps=hist_market_caps or None,
                ),
            )
        except Exception as e:
            yield _sse({"type": "error", "msg": _safe_detail("回测执行失败，请稍后重试", e)})
            return

        # ── Step 4: 基准指数(按策略选默认,前端可 benchmark_code 覆盖)────
        bench_code, bench_name = _resolve_benchmark(
            req.strategy_id, req.benchmark_code
        )
        yield _sse({"type": "progress",
                    "msg": f"获取基准指数 {bench_name}…", "indeterminate": True})
        benchmark = await loop.run_in_executor(
            None,
            lambda: _build_index_benchmark(
                symbol=bench_code,
                name=bench_name,
                start_date=req.start_date,
                end_date=req.end_date,
                initial_capital=req.initial_capital,
            ),
        )

        yield _sse({
            "type": "result",
            "universe_count": len(universe_df),
            "downloaded_count": len(price_data),
            "cap_range": f"{cap_min}~{cap_max}亿",
            "results": [result],
            "benchmark": benchmark,
        })

    return StreamingResponse(generate(), media_type="text/event-stream")


def _build_index_benchmark(
    symbol: str,
    name: str,
    start_date: str,
    end_date: str,
    initial_capital: float,
) -> Optional[Dict[str, Any]]:
    """Build a buy-and-hold benchmark from an index."""
    idx_df = get_index_history(symbol, start_date, end_date)
    if idx_df is None or idx_df.empty:
        return None

    init_price = float(idx_df.iloc[0]["close"])
    equity_curve = [
        {
            "date": str(r["date"].date()),
            "value": round(initial_capital * float(r["close"]) / init_price, 2),
        }
        for r in idx_df.to_dict("records")
    ]

    # 风险指标走共用实现(engine/metrics.py);基准是买入持有,无 win_rate
    from .engine.metrics import compute_risk_metrics, compute_yearly_returns
    vals = [e["value"] for e in equity_curve]
    metrics = compute_risk_metrics(pd.Series(vals), initial_capital)
    metrics["win_rate"] = None
    metrics["trade_count"] = 1
    metrics["initial_capital"] = initial_capital

    return {
        "strategy_name": name,
        "metrics": metrics,
        "equity_curve": equity_curve,
        "yearly_returns": compute_yearly_returns(equity_curve, initial_capital),
        "trades": [],
    }
