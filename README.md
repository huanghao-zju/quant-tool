# quant-tool

量化工具集。

## 工具列表

### [ashare_screener](ashare_screener/) — A 股量化筛选工具

拉取全市场（沪深京 5000+ 只 A 股）行情与财务数据，按可配置条件做基本面筛选。
数据源为 [AkShare](https://akshare.akfamily.xyz/)，详见 [ashare_screener/README.md](ashare_screener/README.md)。

```bash
cd ashare_screener
pip install -r requirements.txt
python -m screener update     # 拉取行情 + 最新财务数据入库
python -m screener screen     # 按 config.yaml 筛选
```
