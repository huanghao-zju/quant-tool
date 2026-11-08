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
python -m screener screen              # 按 config.yaml 筛选（自动记选股留痕）
python -m screener screen -o out.csv   # 同时导出 CSV
python -m screener screen --no-record  # 本次不留痕
python -m screener history             # 查看留痕（回测用）
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

### 常驻后台运行（macOS）

用 launchd 用户级 agent 常驻，登录即起、崩溃自动重启，只监听本机：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.huanghao.ashare-screener-ui.plist
launchctl print gui/$(id -u)/com.huanghao.ashare-screener-ui   # 查看状态
launchctl kickstart -k gui/$(id -u)/com.huanghao.ashare-screener-ui  # 改完代码重启
launchctl bootout gui/$(id -u)/com.huanghao.ashare-screener-ui  # 停止（删 plist 则永久移除）
```

日志在 `~/Library/Logs/ashare-screener-ui.log`。
`nohup` 起的进程会随终端会话被回收，做不到常驻，所以用 launchd。

### 每日自动更新

交易日常（周一至周五）16:00 自动跑一次全量更新，逻辑在
`scripts/daily_update.sh`，配置在
`~/Library/LaunchAgents/com.huanghao.ashare-screener-update.plist`：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.huanghao.ashare-screener-update.plist
launchctl print gui/$(id -u)/com.huanghao.ashare-screener-update   # 查看状态
launchctl kickstart -k gui/$(id -u)/com.huanghao.ashare-screener-update  # 立刻手动跑一次
launchctl bootout gui/$(id -u)/com.huanghao.ashare-screener-update # 停用（删 plist 则永久移除）
```

日志在 `~/Library/Logs/ashare-screener-update.log`。脚本更新成功后会自动
kickstart 一下 UI 服务，让它丢掉 `st.cache_data` 里的旧缓存。

电脑在 16:00 关机时这次会跳过（`RunAtLoad` 为 false，不补跑）。若希望开机
时补跑，把 plist 里 `RunAtLoad` 改成 `true`——代价是每次登录都会全量更新一次。

改时间的话编辑 plist 里的 `StartCalendarInterval`，只列周一到周五各一条；
改完 `launchctl bootout` 再 `bootstrap` 才会生效。

## 报告期口径（容易踩的坑）

东财「业绩报表」里的 `eps` `revenue` `net_profit` `ocf_per_share` `roe`
都是**报告期累计值**，不是年化值。一季报只累计 3 个月，所以同一个
`roe >= 12` 的门槛在不同报告期含义完全不同，实测全市场符合数量差四十倍：

| 报告期 | ROE 中位数 | ROE ≥ 12 的只数 |
|---|---|---|
| 2025 年报 | 4.56 | 2588 |
| 2026 一季报 | 1.10 | 67 |
| 2026 半年报 | 2.08 | 722 |

要年度可比的门槛，用衍生字段 `roe_annual`（= `roe` × 年化倍数，
一季报 ×4、半年报 ×2、三季报 ×4/3、年报 ×1）。界面会显示当前报告期
累计几个月、累计值要乘几才是年化值。

`revenue_yoy` `net_profit_yoy` 是同比增速，本身可比，不受此影响。

## 三个会直接影响结果正确性的地方

**1. `pegy` 不认亏损股。** 公司亏损（`pe_ttm < 0`）且利润下滑（增速为负）时，
「负 PE ÷ 负增速」仍是正数，而且数值往往极小——万科A `pe_ttm=-1.62`、
增速 -25%，算出 `pegy=0.064`，会被 `pegy < 0.5` 判成"极低估"。
修复前全市场有 **441 只**亏损股这样蒙混过关。所以分母 ≤0 或 `pe_ttm ≤0`
时 `pegy` 置空，由筛选条件自然淘汰。

**2. `max_per_industry` 防行业扎堆。** 周期性行业天然符合"低 PE + 高增长"
（盈利高点的周期股），不限制的话结果会全跟着周期走——看起来在分散，
实际是一篮子有色金属。该项在**排序之后**生效，留下每个行业里按 `sort_by`
排最好的 N 只。实测同一套条件：

