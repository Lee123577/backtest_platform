"""
按 key(通常是 IP)的滑动窗口限流器 —— 单一事实来源
====================================================

这段算法此前在五处各写了一份,一字不差:
  - main.py            回测接口       6 次 / 60s
  - analytics/api.py   埋点上报      30 次 / 60s
  - share/api.py       创建分享       5 次 / 60s
  - feedback/service.py 意见反馈       5 次 / 3600s
  - auth/service.py    短信发码      20 次 / 3600s

五份复制品还各自退化出了不同的毛病:share 那份每个请求都全量扫一遍字典
(没有清扫节流),feedback 那份**根本不清扫** —— 每来一个新访客 IP 就在字典里
留一个条目再也不删,公网跑久了就是只增不减的慢性内存增长。收拢到这里之后
这些问题只需要修一次。

语义(与收拢前逐字一致,不要改):
  - 窗口内已有 >= limit 次记录 → 拒绝,且**本次不计入**(否则被限流的客户端
    狂刷会不断把窗口往后推,永远解不了封)
  - 放行才 append 时间戳
  - 清扫只删"窗口内已无任何记录"的 key,不影响活跃 key 的判定

线程安全:FastAPI 把同步路由丢进 anyio 线程池,多个线程会同时进来。
收拢前 feedback / auth 两份是没有加锁的(靠 GIL 侥幸),这里统一上锁。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict

DEFAULT_SWEEP_INTERVAL = 300.0


class SlidingWindowLimiter:
    """进程内滑动窗口计数器。

    进程内 = 多 worker 部署时每个 worker 各算各的,实际放行量是 limit × worker 数。
    本站单 worker 常驻,够用;真要精确全局限流得挪到 Redis。

    Args:
        limit:         窗口内允许的最大次数
        window_sec:    窗口长度(秒)
        sweep_interval_sec: 多久清扫一次空闲 key(秒)。清扫在已持锁的路径里顺手做,
                       不额外加锁,也不起后台线程。
        name:          仅用于调试/可观测,不参与逻辑
    """

    __slots__ = ("limit", "window_sec", "sweep_interval_sec", "name",
                 "_hits", "_lock", "_last_sweep")

    def __init__(self, limit: int, window_sec: float,
                 sweep_interval_sec: float = DEFAULT_SWEEP_INTERVAL,
                 name: str = "") -> None:
        self.limit = limit
        self.window_sec = float(window_sec)
        self.sweep_interval_sec = float(sweep_interval_sec)
        self.name = name
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    def _sweep(self, now: float) -> None:
        """清掉窗口内已无记录的 key。调用方必须已持锁。"""
        if now - self._last_sweep < self.sweep_interval_sec:
            return
        self._last_sweep = now
        stale = [k for k, dq in self._hits.items()
                 if not dq or now - dq[-1] > self.window_sec]
        for k in stale:
            del self._hits[k]

    def allow(self, key: str) -> bool:
        """放行返回 True(并记一次),超限返回 False(不记)。"""
        now = time.time()
        with self._lock:
            self._sweep(now)
            dq = self._hits.setdefault(key, deque())
            while dq and now - dq[0] > self.window_sec:
                dq.popleft()
            if len(dq) >= self.limit:
                return False
            dq.append(now)
            return True

    def retry_after(self, key: str) -> int:
        """还要等几秒才会有名额。给 429 响应配 Retry-After 用;没被限流则 0。"""
        now = time.time()
        with self._lock:
            dq = self._hits.get(key)
            if not dq or len(dq) < self.limit:
                return 0
            return max(1, int(self.window_sec - (now - dq[0])) + 1)

    def reset(self) -> None:
        """清空全部计数。给测试用例逐条隔离用。"""
        with self._lock:
            self._hits.clear()
            self._last_sweep = 0.0

    def tracked_keys(self) -> int:
        """当前字典里有多少 key —— 观察清扫是否真的在回收。"""
        with self._lock:
            return len(self._hits)
