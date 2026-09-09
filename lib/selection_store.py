# -*- coding: utf-8 -*-
# 选股池 / 每策略持仓 数据库存储层
"""
selection_store -- 多策略并行模型的持久化

表:
    trade_selection_pool     每策略选股池 (只记录候选, 幂等键 (strategy, stock_code))
    trade_strategy_position  每策略独立持仓 (共享总资金, 幂等键 (strategy, stock_code))

复用 lib.backtest_data._db_config() 读取 .env 的 MySQL 配置 (pymysql)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from lib.backtest_data import _db_config


# ============================================================
# DDL (幂等)
# ============================================================

_POOL_DDL = """
CREATE TABLE IF NOT EXISTS trade_selection_pool (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  strategy    VARCHAR(64)  NOT NULL,
  stock_code  VARCHAR(16)  NOT NULL,
  trade_date  DATE         NOT NULL,
  rank_no     INT UNSIGNED NULL,
  score       DECIMAL(14,4) NULL,
  reason      VARCHAR(512) NULL,
  name        VARCHAR(64)  NULL,
  open_pct    DECIMAL(8,4) NULL,      -- 入选日开盘涨幅(%)
  cur_pct     DECIMAL(8,4) NULL,      -- 入选日当前涨幅(%)
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  selected_at DATETIME NULL,     -- 入选时间 (取"立即选股"时用户选取的时间)
  source_type VARCHAR(16) NOT NULL DEFAULT 'cron',  -- 选股来源: cron=策略自动(定时), manual=手动(立即选股)
  UNIQUE KEY uk_strategy_code (strategy, stock_code),
  KEY idx_strategy_date (strategy, trade_date),
  KEY idx_code (stock_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_POS_DDL = """
CREATE TABLE IF NOT EXISTS trade_strategy_position (
  id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  strategy   VARCHAR(64) NOT NULL,
  stock_code VARCHAR(16) NOT NULL,
  name       VARCHAR(64) NULL,
  volume     INT UNSIGNED NOT NULL DEFAULT 0,
  cost       DECIMAL(12,4) NOT NULL DEFAULT 0,
  cur_price  DECIMAL(12,4) NULL,
  pnl        DECIMAL(16,2) NOT NULL DEFAULT 0,
  pnl_pct    DECIMAL(8,4) NOT NULL DEFAULT 0,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_strategy_code2 (strategy, stock_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_tables() -> None:
    """建表 (幂等, app 启动时调用一次)"""
    import pymysql
    cfg = _db_config()
    conn = pymysql.connect(**cfg)
    try:
        cur = conn.cursor()
        for ddl in (_POOL_DDL, _POS_DDL):
            cur.execute(ddl)
        # 已有表补列 selected_at / open_pct / cur_pct / trigger (幂等: 已存在则跳过)
        for col, ddl in (
            ("selected_at", "ALTER TABLE trade_selection_pool ADD COLUMN selected_at DATETIME NULL"),
            ("open_pct", "ALTER TABLE trade_selection_pool ADD COLUMN open_pct DECIMAL(8,4) NULL"),
            ("cur_pct", "ALTER TABLE trade_selection_pool ADD COLUMN cur_pct DECIMAL(8,4) NULL"),
            ("trigger", "ALTER TABLE trade_selection_pool ADD COLUMN source_type VARCHAR(16) NOT NULL DEFAULT 'cron'"),
        ):
            try:
                cur.execute(ddl)
                conn.commit()
            except Exception:
                conn.rollback()  # 列已存在
        cur.close()
    finally:
        conn.close()


# ============================================================
# 选股池读写
# ============================================================

def replace_selection_pool_daily(
    per_strategy: Dict[str, List[dict]], trade_date: str,
    selected_at: Optional[str] = None,
    trigger: str = "cron",
) -> int:
    """把最近一次选股结果写入 trade_selection_pool.

    幂等: INSERT ... ON DUPLICATE KEY UPDATE, 同时清掉本策略同来源(trigger)已不在榜的旧候选,
    使表始终反映"每个策略最近一次 trade_date 的候选池"。
    selected_at: 入选时间 (立即选股时用户选取的时间), 缺省=None(用当前时间)。
    trigger: 选股来源 cron=策略自动 / manual=手动, 自动与手动候选各自独立维护, 互不覆盖。
    返回写入/更新的行数。
    """
    import pymysql
    cfg = _db_config()
    conn = pymysql.connect(**cfg)
    inserted = 0
    try:
        cur = conn.cursor()
        for name, rows in (per_strategy or {}).items():
            if not rows:
                continue
            # 1) 删除该策略同 trade_date 同来源的旧候选, 使表始终=最近一次该策略该来源候选池
            cur.execute(
                "DELETE FROM trade_selection_pool WHERE strategy = %s AND trade_date = %s AND source_type = %s",
                (name, trade_date, trigger),
            )
            # 2) upsert 本次候选 (同策略同股只一行)
            sql = """
                INSERT INTO trade_selection_pool
                    (strategy, stock_code, trade_date, rank_no, score, reason, name,
                     open_pct, cur_pct, selected_at, source_type)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    trade_date = VALUES(trade_date),
                    rank_no = VALUES(rank_no),
                    score   = VALUES(score),
                    reason  = VALUES(reason),
                    name    = VALUES(name),
                    open_pct = VALUES(open_pct),
                    cur_pct  = VALUES(cur_pct),
                    selected_at = VALUES(selected_at),
                    source_type = VALUES(source_type)
            """
            for r in rows:
                cur.execute(sql, (
                    name,
                    r.get("stock_code") or r.get("code"),
                    trade_date,
                    r.get("rank_no"),
                    r.get("score"),
                    r.get("reason"),
                    r.get("name"),
                    r.get("open_pct"),
                    r.get("cur_pct"),
                    selected_at,
                    trigger,
                ))
                inserted += cur.rowcount
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return inserted


def query_selection_pool(
    strategy: Optional[str] = None,
    trade_date: Optional[str] = None,
    trigger: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[dict]:
    """查询选股池 (默认最近一次 trade_date 的全部策略候选)

    trigger: 'cron' 只查策略自动选出的候选, 'manual' 只查手动选出的候选, None=不区分。
    """
    import pymysql
    cfg = _db_config()
    where: List[str] = []
    params: List[Any] = []
    if strategy:
        where.append("strategy = %s")
        params.append(strategy)
    if trade_date:
        where.append("trade_date = %s")
        params.append(trade_date)
    if trigger:
        where.append("source_type = %s")
        params.append(trigger)
    sql = ("SELECT strategy, stock_code, trade_date, rank_no, score, reason, name, "
           "open_pct, cur_pct, selected_at, source_type "
           "FROM trade_selection_pool")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY strategy, rank_no, stock_code"
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    conn = pymysql.connect(**cfg)
    rows: List[dict] = []
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    for r in rows:
        r["stock_code"] = str(r["stock_code"])
        r["trade_date"] = str(r["trade_date"]) if r.get("trade_date") else None
        r["selected_at"] = str(r["selected_at"]) if r.get("selected_at") else None
        r["trigger"] = r.get("source_type")   # 兼容下游: trigger 键 = 选股来源
    return rows


def latest_pool_trade_date() -> Optional[str]:
    """最近一次选股写入的 trade_date"""
    import pymysql
    cfg = _db_config()
    conn = pymysql.connect(**cfg)
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(trade_date) FROM trade_selection_pool")
        row = cur.fetchone()
        cur.close()
        return str(row[0]) if row and row[0] else None
    finally:
        conn.close()


# ============================================================
# 每策略持仓读写 (共享总资金 / 独立持仓)
# ============================================================

def upsert_strategy_position(
    strategy: str, code: str, *,
    name: Optional[str] = None,
    volume: int = 0, cost: float = 0.0,
    cur_price: Optional[float] = None,
    pnl: float = 0.0, pnl_pct: float = 0.0,
) -> None:
    import pymysql
    cfg = _db_config()
    conn = pymysql.connect(**cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO trade_strategy_position
                (strategy, stock_code, name, volume, cost, cur_price, pnl, pnl_pct)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                name = VALUES(name), volume = VALUES(volume), cost = VALUES(cost),
                cur_price = VALUES(cur_price), pnl = VALUES(pnl), pnl_pct = VALUES(pnl_pct)
            """,
            (strategy, code, name, int(volume), float(cost), cur_price, pnl, pnl_pct),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def increment_position(
    strategy: str, code: str, side: str, qty: int, price: float
) -> dict:
    """按成交增减某策略持仓. side=buy 加仓, sell 减仓(最多减到 0). 返回最新持仓."""
    import pymysql
    cfg = _db_config()
    conn = pymysql.connect(**cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT volume, cost FROM trade_strategy_position "
            "WHERE strategy=%s AND stock_code=%s FOR UPDATE",
            (strategy, code),
        )
        row = cur.fetchone()
        if side == "buy":
            if row:
                cur.execute(
                    "UPDATE trade_strategy_position SET volume = volume + %s, "
                    "cost = (cost*volume + %s*%s)/(volume+%s) WHERE strategy=%s AND stock_code=%s",
                    (qty, qty, price, qty, strategy, code),
                )
            else:
                cur.execute(
                    "INSERT INTO trade_strategy_position (strategy, stock_code, volume, cost) "
                    "VALUES (%s,%s,%s,%s)",
                    (strategy, code, qty, price),
                )
        else:  # sell
            if row:
                vol = row[0]
                new_vol = max(0, vol - qty)
                cur.execute(
                    "UPDATE trade_strategy_position SET volume = %s WHERE strategy=%s AND stock_code=%s",
                    (new_vol, strategy, code),
                )
        conn.commit()
        cur.execute(
            "SELECT strategy, stock_code, volume, cost, cur_price FROM trade_strategy_position "
            "WHERE strategy=%s AND stock_code=%s",
            (strategy, code),
        )
        res = cur.fetchone()
        cur.close()
        return ({"strategy": strategy, "code": code, "volume": int(res[2]),
                 "cost": float(res[3] or 0)} if res else
                {"strategy": strategy, "code": code, "volume": 0, "cost": 0})
    finally:
        conn.close()


def query_strategy_positions(strategy: Optional[str] = None) -> List[dict]:
    import pymysql
    cfg = _db_config()
    where = "WHERE strategy = %s" if strategy else ""
    params: tuple = (strategy,) if strategy else ()
    conn = pymysql.connect(**cfg)
    rows: List[dict] = []
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute(
            "SELECT strategy, stock_code, name, volume, cost, cur_price, pnl, pnl_pct "
            "FROM trade_strategy_position " + where + " ORDER BY strategy, stock_code",
            params,
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    for r in rows:
        r["stock_code"] = str(r["stock_code"])
    return rows


_WATCH_POOL_FILE = (Path(__file__).resolve().parent.parent
                    / "config" / "watch_pool.yaml")


def _read_watch_pool_codes() -> List[str]:
    """读 config/watch_pool.yaml 的 codes, 读不到返回空列表"""
    try:
        import yaml
        if not _WATCH_POOL_FILE.exists():
            return []
        data = yaml.safe_load(_WATCH_POOL_FILE.read_text(encoding="utf-8")) or {}
        return [str(c).strip() for c in (data.get("codes") or []) if str(c).strip()]
    except Exception:
        return []


def _write_watch_pool_codes(codes: List[str]) -> None:
    """覆盖写入 watch_pool.yaml (带注释说明)"""
    import yaml
    _WATCH_POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# 监控代码列表 -- 策略自动选股结果自动同步写入 (手动选股不入池)\n"
        "# 可手动编辑该文件; 修改后调用 POST /api/live/watch_pool 触发热加载\n\n"
    )
    body = yaml.safe_dump({"codes": codes}, allow_unicode=True,
                          default_flow_style=False)
    _WATCH_POOL_FILE.write_text(header + body, encoding="utf-8")


def sync_pool_to_watch_pool(codes: List[str]) -> List[str]:
    """把策略自动选出的 code 列表并入 watch_pool.yaml (去重), 返回新增 code。

    仅由「自动定时选股」调用; 手动选股不调用本函数。"""
    existing = _read_watch_pool_codes()
    seen = set(existing)
    new = [str(c).strip() for c in (codes or [])
           if str(c).strip() and str(c).strip() not in seen]
    if not new:
        return []
    _write_watch_pool_codes(existing + new)
    return new