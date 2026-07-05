# crisis-monitor

需求唯一权威来源：`SPEC.md`。每次会话先读它。

## 编码约定
- Python 3.11+，依赖仅 `requests` `pandas` `yfinance` `pyyaml`。
- 所有阈值/逻辑参数来自 `config/*.yaml`，代码中不得出现魔法数字。
- 每个 fetcher 返回 `pd.Series`（DatetimeIndex, float），失败时返回 None 并记入故障计数，不得抛异常中断主流程。
- 网络请求统一走 `src/fetchers/base.py` 的 `http_get`（重试3次、支持 HTTPS_PROXY）。
- 状态持久化在 `state.json`，由 Actions 提交回仓库。

## 运行
- 本地：`FRED_API_KEY=xxx PUSH_WEBHOOK_URL=xxx python -m src.main`
- 干跑（不推送）：`python -m src.main --dry-run`
- 回测：`python -m backtest.run_backtest --window 2024`

## 环境变量
- `FRED_API_KEY`：可选；缺失时自动回退 fredgraph.csv 公开接口
- `PUSH_WEBHOOK_URL`：飞书群自定义机器人 webhook
- `PUSH_WEBHOOK_SECRET`：可选；飞书机器人开启签名校验时填写
- `HTTPS_PROXY`：可选代理
