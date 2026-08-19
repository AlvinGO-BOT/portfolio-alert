#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端数据获取层 - 用公开行情接口替代本地MCP
数据源:
  报价:   qt.gtimg.cn (腾讯, GBK编码)  A股/美股/港股
  日K线:  web.ifzq.gtimg.cn (腾讯)
  资金流: push2.eastmoney.com (东方财富, 仅A股)
  汇率:   open.er-api.com
输出: reports/data_{code}_{YYYYMMDD}.json (与 analyze_position.py 对接)
副作用: 更新 config/portfolio.json 的 fx 汇率段和各持仓 peak_price
用法: python3 fetch_data.py [--config config/portfolio.json] [--outdir reports]
"""
import json
import os
import re
import sys
import argparse
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://quote.eastmoney.com/",
}
CST = timezone(timedelta(hours=8))


def http_get(url, decode="utf-8", timeout=15):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception:
        # 部分接口(如东方财富)屏蔽Python urllib指纹, 用curl兜底
        import subprocess
        r = subprocess.run(["curl", "-s", "--max-time", str(timeout), "-A", UA["User-Agent"], url],
                           capture_output=True)
        if r.returncode != 0:
            raise
        raw = r.stdout
    return raw.decode(decode, errors="replace")


def safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


# ============ 汇率 ============

def fetch_fx():
    """USD基准 → USDCNY / HKDCNY"""
    try:
        data = json.loads(http_get("https://open.er-api.com/v6/latest/USD"))
        rates = data.get("rates", {})
        cny = safe_float(rates.get("CNY"))
        hkd = safe_float(rates.get("HKD"))
        if cny <= 0 or hkd <= 0:
            return {}
        return {"USDCNY": round(cny, 4), "HKDCNY": round(cny / hkd, 4)}
    except Exception as e:
        print(f"⚠️ 汇率获取失败: {e}", file=sys.stderr)
        return {}


# ============ 报价 ============

def fetch_quotes(codes):
    """qt.gtimg.cn 批量报价, codes为腾讯格式(sz002384/usMSFT.OQ/hk00700)
    美股接口用不带后缀的代码(usMSFT)查询, 结果映射回带后缀的key
    返回 {code: [字段列表]}"""
    query_map = {}  # 查询代码 -> 原始代码
    qcodes = []
    for code in codes:
        if code.startswith("us") and "." in code:
            base = code.split(".")[0]
            query_map[base] = code
            qcodes.append(base)
        else:
            qcodes.append(code)
    q = ",".join(qcodes)
    try:
        txt = http_get(f"https://qt.gtimg.cn/q={urllib.parse.quote(q)}", decode="gbk")
    except Exception as e:
        print(f"⚠️ 报价获取失败: {e}", file=sys.stderr)
        return {}
    raw = {}
    for m in re.finditer(r'v_(\S+?)="([^"]*)"', txt):
        key, val = m.group(1), m.group(2)
        if val and val != "1":
            raw[key] = val.split("~")
    out = {}
    for qc, fields in raw.items():
        out[query_map.get(qc, qc)] = fields
    return out


def parse_quote_cn(f, kline_nodes):
    """解析A股/港股报价字段"""
    chg_5d = chg_20d = 0.0
    closes = [safe_float(n.get("last")) for n in kline_nodes if safe_float(n.get("last")) > 0]
    if len(closes) >= 6:
        chg_5d = (closes[-1] - closes[-6]) / closes[-6] * 100
    if len(closes) >= 21:
        chg_20d = (closes[-1] - closes[-21]) / closes[-21] * 100
    vol_ratio = safe_float(f[49]) if len(f) > 49 else 0.0
    if vol_ratio <= 0 and len(kline_nodes) >= 6:
        vols = [safe_float(n.get("volume")) for n in kline_nodes[-6:-1]]
        vols = [v for v in vols if v > 0]
        if vols and safe_float(kline_nodes[-1].get("volume")) > 0:
            vol_ratio = safe_float(kline_nodes[-1].get("volume")) / (sum(vols) / len(vols))
    return {
        "name": f[1] if len(f) > 1 else "",
        "price": safe_float(f[3]),
        "prev_close": safe_float(f[4]),
        "open": safe_float(f[5]),
        "high": safe_float(f[33]) if len(f) > 33 else 0.0,
        "low": safe_float(f[34]) if len(f) > 34 else 0.0,
        "change": safe_float(f[31]) if len(f) > 31 else 0.0,
        "change_percent": safe_float(f[32]) if len(f) > 32 else 0.0,
        "volume": safe_float(f[36]) if len(f) > 36 else 0.0,
        "amount": (safe_float(f[37]) if len(f) > 37 else 0.0) * 1e4,
        "turnover_rate": safe_float(f[38]) if len(f) > 38 else 0.0,
        "volume_ratio": vol_ratio,
        "pe_ratio": safe_float(f[39]) if len(f) > 39 else 0.0,
        "pb_ratio": safe_float(f[46]) if len(f) > 46 else 0.0,
        "total_market_cap": (safe_float(f[45]) if len(f) > 45 else 0.0) * 1e8,
        "chg_5d": chg_5d,
        "chg_20d": chg_20d,
        "time": f[30][:8] if len(f) > 30 and f[30] else datetime.now(CST).strftime("%Y%m%d"),
    }


def parse_quote_us(f, kline_nodes):
    """解析美股报价字段(q=usXXXX, 布局与A股不同)"""
    chg_5d = chg_20d = 0.0
    closes = [safe_float(n.get("last")) for n in kline_nodes if safe_float(n.get("last")) > 0]
    if len(closes) >= 6:
        chg_5d = (closes[-1] - closes[-6]) / closes[-6] * 100
    if len(closes) >= 21:
        chg_20d = (closes[-1] - closes[-21]) / closes[-21] * 100
    vol_ratio = 0.0
    if len(kline_nodes) >= 6:
        vols = [safe_float(n.get("volume")) for n in kline_nodes[-6:-1]]
        vols = [v for v in vols if v > 0]
        if vols and safe_float(kline_nodes[-1].get("volume")) > 0:
            vol_ratio = safe_float(kline_nodes[-1].get("volume")) / (sum(vols) / len(vols))
    t = f[30] if len(f) > 30 else ""
    return {
        "name": f[1] if len(f) > 1 else "",
        "price": safe_float(f[3]),
        "prev_close": safe_float(f[4]),
        "open": safe_float(f[5]),
        "high": safe_float(f[33]) if len(f) > 33 else 0.0,
        "low": safe_float(f[34]) if len(f) > 34 else 0.0,
        "change": safe_float(f[31]) if len(f) > 31 else 0.0,
        "change_percent": safe_float(f[32]) if len(f) > 32 else 0.0,
        "volume": safe_float(f[36]) if len(f) > 36 else 0.0,
        "amount": safe_float(f[37]) if len(f) > 37 else 0.0,
        "turnover_rate": 0.0,
        "volume_ratio": vol_ratio,
        "pe_ratio": safe_float(f[39]) if len(f) > 39 else 0.0,
        "pb_ratio": 0.0,
        "total_market_cap": (safe_float(f[45]) if len(f) > 45 else 0.0) * 1e8,
        "chg_5d": chg_5d,
        "chg_20d": chg_20d,
        "time": t[:10],
    }


# ============ K线 ============

def fetch_kline(code, limit=130):
    """腾讯日K线(前复权), 返回 nodes: [{date,open,last,high,low,volume}] 按日期升序"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={urllib.parse.quote(code)},day,,,{limit},qfq")
    try:
        data = json.loads(http_get(url))
        d = (data.get("data") or {}).get(code, {})
        rows = d.get("qfqday") or d.get("day") or []
    except Exception as e:
        print(f"⚠️ K线获取失败 {code}: {e}", file=sys.stderr)
        return []
    nodes = []
    for r in rows:
        if len(r) >= 6:
            nodes.append({
                "date": r[0], "open": safe_float(r[1]), "last": safe_float(r[2]),
                "high": safe_float(r[3]), "low": safe_float(r[4]), "volume": safe_float(r[5]),
            })
    nodes.sort(key=lambda x: x["date"])
    return nodes


