# A 股量化筛选工具

拉取全市场（沪深京 5000+ 只 A 股）行情与财务数据，按可配置条件做基本面筛选。

数据源为 [AkShare](https://akshare.akfamily.xyz/)（免费、无需注册），只用全市场批量接口，
一次请求拿全市场，不会逐股发请求触发风控。

## 架构

```
行情快照（盘中可反复刷新）─┐
                          ├─ SQLite 缓存 ─ 按 config.yaml 筛选 ─ 终端表格 / CSV
财务数据（每季度更新一次）─┘                └─ Streamlit 浏览器界面（同一套筛选逻辑）
```

财务数据来自季报/年报，本身没有"实时"，工具自动取最近已披露的报告期
（未披露则回退上一期）；实时性体现在行情快照（价格、PE/PB、市值）。

## 安装

```bash
pip install -r requirements.txt
```

## 使用

```bash
cd ashare_screener

python -m screener update              # 首次：拉取行情 + 最新财务数据入库
python -m screener update --spot-only  # 盘中只刷新行情快照
python -m screener screen              # 按 config.yaml 筛选
python -m screener screen -o out.csv   # 同时导出 CSV
python -m screener status              # 查看缓存数据时间
python -m screener ui                  # 启动浏览器界面（默认 http://localhost:8501）
```

筛选条件在 `config.yaml` 里配置，字段说明和示例见该文件注释。

### 浏览器界面

`python -m screener ui` 启动后打开 http://localhost:8501 ：

- 左侧栏增删筛选条件、调阈值、选排序字段与输出列，右侧表格即时刷新。
- 顶部「命中」显示满足条件的总数，超出「最多显示」时另标注实际展示了几只。
- 市值、营收、净利这类大额字段，输入框和表格都以「亿」为单位，配置里仍存元。
- 侧栏「刷新行情」/「全量更新」等价于 `update --spot-only` / `update`。
- 行情快照超过 3 天会在页面顶部提示过期。
- 界面以 `config.yaml` 为初始条件；调好后展开「当前条件对应的 config.yaml」
  可把这套条件复制回配置文件，命令行跑出的结果与界面一致。

只监听本机，局域网访问需显式 `--host 0.0.0.0`。换库或换策略文件：

```bash
python -m screener --db other.db ui -c strategies/lowpe.yaml --port 8600
```

## 测试

```bash
cd ashare_screener && python -m pytest tests/ -v
```

测试用合成数据覆盖筛选/缓存逻辑与浏览器界面（Streamlit AppTest 无头跑），不依赖网络。

## 已知限制

- AkShare 底层抓取东方财富页面接口，东财改版会导致列名变化，
  报错时核对 `screener/fetch.py` 里的两个列名映射表。
- 财务字段目前来自东财"业绩报表"（营收、净利、ROE、毛利率等摘要指标），
  如需资产负债率、现金流明细等，可在 `fetch.py` 中按同样模式接入
  `ak.stock_zcfz_em` / `ak.stock_xjll_em` 等批量接口。
- 行情"实时"为快照轮询级别，非逐笔推送。
