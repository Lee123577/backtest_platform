"""
管理员 IP 白名单
================

只有白名单中的 IP 才能调用 paper_trading 写操作 / 手动触发定时任务。
读操作（GET）全部公开 — 只锁写。

表设计简单（项目本身是单人/小团队工具，无 user 概念）：

    paper_admin_ip:
      - ip            VARCHAR(45) PK   IPv4 / IPv6
      - note          VARCHAR(200)     备注（谁的电脑、什么用途）
      - created_at    DATETIME         自动
      - created_by_ip VARCHAR(45)      谁加的（也用 IP 标识，"system" 表示自动加入）

Bootstrap 机制（避免空表锁死）：
  1. 优先：进程启动时读 PAPER_ADMIN_INITIAL_IPS 环境变量
     （格式："1.2.3.4,5.6.7.8"，逗号分隔），INSERT IGNORE 写入
  2. 兜底：第一次有人触发写操作时，若表仍为空，把当前请求 IP 自动加入
     （打 WARNING 日志提醒；适合内部部署，公网部署建议显式设环境变量）
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from starlette.requests import Request

from ..data.data_loader import _get_pool
from ..visit_log import _client_ip, _is_private_ip

logger = logging.getLogger(__name__)


# ── DDL ──────────────────────────────────────────────────────────────────────

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS paper_admin_ip (
        ip             VARCHAR(45)   NOT NULL PRIMARY KEY,
        note           VARCHAR(200),
        created_at     DATETIME      DEFAULT CURRENT_TIMESTAMP,
        created_by_ip  VARCHAR(45)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='paper_trading & 定时任务的写操作白名单 IP'
    """,
]


# ── 模块级状态：避免每次请求都跑 ensure / bootstrap 检查 ──────────────────────

_table_ready = False
_env_bootstrap_done = False
_first_run_checked = False
_first_run_lock = threading.Lock()   # 并发首个写请求只允许一个做 bootstrap


def ensure_table() -> None:
    """启动时确保表存在；进程内只跑一次。"""
    global _table_ready
    if _table_ready:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 paper_admin_ip 表")
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)
    _table_ready = True
    _maybe_bootstrap_from_env()
    logger.info("paper_admin_ip 表已就绪")


def _maybe_bootstrap_from_env() -> None:
    """读环境变量 PAPER_ADMIN_INITIAL_IPS，把里面的 IP INSERT IGNORE 进表。"""
    global _env_bootstrap_done
    if _env_bootstrap_done:
        return
    _env_bootstrap_done = True

    raw = os.environ.get("PAPER_ADMIN_INITIAL_IPS", "").strip()
    if not raw:
        return
    ips = [x.strip() for x in raw.split(",") if x.strip()]
    if not ips:
        return

    conn = _get_pool()
    if conn is None:
        return
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        for ip in ips:
            cur.execute(
                "INSERT IGNORE INTO paper_admin_ip (ip, note, created_by_ip) "
                "VALUES (%s, %s, %s)",
                (ip[:45], "env:PAPER_ADMIN_INITIAL_IPS", "system"),
            )
    logger.info("Bootstrap admin IPs from env: %s", ips)


# ── CRUD ─────────────────────────────────────────────────────────────────────

def list_ips() -> List[Dict[str, Any]]:
    conn = _get_pool()
    if conn is None:
        return []
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ip, note, created_at, created_by_ip "
            "FROM paper_admin_ip ORDER BY created_at"
        )
        return cur.fetchall()


def is_admin(ip: Optional[str]) -> bool:
    if not ip:
        return False
    conn = _get_pool()
    if conn is None:
        return False
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM paper_admin_ip WHERE ip=%s LIMIT 1", (ip[:45],)
        )
        return cur.fetchone() is not None


# ── 缓存版（中间件高频调用，避免逐请求 SELECT）────────────────────────────────
import time as _time  # noqa: E402

_admin_set_cache: set[str] = set()
_admin_cache_loaded_at: float = 0.0
_ADMIN_CACHE_TTL = 60  # 秒


