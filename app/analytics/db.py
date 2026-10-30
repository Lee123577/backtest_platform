"""
转化归因数据库层
==================

1 张自己的表：

  user_event_log —— 业务事件流水(打开页面/回测/注册/加自选/渠道命中…)

为什么不塞进 user_visit_log:那张表是"每个 HTTP 请求一行"的访问流水,量级比
业务事件大两三个数量级,混在一起既查不动也说不清语义。事件表自己建、自己
的 DAO,与访问日志只靠 session_id / user_id 关联。

字段设计要点(相对旧表):
  - `anonymous_id` = 持久设备 ID(=sp_sid cookie),`session_id` = 30 分钟会话 ID。
    旧表把二者混为一谈(session_id 装的是设备 ID),迁移时把旧 session_id
    复制进 anonymous_id 修正语义 —— **只在新增 anonymous_id 列的那一次执行**,
    否则会把后来的会话 ID 覆盖进设备 ID,造成不可逆的数据损坏。
  - `props` JSON:结构化属性,可被 SQL 直接查询(旧 `meta` 是 VARCHAR512 自由串,
    截断还丢数据)。**保留 meta 继续写入**只为兼容尚未迁移的读方。
  - `event_id CHAR(32)` + `ON DUPLICATE KEY UPDATE id=id`:同一次上报被 beacon /
    网络重发投递多次时,靠唯一键静默丢弃。刷新页面是新的一次浏览(新 id),照常计数。
  - `is_bot` / `bot_kind`:UA 打标落字段,真人口径可一键过滤(见 app/analytics/bots.py)。

另外负责给 user_visit_log 补 utm_* 列(首次触达渠道) —— 那张表是既有表,
这里用 INFORMATION_SCHEMA 探测后补列,重复执行安全。
"""
from __future__ import annotations

import logging
from datetime import date as _Date
from typing import Any, Dict, List, Optional, Tuple

from ..data.data_loader import _get_pool

