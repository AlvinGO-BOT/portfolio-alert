#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端每日运行编排器
流程: fetch_data(公开接口) → analyze_position(每只持仓) → build_summary(docs/站点)
用法: python3 run_daily.py [--type morning|noon|evening] [--date YYYY-MM-DD]
不指定 --type 时按北京时间自动判断(5-11点morning / 11-15点noon / 其余evening)
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def run(cmd):
    print(f"\n$ {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True)
    if r.stdout:
        print(r.stdout.strip())
    if r.stderr:
        print(r.stderr.strip(), file=sys.stderr)
    if r.returncode != 0:
        print(f"❌ 步骤失败(exit {r.returncode})", file=sys.stderr)
        sys.exit(r.returncode)


def detect_type():
    h = datetime.now(CST).hour
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 15:
        return "noon"
    return "evening"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", default=None, choices=["morning", "noon", "evening"])
    parser.add_argument("--date", default=None)
    args = parser.parse_args()
    rtype = args.type or detect_type()
    date = args.date or datetime.now(CST).strftime("%Y-%m-%d")
    ymd = date.replace("-", "")
    print(f"=== 持仓提示模型云端运行 {date} {rtype} ===")

    # 1) 拉取数据(公开接口) + 更新fx/peak_price
    run([PY, os.path.join("scripts", "fetch_data.py")])

    # 2) 逐持仓运行策略引擎
    with open(os.path.join(BASE, "config", "portfolio.json"), "r", encoding="utf-8") as f:
        positions = [p for p in json.load(f).get("positions", []) if float(p.get("quantity") or 0) > 0]
    for pos in positions:
        code = pos["code"]
        data_f = os.path.join("reports", f"data_{code}_{ymd}.json")
        out_f = os.path.join("reports", f"report_{date}_{rtype}_{code}.html")
        if not os.path.exists(os.path.join(BASE, data_f)):
            print(f"⚠️ 跳过 {code}: 数据文件缺失", file=sys.stderr)
            continue
        run([PY, os.path.join("scripts", "analyze_position.py"), data_f,
             "--code", code, "--config", os.path.join("config", "portfolio.json"),
             "--output", out_f, "--type", rtype])

    # 3) 组合总览 + 站点输出到 docs/(GitHub Pages)
    run([PY, os.path.join("scripts", "build_summary.py"),
         "--date", date, "--type", rtype,
         "--reports", os.path.join("reports", f"report_{date}_{rtype}_*.html"),
         "--config", os.path.join("config", "portfolio.json"),
         "--output", os.path.join("reports", f"summary_{date}_{rtype}.html"),
         "--site-dir", "docs"])

    print(f"\n✅ 运行完成: docs/index.html 已更新 ({date} {rtype})")


if __name__ == "__main__":
    main()