def _refresh_admin_cache() -> None:
    global _admin_set_cache, _admin_cache_loaded_at
    now = _time.time()
    if now - _admin_cache_loaded_at <= _ADMIN_CACHE_TTL:
        return
    try:
        conn = _get_pool()
        if conn is not None:
            conn.ping(reconnect=True)
            with conn.cursor() as cur:
                cur.execute("SELECT ip FROM paper_admin_ip")
                _admin_set_cache = {
                    (r["ip"] if isinstance(r, dict) else r[0])
                    for r in cur.fetchall()
                }
            _admin_cache_loaded_at = now
    except Exception as e:
        logger.warning("刷新 admin IP 缓存失败: %s", e)
        # 不更新时间戳，下次请求会重试


def is_admin_cached(ip: Optional[str]) -> bool:
    """
    缓存版 is_admin —— TTL 60s 内重复请求不打 DB。
    主要给 VisitLogMiddleware 用：每个请求都跑，不能逐请求查表。

    新增/删除 admin IP 时调 invalidate_admin_cache() 让下次请求立即重载。
    """
    if not ip:
        return False
    _refresh_admin_cache()
    return ip in _admin_set_cache


def whitelist_empty_cached() -> bool:
    """基于 is_admin_cached 同一份缓存判断白名单是否为空，不再单独查一次 COUNT(*)。"""
    _refresh_admin_cache()
    return len(_admin_set_cache) == 0


def invalidate_admin_cache() -> None:
    """add/remove 后立即过期缓存。下次请求会重新加载。"""
    global _admin_cache_loaded_at
    _admin_cache_loaded_at = 0.0


def count_ips() -> int:
    conn = _get_pool()
    if conn is None:
        return 0
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM paper_admin_ip")
        row = cur.fetchone()
        return int(row["c"]) if row else 0


def add_ip(ip: str, note: str, by_ip: str) -> None:
    """添加（或更新备注）。允许覆盖 note，不算"删了再加"。"""
    ip = (ip or "").strip()
    if not ip:
        raise ValueError("IP 不能为空")
    if len(ip) > 45:
        raise ValueError("IP 长度超过 45 字符")
    # 基本格式校验（简单版，允许 IPv4 / IPv6 / 内网）
    import ipaddress
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise ValueError(f"无效的 IP 地址：{ip}")

    conn = _get_pool()
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO paper_admin_ip (ip, note, created_by_ip) "
            "VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE note=VALUES(note)",
            (ip[:45], (note or "")[:200], (by_ip or "")[:45]),
        )
    invalidate_admin_cache()
    logger.info("Admin IP added: %s by %s (%s)", ip, by_ip, note)


def remove_ip(ip: str) -> bool:
    """返回是否真删了一行（不存在则返回 False，不报错）。"""
    conn = _get_pool()
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM paper_admin_ip WHERE ip=%s", (ip[:45],))
        ok = cur.rowcount > 0
    if ok:
        invalidate_admin_cache()
    return ok


# ── FastAPI dependency ───────────────────────────────────────────────────────

def get_request_ip(request: Request) -> str:
    """取请求方真实 IP（复用 visit_log 的取法：XFF > X-Real-IP > peer）。"""
    return _client_ip(request)


def _is_cross_site(source: str, host: str) -> bool:
    """判断 Origin/Referer 是否与本站 Host 不同源。

    只比 netloc(域名:端口),不比协议 —— 反代 TLS 终结后应用侧看到的是
    http,而浏览器 Origin 是 https,比协议会把正常同源请求全拒掉。
    """
    from urllib.parse import urlparse
    try:
        netloc = urlparse(source).netloc
    except ValueError:
        return True
    # netloc 为空(如 Origin: null、畸形值)一律视为跨站
    return not netloc or netloc.lower() != (host or "").lower()