logger = logging.getLogger(__name__)

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS user_event_log (
        id            BIGINT       NOT NULL AUTO_INCREMENT,
        event_id      CHAR(32)     DEFAULT NULL COMMENT '客户端生成的事件唯一ID(uuid hex),用于幂等去重',
        event         VARCHAR(32)  NOT NULL COMMENT '事件名(服务端白名单校验)',
        user_id       INT          DEFAULT NULL COMMENT '登录用户,未登录为 NULL',
        anonymous_id  VARCHAR(64)  DEFAULT NULL COMMENT '持久匿名访客ID(=sp_sid cookie,1年)',
        session_id    VARCHAR(64)  DEFAULT NULL COMMENT '单次会话ID(30分钟滑动窗口)',
        ip            VARCHAR(45)  DEFAULT NULL,
        path          VARCHAR(255) DEFAULT NULL COMMENT '触发页面(站内绝对路径,去query)',
        referer       VARCHAR(512) DEFAULT NULL,
        referer_host  VARCHAR(128) DEFAULT NULL COMMENT 'referer 域名(空=direct)',
        utm_source    VARCHAR(64)  DEFAULT NULL COMMENT '首触渠道',
        utm_medium    VARCHAR(64)  DEFAULT NULL,
        utm_campaign  VARCHAR(64)  DEFAULT NULL,
        utm_content   VARCHAR(64)  DEFAULT NULL,
        utm_term      VARCHAR(64)  DEFAULT NULL,
        is_bot        TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '1=爬虫/自动化;真人=0',
        bot_kind      VARCHAR(16)  DEFAULT NULL COMMENT 'search/ai/seo/scanner/unknown',
        device_type   VARCHAR(16)  DEFAULT NULL COMMENT 'pc/mobile/bot',
        browser       VARCHAR(32)  DEFAULT NULL,
        os            VARCHAR(32)  DEFAULT NULL,
        app_version   VARCHAR(16)  DEFAULT NULL COMMENT '埋点规则版本(UA口径),便于口径回归',
        meta          VARCHAR(512) DEFAULT NULL COMMENT '附加信息(JSON,超长截断)——保留兼容未迁移读方',
        props         JSON         DEFAULT NULL COMMENT '事件结构化属性(取代 meta)',
        created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        UNIQUE KEY uk_event_id (event_id),
        KEY idx_event_time (event, created_at),
        KEY idx_anon (anonymous_id),
        KEY idx_session (session_id),
        KEY idx_user (user_id),
        KEY idx_bot_time (is_bot, created_at),
        KEY idx_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='业务事件流水(转化漏斗/渠道归因)'
    """,
]

# user_visit_log 是既有表,只能补列。MySQL 的 ADD COLUMN 没有 IF NOT EXISTS,
# 得先查 INFORMATION_SCHEMA —— 否则重复执行会因 1060 duplicate column 报错。
_VISIT_LOG_COLUMNS = {
    "utm_source": "VARCHAR(64) DEFAULT NULL COMMENT '首次触达渠道'",
    "utm_medium": "VARCHAR(64) DEFAULT NULL COMMENT '首次触达媒介'",
    "utm_campaign": "VARCHAR(64) DEFAULT NULL COMMENT '首次触达活动'",
    "utm_content": "VARCHAR(64) DEFAULT NULL COMMENT '首次触达素材位'",
    "utm_term": "VARCHAR(64) DEFAULT NULL COMMENT '首次触达关键词'",
}

# user_event_log 存量库补列(同款 INFORMATION_SCHEMA 探测)。已存在的
# session_id/user_id/path/utm_source/utm_medium/utm_campaign/event/ip/created_at
# 不在此列,不要重复加。
_EVENT_COLUMNS = {
    "event_id":     "CHAR(32)     DEFAULT NULL COMMENT '幂等去重键'",
    "anonymous_id": "VARCHAR(64)  DEFAULT NULL COMMENT '持久匿名访客ID=sp_sid'",
    "referer":      "VARCHAR(512) DEFAULT NULL",
    "referer_host": "VARCHAR(128) DEFAULT NULL COMMENT 'referer 域名(空=direct)'",
    "utm_content":  "VARCHAR(64)  DEFAULT NULL",
    "utm_term":     "VARCHAR(64)  DEFAULT NULL",
    "is_bot":       "TINYINT(1)   NOT NULL DEFAULT 0",
    "bot_kind":     "VARCHAR(16)  DEFAULT NULL",
    "device_type":  "VARCHAR(16)  DEFAULT NULL",
    "browser":      "VARCHAR(32)  DEFAULT NULL",
    "os":           "VARCHAR(32)  DEFAULT NULL",
    "app_version":  "VARCHAR(16)  DEFAULT NULL",
    "props":        "JSON         DEFAULT NULL",
}

# 索引同样需要幂等补加(存量库)。新部署时 CREATE 已带上,探测到就跳过。
_EVENT_INDEXES = {
    "uk_event_id": "UNIQUE KEY uk_event_id (event_id)",
    "idx_bot_time": "KEY idx_bot_time (is_bot, created_at)",
    "idx_anon": "KEY idx_anon (anonymous_id)",
}

_ensured = False


def _event_columns_now(cur) -> bool:
    """给 user_event_log 幂等补列。返回本次是否**刚刚**新增了 anonymous_id。

    **每列单独 try/except**:一列失败不能拖垮其余,更不能让整个埋点写不进去
    (补列失败最多是少个字段,埋点必须继续可用)。
    """
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = 'user_event_log'
        """
    )
    have = {(r.get("column_name") or r.get("COLUMN_NAME")) for r in cur.fetchall()}
    added_anon = False
    for col, ddl in _EVENT_COLUMNS.items():
        if col in have:
            continue
        try:
            cur.execute(f"ALTER TABLE user_event_log ADD COLUMN {col} {ddl}")
            logger.info("user_event_log 补列 %s", col)
            if col == "anonymous_id":
                added_anon = True
        except Exception as e:
            logger.warning("user_event_log 补列 %s 失败: %s", col, e)
    return added_anon


def _event_indexes(cur) -> None:
    """幂等补索引(同款探测)。失败只告警,不影响写入。"""
    cur.execute(
        """
        SELECT DISTINCT index_name FROM information_schema.statistics
        WHERE table_schema = DATABASE() AND table_name = 'user_event_log'
        """
    )
    have = {(r.get("index_name") or r.get("INDEX_NAME")) for r in cur.fetchall()}
    for name, ddl in _EVENT_INDEXES.items():
        if name in have:
            continue
        try:
            cur.execute(f"ALTER TABLE user_event_log ADD {ddl}")
            logger.info("user_event_log 补索引 %s", name)
        except Exception as e:
            logger.warning("user_event_log 补索引 %s 失败: %s", name, e)


