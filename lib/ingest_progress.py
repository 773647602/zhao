# -*- coding: utf-8 -*-
# 数据入库脚本进度上报 (供数据获取页面进度条展示)
"""
ingest_progress -- 数据入库脚本运行时上报进度

脚本(日线/分钟数据-国金QMT入库.py)在处理批次时调用 report() 原子写入
outputs/ingest_progress.json; 前端通过 GET /api/data/scripts 读取该文件,
把进度合并到对应脚本条目, 在卡片下方渲染进度条。

字段: script(脚本名) / stage(阶段) / done(已处理) / total(总数) /
      pct(百分比) / message(详情) / status(running|done|error) /
      pid / updated_at
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
_FILE = _PROJ / "outputs" / "ingest_progress.json"
_TMP = _PROJ / "outputs" / "ingest_progress.tmp"


def report(script: str, stage: str = "", done: int = 0, total: int = 0,
           message: str = "", status: str = "running") -> None:
    """上报进度 (原子写, 失败静默)"""
    data = {
        "script": script,
        "stage": stage,
        "done": int(done),
        "total": int(total),
        "pct": round(done / total * 100, 1) if total else 0.0,
        "message": message,
        "status": status,
        "pid": os.getpid(),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        _TMP.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        _TMP.replace(_FILE)  # 原子替换, 避免半写
    except Exception:
        pass


def load() -> dict:
    """读取当前进度 (文件不存在返回空 dict)"""
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
