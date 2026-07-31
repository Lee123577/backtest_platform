"""
共享滑动窗口限流器(app/ratelimit.py)。

这份实现是从五处一模一样的复制品收拢来的，下面的用例钉住收拢前后必须一致的
语义 —— 尤其是"被拒绝的请求不计入窗口"这条：写反了会让被限流的客户端越刷
越解不了封。
"""
import threading

import pytest

from app.ratelimit import SlidingWindowLimiter


def test_allows_up_to_limit_then_rejects():
    lim = SlidingWindowLimiter(limit=3, window_sec=60)
    assert [lim.allow("1.2.3.4") for _ in range(3)] == [True, True, True]
    assert lim.allow("1.2.3.4") is False


def test_keys_are_independent():
    lim = SlidingWindowLimiter(limit=1, window_sec=60)
    assert lim.allow("a") is True
    assert lim.allow("a") is False
    assert lim.allow("b") is True      # 换个 key 不受影响


def test_rejected_request_is_not_counted(monkeypatch):
    """被拒的请求不能把窗口往后推 —— 否则狂刷的客户端永远等不到解封。

    limit=1、窗口 10s：t=0 放行一次；t=5 被拒(不计入)；到 t=11 时
    第一次那笔已滑出窗口，必须重新放行。若拒绝也计入，t=5 那笔会把窗口
    推到 t=15，此时仍被拒。
    """
    now = [0.0]
    monkeypatch.setattr("app.ratelimit.time.time", lambda: now[0])

    lim = SlidingWindowLimiter(limit=1, window_sec=10)
    assert lim.allow("ip") is True
    now[0] = 5.0
    assert lim.allow("ip") is False
    now[0] = 11.0
    assert lim.allow("ip") is True


def test_window_slides(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("app.ratelimit.time.time", lambda: now[0])

    lim = SlidingWindowLimiter(limit=2, window_sec=60)
    assert lim.allow("ip") is True
    assert lim.allow("ip") is True
    assert lim.allow("ip") is False
    now[0] = 61.0                      # 两笔都滑出窗口
    assert lim.allow("ip") is True


def test_sweep_reclaims_idle_keys(monkeypatch):
    """空闲 key 要被回收 —— 收拢前 feedback 那份没有清扫,字典只增不减。"""
    now = [0.0]
    monkeypatch.setattr("app.ratelimit.time.time", lambda: now[0])

    lim = SlidingWindowLimiter(limit=5, window_sec=60, sweep_interval_sec=300)
    for i in range(100):
        lim.allow(f"ip-{i}")
    assert lim.tracked_keys() == 100

    now[0] = 400.0                     # 超过清扫间隔,且这些 key 全已过窗口
    lim.allow("someone-else")          # 清扫搭在正常调用上,不起后台线程
    assert lim.tracked_keys() == 1


def test_sweep_does_not_drop_active_keys(monkeypatch):
    """清扫只能删窗口内已无记录的 key,不能误伤正在计数的。"""
    now = [0.0]
    monkeypatch.setattr("app.ratelimit.time.time", lambda: now[0])

    lim = SlidingWindowLimiter(limit=5, window_sec=600, sweep_interval_sec=100)
    lim.allow("busy")
    now[0] = 200.0                     # 过了清扫间隔，但没过 600s 的窗口
    lim.allow("busy")
    assert lim.tracked_keys() == 1
    assert lim.allow("busy") is True   # 计数仍在，没被清扫抹掉


def test_retry_after_zero_when_not_limited():
    lim = SlidingWindowLimiter(limit=3, window_sec=60)
    lim.allow("ip")
    assert lim.retry_after("ip") == 0
    assert lim.retry_after("从没见过的key") == 0


def test_retry_after_positive_when_limited(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("app.ratelimit.time.time", lambda: now[0])

    lim = SlidingWindowLimiter(limit=1, window_sec=60)
    lim.allow("ip")
    now[0] = 10.0
    assert lim.allow("ip") is False
    ra = lim.retry_after("ip")
    assert 0 < ra <= 60                # 还剩 ~50s


def test_reset_clears_everything():
    lim = SlidingWindowLimiter(limit=1, window_sec=60)
    lim.allow("ip")
    assert lim.allow("ip") is False
    lim.reset()
    assert lim.allow("ip") is True


def test_concurrent_allow_never_exceeds_limit():
    """并发下放行总数不能超过 limit。

    FastAPI 把同步路由丢进 anyio 线程池，多个线程会同时进来 ——
    收拢前 feedback / auth 两份是没加锁的。
    """
    lim = SlidingWindowLimiter(limit=50, window_sec=60)
    granted = []
    lock = threading.Lock()

    def worker():
        for _ in range(20):
            if lim.allow("same-ip"):
                with lock:
                    granted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == 50          # 200 次尝试，恰好放行 50


@pytest.mark.parametrize("limit", [0, 1])
def test_limit_edge_values(limit):
    lim = SlidingWindowLimiter(limit=limit, window_sec=60)
    results = [lim.allow("ip") for _ in range(3)]
    assert results.count(True) == limit