def ensure_tables() -> None:
    """建事件表 + 补列/补索引 + 语义迁移 + 给 user_visit_log 补 utm 列。
    幂等,进程内只真正跑一次。"""
    global _ensured
    if _ensured:
        return
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法初始化 analytics 表")
    with conn.cursor() as cur:
        for sql in DDL_STATEMENTS:
            cur.execute(sql)

        added_anon = _event_columns_now(cur)
        _event_indexes(cur)

        # 🔴 语义迁移:旧 session_id 其实是设备 ID。把旧值搬进 anonymous_id,
        # 让 session_id 从此只装真正的会话。
        #
        # **只在本次刚新增 anonymous_id 列的那一次执行** —— 全表生命周期内
        # 恰好跑一次。若以后无条件重跑,会把新写入的"会话 ID"错当设备 ID
        # 覆盖进 anonymous_id,这是不可逆的数据损坏。
        if added_anon:
            try:
                cur.execute(
                    "UPDATE user_event_log SET anonymous_id = session_id "
                    "WHERE anonymous_id IS NULL AND session_id IS NOT NULL"
                )
                logger.info("user_event_log 语义迁移:旧 session_id → anonymous_id 完成")
            except Exception as e:
                logger.warning("user_event_log 语义迁移失败(忽略): %s", e)

        # user_visit_log 补 utm 列
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = 'user_visit_log'
            """
        )
        have = {(r.get("column_name") or r.get("COLUMN_NAME")) for r in cur.fetchall()}
        for col, ddl in _VISIT_LOG_COLUMNS.items():
            if col in have:
                continue
            try:
                cur.execute(f"ALTER TABLE user_visit_log ADD COLUMN {col} {ddl}")
                logger.info("user_visit_log 补列 %s", col)
            except Exception as e:
                # 补列失败不该拖垮埋点:访问日志照旧不带渠道,事件表仍可用
                logger.warning("user_visit_log 补列 %s 失败: %s", col, e)
    _ensured = True


def visit_log_utm_columns() -> Optional[List[str]]:
    """user_visit_log 上实际存在的 utm 列(visit_log 据此拼 INSERT)。

    不假定 ensure_tables() 一定跑过 —— 补列可能因权限失败,那时访问日志
    必须继续能写,只是少几个字段。

    返回值区分两种情况(调用方会把结果缓存到进程退出，混淆的代价是永久性的)：
      []   —— 确认查到了,表上就是没有这几列
      None —— 库没连上/查询失败,**不知道**。调用方必须择机重试,
              不能把这次的"不知道"当成"没有"缓存下来
    """
    conn = _get_pool()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'user_visit_log'
                  AND column_name IN
                      ('utm_source','utm_medium','utm_campaign','utm_content','utm_term')
                """
            )
            return [
                (r.get("column_name") or r.get("COLUMN_NAME"))
                for r in cur.fetchall()
            ]
    except Exception as e:
        logger.info("探测 user_visit_log utm 列失败(下次重试): %s", e)
        return None


# ── 写入 ─────────────────────────────────────────────────────────────────────

def insert_event(
    event: str,
    user_id: Optional[int] = None,
    anonymous_id: Optional[str] = None,
    session_id: Optional[str] = None,
    event_id: Optional[str] = None,
    ip: Optional[str] = None,
    path: Optional[str] = None,
    referer: Optional[str] = None,
    referer_host: Optional[str] = None,
    utm: Optional[Dict[str, str]] = None,
    is_bot: int = 0,
    bot_kind: Optional[str] = None,
    device_type: Optional[str] = None,
    browser: Optional[str] = None,
    os: Optional[str] = None,
    app_version: Optional[str] = None,
    props: Optional[str] = None,
    meta: Optional[str] = None,
) -> None:
    """写一条事件。重复 event_id 静默丢弃(靠唯一键 uk_event_id)。

    同时写 `meta`(兼容未迁移读方)与 `props`(结构化)。失败由调用方决定是否吞。
    """
    ensure_tables()
    conn = _get_pool()
    if conn is None:
        raise RuntimeError("数据库连接不可用，无法写入事件")
    utm = utm or {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_event_log
                (event_id, event, user_id, anonymous_id, session_id, ip, path,
                 referer, referer_host, utm_source, utm_medium, utm_campaign,
                 utm_content, utm_term, is_bot, bot_kind, device_type, browser,
                 os, app_version, meta, props)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE id = id
            """,
            (event_id, event, user_id, anonymous_id, session_id, ip, path,
             referer, referer_host, utm.get("utm_source"), utm.get("utm_medium"),
             utm.get("utm_campaign"), utm.get("utm_content"), utm.get("utm_term"),
             1 if is_bot else 0, bot_kind, device_type, browser, os, app_version,
             meta, props),
        )


def first_utm_for_user(user_id: int) -> Dict[str, str]:
    """取该用户最早一条带渠道的事件的 utm —— 用于把后续转化挂回原始来源。

    场景:注册(register_success)常发生在落地之后,那时 cookie 里的首触渠道
    可能已过期或被清,直接记会把转化算成"直接访问",不如从用户自己的早期事件回捞。
    """
    conn = _get_pool()
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT utm_source, utm_medium, utm_campaign
                FROM user_event_log
                WHERE user_id = %s AND utm_source IS NOT NULL AND utm_source <> ''
                ORDER BY created_at ASC LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
    except Exception as e:
        logger.info("回捞用户首次渠道失败(忽略): %s", e)
        return {}
    if not row:
        return {}
    return {k: v for k, v in row.items() if v}


# ── 会话派生用的轻查询 ───────────────────────────────────────────────────────

def last_event_for_anon(anonymous_id: str) -> Optional[Dict[str, Any]]:
    """该匿名设备的最后一条事件(session_id + created_at)。查不到/失败返回 None。"""
    conn = _get_pool()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT session_id, created_at
                FROM user_event_log
                WHERE anonymous_id = %s
                ORDER BY id DESC LIMIT 1
                """,
                (anonymous_id,),
            )
            return cur.fetchone()
    except Exception as e:
        logger.info("查询匿名设备最后事件失败(忽略): %s", e)
        return None


