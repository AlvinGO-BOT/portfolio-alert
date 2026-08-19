#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓提示模型 - 组合总览生成器 (移动端优先 v3.0)
将多只持仓的HTML卡片整合为一份手机端友好的组合总览报告
用法:
  python3 build_summary.py --date 2026-08-17 --type evening --reports reports/report_*.html --output summary.html [--site-dir reports/site]
"""
import json
import os
import re
import glob
import shutil
import argparse
from datetime import datetime

TYPE_TXT = {
    "morning": ("盘前计划", "08:00"),
    "noon": ("午间快照", "12:00"),
    "evening": ("盘后复盘", "18:00"),
}

SIGNAL_COLOR = {"🟢": "#2e7d32", "🟡": "#f9a825", "🔴": "#c62828"}


def extract(card, pattern):
    m = re.search(pattern, card)
    return m.group(1).strip() if m else None


def parse_card(card):
    """从个股卡片HTML中提取关键数据（卡片由 analyze_position.py 生成，格式稳定）"""
    info = {}
    sig = extract(card, r'<p style="margin:0;font-size:16px;font-weight:700;">([^<]+)</p>')
    info["signal"] = sig or "…"
    info["signal_emoji"] = info["signal"][0] if info["signal"] else "🟢"
    info["confidence"] = extract(card, r"置信度(\d+)%")
    # 兼容币种符号($/HK$/¥)前缀
    info["price"] = extract(card, r'font-size:26px;font-weight:800;color:#[0-9a-f]{6};">[$HK¥]*([\d.]+)</span>')
    info["day_chg"] = extract(card, r'font-size:14px;color:#[0-9a-f]{6};">([+-][\d.]+%)</span>')
    info["pnl_pct"] = extract(card, r'font-size:13px;font-weight:600;color:#[0-9a-f]{6};margin-left:auto;">([+-][\d.]+%)</span>')
    info["stop_static"] = extract(card, r'font-size:15px;font-weight:700;color:#c62828;">[$HK¥]*([\d.]+)</span>')
    info["stop_ma"] = extract(card, r'font-size:15px;font-weight:700;color:#e65100;">[$HK¥]*([\d.]+)</span>')
    info["take_profit"] = extract(card, r'font-size:15px;font-weight:700;color:#2e7d32;">[$HK¥]*([\d.]+)</span>')
    return info


def main():
    parser = argparse.ArgumentParser(description="组合总览生成器 (移动端优先)")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--type", default="evening", choices=["morning", "noon", "evening"])
    parser.add_argument("--reports", nargs="+", help="持仓卡片HTML文件列表(支持通配)")
    parser.add_argument("--config", default=None)
    parser.add_argument("--output", required=True)
    parser.add_argument("--site-dir", default=None, help="站点目录，提供后同时生成手机端站点入口")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = args.config or os.path.join(base_dir, "config", "portfolio.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    positions = config.get("positions", [])
    strategies = config.get("strategies", {})
    fx = config.get("fx", {})

    def currency_of(pos):
        return pos.get("currency") or ({"美股": "USD", "港股": "HKD"}.get(pos.get("market", ""), "CNY"))

    def rate_of(cur):
        return float(fx.get(f"{cur}CNY", 1.0)) if cur != "CNY" else 1.0

    # 收集报告文件
    report_files = []
    if args.reports:
        for r in args.reports:
            report_files.extend(glob.glob(r))
    else:
        pattern = os.path.join(base_dir, "reports", f"report_{args.date}_{args.type}_*.html")
        report_files = glob.glob(pattern)
    report_files.sort()

    if not report_files:
        print(json.dumps({"ok": False, "message": f"未找到 {args.date} {args.type} 的持仓报告"}, ensure_ascii=False))
        return

    type_txt, type_time = TYPE_TXT.get(args.type, ("持仓提示", ""))

    # 读取卡片 + 解析数据 + 计算总资产(全部折算人民币)
    cards = []
    parsed = []
    total_cost = 0.0   # CNY
    total_mkt = 0.0    # CNY
    by_currency = {}   # {cur: {"mkt": 原币市值, "cny": 折算市值}}
    valid_n = 0
    for rf in report_files:
        with open(rf, "r", encoding="utf-8") as f:
            content = f.read().strip()
        cards.append(content)
        info = parse_card(content)
        # 找到对应持仓配置
        code_key = os.path.basename(rf).split("_")[-1].replace(".html", "")
        pos = next((p for p in positions if p["code"] == code_key), None)
        if pos is None:
            pos = next((p for p in positions if p["code"].split(".")[0] == code_key), None)
        if pos is None and len(parsed) < len(positions):
            pos = positions[len(parsed)]
        if pos:
            info["name"] = pos.get("name", code_key)
            info["code"] = pos.get("code", code_key)
            info["market"] = pos.get("market", "")
            info["quantity"] = pos.get("quantity", 0)
            info["cost"] = pos.get("cost_price", 0)
            info["strategy"] = strategies.get(pos.get("strategy_type"), {}).get("desc", "")
            cur = currency_of(pos)
            rate = rate_of(cur)
            info["currency"] = cur
            info["fx_rate"] = rate
            try:
                px = float(info["price"]) if info["price"] else 0
                qty = float(info["quantity"] or 0)
                if px and qty:
                    mkt_native = px * qty
                    total_cost += pos.get("cost_price", 0) * qty * rate
                    total_mkt += mkt_native * rate
                    bc = by_currency.setdefault(cur, {"mkt": 0.0, "cny": 0.0})
                    bc["mkt"] += mkt_native
                    bc["cny"] += mkt_native * rate
                    valid_n += 1
            except (ValueError, TypeError):
                pass
        parsed.append(info)

    total_pnl = total_mkt - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0
    total_pnl_color = "#e53935" if total_pnl >= 0 else "#43a047"

    # 分币种明细行
    cur_syms = {"USD": "$", "HKD": "HK$"}
    breakdown_parts = []
    for cur in sorted(by_currency.keys()):
        bc = by_currency[cur]
        if cur == "CNY":
            breakdown_parts.append(f"A股/人民币 ¥{bc['mkt']:,.0f}")
        else:
            sym = cur_syms.get(cur, "")
            breakdown_parts.append(f"{cur} {sym}{bc['mkt']:,.2f} ≈ ¥{bc['cny']:,.0f}")
    breakdown_html = " · ".join(breakdown_parts)
    fx_txt = " · ".join(f"{k} {v}" for k, v in fx.items() if isinstance(v, (int, float)))

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    pos_count = len(positions)

    # ===== 构建持仓数据JSON（嵌入页面供客户端重算） ====
    pos_data_list = []
    for info in parsed:
        pos_cfg = next((p for p in positions if p.get("code") == info.get("code")), {})
        pos_data_list.append({
            "code": info.get("code", ""),
            "name": info.get("name", ""),
            "market": info.get("market", ""),
            "currency": info.get("currency", "CNY"),
            "cost_price": float(info.get("cost", 0) or 0),
            "quantity": float(info.get("quantity", 0) or 0),
            "strategy_type": pos_cfg.get("strategy_type", "swing_small"),
            "peak_price": float(pos_cfg.get("peak_price", 0) or 0),
            "current_price": float(info.get("price", 0) or 0),
            "ma20": float(info.get("stop_ma", 0) or 0),
            "fx_rate": float(info.get("fx_rate", 1) or 1),
            "pnl_pct": info.get("pnl_pct", ""),
        })
    positions_json = json.dumps({"positions": pos_data_list, "fx": fx}, ensure_ascii=False)

    # ===== 交易面板CSS ====
    trade_css = """
  .trade-fab{position:fixed;right:16px;bottom:82px;z-index:20;background:linear-gradient(135deg,#e65100,#f57c00);color:#fff;border:none;border-radius:50%;width:52px;height:52px;font-size:22px;box-shadow:0 4px 14px rgba(230,81,0,0.45);cursor:pointer;display:flex;align-items:center;justify-content:center;}
  .trade-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:100;justify-content:center;align-items:flex-end;}
  .trade-overlay.active{display:flex;}
  .trade-modal{background:#fff;border-radius:20px 20px 0 0;width:100%;max-width:520px;max-height:88vh;overflow-y:auto;padding:20px 18px 34px;-webkit-animation:slideUp .3s ease;animation:slideUp .3s ease;}
  @-webkit-keyframes slideUp{from{transform:translateY(100%)}to{transform:translateY(0)}}
  @keyframes slideUp{from{transform:translateY(100%)}to{transform:translateY(0)}}
  .trade-modal h2{margin:0 0 16px;font-size:18px;font-weight:800;color:#1a1a2e;display:flex;justify-content:space-between;align-items:center;}
  .trade-close{background:none;border:none;font-size:22px;color:#999;cursor:pointer;padding:0;line-height:1;}
  .ts{margin-bottom:16px;}
  .tl{font-size:13px;font-weight:600;color:#555;margin-bottom:8px;display:block;}
  .pos-list{display:flex;flex-direction:column;gap:8px;}
  .pos-opt{display:flex;align-items:center;gap:10px;padding:12px 14px;border:2px solid #e0e0e0;border-radius:12px;cursor:pointer;transition:all .2s;}
  .pos-opt.selected{border-color:#1565c0;background:#e3f2fd;}
  .pos-opt .pn{font-size:15px;font-weight:700;color:#222;}
  .pos-opt .pi{font-size:11px;color:#999;margin-top:2px;}
  .act-toggle{display:flex;gap:8px;}
  .act-btn{flex:1;padding:12px;border:2px solid #e0e0e0;border-radius:12px;background:#fff;font-size:15px;font-weight:600;cursor:pointer;text-align:center;color:#666;transition:all .2s;}
  .act-btn.selected.buy{border-color:#e53935;background:#ffebee;color:#c62828;}
  .act-btn.selected.sell{border-color:#43a047;background:#e8f5e9;color:#2e7d32;}
  .ti{width:100%;padding:14px 16px;border:2px solid #e0e0e0;border-radius:12px;font-size:16px;font-weight:600;color:#222;outline:none;transition:border-color .2s;-webkit-appearance:none;appearance:none;}
  .ti:focus{border-color:#1565c0;}
  .ti::placeholder{color:#ccc;font-weight:400;}
  .trade-submit{width:100%;padding:16px;background:linear-gradient(135deg,#1565c0,#0d47a1);color:#fff;border:none;border-radius:14px;font-size:17px;font-weight:700;cursor:pointer;margin-top:8px;box-shadow:0 3px 12px rgba(13,71,161,0.3);}
  .trade-result{display:none;margin-top:16px;padding:16px;background:#f5f7fa;border-radius:12px;border:1px solid #e0e0e0;}
  .trade-result.active{display:block;}
  .rs-sig{text-align:center;padding:12px;border-radius:10px;margin:0 0 12px;font-size:18px;font-weight:800;}
  .rs-row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eee;font-size:14px;}
  .rs-row:last-child{border-bottom:none;}
  .rs-l{color:#666;}
  .rs-v{font-weight:700;color:#222;}
  .sync-btn{display:block;width:100%;padding:14px;margin-top:10px;border:2px solid #1565c0;border-radius:10px;background:#1565c0;color:#fff;font-size:15px;font-weight:600;cursor:pointer;transition:all .2s;}
  .sync-btn:active{transform:scale(0.98);}
  .sync-btn.copied{background:#2e7d32;border-color:#2e7d32;color:#fff;}
  .toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,0.8);color:#fff;padding:8px 16px;border-radius:8px;font-size:13px;z-index:10001;opacity:0;transition:opacity .3s;pointer-events:none;}
  .toast.show{opacity:1;}
  .trade-note{font-size:11px;color:#999;margin-top:10px;line-height:1.6;text-align:center;}
"""

    # ===== 交易面板HTML ====
    trade_html = """
<script type="application/json" id="portfolio-data">__POSITIONS_JSON__</script>
<div class="trade-overlay" id="trade-overlay" onclick="if(event.target===this)closeTrade()">
  <div class="trade-modal">
    <h2>📊 交易录入 <button class="trade-close" onclick="closeTrade()">✕</button></h2>
    <div class="ts"><span class="tl">选择持仓</span><div class="pos-list" id="pos-list"></div></div>
    <div class="ts" id="new-fields" style="display:none">
      <span class="tl">新增持仓信息</span>
      <input class="ti" id="n-query" placeholder="输入代码或名称（如 sz002384 / 东山精密 / MSFT）" style="margin-bottom:4px" oninput="detectQuery()">
      <div id="n-query-hint" style="font-size:11px;margin:0 0 8px 2px;display:none;"></div>
      <select class="ti" id="n-market" style="margin-bottom:8px"><option value="A股">A股</option><option value="美股">美股</option><option value="港股">港股</option></select>
      <select class="ti" id="n-strategy"><option value="trend_leader">趋势龙头(20%减半)</option><option value="swing_small">震荡小票(10/20/30分批)</option><option value="ma20_trail">MA20移动止盈</option></select>
    </div>
    <div class="ts" id="action-fields">
      <span class="tl">交易方向</span>
      <div class="act-toggle">
        <div class="act-btn buy" data-act="buy" onclick="setAction('buy')">🔴 买入加仓</div>
        <div class="act-btn sell selected" data-act="sell" onclick="setAction('sell')">🟢 卖出减仓</div>
      </div>
    </div>
    <div class="ts"><span class="tl">成交价格</span><input type="number" class="ti" id="t-price" placeholder="输入实际成交价格" step="0.01"></div>
    <div class="ts"><span class="tl">成交数量</span><input type="number" class="ti" id="t-qty" placeholder="输入成交数量" step="1"></div>
    <button class="trade-submit" onclick="submitTrade()">确认交易并重新计算策略</button>
    <div class="trade-result" id="trade-result"></div>
    <button class="sync-btn" id="sync-btn" style="display:none" onclick="autoSendToAssistant()">📤 重新打开助手发送</button>
    <div class="trade-note">交易后策略信号即时重算（基于当前价）<br>确认后将自动打开助手并携带指令，在助手中发送即可触发完整模型重跑</div>
    <div class="toast" id="toast"></div>
  </div>
</div>
""".replace("__POSITIONS_JSON__", positions_json)

    # ===== 交易面板JavaScript ====
    trade_js = r"""
const DATA=JSON.parse(document.getElementById('portfolio-data').textContent);
let selCode=null,selAction='sell',isNew=false,tradeLog=[];
function openTrade(){renderPosList();document.getElementById('trade-overlay').classList.add('active');}
function closeTrade(){document.getElementById('trade-overlay').classList.remove('active');}
function curSym(c){return c==='USD'?'$':c==='HKD'?'HK$':'';}
function pnlCol(p){try{const v=parseFloat(String(p).replace('%','').replace('+',''));return v>=0?'#e53935':'#43a047';}catch(e){return '#666';}}
function renderPosList(){
  const c=document.getElementById('pos-list');let h='';
  DATA.positions.forEach(p=>{
    const s=curSym(p.currency);const a=selCode===p.code&&!isNew?' selected':'';
    const pc=pnlCol(p.pnl_pct);
    h+=`<div class="pos-opt${a}" onclick="selPos('${p.code}')"><div style="flex:1"><div class="pn">${p.name}</div><div class="pi">${p.code} · ${p.market} · 成本${s}${p.cost_price.toFixed(2)} · ${p.quantity}股 · <span style="color:${pc};font-weight:600">${p.pnl_pct||'--'}</span></div></div></div>`;
  });
  h+=`<div class="pos-opt${isNew?' selected':''}" onclick="selNew()" style="border-style:dashed;justify-content:center;color:#1565c0;font-weight:600;font-size:14px;">+ 新增持仓</div>`;
  c.innerHTML=h;
}
function selPos(code){selCode=code;isNew=false;document.getElementById('new-fields').style.display='none';document.getElementById('action-fields').style.display='block';renderPosList();}
function selNew(){isNew=true;selCode=null;document.getElementById('new-fields').style.display='block';document.getElementById('action-fields').style.display='none';renderPosList();}
function detectQuery(){
  const v=document.getElementById('n-query').value.trim();
  const h=document.getElementById('n-query-hint');
  if(!v){h.style.display='none';return;}
  const isCode=/^([a-zA-Z]{1,3})?\d{4,6}$/.test(v)||/^[a-zA-Z]{1,5}(\.[A-Z]{1,3})?$/.test(v)||/^\d{4,5}$/.test(v);
  if(isCode){h.style.display='block';h.style.color='#2e7d32';h.innerHTML='✅ 检测到代码格式，同步后将自动搜索匹配名称';}
  else{h.style.display='block';h.style.color='#1565c0';h.innerHTML='✅ 检测到名称，同步后将自动搜索匹配代码';}
}
function setAction(act){selAction=act;document.querySelectorAll('.act-btn').forEach(b=>b.classList.toggle('selected',b.dataset.act===act));}
function submitTrade(){
  const price=parseFloat(document.getElementById('t-price').value);
  const qty=parseFloat(document.getElementById('t-qty').value);
  if(!price||price<=0){alert('请输入有效的成交价格');return;}
  if(!qty||qty<=0){alert('请输入有效的成交数量');return;}
  if(isNew){
    const query=document.getElementById('n-query').value.trim();
    const market=document.getElementById('n-market').value;
    const strategy=document.getElementById('n-strategy').value;
    if(!query){alert('请输入股票代码或名称');return;}
    const isCode=/^([a-zA-Z]{1,3})?\d{4,6}$/.test(query)||/^[a-zA-Z]{1,5}(\.[A-Z]{1,3})?$/.test(query)||/^\d{4,5}$/.test(query);
    const label=isCode?'代码':'名称';
    const cmd=`新增持仓：${label}${query} 成本${price} 数量${qty}股 市场预判${market} 策略${strategy}。请搜索确认标的代码和名称后添加到portfolio.json并重新运行策略引擎和重新部署站点。`;
    tradeLog.push({type:'new',query,isCode,market,strategy,price,qty,cmd});
    showNewResult(query,isCode,price,qty,strategy);
    document.getElementById('sync-btn').style.display='block';
    openAssistant(cmd);
    return;
  }
  if(!selCode){alert('请先选择持仓');return;}
  const pos=DATA.positions.find(p=>p.code===selCode);
  if(!pos)return;
  let newCost,newQty;
  if(selAction==='buy'){
    newQty=pos.quantity+qty;
    newCost=(pos.cost_price*pos.quantity+price*qty)/newQty;
  }else{
    newQty=pos.quantity-qty;
    if(newQty<=0){newQty=0;newCost=0;}
    else newCost=(pos.cost_price*pos.quantity-price*qty)/newQty;
  }
  const a=recalc(pos,newCost,newQty>0?price:pos.current_price,newQty);
  pos.cost_price=newCost;pos.quantity=newQty;
  if(price>pos.peak_price)pos.peak_price=price;
  pos.current_price=price;
  pos.pnl_pct=(a.profitPct>=0?'+':'')+a.profitPct.toFixed(2)+'%';
  let cmd;
  if(newQty<=0){
    cmd=`交易同步：${pos.name}(${pos.code}) 清仓 ${qty}股@${price}。请更新portfolio.json中该持仓quantity=0并重新运行策略引擎和重新部署站点。`;
  }else{
    const actTxt=selAction==='buy'?'买入加仓':'卖出减仓';
    const stratNote=selAction==='sell'&&newQty>0&&pos.strategy_type!=='ma20_trail'?'（建议切换为ma20_trail移动止盈策略）':'';
    cmd=`交易同步：${pos.name}(${pos.code}) ${actTxt} ${qty}股@${price}，新移动成本${newCost.toFixed(2)}，剩余${newQty}股${stratNote}。请更新portfolio.json中cost_price=${newCost.toFixed(2)}、quantity=${newQty}并重新运行策略引擎和重新部署站点。`;
  }
  tradeLog.push({type:'trade',code:pos.code,name:pos.name,action:selAction,price,qty,newCost,newQty,cmd});
  showResult(pos,a);
  document.getElementById('sync-btn').style.display='block';
  openAssistant(cmd);
  updateTotals();
  renderPosList();
}
function recalc(pos,cost,price,qty){
  if(qty<=0||cost<=0){return{signal:'✅已清仓',signalColor:'#2e7d32',signalBg:'#e8f5e9',profitPct:0,stopPrice:0,tpPrice:0,tpTxt:'—',ma20:0,sym:'',cost:0};}
  const profitPct=(price-cost)/cost*100;
  const stopPrice=cost*0.9;
  const ma20=pos.ma20;
  const peak=Math.max(pos.peak_price,price);
  const ddFromPeak=(price-peak)/peak*100;
  const isTrail=pos.strategy_type==='ma20_trail';
  const sym=curSym(pos.currency);
  let tpPrice,tpTxt;
  if(isTrail){tpPrice=ma20;tpTxt='MA20止盈线';}
  else if(pos.strategy_type==='trend_leader'){tpPrice=cost*1.2;tpTxt='+20%减50%';}
  else{tpPrice=cost*1.1;tpTxt='+10%卖1/3';}
  let signal,signalColor,signalBg;
  if(profitPct<=-10){signal='🔴止损';signalColor='#c62828';signalBg='#ffebee';}
  else if(ddFromPeak<=-10){signal='🔴清仓';signalColor='#b71c1c';signalBg='#ffcdd2';}
  else if(ma20>0&&price<ma20&&(isTrail||profitPct>0)){signal=isTrail?'🔴止盈离场':'🔴止损';signalColor='#c62828';signalBg='#ffebee';}
  else if(!isTrail&&profitPct>=(pos.strategy_type==='trend_leader'?20:10)){signal='🟡减仓';signalColor='#f9a825';signalBg='#fff8e1';}
  else{signal='🟢持有';signalColor='#2e7d32';signalBg='#e8f5e9';}
  return{signal,signalColor,signalBg,profitPct,stopPrice,tpPrice,tpTxt,ma20,peak,ddFromPeak,sym,cost};
}
function showResult(pos,a){
  const r=document.getElementById('trade-result');r.classList.add('active');
  if(pos.quantity<=0){
    const t=tradeLog[tradeLog.length-1];
    r.innerHTML=`<div class="rs-sig" style="background:#e8f5e9;color:#2e7d32;">✅ 已清仓</div><div class="rs-row"><span class="rs-l">标的</span><span class="rs-v">${pos.name} (${pos.code})</span></div><div class="rs-row"><span class="rs-l">清仓价格</span><span class="rs-v">${a.sym}${t.price.toFixed(2)}</span></div><div class="rs-row"><span class="rs-l">成交数量</span><span class="rs-v">${t.qty}股</span></div>`;
    return;
  }
  const pc=a.profitPct>=0?'#e53935':'#43a047';
  r.innerHTML=`<div class="rs-sig" style="background:${a.signalBg};color:${a.signalColor};">${a.signal}</div>
    <div class="rs-row"><span class="rs-l">标的</span><span class="rs-v">${pos.name} (${pos.code})</span></div>
    <div class="rs-row"><span class="rs-l">新移动成本</span><span class="rs-v">${a.sym}${a.cost.toFixed(2)}</span></div>
    <div class="rs-row"><span class="rs-l">剩余数量</span><span class="rs-v">${pos.quantity}股</span></div>
    <div class="rs-row"><span class="rs-l">当前价格</span><span class="rs-v">${a.sym}${pos.current_price.toFixed(2)}</span></div>
    <div class="rs-row"><span class="rs-l">浮盈浮亏</span><span class="rs-v" style="color:${pc}">${a.profitPct>=0?'+':''}${a.profitPct.toFixed(2)}%</span></div>
    <div class="rs-row"><span class="rs-l">静态止损(成本-10%)</span><span class="rs-v" style="color:#c62828">${a.sym}${a.stopPrice.toFixed(2)}</span></div>
    <div class="rs-row"><span class="rs-l">MA20止盈/止损</span><span class="rs-v" style="color:#e65100">${a.sym}${a.ma20.toFixed(2)}</span></div>
    <div class="rs-row"><span class="rs-l">止盈位</span><span class="rs-v" style="color:#2e7d32">${a.sym}${a.tpPrice.toFixed(2)} (${a.tpTxt})</span></div>`;
}
function showNewResult(query,isCode,price,qty,strategy){
  const r=document.getElementById('trade-result');r.classList.add('active');
  const stratName={'trend_leader':'趋势龙头','swing_small':'震荡小票','ma20_trail':'MA20移动止盈'}[strategy]||strategy;
  const inputLabel=isCode?'输入代码':'输入名称';
  const autoLabel=isCode?'匹配名称':'匹配代码';
  r.innerHTML=`<div class="rs-sig" style="background:#e3f2fd;color:#1565c0;">📝 新增持仓待确认</div>
    <div class="rs-row"><span class="rs-l">${inputLabel}</span><span class="rs-v">${query}</span></div>
    <div class="rs-row"><span class="rs-l">${autoLabel}</span><span class="rs-v" style="color:#999;">⏳ 同步后自动搜索确认</span></div>
    <div class="rs-row"><span class="rs-l">买入价格</span><span class="rs-v">${price.toFixed(2)}</span></div>
    <div class="rs-row"><span class="rs-l">买入数量</span><span class="rs-v">${qty}股</span></div>
    <div class="rs-row"><span class="rs-l">策略类型</span><span class="rs-v">${stratName}</span></div>
    <div class="rs-row"><span class="rs-l">静态止损(成本-10%)</span><span class="rs-v" style="color:#c62828">${(price*0.9).toFixed(2)}</span></div>
    <p style="font-size:12px;color:#999;margin-top:10px;line-height:1.6;">助手已自动打开并携带指令，在助手中发送即可自动搜索匹配并完成持仓添加。</p>`;
}
function showSyncButton(){
  const btn=document.getElementById('sync-btn');
  btn.style.display='block';
  autoCopyInstruction();
}
function showToast(msg){
  const t=document.getElementById('toast');
  if(!t)return;
  t.textContent=msg;t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),2500);
}
function buildSyncCmd(){
  if(tradeLog.length===0)return '';
  if(tradeLog.length>1){
    return '交易同步（多条）：\n'+tradeLog.map(t=>t.cmd.replace(/^交易同步：|^新增持仓：/,'')).join('\n')+'\n请一并更新portfolio.json并重新运行策略引擎和重新部署站点。';
  }
  return tradeLog[0].cmd;
}
function autoCopyInstruction(){
  const cmd=buildSyncCmd();
  if(!cmd)return;
  const btn=document.getElementById('sync-btn');
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(cmd).then(()=>{
      showToast('✅ 指令已自动复制');
    }).catch(()=>{});
  }else{
    const ta=document.createElement('textarea');ta.value=cmd;ta.style.position='fixed';ta.style.opacity='0';
    document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');showToast('✅ 指令已自动复制');}catch(e){}
    document.body.removeChild(ta);
  }
}
function autoSendToAssistant(){
  const cmd=buildSyncCmd();
  if(!cmd){showToast('⚠️ 没有待同步的交易');return;}
  openAssistant(cmd);
}
function fallbackCopyAndOpen(cmd,btn){
  const ta=document.createElement('textarea');ta.value=cmd;ta.style.position='fixed';ta.style.opacity='0';
  document.body.appendChild(ta);ta.select();
  try{document.execCommand('copy');}catch(e){}
  document.body.removeChild(ta);
  openAssistant(cmd);
}
function openAssistant(cmd){
  if(!cmd)return;
  const btn=document.getElementById('sync-btn');
  // 同步打开助手（在用户手势内执行，避免弹窗拦截）+ URL携带指令自动填充
  const url='https://www.workbuddy.cn/?q='+encodeURIComponent(cmd);
  let opened=false;
  try{const w=window.open(url,'_blank');opened=!!w;}catch(e){}
  // 异步复制指令到剪贴板（URL参数的备份方案）
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(cmd).then(()=>{
      showToast(opened?'✅ 指令已复制，助手已打开':'✅ 指令已复制，请打开助手粘贴发送');
    }).catch(()=>{});
  }else{
    const ta=document.createElement('textarea');ta.value=cmd;ta.style.position='fixed';ta.style.opacity='0';
    document.body.appendChild(ta);ta.select();try{document.execCommand('copy');}catch(e){}
    document.body.removeChild(ta);
  }
  if(btn){btn.classList.add('copied');btn.textContent=opened?'✅ 助手已打开，指令已复制':'📤 再次发送 / 重新复制';}
  showToast(opened?'📤 正在打开助手，指令已复制':'✅ 指令已复制，请打开助手粘贴发送');
  setTimeout(()=>{if(btn){btn.classList.remove('copied');btn.textContent='📤 重新打开助手发送';}},3000);
}
function copySync(){
  const cmd=buildSyncCmd();
  if(!cmd)return;
  const btn=document.getElementById('sync-btn');
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(cmd).then(()=>showCopied(btn)).catch(()=>fallbackCopy(cmd,btn));
  }else{fallbackCopy(cmd,btn);}
}
function updateTotals(){
  let tm=0,tc=0;
  DATA.positions.forEach(p=>{
    if(p.quantity>0&&p.current_price>0){tm+=p.current_price*p.quantity*p.fx_rate;tc+=p.cost_price*p.quantity*p.fx_rate;}
  });
  const pnl=tm-tc;const pct=tc>0?(pnl/tc*100):0;const col=pnl>=0?'#e53935':'#43a047';
  const el=document.getElementById('total-mkt');
  if(el){el.textContent='¥'+tm.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});el.style.color=col;}
  const ec=document.getElementById('total-cost');
  if(ec)ec.textContent='总成本 ¥'+tc.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
  const ep=document.getElementById('total-pnl');
  if(ep){ep.textContent=(pnl>=0?'+':'')+pnl.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});ep.style.color=col;}
  const epct=document.getElementById('total-pnl-pct');
  if(epct){epct.textContent=(pct>=0?'+':'')+pct.toFixed(2)+'%';epct.style.color=col;}
}
"""

    # 信号总览条（每只股票一张小卡，横向滚动）
    strip = []
    for info in parsed:
        color = SIGNAL_COLOR.get(info.get("signal_emoji", "🟢"), "#333")
        pnl = info.get("pnl_pct", "--")
        # 计算持仓市值和盈亏金额
        cur = info.get("currency", "CNY")
        csym = {"USD": "$", "HKD": "HK$"}.get(cur, "")
        try:
            px = float(info.get("price", 0) or 0)
            qty = float(info.get("quantity", 0) or 0)
            cst = float(info.get("cost", 0) or 0)
            rate = float(info.get("fx_rate", 1) or 1)
        except (ValueError, TypeError):
            px = qty = cst = 0
            rate = 1
        mkt_val = px * qty if px and qty else 0
        pnl_amt = (px - cst) * qty if px and qty and cst else 0
        pnl_c = "#e53935" if pnl_amt >= 0 else "#43a047"
        pnl_sign = "+" if pnl_amt >= 0 else "-"
        abs_pnl = abs(pnl_amt)
        if cur == "CNY":
            val_txt = f"¥{mkt_val:,.0f}" if mkt_val else "—"
            pnl_txt = f"{pnl_sign}¥{abs_pnl:,.0f}" if mkt_val else "—"
        else:
            cny_mkt = mkt_val * rate if mkt_val and rate else 0
            cny_pnl = pnl_amt * rate if mkt_val and rate else 0
            abs_cny_pnl = abs(cny_pnl)
            val_txt = f"{csym}{mkt_val:,.0f}<span style='color:#bbb;'>≈¥{cny_mkt:,.0f}</span>" if mkt_val else "—"
            pnl_txt = f"{pnl_sign}{csym}{abs_pnl:,.0f}<span style='color:#bbb;'>≈{pnl_sign}¥{abs_cny_pnl:,.0f}</span>" if mkt_val else "—"
        strip.append(
            f'<div style="flex:0 0 auto;min-width:145px;background:#fff;border-radius:12px;'
            f'padding:10px 12px;box-shadow:0 1px 4px rgba(0,0,0,0.08);border-top:3px solid {color};">'
            f'<div style="font-size:13px;font-weight:700;color:#222;">{info.get("name","")}</div>'
            f'<div style="font-size:11px;color:#999;margin-top:1px;">{info.get("code","")} · {info.get("market","")}</div>'
            f'<div style="font-size:15px;font-weight:800;color:{color};margin-top:6px;">{info.get("signal","…")}</div>'
            f'<div style="font-size:11px;color:#666;margin-top:3px;">浮盈 <b style="color:{pnl_color(pnl)};">{pnl}</b></div>'
            f'<div style="font-size:11px;color:#666;margin-top:2px;">持仓 <b style="color:#333;">{val_txt}</b></div>'
            f'<div style="font-size:11px;color:#666;margin-top:2px;">盈亏 <b style="color:{pnl_c};">{pnl_txt}</b></div>'
            f'</div>'
        )
    strip_html = "".join(strip)

    # 卡片列表
    cards_html = "".join(cards)

    # 历史报告列表（扫描 reports 下其他 summary，站点模式展示）
    history_rows = ""
    if args.site_dir:
        site_dir = args.site_dir
        os.makedirs(site_dir, exist_ok=True)
        all_summaries = sorted(glob.glob(os.path.join(base_dir, "reports", "summary_*.html")), reverse=True)
        current_abs = os.path.abspath(args.output)
        for hs in all_summaries:
            if os.path.abspath(hs) == current_abs:
                continue
            fn = os.path.basename(hs)
            dst = os.path.join(site_dir, "history_" + fn)
            try:
                shutil.copy(hs, dst)
                m = re.match(r"summary_(\d{4}-\d{2}-\d{2})_(\w+)\.html", fn)
                if m:
                    d, t = m.group(1), m.group(2)
                    ttxt = TYPE_TXT.get(t, (t, ""))[0]
                    history_rows += f'<a href="history_{fn}" style="display:block;background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:8px;color:#222;text-decoration:none;box-shadow:0 1px 3px rgba(0,0,0,0.06);"><div style="font-size:14px;font-weight:600;">{d} {ttxt}</div><div style="font-size:11px;color:#999;margin-top:2px;">点击查看历史提示</div></a>'
            except OSError:
                pass
    hist_section = f'<div class="sec-title">📜 历史报告</div><div style="display:block;">{history_rows}</div>' if history_rows else ""

    # 移动端主模板
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>持仓组合 · {args.date} {type_txt}</title>
<style>
  * {{ box-sizing:border-box; -webkit-tap-highlight-color:transparent; }}
  body {{ margin:0; background:#eef1f5; font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif; font-size:15px; color:#222; }}
  .wrap {{ max-width:520px; margin:0 auto; padding:12px 12px 40px; }}
  .topbar {{ position:sticky; top:0; z-index:10; background:linear-gradient(135deg,#1a1a2e,#16213e); color:#fff; border-radius:0 0 16px 16px; padding:16px 18px 14px; margin:-12px -12px 14px; box-shadow:0 2px 10px rgba(0,0,0,0.2); }}
  .topbar h1 {{ margin:0; font-size:19px; font-weight:800; letter-spacing:0.5px; }}
  .topbar .sub {{ font-size:12px; color:rgba(255,255,255,0.65); margin-top:3px; }}
  .topbar .chips {{ display:flex; gap:6px; margin-top:10px; flex-wrap:wrap; }}
  .chip {{ background:rgba(255,255,255,0.14); padding:3px 10px; border-radius:10px; font-size:11px; color:rgba(255,255,255,0.85); }}
  .asset-card {{ background:linear-gradient(135deg,#1565c0,#0d47a1); border-radius:14px; padding:16px 18px; color:#fff; margin-bottom:14px; box-shadow:0 3px 12px rgba(13,71,161,0.25); }}
  .asset-card .lbl {{ font-size:11px; color:rgba(255,255,255,0.7); }}
  .asset-card .big {{ font-size:26px; font-weight:800; margin-top:2px; }}
  .asset-card .grid {{ display:flex; justify-content:space-between; margin-top:12px; padding-top:12px; border-top:1px solid rgba(255,255,255,0.18); }}
  .asset-card .grid div {{ text-align:center; flex:1; }}
  .asset-card .grid .v {{ font-size:16px; font-weight:700; }}
  .asset-card .grid .k {{ font-size:10px; color:rgba(255,255,255,0.65); margin-top:2px; }}
  .sec-title {{ font-size:13px; font-weight:700; color:#444; margin:16px 0 8px; padding-left:10px; border-left:3px solid #1565c0; }}
  .strip {{ display:flex; gap:10px; overflow-x:auto; padding-bottom:8px; scrollbar-width:none; }}
  .strip::-webkit-scrollbar {{ display:none; }}
  .cards {{ display:flex; flex-direction:column; gap:14px; }}
  .cards section {{ max-width:100% !important; margin:0 !important; box-shadow:0 2px 10px rgba(0,0,0,0.10) !important; }}
  .cards section section {{ box-shadow:none !important; }}
  .history {{ display:none; }}
  .footer {{ text-align:center; padding:18px 10px 6px; font-size:11px; color:#9aa; line-height:1.7; }}
  .refresh-btn {{ position:fixed; right:16px; bottom:22px; z-index:20; background:#1565c0; color:#fff; border:none; border-radius:50%; width:52px; height:52px; font-size:20px; box-shadow:0 4px 14px rgba(21,101,192,0.45); cursor:pointer; display:flex; align-items:center; justify-content:center; }}
  {trade_css}
  @media (min-width:521px) {{
    body {{ background:#dde3ea; }}
    .wrap {{ box-shadow:0 0 24px rgba(0,0,0,0.10); background:#eef1f5; min-height:100vh; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <h1>📈 持仓组合监控</h1>
    <div class="sub">{args.date} {type_txt}({type_time}) · 共{pos_count}只持仓 · 更新 {now}</div>
    <div class="chips">
      <span class="chip">🟢持有</span><span class="chip">🟡减仓50%</span><span class="chip">🔴止损/清仓</span>
    </div>
  </div>

  <div class="asset-card">
    <div class="lbl">持仓总市值 / 总成本（折算人民币）</div>
    <div class="big" id="total-mkt" style="color:{total_pnl_color};">¥{total_mkt:,.2f}</div>
    <div id="total-cost" style="font-size:12px;color:rgba(255,255,255,0.8);margin-top:2px;">总成本 ¥{total_cost:,.2f}</div>
    <div style="font-size:11px;color:rgba(255,255,255,0.7);margin-top:4px;">{breakdown_html}</div>
    <div class="grid">
      <div><div class="v" id="total-pnl" style="color:{total_pnl_color};">{total_pnl:+,.2f}</div><div class="k">累计盈亏(¥)</div></div>
      <div><div class="v" id="total-pnl-pct" style="color:{total_pnl_color};">{total_pnl_pct:+.2f}%</div><div class="k">收益率</div></div>
      <div><div class="v">{pos_count}</div><div class="k">持仓数</div></div>
    </div>
    <div style="font-size:10px;color:rgba(255,255,255,0.55);margin-top:8px;">汇率 {fx_txt}（{fx.get('updated_at','')}）</div>
  </div>

  <div class="sec-title">⚡ 今日信号</div>
  <div class="strip">{strip_html}</div>

  <div class="sec-title">📋 个股详情</div>
  <div class="cards">{cards_html}</div>

  {hist_section}

  <div class="footer">
    规则: 静态止损-10% · MA20移动止损 · 新高回撤10%清仓 · 高位放量滞涨/长上影/双顶减仓50%<br>
    震荡小票止盈10/20/30%分批 · 趋势龙头浮盈20%减半 · 减仓后剩余仓位MA20移动止盈<br>
    外币持仓按实时汇率折算人民币 · 数据来自腾讯自选股 · 仅供参考，不构成投资建议
  </div>
</div>
<button class="trade-fab" onclick="openTrade()">💰</button>
<button class="refresh-btn" onclick="location.reload()">🔄</button>
{trade_html}
<script>{trade_js}</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    print(json.dumps({"ok": True, "output": args.output, "cards": len(cards), "total_mkt": round(total_mkt, 2), "total_pnl": round(total_pnl, 2), "generated_at": now}, ensure_ascii=False, indent=2))

    # 站点模式：复制最新 summary 为 latest.html + 生成 index.html（含历史列表）
    if args.site_dir:
        site_dir = args.site_dir
        os.makedirs(site_dir, exist_ok=True)
        shutil.copy(args.output, os.path.join(site_dir, "latest.html"))
        with open(os.path.join(site_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print(json.dumps({"site": site_dir, "ok": True}, ensure_ascii=False))


def pnl_color(pnl):
    """根据浮盈文本给颜色（红涨绿跌）"""
    try:
        v = float(str(pnl).replace("%", "").replace("+", ""))
        return "#e53935" if v >= 0 else "#43a047"
    except (ValueError, TypeError):
        return "#666"


if __name__ == "__main__":
    main()
