#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓提示模型 v2.0 - 策略分析引擎
规则体系(用户自定义):
  1. 两种策略模式:
     - swing_small(震荡小票): 阶梯止盈 10%/20%/30% 分批
     - trend_leader(趋势赛道/主线龙头): 浮盈20%减仓50%
  2. 静态止损: 成本下方-10%
  3. MA20移动止损: 浮盈状态跌破MA20离场
  4. 新高回撤10%清仓: 从跟踪峰值回撤10%清仓
  5. 高位技术形态(放量滞涨/长上影/双顶): 直接提示减仓50%

用法:
  python3 analyze_position.py <data_file.json> --code <code> [--config config/portfolio.json] [--output report.html]
"""
import json
import sys
import os
import argparse
from datetime import datetime

def safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default

def pct_str(v):
    return "-" if v is None else f"{v:+.2f}%"

def money_str(v):
    v = safe_float(v)
    if abs(v) >= 1e8:
        return f"{v/1e8:.2f}亿"
    if abs(v) >= 1e4:
        return f"{v/1e4:.2f}万"
    return f"{v:.0f}元"

# ============ 技术形态检测 ============

def detect_technical_signals(kline_nodes, quote):
    """检测高位放量滞涨/长上影线/双顶信号
    返回: (是否有信号, 信号描述列表)
    """
    if not kline_nodes:
        return False, []
    nodes = sorted(kline_nodes, key=lambda x: x.get("date", ""))
    signals = []

    price = safe_float(quote.get("price", quote.get("close")))
    vol_ratio = safe_float(quote.get("volume_ratio"))
    chg = safe_float(quote.get("change_percent"))
    today_high = safe_float(quote.get("high"))
    today_low = safe_float(quote.get("low"))
    today_close = safe_float(quote.get("price", quote.get("close")))
    today_open = safe_float(quote.get("open"))

    # 近20日高点（用K线最后20根）
    recent = nodes[-20:] if len(nodes) >= 20 else nodes
    recent_highs = [safe_float(n.get("high")) for n in recent if safe_float(n.get("high")) > 0]
    recent_lows = [safe_float(n.get("low")) for n in recent if safe_float(n.get("low")) > 0]
    if not recent_highs:
        return False, []
    peak20 = max(recent_highs)
    low20 = min(recent_lows)
    in_high_zone = price >= peak20 * 0.90  # 处于近20日高位区

    # ---- 信号1: 放量滞涨 ----
    # 量比>=1.5 且 (涨幅<=0.5% 或 收阴) 且 处于高位
    if in_high_zone and vol_ratio >= 1.5 and chg <= 0.5:
        signals.append(f"⚠️高位放量滞涨(量比{vol_ratio:.1f},涨幅{chg:+.2f}%)")

    # ---- 信号2: 长上影线 ----
    # 当日上影长度占振幅比例>=0.5，且收盘低于最高价较多
    if today_high > 0 and today_low > 0 and today_high > today_low:
        upper_shadow = today_high - max(today_close, today_open)
        body = abs(today_close - today_open)
        rng = today_high - today_low
        if upper_shadow / rng >= 0.5 and upper_shadow > body * 1.2 and in_high_zone:
            signals.append(f"⚠️长上影线(上影{upper_shadow:.2f}占振幅{upper_shadow/rng*100:.0f}%)")

    # ---- 信号3: 双顶(需右顶确认,防止把突破创新高误判为双顶) ----
    # 条件: 近20日最高点A与次高点B |A-B|/A<=3%, 间隔>=3日, 中间回落>=5%
    # 确认: 右顶必须已滞涨(收盘明显低于最高) 或 其后收阴K线 或 已跌破两顶间谷底(颈线)
    # 排除: 右顶为最新一根K线且收盘=最高价(光头阳线,正在突破), 不判双顶
    if len(recent) >= 10:
        peaks = sorted(recent, key=lambda n: safe_float(n.get("high")), reverse=True)
        top = peaks[0]
        top_high = safe_float(top.get("high"))
        top_idx = recent.index(top)
        top_close = safe_float(top.get("last", top.get("close")))
        last_node = recent[-1]
        last_close = safe_float(last_node.get("last", last_node.get("close")))
        last_high = safe_float(last_node.get("high"))
        for cand in peaks[1:5]:
            cand_high = safe_float(cand.get("high"))
            cand_idx = recent.index(cand)
            gap = abs(top_idx - cand_idx)
            diff_pct = abs(top_high - cand_high) / top_high * 100
            if gap >= 3 and diff_pct <= 3:
                # 两高点之间是否有回落
                lo_btw = min(safe_float(n.get("low")) for n in recent[min(top_idx, cand_idx):max(top_idx, cand_idx)+1])
                if lo_btw <= min(top_high, cand_high) * 0.95:
                    # ---- 右顶确认检查 ----
                    stale_at_top = top_high > 0 and top_close < top_high * 0.992  # 右顶当日滞涨
                    after = recent[top_idx+1:] if top_idx < len(recent)-1 else []
                    fall_after = any(safe_float(n.get("last", n.get("close"))) < safe_float(n.get("open")) for n in after)
                    broke_neck = any(safe_float(n.get("low")) < lo_btw for n in after)
                    if stale_at_top or fall_after or broke_neck:
                        signals.append(f"⚠️双顶形态(高点{top_high:.2f}/{cand_high:.2f},差{diff_pct:.1f}%)")
                        break

    return len(signals) > 0, signals

# ============ 策略规则引擎 ============

def apply_strategy(pos, quote, kline_nodes, fundflow, technical, strategies, peak_price):
    """根据策略规则计算操作信号"""
    cost = safe_float(pos.get("cost_price"))
    price = safe_float(quote.get("price", quote.get("close")))
    if cost <= 0 or price <= 0:
        return {"signal": "⚠️数据异常", "signal_en": "data_error", "confidence": 0,
                "reason": "成本价或现价数据缺失"}

    stype = pos.get("strategy_type", "swing_small")
    rules = strategies.get(stype, strategies.get("swing_small"))
    sl_pct = safe_float(rules.get("stop_loss_pct", -10.0))
    tp = rules.get("take_profit", {})
    trail_ma_n = safe_float(rules.get("trailing_stop_ma", 20))
    dd_clear_pct = safe_float(rules.get("peak_drawdown_clear_pct", 10.0))
    is_trail = (tp.get("mode") == "trail") or (stype == "ma20_trail")

    profit_pct = (price - cost) / cost * 100.0

    # ---- 行情数据 ----
    chg_1d = safe_float(quote.get("change_percent"))
    chg_5d = safe_float(quote.get("chg_5d"))
    chg_20d = safe_float(quote.get("chg_20d"))
    vol_ratio = safe_float(quote.get("volume_ratio"))
    main_net = safe_float(fundflow.get("MainNetFlow")) if fundflow else 0.0
    main_net_5d = safe_float(fundflow.get("MainNetFlow5D")) if fundflow else 0.0

    ma_dict = (technical or {}).get("ma", {})
    ma20 = safe_float(ma_dict.get("MA_20"))
    ma60 = safe_float(ma_dict.get("MA_60"))
    ma5 = safe_float(ma_dict.get("MA_5"))
    macd_d = (technical or {}).get("macd", {})
    trend, trend_note = trend_from_ma(price, ma_dict)
    macd_txt = macd_signal(macd_d)

    # ---- 静态止损: 成本下方-10% ----
    stop_price = cost * (1 + sl_pct / 100.0)

    # ---- 新高回撤: 峰值价 ----
    peak = peak_price if peak_price and peak_price > 0 else price
    dd_from_peak = (price - peak) / peak * 100.0

    # ---- MA20移动止损 ----
    trail_stop = 0.0
    if is_trail and ma20 > 0:
        trail_stop = ma20  # 移动止盈模式：MA20即离场线(不论盈亏状态)
    elif ma20 > 0 and profit_pct > 0:
        trail_stop = ma20  # 浮盈状态跌破MA20离场
    elif ma20 > 0:
        trail_stop = stop_price

    # ---- 高位技术形态检测 ----
    has_tech, tech_signals = detect_technical_signals(kline_nodes, quote)

    # ---- 止盈档位判断 ----
    tp_info = None
    if tp.get("mode") == "trail":
        # MA20移动止盈: 无固定止盈价，MA20即止盈线
        tp_info = {"mode": "trail", "pct": None, "price": ma20 if ma20 > 0 else None,
                   "action": "pending", "note": "跟随趋势，跌破MA20即止盈离场"}
    elif tp.get("mode") == "half":
        tp_pct = safe_float(tp.get("pct", 20.0))
        tp_price = cost * (1 + tp_pct / 100.0)
        if profit_pct >= tp_pct:
            tp_info = {"mode": "half", "pct": tp_pct, "price": tp_price, "action": tp.get("action", "sell_half"),
                       "note": f"浮盈{profit_pct:.1f}%≥{tp_pct}%，减仓50%"}
        else:
            tp_info = {"mode": "half", "pct": tp_pct, "price": tp_price, "action": "pending",
                       "note": f"距{tp_pct}%止盈线还有{tp_pct-profit_pct:.1f}%"}
    elif tp.get("mode") == "ladder":
        levels = sorted(tp.get("levels", []), key=lambda x: safe_float(x.get("pct", 0)))
        reached = [lv for lv in levels if profit_pct >= safe_float(lv.get("pct", 0))]
        next_lv = None
        for lv in levels:
            if profit_pct < safe_float(lv.get("pct", 0)):
                next_lv = lv
                break
        tp_info = {"mode": "ladder", "levels": levels, "reached": reached, "next": next_lv,
                   "price": cost * (1 + safe_float(next_lv.get("pct", 0)) / 100.0) if next_lv else None,
                   "action": reached[-1].get("action") if reached else "pending",
                   "note": (f"已达{reached[-1]['pct']}%档({reached[-1]['action']})" if reached
                            else (f"距{next_lv['pct']}%档还有{next_lv['pct']-profit_pct:.1f}%" if next_lv else "全部档位已达成"))}

    # ============ 信号综合判定(优先级从高到低) ============
    signal, signal_en, confidence, reasons = "🟡观望", "observe", 50, []

    # P0: 静态止损
    if profit_pct <= sl_pct:
        signal, signal_en, confidence = "🔴止损", "stop", 90
        reasons.append(f"浮亏{profit_pct:.1f}%已达静态止损线{sl_pct:.1f}%(成本-10%)")
    elif price <= stop_price:
        signal, signal_en, confidence = "🔴止损", "stop", 88
        reasons.append(f"现价{price:.2f}跌破静态止损价{stop_price:.2f}")

    # P1: 新高回撤10%清仓
    elif dd_from_peak <= -dd_clear_pct:
        signal, signal_en, confidence = "🔴清仓", "clear", 85
        reasons.append(f"从峰值{peak:.2f}回撤{abs(dd_from_peak):.1f}%≥{dd_clear_pct:.0f}%，触发清仓")

    # P2: MA20移动止损/移动止盈
    elif ma20 > 0 and price < ma20 and (is_trail or profit_pct > 0):
        if is_trail:
            signal, signal_en, confidence = "🔴止盈离场", "stop", 85
            reasons.append(f"已跌破MA20({ma20:.2f})，按移动止盈规则全部离场(减仓后剩余仓位)")
        else:
            signal, signal_en, confidence = "🔴止损", "stop", 80
            reasons.append(f"浮盈状态跌破MA20({ma20:.2f})，移动止损离场")

    # P3: 高位技术形态 → 减仓50% (trail模式仅预警不再二次减仓)
    elif has_tech and not is_trail:
        signal, signal_en, confidence = "🟡减仓50%", "reduce", 72
        reasons.append("；".join(tech_signals))
        reasons.append("高位技术见顶信号，无需等回撤标准，直接减仓50%")

    # P4: 止盈触发
    elif tp_info and tp_info["action"] not in ("pending",):
        signal, signal_en, confidence = "🟡减仓", "reduce", 65
        reasons.append(f"{tp_info['note']}，建议执行{tp_info['action']}")
        if main_net < 0:
            confidence = min(85, confidence + 15)
            reasons.append(f"主力净流出{money_str(main_net)}，止盈信号强化")

    # P5: 趋势向上 + 资金流入 → 持有
    elif (ma20 <= 0 or price >= ma20) and main_net >= 0:
        signal, signal_en, confidence = "🟢持有", "hold", 70 if not is_trail else 75
        if is_trail:
            reasons.append(f"剩余仓位跟随趋势，站上MA20({ma20:.2f})，持有以待移动止盈")
            if has_tech:
                reasons.append(f"⚠️形态预警({'；'.join(tech_signals)})——剩余仓位不再二次减仓，以跌破MA20为唯一离场线")
        else:
            reasons.append(f"站上MA20({ma20:.2f})且主力净流入{money_str(main_net)}")
        if chg_5d > 15:
            confidence = min(85, confidence + 10)
            reasons.append(f"近5日{pct_str(chg_5d)}，短线偏强")
        if tp_info and tp_info["action"] == "pending" and not is_trail:
            reasons.append(f"未达止盈线({tp_info['note']})")

    # P6: 资金流出 + 趋势弱 → 观望/减仓
    elif main_net < 0 and chg_20d < 0:
        signal, signal_en, confidence = "🟡减仓", "reduce", 60
        reasons.append(f"主力净流出{money_str(main_net)}且近20日{pct_str(chg_20d)}")

    return {
        "signal": signal, "signal_en": signal_en, "confidence": confidence,
        "reason": "；".join(reasons),
        "profit_pct": profit_pct, "stop_price": stop_price,
        "peak_price": peak, "dd_from_peak": dd_from_peak,
        "trail_stop": trail_stop, "tp_info": tp_info, "is_trail": is_trail,
        "trend": trend, "trend_note": trend_note, "macd": macd_txt,
        "main_net": main_net, "main_net_5d": main_net_5d,
        "ma20": ma20, "ma60": ma60, "tech_signals": tech_signals,
    }

def trend_from_ma(price, ma_dict):
    ma5 = safe_float(ma_dict.get("MA_5"))
    ma10 = safe_float(ma_dict.get("MA_10"))
    ma20 = safe_float(ma_dict.get("MA_20"))
    ma60 = safe_float(ma_dict.get("MA_60"))
    above, total = 0, 0
    for ma in (ma5, ma10, ma20, ma60):
        if ma > 0:
            total += 1
            if price >= ma:
                above += 1
    if total == 0:
        return "→震荡", "数据不足"
    if above == total:
        return "↑上升", f"站上全部均线(MA20={ma20:.2f})"
    if above >= total * 0.5:
        return "→震荡", f"站上{above}/{total}条均线(MA20={ma20:.2f})"
    return "↓下降", f"仅站上{above}/{total}条均线(MA20={ma20:.2f})"

def macd_signal(macd_dict):
    dif = safe_float(macd_dict.get("DIF"))
    dea = safe_float(macd_dict.get("DEA"))
    hist = safe_float(macd_dict.get("MACD"))
    if dif == 0 and dea == 0:
        return "数据不足"
    if dif > dea and hist > 0:
        return "MACD金叉/红柱"
    if dif < dea and hist < 0:
        return "MACD死叉/绿柱"
    if dif > dea:
        return "DIF在DEA上方"
    return "DIF在DEA下方"

# ============ HTML 卡片生成 ============

SIGNAL_STYLE = {
    "hold":     ("#2e7d32", "#e8f5e9"),
    "reduce":   ("#f9a825", "#fff8e1"),
    "stop":     ("#c62828", "#ffebee"),
    "clear":    ("#b71c1c", "#ffcdd2"),
    "observe":  ("#546e7a", "#eceff1"),
    "data_error": ("#455a64", "#eceff1"),
}

def gen_holding_value_line(currency, csym, price, cost, qty_f, fx_rate):
    """生成持仓市值和盈亏金额行(所有币种都显示)"""
    if qty_f <= 0:
        return ""
    mkt_val = price * qty_f
    pnl_amt = (price - cost) * qty_f
    pnl_color = "#e53935" if pnl_amt >= 0 else "#43a047"
    pnl_sign = "+" if pnl_amt >= 0 else "-"
    abs_pnl = abs(pnl_amt)
    if currency == "CNY":
        return (f'<div style="font-size:11px;color:#666;margin:-6px 0 10px;">'
                f'持仓市值 <b style="color:#333;">¥{mkt_val:,.2f}</b>'
                f' · 盈亏 <b style="color:{pnl_color};">{pnl_sign}¥{abs_pnl:,.2f}</b>'
                f'</div>')
    else:
        cny_mkt = mkt_val * fx_rate if fx_rate > 0 else 0
        cny_pnl = pnl_amt * fx_rate if fx_rate > 0 else 0
        abs_cny_pnl = abs(cny_pnl)
        return (f'<div style="font-size:11px;color:#666;margin:-6px 0 10px;">'
                f'持仓市值 <b style="color:#333;">{csym}{mkt_val:,.2f}</b>'
                f'{f" ≈ ¥{cny_mkt:,.0f}" if cny_mkt else ""}'
                f' · 盈亏 <b style="color:{pnl_color};">{pnl_sign}{csym}{abs_pnl:,.2f}</b>'
                f'{f" ≈ {pnl_sign}¥{abs_cny_pnl:,.0f}" if cny_pnl else ""}'
                f'（汇率 {currency}/CNY = {fx_rate:.4f}）'
                f'</div>')


def gen_html_card(pos, quote, analysis, ts, fx=None):
    name = pos.get("name", "未知")
    code = pos.get("code", "")
    market = pos.get("market", "")
    cost = safe_float(pos.get("cost_price"))
    qty = pos.get("quantity")
    price = safe_float(quote.get("price", quote.get("close")))
    profit_pct = analysis["profit_pct"]
    stype = pos.get("strategy_type", "swing_small")
    stype_name = {"swing_small": "震荡小票", "trend_leader": "趋势龙头", "ma20_trail": "MA20移动止盈"}.get(stype, stype)

    # 币种处理
    currency = pos.get("currency") or ({"美股": "USD", "港股": "HKD"}.get(market, "CNY"))
    csym = {"USD": "$", "HKD": "HK$"}.get(currency, "")
    fx = fx or {}
    fx_rate = safe_float(fx.get(f"{currency}CNY"), 0)
    qty_f = safe_float(qty)
    cny_mkt = price * qty_f * fx_rate if fx_rate > 0 else 0

    sig_key = analysis["signal_en"]
    conf = analysis["confidence"]
    color, bg = SIGNAL_STYLE.get(sig_key, ("#455a64", "#eceff1"))

    up_color, down_color = "#e53935", "#43a047"
    pcolor = up_color if profit_pct >= 0 else down_color

    chg_1d = safe_float(quote.get("change_percent"))
    chg_5d = safe_float(quote.get("chg_5d"))
    chg_20d = safe_float(quote.get("chg_20d"))
    vratio = safe_float(quote.get("volume_ratio"))
    turnover = safe_float(quote.get("turnover_rate"))
    pe = safe_float(quote.get("pe_ratio"))
    pb = safe_float(quote.get("pb_ratio"))
    main_net = analysis["main_net"]
    main_net_5d = analysis["main_net_5d"]

    # 市值（用于标注小票/龙头）
    mcap = safe_float(quote.get("total_market_cap"))

    # 止盈止损展示
    stop_p = analysis["stop_price"]
    trail_p = analysis["trail_stop"] if analysis["trail_stop"] > 0 else stop_p
    tp = analysis["tp_info"]
    if tp and tp.get("mode") == "trail":
        tp_price = trail_p
        tp_txt = "MA20移动止盈"
    elif tp:
        if tp["mode"] == "half":
            tp_price = tp["price"]
            tp_txt = f"{tp_price:.2f} (+{tp['pct']:.0f}%)" if tp["action"] == "pending" else f"已达标→{tp['action']}"
        else:
            if tp["reached"]:
                tp_price = tp["price"] or price
                tp_txt = f"已达{tp['reached'][-1]['pct']:.0f}%档→{tp['reached'][-1]['action']}"
            else:
                tp_price = tp["price"] or price
                nxt = tp["next"]
                tp_txt = f"{tp_price:.2f} (+{nxt['pct']:.0f}%)" if nxt else "全部档位达成"
    else:
        tp_price, tp_txt = price, "—"

    # 盈亏比
    rr = abs((tp_price - price) / max(price - stop_p, 0.01))

    # 技术信号区
    tech_html = ""
    if analysis["tech_signals"]:
        tech_html = "".join(
            f'<div style="background:#ffebee;border:1px solid #ffcdd2;border-radius:6px;padding:5px 8px;margin-bottom:6px;font-size:11px;color:#b71c1c;">{s}</div>'
            for s in analysis["tech_signals"])

    qty_txt = f" · 持仓{qty}股" if qty else ""
    mcap_txt = f" · 市值{money_str(mcap)}" if mcap and market == "A股" else ""

    html = f"""<section style="max-width:480px;margin:0 auto;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;border-radius:14px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.12);border:1px solid #eee;">
  <section style="background:linear-gradient(135deg,{color},#1a1a2e);color:white;padding:18px 20px;">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <div>
        <h1 style="margin:0;font-size:18px;font-weight:700;">📊 {name} 持仓提示</h1>
        <p style="margin:3px 0 0;font-size:11px;color:rgba(255,255,255,0.7);">{code} · {market}{qty_txt}{mcap_txt}</p>
      </div>
      <span style="background:rgba(255,255,255,0.2);padding:3px 12px;border-radius:12px;font-size:12px;white-space:nowrap;">置信度{conf}%</span>
    </div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;">
      <p style="margin:0;font-size:16px;font-weight:700;">{analysis['signal']}</p>
      <span style="background:rgba(255,255,255,0.15);padding:2px 10px;border-radius:10px;font-size:11px;">策略:{stype_name}</span>
    </div>
    <p style="margin:4px 0 0;font-size:11px;color:rgba(255,255,255,0.75);">{analysis['trend']} · {analysis['macd']} · 峰值回撤{analysis['dd_from_peak']:.1f}%</p>
  </section>
  <section style="padding:16px 16px 12px;background:#fff;">
    <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:12px;">
      <span style="font-size:26px;font-weight:800;color:{pcolor};">{csym}{price:.2f}</span>
      <span style="font-size:14px;color:{up_color if chg_1d>=0 else down_color};">{pct_str(chg_1d)}</span>
      <span style="font-size:12px;color:#999;">成本 {csym}{cost:.2f}</span>
      <span style="font-size:13px;font-weight:600;color:{pcolor};margin-left:auto;">{pct_str(profit_pct)}</span>
    </div>
    {gen_holding_value_line(currency, csym, price, cost, qty_f, fx_rate)}
    <div style="display:flex;gap:8px;margin-bottom:12px;">
      <div style="flex:1;text-align:center;background:#f5f5f5;border-radius:8px;padding:8px 4px;">
        <div style="font-size:14px;font-weight:700;color:{up_color if chg_1d>=0 else down_color};">{pct_str(chg_1d)}</div>
        <div style="font-size:10px;color:#999;">1日</div>
      </div>
      <div style="flex:1;text-align:center;background:#f5f5f5;border-radius:8px;padding:8px 4px;">
        <div style="font-size:14px;font-weight:700;color:{up_color if chg_5d>=0 else down_color};">{pct_str(chg_5d)}</div>
        <div style="font-size:10px;color:#999;">5日</div>
      </div>
      <div style="flex:1;text-align:center;background:#f5f5f5;border-radius:8px;padding:8px 4px;">
        <div style="font-size:14px;font-weight:700;color:{up_color if chg_20d>=0 else down_color};">{pct_str(chg_20d)}</div>
        <div style="font-size:10px;color:#999;">20日</div>
      </div>
      <div style="flex:1;text-align:center;background:#f5f5f5;border-radius:8px;padding:8px 4px;">
        <div style="font-size:14px;font-weight:700;color:{up_color if main_net>=0 else down_color};">{money_str(main_net)}</div>
        <div style="font-size:10px;color:#999;">主力净流入</div>
      </div>
    </div>
    {tech_html}
    <div style="background:{bg};border-radius:8px;padding:12px;margin-bottom:10px;">
      <strong style="font-size:13px;color:{color};">🎯 信号依据</strong>
      <p style="margin:5px 0 0;font-size:12px;color:#444;line-height:1.6;">{analysis['reason']}</p>
      <p style="margin:4px 0 0;font-size:11px;color:#777;">{analysis['trend_note']} · 主力5日{money_str(main_net_5d)} · 量比{vratio:.1f} · 换手{turnover:.1f}%</p>
      {f"<p style='margin:4px 0 0;font-size:11px;color:#777;'>PE(TTM) {pe:.1f} · PB {pb:.2f} · 估值偏高需注意</p>" if pe > 50 else f"<p style='margin:4px 0 0;font-size:11px;color:#777;'>PE(TTM) {pe:.1f} · PB {pb:.2f}</p>"}
    </div>
    <div style="background:#fafafa;border-radius:8px;padding:12px;margin-bottom:10px;">
      <strong style="font-size:12px;color:#666;">🛡️ 止盈止损参考</strong>
      <div style="display:flex;justify-content:space-between;margin-top:8px;">
        <div style="flex:1;"><span style="font-size:12px;color:#c62828;">静态止损</span><br><span style="font-size:15px;font-weight:700;color:#c62828;">{csym}{stop_p:.2f}</span><br><span style="font-size:10px;color:#999;">成本-10%</span></div>
        <div style="flex:1;text-align:center;"><span style="font-size:12px;color:#e65100;">{'MA20止盈线' if analysis['is_trail'] else '移动止损'}</span><br><span style="font-size:15px;font-weight:700;color:#e65100;">{csym}{trail_p:.2f}</span><br><span style="font-size:10px;color:#999;">MA20</span></div>
        <div style="flex:1;text-align:right;"><span style="font-size:12px;color:#2e7d32;">止盈</span><br><span style="font-size:15px;font-weight:700;color:#2e7d32;">{csym}{tp_price:.2f}</span><br><span style="font-size:10px;color:#999;">{tp_txt}</span></div>
      </div>
      <div style="margin-top:8px;padding-top:8px;border-top:1px dashed #ddd;font-size:11px;color:#777;">
        盈亏比 {rr:.1f}:1 · 峰值跟踪 {analysis['peak_price']:.2f}(回撤{analysis['dd_from_peak']:+.1f}%) · MA20={analysis['ma20']:.2f} · MA60={analysis['ma60']:.2f}
      </div>
    </div>
    <div style="background:{bg};border-radius:8px;padding:12px;">
      <strong style="font-size:13px;color:{color};">💡 操作建议</strong>
      <p style="margin:5px 0 0;font-size:12px;color:#444;line-height:1.6;">{op_advice(analysis, price, stype_name)}</p>
    </div>
  </section>
  <footer style="text-align:center;padding:10px;font-size:10px;color:#999;background:#fafafa;">
    数据来自腾讯自选股 · {ts} | 仅供参考，不构成投资建议 | 本分析由通达信AI智能体生成
  </footer>