def count_distinct_sessions_for_anon(anonymous_id: str) -> int:
    """该匿名设备的历史不同 session_id 去重计数(判新老访客/算访问次数)。"""
    conn = _get_pool()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(DISTINCT session_id) AS n
                FROM user_event_log
                WHERE anonymous_id = %s AND session_id IS NOT NULL AND session_id <> ''
                """,
                (anonymous_id,),
            )
            row = cur.fetchone()
        return int(row["n"] or 0) if row else 0
    except Exception as e:
        logger.info("统计匿名设备会话数失败(忽略): %s", e)
        return 0


def days_since_last_event(anonymous_id: str) -> Optional[int]:
    """该设备最后一次事件距今天数(给 return_visit 算 days_since_prev)。"""
    conn = _get_pool()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DATEDIFF(NOW(), created_at) AS d
                FROM user_event_log
                WHERE anonymous_id = %s
                ORDER BY id DESC LIMIT 1
                """,
                (anonymous_id,),
            )
            row = cur.fetchone()
        return int(row["d"]) if row and row["d"] is not None else None
    except Exception as e:
        logger.info("计算上次事件距今天数失败(忽略): %s", e)
        return None


def has_event_today(anonymous_id: str, event: str) -> bool:
    """该设备今天是否已记过某事件(return_visit 当日去重)。失败返回 False。"""
    conn = _get_pool()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 AS x FROM user_event_log
                WHERE anonymous_id = %s AND event = %s
                  AND DATE(created_at) = CURDATE()
                LIMIT 1
                """,
                (anonymous_id, event),
            )
            return cur.fetchone() is not None
    except Exception as e:
        logger.info("查询当日事件失败(忽略): %s", e)
        return False


# ── 管理员排除 ───────────────────────────────────────────────────────────────
# 管理员自测会把"真人访客"分母刷高(实测 153 条 page_view 里 14 条来自站主账号),
# 所有统计查询都得排除。集合为空时**必须跳过条件** —— `NOT IN ()` 是非法 SQL。

def _admin_filter_clause() -> Tuple[str, List[int]]:
    try:
        from ..auth.admin import admin_user_ids
        ids = sorted(int(i) for i in admin_user_ids() if i)
    except Exception as e:
        # 取不到就这一次不排除(宁可多算也不报错),下次请求还会重试
        logger.info("取管理员 user_id 失败(本次不排除): %s", e)
        ids = []
    if not ids:
        return "", []
    placeholders = ", ".join(["%s"] * len(ids))
    return f" AND COALESCE(user_id, 0) NOT IN ({placeholders})", ids


# ── 漏斗 / 渠道统计 ──────────────────────────────────────────────────────────

def count_visitors(start: _Date, end: _Date) -> int:
    """区间内的独立访客数 —— 访问日志侧(含爬虫),仅作对照。

    优先用 session_id(同一浏览器稳定);它是后加的字段,老数据为空,
    此时退回 IP 去重(偏低估,同一出口 IP 的多人算一个,标注清楚即可)。
    """
    conn = _get_pool()
    if conn is None:
        return 0
    clause, extra = _admin_filter_clause()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(DISTINCT COALESCE(session_id, ip)) AS n
            FROM user_visit_log
            WHERE visit_time >= %s AND visit_time < %s + INTERVAL 1 DAY
              AND (device_type IS NULL OR device_type <> 'bot'){clause}
            """,
            (start, end, *extra),
        )
        row = cur.fetchone()
    return int(row["n"] or 0) if row else 0


