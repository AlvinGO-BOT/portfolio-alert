# 持仓提示模型（云端版）

跨市场(A股/美股/港股)持仓监控，按自定义止盈止损策略每日三次自动生成移动端提示页面。

## 架构

- **定时**: GitHub Actions（北京时间 08:00 / 12:00 / 18:10，周一至周五），电脑关机不影响
- **数据**: 公开行情接口（腾讯报价/K线 + 东方财富资金流 + open.er-api汇率），无MCP依赖
- **策略**: `scripts/analyze_position.py`（止损-10% / MA20移动止盈 / 新高回撤10%清仓 / 高位形态减仓50% / 阶梯止盈）
- **发布**: `docs/` 目录 → GitHub Pages 手机站点

## 目录

```
config/portfolio.json   持仓与策略配置(修改持仓编辑此文件)
scripts/fetch_data.py   数据获取层(公开接口)
scripts/analyze_position.py  策略分析引擎
scripts/build_summary.py     组合总览+移动端站点生成
run_daily.py            编排器: 拉数据→逐持仓分析→生成站点
docs/                   站点输出(GitHub Pages)
.github/workflows/update.yml  定时任务
```

## 手动触发

GitHub 仓库 → Actions → portfolio-update → Run workflow → 选择 morning/noon/evening。

## 修改持仓

编辑 `config/portfolio.json` 的 positions 后提交即可，下次运行自动生效。
