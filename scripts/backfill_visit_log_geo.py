"""
回填 back_test.user_visit_log 的地理位置字段
=========================================

扫描 ``country IS NULL`` 的行，用 ip2region 离线 xdb 查询出
country / region / city / isp 并写回数据库。完全离线，不走网络。

用法
----
.. code-block:: bash

    # 处理所有待回填行
    python scripts/backfill_visit_log_geo.py

    # 限制本次最多处理多少行（用于试跑 / 防止单次过长）
    python scripts/backfill_visit_log_geo.py --limit 5000

    # 指定 xdb 文件位置（默认 data/ip2region_v4.xdb）
    python scripts/backfill_visit_log_geo.py --xdb /path/to/ip2region_v4.xdb

规则
----
* 内网 / 保留地址（ip2region 返回 Reserved，或本地匹配 RFC 1918）→ country='内网'
* 无效 IP（解析失败 / 'unknown'）                                   → country='Unknown'
* 查询为空                                                          → country='Unknown'
* 有效返回                                                          → 拆 ``|`` 后入库
* 全部用 country 字段非 NULL 来标识"已处理"，避免下次重复扫描
"""
from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

# 让脚本能找到 app 包
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ip2region.util as ip_util             # noqa: E402
import ip2region.searcher as ip_xdb          # noqa: E402

from app.data.data_loader import _get_pool   # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill_geo")


DEFAULT_XDB = ROOT / "data" / "ip2region_v4.xdb"
LAN = "内网"
UNKNOWN = "Unknown"


# ─────────────────────────── 工具函数 ───────────────────────────

def _is_private_ip(ip: str) -> bool:
    """RFC 1918 / loopback / link-local 等内网地址。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _parse_region(region_str: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    把 ip2region 的 ``country|region|city|isp|cc`` 拆成 (country, region, city, isp)。

    字段为 "0" 视为缺失返回 None。
    """
    if not region_str:
        return None, None, None, None

    parts = region_str.split("|")
    # 兼容长度不足的情况
    while len(parts) < 5:
        parts.append("0")

    def _norm(v: str) -> Optional[str]:
        v = (v or "").strip()
        return None if v in ("", "0") else v

    country = _norm(parts[0])
    region  = _norm(parts[1])
    city    = _norm(parts[2])
    isp     = _norm(parts[3])

    # ip2region 对保留地址返回 "Reserved|Reserved|Reserved|0|0"
    if country and country.lower() == "reserved":
        return LAN, None, None, None

    return country, region, city, isp


# ─────────────────────────── 主流程 ─────────────────────────────

def backfill(xdb_path: Path, limit: Optional[int], batch_size: int = 500) -> None:
    if not xdb_path.exists():
        raise FileNotFoundError(f"xdb 文件不存在: {xdb_path}")

    logger.info("校验 xdb 文件: %s", xdb_path)
    ip_util.verify_from_file(str(xdb_path))

    logger.info("加载 xdb 到内存（content 缓存模式，线程安全）…")
    buf = ip_util.load_content_from_file(str(xdb_path))
    searcher = ip_xdb.new_with_buffer(ip_util.IPv4, buf)

    conn = _get_pool()
    if conn is None:
        raise RuntimeError("无法连接 MySQL")
    conn.ping(reconnect=True)

    # 统计待处理行数
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM back_test.user_visit_log WHERE country IS NULL")
        total_pending = int(cur.fetchone()["n"])
    logger.info("待回填行数: %d", total_pending)

    if total_pending == 0:
        logger.info("没有待回填行，退出。")
        return

    target = total_pending if limit is None else min(total_pending, limit)
    logger.info("本次最多处理: %d 行（batch=%d）", target, batch_size)

    processed = 0
    matched = 0
    lan_cnt = 0
    unknown_cnt = 0
    t0 = time.time()

    while processed < target:
        take = min(batch_size, target - processed)
        # 注意：UPDATE 后下次 SELECT WHERE country IS NULL 自动跳过本次行，无需 offset
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, ip FROM back_test.user_visit_log "
                "WHERE country IS NULL "
                "ORDER BY id LIMIT %s",
                (take,),
            )
            rows = cur.fetchall()
        if not rows:
            break

        updates = []
        for r in rows:
            ip = (r["ip"] or "").strip()
            if not ip or ip == "unknown":
                updates.append((UNKNOWN, None, None, None, r["id"]))
                unknown_cnt += 1
                continue

            if _is_private_ip(ip):
                updates.append((LAN, None, None, None, r["id"]))
                lan_cnt += 1
                continue

            try:
                region_str = searcher.search(ip)
            except Exception as e:
                logger.warning("查询失败 id=%s ip=%s: %s", r["id"], ip, e)
                updates.append((UNKNOWN, None, None, None, r["id"]))
                unknown_cnt += 1
                continue

            country, region, city, isp = _parse_region(region_str)
            if country is None:
                updates.append((UNKNOWN, None, None, None, r["id"]))
                unknown_cnt += 1
            else:
                if country == LAN:
                    lan_cnt += 1
                else:
                    matched += 1
                updates.append((country, region, city, isp, r["id"]))

        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE back_test.user_visit_log "
                "SET country=%s, region=%s, city=%s, isp=%s "
                "WHERE id=%s",
                updates,
            )

        processed += len(rows)
        if processed % (batch_size * 4) == 0 or processed >= target:
            elapsed = time.time() - t0
            rate = processed / elapsed if elapsed > 0 else 0
            logger.info(
                "进度 %d/%d  匹配=%d 内网=%d 未知=%d  (%.0f rows/s)",
                processed, target, matched, lan_cnt, unknown_cnt, rate,
            )

    logger.info(
        "完成。处理=%d 匹配=%d 内网=%d 未知=%d 耗时=%.1fs",
        processed, matched, lan_cnt, unknown_cnt, time.time() - t0,
    )


# ─────────────────────────── CLI ────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="回填 user_visit_log 地理位置字段")
    p.add_argument("--xdb", default=str(DEFAULT_XDB),
                   help=f"ip2region xdb 文件路径 (默认: {DEFAULT_XDB})")
    p.add_argument("--limit", type=int, default=None,
                   help="本次最多处理多少行（默认全部）")
    p.add_argument("--batch", type=int, default=500,
                   help="批量 UPDATE 的大小 (默认 500)")
    args = p.parse_args()

    try:
        backfill(Path(args.xdb), args.limit, args.batch)
    except Exception as e:
        logger.error("回填失败: %s", e, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