def page_breakdown(start: _Date, end: _Date, limit: int = 20) -> List[Dict[str, Any]]:
    """按页面拆的 PV/UV —— 只数 page_view 事件,即只数真人。

    和 user_visit_log 里那份"每页多少 UV"不是一回事:那份含爬虫,而这份的每一行
    都对应一次真实的浏览器渲染;跑 JS 的无头浏览器靠 is_bot=0 再筛一道。
    UV 按设备(anonymous_id)去重,理由见 count_events。
    """
    ensure_tables()
    conn = _get_pool()
    if conn is None:
        return []
    clause, extra = _admin_filter_clause()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT path,
                   COUNT(*) AS pv,
                   COUNT(DISTINCT COALESCE(anonymous_id, CAST(user_id AS CHAR), ip)) AS uv
              FROM user_event_log
             WHERE event = 'page_view' AND is_bot = 0
               AND created_at >= %s AND created_at < %s + INTERVAL 1 DAY
               AND path IS NOT NULL AND path <> ''{clause}
             GROUP BY path
             ORDER BY uv DESC, pv DESC
             LIMIT %s
            """,
            (start, end, *extra, int(limit)),
        )
        return [{"path": r["path"], "pv": int(r["pv"] or 0), "uv": int(r["uv"] or 0)}
                for r in cur.fetchall()]


def first_event_date(event: str) -> Optional[str]:
    """某个事件最早出现在哪天。

    给"漏斗第一层换了口径"的过渡期用:page_view 是后加的,而下游事件有历史
    数据,窗口跨过启用日时转化率会算出 300% 这种数。把启用日标出来,
    读的人就知道那段是没数据而不是真的暴涨;数据攒够之后这行自己消失。
    """
    ensure_tables()
    conn = _get_pool()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MIN(created_at) AS d FROM user_event_log WHERE event = %s",
            (event,),
        )
        row = cur.fetchone()
    d = row["d"] if row else None
    return str(d.date()) if d else None


def count_events(start: _Date, end: _Date) -> Dict[str, int]:
    """区间内各事件的独立主体数(同一访客重复触发只算一次)。排除管理员与爬虫。

    🔴 去重键必须是 `anonymous_id`(持久设备 ID),**不能是 session_id**:
    session_id 现在是 30 分钟会话,拿它去重数的是"会话数",同一个人来三次就
    算三个访客,漏斗各层转化率全部失真。老数据的设备 ID 已由迁移搬进
    anonymous_id,所以跨上线日的窗口口径也是一致的。
    `is_bot = 0`:UA 打标为爬虫/自动化的(含跑 JS 的无头浏览器)一律不算真人。
    """
    ensure_tables()
    conn = _get_pool()
    if conn is None:
        return {}
    clause, extra = _admin_filter_clause()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT event, COUNT(DISTINCT COALESCE(anonymous_id, CAST(user_id AS CHAR), ip)) AS n
            FROM user_event_log
            WHERE is_bot = 0
              AND created_at >= %s AND created_at < %s + INTERVAL 1 DAY{clause}
            GROUP BY event
            """,
            (start, end, *extra),
        )
        return {r["event"]: int(r["n"] or 0) for r in cur.fetchall()}


def channel_breakdown(start: _Date, end: _Date, limit: int = 20) -> List[Dict[str, Any]]:
    """按 utm_source 拆分的事件数 —— 哪个渠道带来了转化。排除管理员与爬虫(is_bot=1)。"""
    ensure_tables()
    conn = _get_pool()
    if conn is None:
        return []
    clause, extra = _admin_filter_clause()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COALESCE(NULLIF(utm_source, ''), '(直接/未标记)') AS source,
                   event, COUNT(*) AS n
            FROM user_event_log
            WHERE is_bot = 0
              AND created_at >= %s AND created_at < %s + INTERVAL 1 DAY{clause}
            GROUP BY source, event
            ORDER BY n DESC
            LIMIT %s
            """,
            (start, end, *extra, max(1, min(limit, 200))),
        )
        return cur.fetchall()
