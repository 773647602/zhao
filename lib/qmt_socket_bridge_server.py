#coding:gbk
r"""QMT socket bridge server (pure stdlib, no _ctypes / no pyzmq).

Deploy target: D:\GuoJin Securities QMT Trading Terminal\python\QMT_SOCKET_BRIDGE.py
Load and run this file in the QMT strategy editor (same as BIGQMT_DRYRUN_NO_REDIS.py).

Why this exists: the QMT editor sandbox lacks _ctypes, so pyzmq (and therefore
the bigqmt_signal_trader zmq bridge) cannot start. This bridge speaks a tiny
length-prefixed JSON protocol over plain TCP, which needs nothing beyond the
standard library.

Protocol (client -> server, then server -> client):
    4-byte big-endian length + UTF-8 JSON body
    request : {"id": int, "method": str, "params": {...}}
    response: {"id": int, "ok": true,  "result": ...}
              {"id": int, "ok": false, "error": str}

Thread safety: QMT only allows ContextInfo calls from the strategy callback
thread. A background accept thread queues requests; the official
run_time("adjust", ...) callback drains the queue and answers from the strategy
thread, exactly like the upstream redis_rpc runtime.

Supported methods (read-only):
    ping                     -> {"pong": ts, "account": ...}
    get_full_tick            {"codes": [...]}          -> {code: tick_dict}
    get_market_data_ex       {"field_list","stock_list","period","start_time",
                              "end_time","count","dividend_type"}
                             -> {code: {"columns":[...], "records":[[...]]}}
    get_stock_list_in_sector {"sector": str}           -> [code, ...]
"""
import datetime
import json
import os
import socket
import struct
import sys
import threading
import traceback

HOST = "127.0.0.1"
PORT = 15055
ADJUST_INTERVAL = "100nMilliSecond"
BATCH_PER_ADJUST = 20
MAX_MSG_BYTES = 32 * 1024 * 1024

_log_path = ""