# ============ 技术指标(本地计算) ============

def calc_technical(nodes):
    """从K线收盘价计算 MA5/10/20/60 和 MACD(12,26,9)"""
    closes = [n["last"] for n in nodes if n.get("last")]
    ma = {}
    for n in (5, 10, 20, 60):
        if len(closes) >= n:
            ma[f"MA_{n}"] = round(sum(closes[-n:]) / n, 4)
    # MACD
    macd = {"DIF": 0.0, "DEA": 0.0, "MACD": 0.0}
    if len(closes) >= 35:
        ema12 = closes[0]
        ema26 = closes[0]
        difs = []
        dea = 0.0
        for i, c in enumerate(closes):
            ema12 = ema12 * 11 / 13 + c * 2 / 13
            ema26 = ema26 * 25 / 27 + c * 2 / 27
            dif = ema12 - ema26
            if i == 0:
                dea = dif
            else:
                dea = dea * 8 / 10 + dif * 2 / 10
            difs.append(dif)
        macd = {"DIF": round(difs[-1], 4), "DEA": round(dea, 4),
                "MACD": round(2 * (difs[-1] - dea), 4)}
    return {"ma": ma, "macd": macd}


# ============ 资金流(东方财富, A股) ============

def fetch_fundflow(code):
    """A股日度主力资金流: 新浪(主源) / 东方财富(备源)
    返回 {MainNetFlow, MainNetFlow5D}(元); 非A股返回 {}"""
    m = re.match(r"^(sz|sh)(\d{6})$", code)
    if not m:
        return {}
    # ---- 主源: 新浪(超大单+大单净额) ----
    try:
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"MoneyFlow.ssl_qsfx_lscjfb?page=1&num=6&sort=opendate&asc=0&daima={code}")
        txt = http_get(url)
        rows = json.loads(txt) if txt.strip() else []
        if rows:
            mains = [safe_float(r.get("r0_net")) + safe_float(r.get("r1_net")) for r in rows]
            return {"MainNetFlow": mains[0], "MainNetFlow5D": sum(mains[:5]),
                    "EndDate": rows[0].get("opendate", ""), "source": "sina"}
    except Exception as e:
        print(f"⚠️ 新浪资金流失败 {code}: {e}", file=sys.stderr)
    # ---- 备源: 东方财富 ----
    secid = ("0." if m.group(1) == "sz" else "1.") + m.group(2)
    url = ("https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get?"
           f"lmt=6&klt=101&secid={secid}&secid2={secid}"
           "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
           "&ut=b2884a393a59ad64002292a3e90d46a5")
    try:
        data = json.loads(http_get(url))
        lines = ((data.get("data") or {}).get("klines")) or []
        mains = [safe_float(l.split(",")[1]) for l in lines if l]
        if mains:
            return {"MainNetFlow": mains[-1], "MainNetFlow5D": sum(mains[-5:]),
                    "EndDate": lines[-1].split(",")[0], "source": "eastmoney"}
    except Exception as e:
        print(f"⚠️ 资金流获取失败 {code}: {e}", file=sys.stderr)
    return {}


