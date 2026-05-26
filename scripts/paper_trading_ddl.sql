-- ──────────────────────────────────────────────────────────────
-- 小市值策略 · 实盘观察（Paper Trading）数据表
-- 5 张表，全部 InnoDB + utf8mb4
--
-- 使用方式（任选其一）：
--   1. mysql -u root -p your_db_name < scripts/paper_trading_ddl.sql
--   2. python -c "from app.paper_trading import db; db.ensure_tables()"
--   3. 首次运行 scripts/daily_signal.py 时会自动建表
--
-- 与 app/paper_trading/db.py 中 DDL_STATEMENTS 保持一致。
-- 修改时请同步两边。
-- ──────────────────────────────────────────────────────────────

-- 1) 账户全局状态（单行，id=1）
CREATE TABLE IF NOT EXISTS paper_account (
    id                  TINYINT       NOT NULL PRIMARY KEY,
    initial_capital     DECIMAL(18,2) NOT NULL,
    cash                DECIMAL(18,2) NOT NULL,
    last_rebalance_date DATE,
    rebalance_counter   INT           NOT NULL DEFAULT 0,
    strategy_params     JSON,
    updated_at          DATETIME      DEFAULT CURRENT_TIMESTAMP
                                      ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='小市值策略模拟账户全局状态';


-- 2) 当前持仓
CREATE TABLE IF NOT EXISTS paper_holdings (
    code        CHAR(6)        NOT NULL PRIMARY KEY,
    name        VARCHAR(20),
    shares      INT            NOT NULL,
    buy_price   DECIMAL(10,3)  NOT NULL,
    buy_date    DATE           NOT NULL,
    cost        DECIMAL(18,2)  NOT NULL,
    updated_at  DATETIME       DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='当前持仓';


-- 3) 每日运行记录（主表，按 run_date 唯一）
CREATE TABLE IF NOT EXISTS paper_signal_run (
    run_id          BIGINT        NOT NULL AUTO_INCREMENT PRIMARY KEY,
    run_date        DATE          NOT NULL,
    strategy        VARCHAR(32)   NOT NULL DEFAULT 'small_cap',
    params          JSON,
    universe_size   INT,
    selected_count  INT,
    is_rebalance    TINYINT       NOT NULL DEFAULT 0,
    stop_loss_count INT           NOT NULL DEFAULT 0,
    capital         DECIMAL(18,2),
    total_value     DECIMAL(18,2),
    position_value  DECIMAL(18,2),
    cash            DECIMAL(18,2),
    cum_return      DECIMAL(10,6),
    status          VARCHAR(16)   NOT NULL DEFAULT 'success',
    error_msg       TEXT,
    notes           TEXT,
    created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_run_date (run_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日运行记录';


-- 4) 每次运行的持仓/交易明细（action=买入/卖出/止损卖出/持有）
CREATE TABLE IF NOT EXISTS paper_signal_position (
    id          BIGINT         NOT NULL AUTO_INCREMENT PRIMARY KEY,
    run_id      BIGINT         NOT NULL,
    run_date    DATE           NOT NULL,
    code        CHAR(6)        NOT NULL,
    name        VARCHAR(20),
    market_cap  DECIMAL(18,4),
    price       DECIMAL(10,3),
    shares      INT,
    amount      DECIMAL(18,2),
    action      VARCHAR(16),
    INDEX idx_run_id (run_id),
    INDEX idx_run_date (run_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='每次运行的建议持仓/交易明细，action=买入/卖出/止损卖出/持有';


-- 5) 管理员 IP 白名单（实盘/定时任务写操作鉴权）
-- 同步自 app/paper_trading/admin_ip.py DDL_STATEMENTS
CREATE TABLE IF NOT EXISTS paper_admin_ip (
    ip             VARCHAR(45)   NOT NULL PRIMARY KEY,
    note           VARCHAR(200),
    created_at     DATETIME      DEFAULT CURRENT_TIMESTAMP,
    created_by_ip  VARCHAR(45)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='paper_trading & 定时任务的写操作白名单 IP';

-- Bootstrap：要么用环境变量 PAPER_ADMIN_INITIAL_IPS=1.2.3.4,5.6.7.8 启动；
-- 要么留空，第一次有人点"立即扫描"或"保存参数"时，那个 IP 会被自动加入。
-- 也可以预先手动插入：
--   INSERT INTO paper_admin_ip (ip, note) VALUES ('1.2.3.4', '我的笔记本');


-- 6) 每日净值快照 + 上证综指基准
CREATE TABLE IF NOT EXISTS paper_equity_daily (
    trade_date           DATE          NOT NULL PRIMARY KEY,
    total_value          DECIMAL(18,2) NOT NULL,
    position_value       DECIMAL(18,2) NOT NULL,
    cash                 DECIMAL(18,2) NOT NULL,
    daily_return         DECIMAL(10,6),
    cum_return           DECIMAL(10,6),
    benchmark_close      DECIMAL(10,3),
    benchmark_cum_return DECIMAL(10,6),
    created_at           DATETIME      DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日净值快照（含上证综指基准）';
