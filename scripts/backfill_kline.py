"""
stock_kline 补全脚本（资源友好版）
==================================
仅运行 update_kline，逐日补全 stock_kline 数据，不触发 index/north/finance/dividend 等其他步骤。

用法：
  python3 scripts/backfill_kline.py 2026-05-18 2026-05-29
  python3 scripts/backfill_kline.py 2026-05-26          # 单日

可选环境变量：
  SKIP_EM=1        跳过 EM 探测，全走 sina（云服务器推荐）
  MAX_WORKERS=4    sina 并发数，**默认 4，硬上限 8**（保护 FastAPI 不被挤死）
  SKIP_SNAP=1      跳过估值快照（market_cap/pe/pb 写 NULL，单纯补 K 线时最快）
  NICE=10          进程 niceness，越大越不抢 CPU。默认 10，FastAPI 默认 0 = 优先

资源保护机制（自动开启，无需配置）：
  - **锁文件** /tmp/backfill_kline.lock：同时只允许一个实例跑，避免重复进程叠加压垮内存
  - **os.nice(10)**：把自己优先级降到 FastAPI 之下，被抢占不卡服务
  - **MAX_WORKERS 硬上限 8**：超过 8 会被强制裁到 8 并打 WARNING
  - **每 500 只 micro-sleep 0.3s**：给 FastAPI 留 CPU 周期

依赖 daily_update.py 中已修复的 _fetch_one（EM 自动降级到 sina）。
"""
import argparse
import atexit
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

# 在 import 任何 requests/urllib 相关模块之前一次性清空代理 env，
# 避免后续多线程并发时 _call_no_proxy 反复修改 os.environ 引发竞态
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)


# ── 资源保护：lock file + nice ─────────────────────────────────────────────────

LOCK_PATH = Path(os.environ.get("BACKFILL_LOCK", "/tmp/backfill_kline.lock"))
WORKER_HARD_CAP = 8


def _acquire_lock():
    """简单 PID 锁。已有进程在跑则退出，避免叠加运行压垮服务器。"""
    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text().strip())
            # 检查 PID 是否还活着（Linux）
            try:
                os.kill(old_pid, 0)
                alive = True
            except (ProcessLookupError, PermissionError):
                alive = False
            except Exception:
                alive = True   # 不确定就保守认为还活着

            if alive:
                print(
                    f"❌ 已有 backfill_kline 进程在跑 (PID={old_pid})，本次退出。\n"
                    f"   如确认是僵尸锁，删除 {LOCK_PATH} 后重试。",
                    file=sys.stderr,
                )
                sys.exit(2)
            else:
                print(
                    f"⚠️ 检测到陈旧锁 (PID={old_pid} 已不存在)，清理后继续",
                    file=sys.stderr,
                )
        except (ValueError, OSError):
            pass  # 锁文件破损，覆盖之

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(str(os.getpid()))
    atexit.register(_release_lock)


def _release_lock():
    try:
        if LOCK_PATH.exists():
            content = LOCK_PATH.read_text().strip()
            if content == str(os.getpid()):
                LOCK_PATH.unlink()
    except Exception:
        pass


def _self_nice():
    """把自己优先级降低，给 FastAPI 让 CPU。Windows 无 nice，跳过。"""
    if not hasattr(os, "nice"):
        return
    try:
        n = int(os.environ.get("NICE", "10"))
        os.nice(n)
    except (OSError, ValueError) as e:
        print(f"设置 nice 失败（忽略）: {e}", file=sys.stderr)


def _cap_workers():
    """把 MAX_WORKERS 硬裁到上限，避免用户传 50 直接打满服务器。"""
    cur = int(os.environ.get("MAX_WORKERS", "4"))
    if cur > WORKER_HARD_CAP:
        print(
            f"⚠️ MAX_WORKERS={cur} 超过安全上限 {WORKER_HARD_CAP}，"
            f"已自动降到 {WORKER_HARD_CAP}（保护 FastAPI）",
            file=sys.stderr,
        )
        os.environ["MAX_WORKERS"] = str(WORKER_HARD_CAP)
    elif cur < 1:
        os.environ["MAX_WORKERS"] = "1"


def _print_resource_info():
    """启动时打印一行资源状态，方便用户判断服务器负载。"""
    try:
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF)
        rss_mb = ru.ru_maxrss / 1024
        print(f"📊 启动 RSS ≈ {rss_mb:.1f} MB", file=sys.stderr)
    except Exception:
        pass

    # 尝试报告系统负载（Linux）
    try:
        load1, load5, load15 = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        print(
            f"📊 系统负载 1/5/15min = {load1:.2f}/{load5:.2f}/{load15:.2f} "
            f"(CPU={cpu_count} 核)，若 1min 已 > {cpu_count} 建议先停 FastAPI 再跑",
            file=sys.stderr,
        )
    except (AttributeError, OSError):
        pass


# 先做资源保护，再加载重量级 daily_update 模块（避免 lock 失败白白 import）
_acquire_lock()
_self_nice()
_cap_workers()
_print_resource_info()


import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("daily_update", ROOT / "scripts" / "daily_update.py")
du = importlib.util.module_from_spec(spec)
spec.loader.exec_module(du)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("start", help="开始日期 YYYY-MM-DD")
    p.add_argument("end", nargs="?", help="结束日期 YYYY-MM-DD（不填则只跑 start）")
    args = p.parse_args()

    d0 = date.fromisoformat(args.start)
    d1 = date.fromisoformat(args.end) if args.end else d0

    # 一次性拉估值快照，注入到 daily_update 模块复用，避免逐日 30 秒 EM 超时
    if os.getenv("SKIP_SNAP"):
        du.log.info("SKIP_SNAP=1，估值快照置空，market_cap/pe/pb 将写 NULL")
        du._cached_snap = {}
    else:
        du.log.info("一次性拉取估值快照（整个 backfill 复用）…")
        du._cached_snap = du._get_valuation_snap()
        du.log.info(f"估值快照已缓存: {len(du._cached_snap)} 只")

    conn = du.get_conn()
    try:
        cur = d0
        while cur <= d1:
            if not du.is_trading_day(cur):
                du.log.info(f"跳过非交易日 {cur}")
                cur += timedelta(days=1)
                continue
            try:
                du.update_kline(conn, cur.strftime("%Y-%m-%d"))
            except Exception as e:
                du.log.error(f"{cur} update_kline 异常: {e}", exc_info=True)
            cur += timedelta(days=1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