# ============ 主流程 ============

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--outdir", default=None)
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = args.config or os.path.join(base_dir, "config", "portfolio.json")
    outdir = args.outdir or os.path.join(base_dir, "reports")
    os.makedirs(outdir, exist_ok=True)

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    positions = [p for p in config.get("positions", []) if safe_float(p.get("quantity")) > 0]
    codes = [p["code"] for p in positions]

    # 汇率更新
    fx = fetch_fx()
    if fx:
        fx["updated_at"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
        fx["note"] = "云端自动化运行时通过 open.er-api.com 更新"
        config["fx"] = fx

    quotes = fetch_quotes(codes)
    today = datetime.now(CST).strftime("%Y%m%d")
    results = []
    for pos in positions:
        code = pos["code"]
        nodes = fetch_kline(code)
        f = quotes.get(code)
        if not f or not nodes:
            print(json.dumps({"ok": False, "code": code, "message": "报价或K线缺失"}, ensure_ascii=False))
            results.append({"code": code, "ok": False})
            continue
        if code.startswith("us"):
            quote = parse_quote_us(f, nodes)
        else:
            quote = parse_quote_cn(f, nodes)
        quote["code"] = code
        technical = calc_technical(nodes)
        fundflow = fetch_fundflow(code)
        data = {"quote": quote, "kline": {"nodes": nodes},
                "fundflow": fundflow, "technical": technical}
        out = os.path.join(outdir, f"data_{code}_{today}.json")
        with open(out, "w", encoding="utf-8") as fo:
            json.dump(data, fo, ensure_ascii=False)
        # 峰值跟踪更新(用收盘/现价)
        close = safe_float(quote.get("price"))
        old_peak = safe_float(pos.get("peak_price"))
        if close > 0:
            pos["peak_price"] = round(max(old_peak, close), 2)
        results.append({"code": code, "name": quote.get("name"), "price": close, "ok": True})
        print(json.dumps(results[-1], ensure_ascii=False))

    # 回写配置(fx + peak_price)
    config["updated_at"] = datetime.now(CST).strftime("%Y-%m-%d")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    print(json.dumps({"ok": all(r.get("ok") for r in results), "count": len(results),
                      "fx": config.get("fx", {})}, ensure_ascii=False))


if __name__ == "__main__":
    main()
