# -*- coding: utf-8 -*-
# 市场量能 (trade_market_volume): 记录每日沪深两市成交总额, 收盘后自动更新
"""
表: trade_market_volume
    trade_date  DATE   PK 交易日
    sh_amount   BIGINT     沪市成交额(元)
    sz_amount   BIGINT     深市成交额(元)
    total_amount BIGINT    两市成交总额(元)
    source      VARCHAR(20) 数据源(新浪指数)
    updated_at  DATETIME   最近更新时间

幂等键: trade_date (每日一行, upsert 覆盖)
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

_cached_table_ok = False


def _conn():
    import pymysql
    from lib.backtest_data import _db_config
    return pymysql.connect(**_db_config())


def ensure_table() -> None:
    """确保 trade_market_volume 表存在 (幂等)."""
    global _cached_table_ok
    if _cached_table_ok:
        return
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS trade_market_volume ("
            "  trade_date DATE NOT NULL,"
            "  sh_amount BIGINT NOT NULL DEFAULT 0,"
            "  sz_amount BIGINT NOT NULL DEFAULT 0,"
            "  total_amount BIGINT NOT NULL DEFAULT 0,"
            "  source VARCHAR(20) NOT NULL DEFAULT 'sina',"
            "  updated_at DATETIME NOT NULL,"
            "  PRIMARY KEY (trade_date)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()
    _cached_table_ok = True


def upsert_market_volume(trade_date: str, amounts: Dict[str, float],
                         source: str = "sina") -> None:
    """写入某交易日两市成交额 (幂等 upsert). amounts: {sh, sz, total}"""
    ensure_table()
    sh = int(round(float(amounts.get("sh") or 0.0)))
    sz = int(round(float(amounts.get("sz") or 0.0)))
    total = int(round(float(amounts.get("total") or 0.0)))
    if total <= 0:
        return
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO trade_market_volume"
            " (trade_date, sh_amount, sz_amount, total_amount, source, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON DUPLICATE KEY UPDATE"
            "  sh_amount=VALUES(sh_amount), sz_amount=VALUES(sz_amount),"
            "  total_amount=VALUES(total_amount), source=VALUES(source),"
            "  updated_at=VALUES(updated_at)",
            (trade_date, sh, sz, total, source, datetime.now()),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def query_market_volume(trade_date: Optional[str] = None
                        ) -> List[dict]:
    """按 trade_date 排序查询市场量能; 不传日期返回全部(倒序)."""
    ensure_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        if trade_date:
            cur.execute(
                "SELECT trade_date, sh_amount, sz_amount, total_amount, source, updated_at"
                " FROM trade_market_volume WHERE trade_date=%s ORDER BY trade_date",
                (trade_date,))
        else:
            cur.execute(
                "SELECT trade_date, sh_amount, sz_amount, total_amount, source, updated_at"
                " FROM trade_market_volume ORDER BY trade_date DESC"
                " LIMIT 60")
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    return [_row(r) for r in rows]


def _row(r) -> dict:
    return {
        "trade_date": r[0].strftime("%Y-%m-%d"),
        "sh_amount": int(r[1]),
        "sz_amount": int(r[2]),
        "total_amount": int(r[3]),
        "source": r[4],
        "updated_at": r[5].strftime("%Y-%m-%d %H:%M:%S"),
    }