</section>"""
    return html

def op_advice(analysis, price, stype_name):
    sig = analysis["signal_en"]
    is_trail = analysis.get("is_trail")
    if sig == "clear":
        return (f"<strong>清仓离场</strong>：从峰值{analysis['peak_price']:.2f}回撤{abs(analysis['dd_from_peak']):.1f}%已达10%红线，"
                "按新高回撤规则无条件清仓，落袋为安。")
    if sig == "stop" and is_trail:
        return (f"<strong>止盈离场</strong>：已跌破MA20({analysis['ma20']:.2f})，按移动止盈规则卖出全部剩余仓位，"
                "本轮趋势结束，利润落袋。")
    if sig == "stop":
        return (f"<strong>止损离场</strong>：{analysis['reason']}。"
                "到价即卖，不犹豫、不补仓，执行交易纪律。")
    if sig == "reduce":
        return (f"<strong>减仓50%</strong>：{analysis['reason']}。"
                f"剩余仓位移动止损保护(MA20={analysis['ma20']:.2f})，跌破即离场。")
    if sig == "hold" and is_trail:
        return (f"<strong>继续持有(移动止盈模式)</strong>：{analysis['reason']}。"
                f"唯一离场线 MA20={analysis['ma20']:.2f}，跌破即全部止盈离场；"
                f"静态止损{analysis['stop_price']:.2f}兜底，峰值回撤10%清仓。")
    if sig == "hold":
        tp_note = ""
        if analysis.get("tp_info") and analysis["tp_info"]["action"] == "pending":
            tp_note = f"，距止盈线还有{analysis['tp_info']['note'].replace('距', '').replace('止盈线', '')}"
        return (f"<strong>继续持有</strong>：{analysis['reason']}{tp_note}。"
                f"静态止损{analysis['stop_price']:.2f}，峰值回撤10%即清仓，出现放量滞涨/长上影/双顶信号减仓50%。")
    return (f"<strong>观望等待</strong>：{analysis['reason']}。"
            "等待方向选择：放量站上MA20确认后持有，跌破静态止损价立即离场。")

# ============ 主流程 ============

def main():
    parser = argparse.ArgumentParser(description="持仓提示模型-策略分析引擎v2")
    parser.add_argument("data_file", help="行情数据JSON文件")
    parser.add_argument("--code", required=True, help="股票代码")
    parser.add_argument("--config", default=None, help="持仓配置文件路径")
    parser.add_argument("--output", default=None, help="输出HTML文件路径")
    parser.add_argument("--type", default="evening", choices=["morning", "noon", "evening"])
    args = parser.parse_args()

    with open(args.data_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    quote = data.get("quote", {})
    kline_nodes = data.get("kline", {}).get("nodes", []) or data.get("kline_nodes", [])
    fundflow = data.get("fundflow", {})
    technical = data.get("technical", {})

    config_path = args.config or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "portfolio.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    pos = None
    for p in config.get("positions", []):
        if p.get("code") == args.code:
            pos = p
            break
    if pos is None:
        print(json.dumps({"ok": False, "message": f"配置中未找到持仓 {args.code}"}, ensure_ascii=False))
        sys.exit(1)

    strategies = config.get("strategies", {})
    peak_price = safe_float(pos.get("peak_price")) if pos.get("peak_price") else None

    analysis = apply_strategy(pos, quote, kline_nodes, fundflow, technical, strategies, peak_price)

    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M")

    if args.output:
        html = gen_html_card(pos, quote, analysis, ts, fx=config.get("fx", {}))
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"已生成报告: {args.output}")

    # 若现价创新高，输出建议更新peak_price
    new_peak = max(peak_price or 0, safe_float(quote.get("price", quote.get("close"))))

    summary = {
        "ok": True, "code": args.code, "name": pos.get("name"),
        "signal": analysis["signal"], "confidence": analysis["confidence"],
        "price": safe_float(quote.get("price", quote.get("close"))),
        "cost": pos.get("cost_price"), "profit_pct": round(analysis["profit_pct"], 2),
        "stop_price": round(analysis["stop_price"], 2),
        "peak_price": round(new_peak, 2),
        "reason": analysis["reason"],
        "type": args.type, "generated_at": ts,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
