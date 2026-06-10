"""
连接池 db_pool —— 上限 / 背压 / 借还 / 坏连接剔除。
用 mock 连接,不连真 MySQL。
"""
import queue
import threading
import time

import pytest

from app.data import db_pool


class _MockConn:
    def __init__(self):
        self.closed = False

    def ping(self, reconnect=True):
        if self.closed:
            raise RuntimeError("dead connection")

    def close(self):
        self.closed = True


@pytest.fixture
def fresh_pool(monkeypatch):
    """每个测试重置池状态 + mock 连接工厂,MAX=3 便于测上限。"""
    monkeypatch.setattr(db_pool, "_new_connection", lambda: _MockConn())
    monkeypatch.setattr(db_pool, "MAX_CONNECTIONS", 3)
    monkeypatch.setattr(db_pool, "_pool", queue.Queue(maxsize=3))
    monkeypatch.setattr(db_pool, "_created_count", 0)
    yield


def test_borrow_creates_up_to_max(fresh_pool):
    conns = [db_pool.borrow(timeout=0.5) for _ in range(3)]
    assert all(c is not None for c in conns)
    assert db_pool.stats()["created"] == 3


def test_backpressure_blocks_then_times_out(fresh_pool):
    held = [db_pool.borrow(timeout=0.5) for _ in range(3)]  # 占满
    assert all(c is not None for c in held)
    # 第 4 个:池空 + 已达上限 → 阻塞到超时 → None
    t0 = time.time()
    extra = db_pool.borrow(timeout=0.5)
    assert extra is None
    assert time.time() - t0 >= 0.4  # 确实等了 timeout


def test_release_returns_to_pool(fresh_pool):
    c = db_pool.borrow(timeout=0.5)
    assert db_pool.stats()["in_use"] == 1
    db_pool.release(c)
    assert db_pool.stats()["idle_in_pool"] == 1
    # 再借应复用同一条,不新建
    c2 = db_pool.borrow(timeout=0.5)
    assert c2 is c
    assert db_pool.stats()["created"] == 1


def test_get_conn_auto_returns(fresh_pool):
    with db_pool.get_conn(timeout=0.5) as c:
        assert c is not None
        assert db_pool.stats()["in_use"] == 1
    # 退出 with 后自动归还
    assert db_pool.stats()["idle_in_pool"] == 1


def test_dead_connection_discarded_on_borrow(fresh_pool):
    c = db_pool.borrow(timeout=0.5)
    c.close()                 # 模拟连接死掉
    db_pool.release(c)        # 归还坏连接
    # 再借:ping 失败 → discard → 新建一条好的
    c2 = db_pool.borrow(timeout=0.5)
    assert c2 is not None and not c2.closed


def test_concurrent_borrow_respects_cap(fresh_pool):
    """5 线程并发借,MAX=3 → 恰好 3 成功 2 超时。"""
    results = []

    def worker():
        c = db_pool.borrow(timeout=0.5)
        results.append(c is not None)
        if c is not None:
            time.sleep(1.0)   # 持有,逼后来者背压
            db_pool.release(c)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 3
    assert results.count(False) == 2