| max_per_industry | 命中 | 行业数 | 最大单行业 | 前三行业占比 |
|---|---|---|---|---|
| 不限 | 50 | 26 | 9（工业金属） | 34% |
| 5 | 50 | 27 | 5 | 26% |
| 3 | 50 | 30 | 3 | 18% |
| 2 | 50 | 35 | 2 | 12% |

**3. `has_dividend` 区分「不分红」与「股息低」。** `div_yield` 缺失实际是
"查不到分红方案"，全市场 34.8% 如此，多数是公司从未分红。计算 `pegy`
时按 0 计入分母，会把这部分公司从"因不增长被淘汰"变成"中性"，反而抬高
其 `pegy`。要卡分红就直接筛 `has_dividend == 1`。

## 连续正增长（挡单期暴增）

`revenue_stable` / `profit_stable` 是两个 0/1 字段：最近 N 期
（`stability_periods`，默认 2）的**同比增速全部 > 0** 才算 1。周期股常是
一期暴增、下一期转负，单看最新一期容易误判。

⚠ 这两个字段**只基于 yoy 增速**，不看累计绝对值。一季报营收只累计 3 个月、
半年报累计 6 个月——实测 96.6% 的公司半年报营收都大于一季报，那是**口径差异
不是增长**。增速本身已是同比，与报告期长度无关，才可以横向比较。

```yaml
filters:
  - {field: revenue_stable, op: "==", value: 1}
  - {field: profit_stable,  op: "==", value: 1}
```

## 选股留痕（回测的基础）

每次 `screen` 会自动把结果连同当时的条件写进 `screen_history` /
`screen_history_items` 两张表，包含**入选时的价格、PE、PEGY、名次**——
这样"入选即买入"这个假设事后可以复现。

```bash
python -m screener history                 # 看留痕列表
python -m screener history --prune-days 180  # 顺手清理 180 天前的
```

⚠ 当前只存不下单判定，还**没有**自动回测收益：历史行情接口
`ak.stock_zh_a_hist` 是逐股的，拉全市场 5000 只必然触发风控，与本项目
"只用批量接口"的核心设计冲突。留痕先攒起来，后续可以只对入选的几十只
回补历史价格做超额收益统计，成本可控。

留痕表随 `screen` 自动积累，`data/` 已在 .gitignore 里，不会进版本库。

## 已知限制

- AkShare 底层抓取东方财富页面接口，东财改版会导致列名变化，
  报错时核对 `screener/fetch.py` 里的两个列名映射表。
- 财务字段目前来自东财"业绩报表"（营收、净利、ROE、毛利率等摘要指标），
  如需资产负债率、现金流明细等，可在 `fetch.py` 中按同样模式接入
  `ak.stock_zcfz_em` / `ak.stock_xjll_em` 等批量接口。
- 行情"实时"为快照轮询级别，非逐笔推送。
- `update --spot-only` 只动行情，财务数据不变；只刷行情会得到
  「今天的价格 + 旧的基本面」，界面会在财务落后一个报告期时告警。
- 全量更新要分页拉几十次、耗时数分钟。在界面里跑时中途刷新页面或操作
  控件会打断脚本运行，已完成的阶段保留、未完成的丢弃；网络慢时建议
  在终端跑 `python -m screener update`。

---

## 彭博社新闻简报（飞书推送）

每天早晚两次，把彭博社的中国市场消息和全球头条推到飞书群。

### 能拿到什么、拿不到什么

只走彭博官方 sitemap（`robots.txt` 里显式声明的那几个），**不抓页面**：

| | 状态 |
|---|---|
| 标题、发布时间、原文链接 | ✅ 从 `sitemaps/news/latest.xml` 直接拿，约 2 天全量 500+ 条 |
| 正文 / 摘要 | ❌ 付费墙后，首页与文章页对非订阅请求一律 403 |

