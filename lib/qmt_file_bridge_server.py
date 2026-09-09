#coding:gbk
r"""QMT file bridge server (pure stdlib: no socket, no threading, no _ctypes).

Why: the QMT strategy editor sandbox has no _ctypes (pyzmq impossible) and may
also block socket.bind / background threads. This bridge needs only the two
things every QMT strategy can do: run_time callbacks and file read/write.

How it works (shared-directory RPC):
    app  -> writes  D:\qmt_file_bridge\req.json    {"sid","id","method","params"}
    QMT  -> run_time("adjust", 100ms) drains req, calls ContextInfo,
            writes   D:\qmt_file_bridge\resp.json  {"sid","id","ok","result"/"error"}
    QMT  -> every adjust also writes D:\qmt_file_bridge\hb.json (heartbeat)

Deploy: paste this whole file into a NEW strategy in the QMT strategy editor
and start it in REAL TRADING (live) mode so init()/run_time keep running.
Do NOT use backtest run -- the process exits when backtest finishes.

Supported methods (read-only):
    ping
    get_full_tick            {"codes": [...]}
    get_market_data_ex       {"field_list","stock_list","period","start_time",
                              "end_time","count","dividend_type"}
    get_stock_list_in_sector {"sector": str}
"""
import datetime
import json
import os

BRIDGE_DIR = r"D:\qmt_file_bridge"
REQ_FILE = "req.json"
RESP_FILE = "resp.json"
HB_FILE = "hb.json"
ADJUST_INTERVAL = "100nMilliSecond"

_state = {"scheduled": False, "last_key": None, "errors": 0}


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _write_json(name, obj):
    tmp = os.path.join(BRIDGE_DIR, name + ".tmp")
    dst = os.path.join(BRIDGE_DIR, name)
    try:
        if not os.path.isdir(BRIDGE_DIR):
            os.makedirs(BRIDGE_DIR)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, default=str)
        if os.path.exists(dst):
            os.remove(dst)
        os.rename(tmp, dst)
        return True
    except Exception:
        return False


def _read_json(name):
    path = os.path.join(BRIDGE_DIR, name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _log(msg):
    text = "%s [qmt_file_bridge] %s" % (
        datetime.datetime.now().strftime("%H:%M:%S"), msg)
    try:
        print(text)
    except Exception:
        pass
    try:
        with open(os.path.join(BRIDGE_DIR, "bridge.log"), "a",
                  encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


def _to_jsonable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return dict((str(k), _to_jsonable(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return str(value)


def _frame_to_payload(df):
    """DataFrame -> {"columns","index","records"} (or None)."""
    try:
        columns = [str(c) for c in df.columns]
        records = df.values.tolist()
    except Exception:
        try:
            columns = [str(c) for c in df.keys()]
            records = [list(r) for r in df]
        except Exception:
            return None
    out = []
    for row in records:
        out.append([None if (v is None or v != v) else _to_jsonable(v)
                    for v in row])
    try:
        index = [str(i) for i in df.index]
    except Exception:
        index = []
    return {"columns": columns, "records": out, "index": index}


# ----------------------------------------------------------------------
# method handlers (run on the QMT strategy thread)
# ----------------------------------------------------------------------
def _h_full_tick(C, params):
    codes = [str(c) for c in (params.get("codes") or [])]
    return _to_jsonable(C.get_full_tick(codes) or {})


def _h_market_data_ex(C, params):
    field_list = list(params.get("field_list") or [])
    stock_list = [str(s) for s in (params.get("stock_list") or [])]
    period = str(params.get("period") or "1d")
    start_time = str(params.get("start_time") or "")
    end_time = str(params.get("end_time") or "")
    count = int(params.get("count") if params.get("count") is not None else -1)
    dividend_type = str(params.get("dividend_type") or "none")

    # try to make sure local store has the range (best effort)
    if period.endswith("d") and start_time:
        for code in stock_list:
            try:
                C.download_history_data(code, period, start_time,
                                        end_time or datetime.datetime.now()
                                        .strftime("%Y%m%d"))
            except Exception:
                pass

    raw = None
    errors = []
    try:
        raw = C.get_market_data_ex(field_list, stock_list, period,
                                   start_time, end_time, count, dividend_type)
    except Exception as exc:
        errors.append("positional: %s" % exc)
    if raw is None:
        try:
            raw = C.get_market_data_ex(field_list=field_list,
                                       stock_list=stock_list, period=period,
                                       start_time=start_time, end_time=end_time,
                                       count=count, dividend_type=dividend_type)
        except Exception as exc:
            errors.append("kwargs: %s" % exc)
    if raw is None and hasattr(C, "get_market_data"):
        try:
            raw = C.get_market_data(field_list, stock_list, period,
                                    start_time, end_time, count, dividend_type)
        except Exception as exc:
            errors.append("get_market_data: %s" % exc)
    if raw is None:
        raise RuntimeError(" | ".join(errors))

    out = {}
    for code in stock_list:
        item = raw.get(code)
        if item is None:
            continue
        payload = _frame_to_payload(item)
        if payload is not None:
            out[code] = payload
    return out


def _h_sector(C, params):
    sector = str(params.get("sector") or "")
    try:
        data = C.get_stock_list_in_sector(sector)
    except Exception:
        data = C.get_stock_list_in_sector(sector, -1)
    return _to_jsonable(list(data or []))


def _handle(C, req):
    method = str(req.get("method") or "")
    params = req.get("params") or {}
    if method == "ping":
        return {"pong": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "server": "qmt_file_bridge"}
    if method == "get_full_tick":
        return _h_full_tick(C, params)
    if method == "get_market_data_ex":
        return _h_market_data_ex(C, params)
    if method == "get_stock_list_in_sector":
        return _h_sector(C, params)
    raise RuntimeError("unsupported method: %s" % method)


# ----------------------------------------------------------------------
# QMT strategy entry points
# ----------------------------------------------------------------------
def init(C):
    _state["scheduled"] = True
    try:
        if not os.path.isdir(BRIDGE_DIR):
            os.makedirs(BRIDGE_DIR)
    except Exception as exc:
        _log("makedirs failed: %s" % exc)
    start = (datetime.datetime.now()
             + datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        C.run_time("adjust", ADJUST_INTERVAL, start)
        _log("ready: dir=%s interval=%s" % (BRIDGE_DIR, ADJUST_INTERVAL))
    except Exception as exc:
        _log("run_time failed: %s (handlebar fallback only)" % exc)


def adjust(C):
    _write_json(HB_FILE, {"hb": time_now(), "errors": _state["errors"]})
    req = _read_json(REQ_FILE)
    if not isinstance(req, dict):
        return
    key = (str(req.get("sid")), int(req.get("id") or 0))
    if key == _state["last_key"]:
        return  # already served
    _state["last_key"] = key
    try:
        result = _handle(C, req)
        resp = {"sid": req.get("sid"), "id": req.get("id"),
                "ok": True, "result": result}
    except Exception as exc:
        _state["errors"] += 1
        resp = {"sid": req.get("sid"), "id": req.get("id"),
                "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    _write_json(RESP_FILE, resp)


def time_now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def handlebar(C):
    # drain even when run_time is unavailable in this build
    try:
        adjust(C)
    except Exception:
        pass


def stop(C=None):
    _log("stopped")
