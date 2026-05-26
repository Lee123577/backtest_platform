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
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from starlette.requests import Request

from ..data.data_loader import _get_pool
from ..visit_log import _client_ip

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
    logger.info("Admin IP added: %s by %s (%s)", ip, by_ip, note)


def remove_ip(ip: str) -> bool:
    """返回是否真删了一行（不存在则返回 False，不报错）。"""
    conn = _get_pool()
    conn.ping(reconnect=True)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM paper_admin_ip WHERE ip=%s", (ip[:45],))
        return cur.rowcount > 0


# ── FastAPI dependency ───────────────────────────────────────────────────────

def get_request_ip(request: Request) -> str:
    """取请求方真实 IP（复用 visit_log 的取法：XFF > X-Real-IP > peer）。"""
    return _client_ip(request)


def require_admin_ip(request: Request) -> str:
    """
    Dependency：校验请求 IP 是否在白名单。返回 IP 字符串。
    不在白名单则抛 403。

    First-run bootstrap：进程启动后首次写操作时，若表为空，自动把当前 IP
    加入并放行。仅触发一次（_first_run_checked），后续依然走 is_admin 校验。
    """
    global _first_run_checked
    ensure_table()
    ip = get_request_ip(request)

    if not _first_run_checked:
        _first_run_checked = True
        if count_ips() == 0:
            try:
                add_ip(ip, "first-run bootstrap", "system")
                logger.warning(
                    "⚠ First-run bootstrap：IP %s 已自动加入白名单。"
                    "如部署在公网，请尽快通过 UI 或 SQL 检查并清理意外的 IP。",
                    ip,
                )
                return ip
            except Exception as e:
                logger.error("First-run bootstrap 失败：%s", e)

    if not is_admin(ip):
        raise HTTPException(
            status_code=403,
            detail=f"IP {ip} 不在管理白名单，无权进行写操作。"
                   f"如果是新设备，请联系已授权的管理员添加。",
        )
    return ip