这不是妥协，是边界：搬运正文既侵版权也过不了 WAF。日报只做「标题 + 链接」，
点进去看不看得到全文取决于你有没有彭博订阅。

WAF 会拦普通请求（curl 和普通 requests 都是 403），所以客户端用
`curl_cffi` 模拟 Chrome 的 TLS 指纹。sitemap 路径对爬虫放行，实测稳定。

### 前置条件

**必须能连上 bloomberg.com。** 脚本不会去连 VPN ——
VPN 用 TUN/增强模式时走系统路由，launchd 任务天然生效；
如果是「系统代理」模式而 Python 读不到，在 `config.yaml` 里填
`news.proxy: http://127.0.0.1:7890`。

### 配置飞书

1. 飞书里建一个群（只有自己也行）→ 群设置 → 群机器人 → 添加机器人 →
   **自定义机器人**
2. 复制 Webhook 地址，填进 `config.yaml`：

```yaml
news:
  feishu:
    webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx"
    secret: ""          # 安全设置里勾了「签名校验」才填
```

3. 验证连通：

```bash
/usr/local/bin/python3 -m screener.news --test-webhook
```

### 日常用法

```bash
python -m screener.news --preview        # 看今天会推什么，不发送
python -m screener.news --dry-run        # 打印完整卡片 JSON
python -m screener.news --slot morning   # 手动推早报
python -m screener.news --force          # 忽略时间窗口和已推送标记
```

### 定时部署

```bash
cp deploy/com.huanghao.ashare-news.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.huanghao.ashare-news.plist
```

日志：`~/Library/Logs/ashare-news.log`

plist 用 `StartInterval 1200`（每 20 分钟唤醒）而不是定点触发，
配合 python 侧的窗口判断实现**补推**：

- 窗口：早 8:30–11:00、晚 20:30–23:00（`config.yaml` 可改）
- 落在窗口内且当日该时段未成功推送 → 抓取并推送
- 8:30 时 VPN 没开 → 静默退出，9:10 VPN 连上后自动补上
- 已推过 → 直接退出，不会刷屏
- 窗口快结束仍未成功 → 发一张红色告警卡片，提示检查 VPN

### 内容编排

每次 10 条，中国市场优先（默认 6 + 4）：

- **中国市场**：标题命中地域/政策/货币词（China、Beijing、PBOC、yuan、A-shares…）
  或中国公司名（Alibaba、BYD、CATL…）。排序额外加分给市场类词
  （stock、IPO、GDP、loan…），降权给社会新闻（earthquake、kills…）——
  否则「西藏地震」会压过「A 股进入熊市」。
- **全球头条**：不含中国关键词的条目，按宏观权重词 + 板块 + 时新度打分。
- **两栏互补不重复**。中国栏空出来的名额让给头条（周末中国稿极少，
  不补的话会收到只有头条的空壳卡片）；但头条新闻绝不塞进中国栏冒充中国消息。

**事件级去重**：彭博对同一事件常发多篇（「A股进入熊市」+「中国股票跌入熊市，
2.4 万亿美元蒸发」），URL 不同但内容重复。按标题相似度 + 显著词共现 +
地区词冲突保护合并，保留分数最高的一条。错杀比冗余更糟，所以规则偏保守
（Hermès 和 Gucci 都在讲中国消费降级，但各自保留）。

**跨次去重**：推送过的 URL 记进 `data/news.db`，14 天内不再出现。
见过但没进 top 10 的不记录 —— 下次还有机会。

### 已知限制

- 依赖 VPN 在线。VPN 没开时那一次推不出来，只能靠窗口内补推或告警。
- 只有标题，没有摘要。想要中文摘要得再接一个翻译/LLM 环节。
- 关键词是硬编码在 `screener/news/rank.py` 里的英文列表。彭博英文标题
  覆盖得到，但如果哪天要扩到日文/德文源，得另做。
- 周末彭博中国相关稿件极少，中国栏经常是空的，属于正常现象。