def _log(msg):
    text = "%s [qmt_socket_bridge] %s" % (
        datetime.datetime.now().strftime("%H:%M:%S"), msg)
    try:
        print(text)
    except Exception:
        pass
    global _log_path
    try:
        if not _log_path:
            base = os.path.dirname(os.path.abspath(__file__))
            _log_path = os.path.join(base, "qmt_socket_bridge.log")
        with open(_log_path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------
# state
# ----------------------------------------------------------------------
_pending = []                 # [(conn, request_dict)]
_pending_lock = threading.Lock()
_conns = []
_conns_lock = threading.Lock()
_server_sock = None
_stop = threading.Event()
_scheduled = False
_last_adjust = [0.0]


def _send_msg(conn, obj):
    payload = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
    conn.sendall(struct.pack(">I", len(payload)) + payload)


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _client_loop(conn, addr):
    _log("client connected %s" % (addr,))
    try:
        while not _stop.is_set():
            head = _recv_exact(conn, 4)
            if head is None:
                break
            (size,) = struct.unpack(">I", head)
            if size > MAX_MSG_BYTES:
                _send_msg(conn, {"id": 0, "ok": False, "error": "oversized request"})
                continue
            body = _recv_exact(conn, size)
            if body is None:
                break
            try:
                req = json.loads(body.decode("utf-8"))
            except Exception as exc:
                _send_msg(conn, {"id": 0, "ok": False, "error": "bad json: %s" % exc})
                continue
            with _pending_lock:
                _pending.append((conn, req))
    except Exception as exc:
        _log("client loop end %s: %s" % (addr, exc))
    finally:
        try:
            conn.close()
        except Exception:
            pass
        with _pending_lock:
            for item in list(_pending):
                if item[0] is conn:
                    _pending.remove(item)
        with _conns_lock:
            if conn in _conns:
                _conns.remove(conn)
        _log("client disconnected %s" % (addr,))


def _accept_loop():
    global _server_sock
    try:
        _server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _server_sock.bind((HOST, PORT))
        _server_sock.listen(8)
        _server_sock.settimeout(1.0)
        _log("listening tcp://%s:%d (pid=%s)" % (HOST, PORT, os.getpid()))
    except Exception as exc:
        _log("bind FAILED %s" % exc)
        return
    while not _stop.is_set():
        try:
            conn, addr = _server_sock.accept()
        except socket.timeout:
            continue
        except Exception as exc:
            if not _stop.is_set():
                _log("accept error: %s" % exc)
            break
        conn.settimeout(None)
        t = threading.Thread(target=_client_loop, args=(conn, addr))
        t.daemon = True
        t.start()
        with _conns_lock:
            _conns.append(conn)
    try:
        _server_sock.close()
    except Exception:
        pass


# ----------------------------------------------------------------------
# request handling (runs on the QMT strategy thread)
# ----------------------------------------------------------------------
def _to_jsonable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return str(value)


def _frame_to_payload(df):
    """DataFrame -> {"columns": [...], "records": [[...]]} (or decline)."""
    columns = None
    records = None
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
        out.append([None if (v is None or v != v) else _to_jsonable(v) for v in row])
    try:
        index = [str(i) for i in df.index]
    except Exception:
        try:
            index = [str(i) for i in range(len(out))]
        except Exception:
            index = []
    return {"columns": columns, "records": out, "index": index}


def _handle(C, req):
    method = str(req.get("method") or "")
    params = req.get("params") or {}
    rid = req.get("id")

    if method == "ping":
        return {"id": rid, "ok": True,
                "result": {"pong": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           "server": "qmt_socket_bridge"}}

    if method == "get_full_tick":
        codes = [str(c) for c in (params.get("codes") or [])]
        data = C.get_full_tick(codes) or {}
        return {"id": rid, "ok": True, "result": _to_jsonable(data)}

    if method == "get_market_data_ex":
        field_list = list(params.get("field_list") or [])
        stock_list = [str(s) for s in (params.get("stock_list") or [])]
        period = str(params.get("period") or "1d")
        start_time = str(params.get("start_time") or "")
        end_time = str(params.get("end_time") or "")
        count = int(params.get("count") if params.get("count") is not None else -1)
        dividend_type = str(params.get("dividend_type") or "none")
        raw = None
        errors = []
        # Shape 1: xtdata-style kwargs (works on most QMT builds)
        try:
            raw = C.get_market_data_ex(field_list, stock_list, period,
                                       start_time, end_time, count, dividend_type)
        except Exception as exc:
            errors.append("kwargs-positional: %s" % exc)
        # Shape 2: keyword-only
        if raw is None:
            try:
                raw = C.get_market_data_ex(field_list=field_list, stock_list=stock_list,
                                           period=period, start_time=start_time,
                                           end_time=end_time, count=count,
                                           dividend_type=dividend_type)
            except Exception as exc:
                errors.append("kwargs-only: %s" % exc)
        # Shape 3: get_market_data fallback
        if raw is None and hasattr(C, "get_market_data"):
            try:
                raw = C.get_market_data(field_list, stock_list, period,
                                        start_time, end_time, count, dividend_type)
            except Exception as exc:
                errors.append("get_market_data: %s" % exc)
        if raw is None:
            return {"id": rid, "ok": False, "error": " | ".join(errors)}
        out = {}
        for code in list(stock_list) + [k for k in raw if k not in stock_list]:
            item = raw.get(code)
            if item is None:
                continue
            payload = _frame_to_payload(item)
            if payload is not None:
                out[code] = payload
        return {"id": rid, "ok": True, "result": out}

    if method == "get_stock_list_in_sector":
        sector = str(params.get("sector") or "")
        data = None
        try:
            data = C.get_stock_list_in_sector(sector)
        except Exception:
            data = C.get_stock_list_in_sector(sector, -1)
        return {"id": rid, "ok": True, "result": _to_jsonable(list(data or []))}

    return {"id": rid, "ok": False, "error": "unsupported method: %s" % method}


def _drain(C):
    with _pending_lock:
        batch = _pending[:BATCH_PER_ADJUST]
        for item in batch:
            _pending.remove(item)
    for conn, req in batch:
        try:
            resp = _handle(C, req)
        except Exception as exc:
            resp = {"id": req.get("id"), "ok": False,
                    "error": "%s: %s" % (type(exc).__name__, exc)}
            _log("handler error: %s\n%s" % (exc, traceback.format_exc()))
        try:
            _send_msg(conn, resp)
        except Exception as exc:
            _log("send failed: %s" % exc)


def _schedule(C):
    global _scheduled
    if _scheduled:
        return
    _scheduled = True
    start = (datetime.datetime.now() + datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        C.run_time("adjust", ADJUST_INTERVAL, start)
        _log("scheduled adjust interval=%s" % ADJUST_INTERVAL)
    except Exception as exc:
        _log("run_time failed: %s (falling back to handlebar drain)" % exc)


# ----------------------------------------------------------------------
# QMT strategy entry points (names are what the editor calls)
# ----------------------------------------------------------------------
def init(C):
    _log("init called")
    try:
        sys.setswitchinterval(0.005)
    except Exception:
        pass
    _schedule(C)
    t = threading.Thread(target=_accept_loop)
    t.daemon = True
    t.start()
    _log("ready: load/run finished, bridge accepting on %s:%d" % (HOST, PORT))


def adjust(C):
    now = datetime.datetime.now().timestamp()
    _last_adjust[0] = now
    _drain(C)


def handlebar(C):
    # Drain even if run_time is unavailable in this build.
    _drain(C)


def stop(C=None):
    _stop.set()
    with _conns_lock:
        for c in _conns:
            try:
                c.close()
            except Exception:
                pass
    _log("stopped")
