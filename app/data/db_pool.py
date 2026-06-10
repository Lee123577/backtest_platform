"""
MySQL 连接池
============

设计动机
---------
旧实现 ``data_loader._get_pool()`` 用 ``threading.local`` 给每个线程留一个
**永不关闭**的 pymysql 连接。FastAPI 默认线程池 + 异步 ``run_in_executor``
长期运行后,连接数会无限增长直到 MySQL ``max_connections`` 报错;线程死亡
后连接也不会归还。

这里加一层真池(基于 ``queue.Queue``),关键约束:

  - **maxconnections 上限**:全进程同一时刻至多 ``MAX_CONNECTIONS`` 个 pymysql
    连接存活,超过则借连接的调用方阻塞 / 超时
  - **借/还接口**:``borrow()`` / ``release()``,新代码用 ``get_conn()`` 上下文管理
  - **进程退出清理**:``atexit`` 关掉所有空闲连接
  - **坏连接自动剔除**:``borrow()`` 出来时 ``ping``,失败则关掉 + 减计数

旧代码继续通过 ``_get_pool()`` 拿到 thread-local 连接(背后从池借,**不归还**
直到进程退出)。因为 FastAPI 工作线程数有限(默认 ≤ 32),正常情况下不会
超过 MAX_CONNECTIONS。
"""
from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
from contextlib import contextmanager
from typing import Optional

import pymysql
from pymysql.cursors import DictCursor

from ..config import settings

logger = logging.getLogger(__name__)

# 上限 50:必须 ≥ 进程最大并发线程数,否则 _get_pool() 的"每线程长持"模式
# 会耗尽池。本应用 workers=1,线程来源 = anyio sync-route 线程池(默认上限 40)
# + asyncio 默认 executor(2 核机约 6)≈ 46 峰值。设 50 留头(实际常驻仅 ~6-10
# 条,50 只是天花板,不会预先建满)。MySQL 默认 max_connections=151,50 安全。
# 真正高并发再用 get_conn() 上下文管理器(借→还,不长持)。
MAX_CONNECTIONS: int = int(os.environ.get("DB_MAX_CONNECTIONS", "50"))
BORROW_TIMEOUT: float = float(os.environ.get("DB_BORROW_TIMEOUT", "30"))

_pool: "queue.Queue" = queue.Queue(maxsize=MAX_CONNECTIONS)
_created_count: int = 0
_lock = threading.Lock()


def _new_connection() -> Optional["pymysql.connections.Connection"]:
    try:
        return pymysql.connect(
            host=settings.MYSQL_HOST,
            port=settings.MYSQL_PORT,
            user=settings.MYSQL_USER,
            password=settings.MYSQL_PASSWORD,
            database=settings.MYSQL_DATABASE,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
        )
    except Exception as e:
        logger.error(
            "创建 MySQL 连接失败 (host=%s db=%s): %s",
            settings.MYSQL_HOST, settings.MYSQL_DATABASE, e,
        )
        return None


def _discard(conn) -> None:
    """关掉坏连接 + 减计数。"""
    global _created_count
    with _lock:
        _created_count = max(0, _created_count - 1)
    try:
        conn.close()
    except Exception:
        pass


def borrow(timeout: float = BORROW_TIMEOUT) -> Optional["pymysql.connections.Connection"]:
    """
    从池里借一个连接。

    路径:
      1. 池里有空闲 → ping 后返回(坏连接丢弃,继续走 2/3)
      2. 池空 + 已创建数 < MAX → 新建
      3. 池空 + 已到上限 → 阻塞等到 ``timeout`` 秒,超时返回 None
    """
    global _created_count
    # ── 1. 池里有空闲 ───────────────────────────────────────────────────
    try:
        conn = _pool.get_nowait()
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            _discard(conn)
    except queue.Empty:
        pass

    # ── 2. 新建(未到上限)────────────────────────────────────────────────
    with _lock:
        if _created_count < MAX_CONNECTIONS:
            conn = _new_connection()
            if conn is not None:
                _created_count += 1
                return conn

    # ── 3. 已到上限:阻塞等 ───────────────────────────────────────────────
    try:
        conn = _pool.get(timeout=timeout)
    except queue.Empty:
        logger.error(
            "连接池耗尽 (MAX=%d, in_use=%d),等待 %ss 超时",
            MAX_CONNECTIONS, _created_count, timeout,
        )
        return None
    try:
        conn.ping(reconnect=True)
        return conn
    except Exception:
        _discard(conn)
        return None


def release(conn) -> None:
    """归还连接到池。池满则关掉。"""
    if conn is None:
        return
    try:
        _pool.put_nowait(conn)
    except queue.Full:
        _discard(conn)


def discard(conn) -> None:
    """显式扔掉坏连接 + 减计数。调用方已知连接不可用时用。"""
    if conn is None:
        return
    _discard(conn)


@contextmanager
def get_conn(timeout: float = BORROW_TIMEOUT):
    """
    借一个连接,with 退出时自动归还。**新代码请用这个接口。**

    用法::

        from app.data.db_pool import get_conn

        with get_conn() as conn:
            if conn is None:
                return  # DB 不可用
            with conn.cursor() as cur:
                cur.execute(...)
    """
    conn = borrow(timeout=timeout)
    try:
        yield conn
    finally:
        release(conn)


def stats() -> dict:
    """诊断用:返回池当前状态。"""
    with _lock:
        created = _created_count
    idle = _pool.qsize()
    return {
        "max": MAX_CONNECTIONS,
        "created": created,
        "idle_in_pool": idle,
        "in_use": max(0, created - idle),
    }


@atexit.register
def _cleanup_on_exit() -> None:
    """进程退出时关掉所有空闲连接。借出去的连接靠 OS 回收。"""
    while True:
        try:
            conn = _pool.get_nowait()
        except queue.Empty:
            break
        try:
            conn.close()
        except Exception:
            pass
