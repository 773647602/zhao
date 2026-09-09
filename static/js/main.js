// AI 量化交易系统 -- 通用前端工具
// =============================================================
// 提供:  请求封装 / 数字格式化 / 时间格式化 / 通知

window.App = (function () {
  'use strict';

  /** REST 请求 (GET) */
  async function request(url, opts = {}) {
    opts.headers = Object.assign(
      { 'Content-Type': 'application/json' },
      opts.headers || {}
    );
    if (opts.body && typeof opts.body !== 'string') {
      opts.body = JSON.stringify(opts.body);
    }
    const r = await fetch(url, opts);
    const ct = r.headers.get('content-type') || '';
    if (ct.includes('application/json')) {
      return await r.json();
    }
    return await r.text();
  }

  const get = (url) => request(url, { method: 'GET' });

  /**
   * POST：HTTP 非 2xx 时统一成 { ok: false, message }，避免业务里 typeof r === 'object' && r.ok 误判
   */
  async function post(url, body) {
    const opts = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: typeof body === 'string' ? body : JSON.stringify(body),
    };
    const r = await fetch(url, opts);
    const ct = r.headers.get('content-type') || '';
    if (ct.includes('application/json')) {
      const data = await r.json();
      if (!r.ok) {
        let msg = 'HTTP ' + r.status;
        if (data && data.detail !== undefined) {
          msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
        } else if (data && data.message) {
          msg = data.message;
        }
        return { ok: false, message: msg };
      }
      return data;
    }
    const text = await r.text();
    if (!r.ok) {
      return {
        ok: false,
        message: 'HTTP ' + r.status + (text ? ': ' + text.slice(0, 160) : ''),
      };
    }
    return text;
  }

  /** 数字格式化 */
  function fmtNum(v, digits = 2) {
    if (v === null || v === undefined || isNaN(v)) return '-';
    return Number(v).toLocaleString('zh-CN', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }
  function fmtPct(v, digits = 2) {
    if (v === null || v === undefined || isNaN(v)) return '-';
    return (Number(v) * 100).toFixed(digits) + '%';
  }
  function fmtSign(v, digits = 2, suffix = '') {
    if (v === null || v === undefined || isNaN(v)) return '-';
    const n = Number(v);
    const sign = n > 0 ? '+' : '';
    return sign + n.toFixed(digits) + suffix;
  }
  function pnlClass(v) {
    if (v === null || v === undefined || isNaN(v)) return '';
    return Number(v) > 0 ? 'pos' : (Number(v) < 0 ? 'neg' : '');
  }

  /** 信号 / 订单时间戳格式化:
   *  ISO 输入 (YYYY-MM-DDTHH:MM:SS) -> 短显示
   *  - 同一年: '04-15 15:00'
   *  - 缺失: '-'
   */
  function fmtSignalTs(ts) {
    if (!ts || typeof ts !== 'string') return '-';
    // 兼容 ISO 与 'YYYY-MM-DD HH:MM:SS'
    const m = ts.match(/^(\d{4})-(\d{2})-(\d{2})[T\s](\d{2}):(\d{2})(?::\d{2})?/);
    if (!m) return ts;
    return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
  }

  /** 简单 toast */
  function toast(msg, kind = 'info') {
    const el = document.createElement('div');
    const colors = {
      info:    'bg-indigo-600',
      success: 'bg-green-600',
      warn:    'bg-yellow-600',
      danger:  'bg-red-600',
    };
    el.className =
      'fixed top-4 right-4 z-[200] max-w-md px-4 py-3 rounded-lg text-white shadow-2xl text-sm font-medium leading-snug ring-2 ring-white/20 ' +
      (colors[kind] || colors.info);
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3000);
  }

  // 事件流 / 告警 -- 把 events 数组里的元素映射成 {level, time, msg, badge_class}
  // 兼容两种来源:
  //   1) 主循环 loop_cycle 事件: {ts, type:"loop_cycle", signal_count, duration_ms}
  //   2) 控制/告警事件: {ts, level: "INFO|WARN|CRITICAL|FATAL", title, message?, source?}
  // 友好化: 事件流面向用户, 不显示原始 type/字段名, 翻译成中文
  const _LEVEL_TEXT = {
    INFO:     '提示',
    WARN:     '警告',
    CRITICAL: '严重',
    FATAL:    '致命',
    TICK:     '心跳',
  };
  function alertLevelText(e) {
    if (!e) return '';
    const lv = (e.level || (e.type === 'loop_cycle' ? 'TICK' : 'INFO')).toUpperCase();
    return _LEVEL_TEXT[lv] || lv;
  }
  function alertBadgeClass(e) {
    if (!e) return 'badge-gray';
    const lv = (e.level || (e.type === 'loop_cycle' ? 'TICK' : 'INFO')).toUpperCase();
    const map = {
      INFO:     'badge-info',
      WARN:     'badge-warning',
      CRITICAL: 'badge-danger',
      FATAL:    'badge-danger',
      TICK:     'badge-gray',
    };
    return map[lv] || 'badge-gray';
  }
  function alertTimeText(e) {
    if (!e || !e.ts) return '';
    const m = String(e.ts).match(/(\d{2}):(\d{2}):(\d{2})/);
    return m ? `${m[1]}:${m[2]}:${m[3]}` : String(e.ts).slice(-8);
  }
  function alertMsgText(e) {
    if (!e) return '';
    if (e.type === 'loop_cycle') {
      const sc = e.signal_count != null ? e.signal_count : 0;
      const ms = e.duration_ms != null ? e.duration_ms : 0;
      const tail = sc === 0 ? '本轮无新信号' : `本轮触发 ${sc} 个信号`;
      return `策略巡检 · ${tail} · 耗时 ${ms}ms`;
    }
    const head = e.title || e.message || '';
    const src  = e.source ? ` (来自 ${e.source})` : '';
    return head + src;
  }

  return {
    get, post, request,
    fmtNum, fmtPct, fmtSign, pnlClass, fmtSignalTs, toast,
    alertLevelText, alertBadgeClass, alertTimeText, alertMsgText,
  };
})();
