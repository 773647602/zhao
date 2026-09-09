# -*- coding: utf-8 -*-
# 系统状态路由 -- REST
"""
GET /api/system/health   -- 健康检查 (xtdata / DASHSCOPE / state / registry)
"""

from __future__ import annotations
import os
import sys

from fastapi import APIRouter

from lib.paths import setup_sys_path, OUTPUTS_LIVE_STATE, OUTPUTS_RESEARCH
setup_sys_path()

router = APIRouter()


@router.get("/health")
def health():
    rows = []

    rows.append({"item": "Python 版本", "value": sys.version.split()[0], "status": "OK"})

    try:
        from xtquant import xtdata
        xtdata.connect()
        rows.append({"item": "xtdata 行情", "value": "已连接", "status": "OK"})
    except Exception as e:
        rows.append({"item": "xtdata 行情", "value": str(e)[:60], "status": "ERROR"})

    if os.environ.get("DASHSCOPE_API_KEY"):
        rows.append({"item": "DASHSCOPE_API_KEY", "value": "已配置", "status": "OK"})
    else:
        rows.append({"item": "DASHSCOPE_API_KEY",
                     "value": "未配置 (Charles 不可用)", "status": "WARN"})

    if os.environ.get("QMT_PATH"):
        rows.append({"item": "QMT_PATH",
                     "value": os.environ["QMT_PATH"], "status": "OK"})
    else:
        rows.append({"item": "QMT_PATH",
                     "value": "未配置 (实盘下单不可用)", "status": "WARN"})

    if OUTPUTS_LIVE_STATE.exists():
        import json
        try:
            s = json.loads(OUTPUTS_LIVE_STATE.read_text(encoding="utf-8"))
            rows.append({"item": "live_state.json",
                         "value": f"updated_at={s.get('_updated_at', '?')}", "status": "OK"})
        except Exception as e:
            rows.append({"item": "live_state.json",
                         "value": f"解析失败: {e}", "status": "ERROR"})
    else:
        rows.append({"item": "live_state.json",
                     "value": "不存在 (启动模拟盘后会自动创建)", "status": "WARN"})

    if OUTPUTS_RESEARCH.exists():
        n = len(list(OUTPUTS_RESEARCH.glob("morning_brief_*.html")))
        rows.append({"item": "晨会分析 HTML", "value": f"{n} 份",
                     "status": "OK" if n > 0 else "WARN"})
    else:
        rows.append({"item": "晨会分析 HTML",
                     "value": "目录不存在", "status": "WARN"})

    return rows