def _reject_cross_site(request: Request) -> None:
    """以 IP 为身份的写接口的 CSRF 防线:带 Origin/Referer 的浏览器请求必须同源。

    IP 只认来源地址、不认 cookie,所以 SameSite 保护不了它 —— 管理员(或同一
    出口 IP 的任何人)浏览恶意网页时,跨站表单 POST 是简单请求、无预检,能直接
    命中无 body 的写接口(如 /api/tasks/{name}/run)。同样的道理也适用于按 IP
    存的访客看板布局(/api/my_board/layout)。
    curl/脚本不带 Origin/Referer,不受影响,仍走各自的身份校验。
    """
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    if not source:
        return
    host = request.headers.get("host") or ""
    if _is_cross_site(source, host):
        raise HTTPException(
            status_code=403,
            detail="跨站请求被拒绝:该写操作只接受本站页面或无 Origin 的脚本调用。",
        )


# 给模块外用的公开名(my_board 的访客布局写入也要这道防线)。
# 本模块内部沿用 _reject_cross_site,测试也钉的是那个名字。
reject_cross_site = _reject_cross_site


def require_admin_ip(request: Request) -> str:
    """
    Dependency：校验请求 IP 是否在白名单。返回 IP 字符串。
    不在白名单则抛 403。

    First-run bootstrap：进程启动后首次写操作时，若表为空，自动把当前 IP
    加入并放行。仅触发一次（_first_run_checked），后续依然走 is_admin 校验。
    """
    global _first_run_checked
    _reject_cross_site(request)
    ensure_table()
    ip = get_request_ip(request)

    if not _first_run_checked:
        with _first_run_lock:   # 双检:并发的两个首个写请求不能都走 bootstrap
            if not _first_run_checked:
                if count_ips() == 0:
                    # 安全加固：仅在来自内网/本机的首个写请求上自动自举，
                    # 公网首个访问者无法借此成为管理员。反代部署必须配置
                    # TRUSTED_PROXIES，否则直连 IP 为代理地址，需由内网访问初始化。
                    if _is_private_ip(ip):
                        try:
                            add_ip(ip, "first-run bootstrap", "system")
                            _first_run_checked = True
                            logger.warning(
                                "⚠ First-run bootstrap：内网 IP %s 已自动加入白名单。"
                                "如非预期，请通过 UI 或 SQL 清理。",
                                ip,
                            )
                            return ip
                        except Exception as e:
                            logger.error("First-run bootstrap 失败：%s", e)
                    # 注意：这里不能把 _first_run_checked 设为 True——公网 IP
                    # (或内网自举失败)不代表白名单已初始化完成，必须留给后续
                    # 内网请求重试自举的机会，否则白名单会永久卡在空表状态。
                    raise HTTPException(
                        403,
                        detail="管理白名单为空且未配置 PAPER_ADMIN_INITIAL_IPS。"
                               "请从内网/本机访问完成初始化，或在 .env 设置"
                               " PAPER_ADMIN_INITIAL_IPS 后再试。",
                    )
                _first_run_checked = True

    if not is_admin(ip):
        raise HTTPException(
            status_code=403,
            detail=f"IP {ip} 不在管理白名单，无权进行写操作。"
                   f"如果是新设备，请联系已授权的管理员添加。",
        )
    return ip


def require_admin_ip_no_bootstrap(request: Request) -> str:
    """同 require_admin_ip，但**不走首次自举**。

    给那些"普通访客也会打到"的写接口用(如 /api/my_board/layout —— 拖一下
    卡片就会 POST)。自举分支只该由管理员自觉访问管理端点来触发;挂在访客
    随手就能碰到的接口上，等于把"白名单为空 + 客户端 IP 看着像内网"这个
    组合的暴露面无谓地放大了(反代未配 TRUSTED_PROXIES 时,所有请求的
    直连 IP 都是 127.0.0.1,正好命中 _is_private_ip)。
    """
    _reject_cross_site(request)
    ensure_table()
    ip = get_request_ip(request)
    if not is_admin(ip):
        raise HTTPException(
            status_code=403,
            detail=f"IP {ip} 不在管理白名单，无权进行此写操作。",
        )
    return ip
