"""浏览器界面测试。

用 Streamlit 官方的 AppTest 在无头模式下真跑一遍 app.py，数据来自临时
SQLite（合成数据），不联网。未安装 streamlit 时整个文件跳过。
"""

import datetime as dt

import pandas as pd
import pytest

streamlit = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from screener.cache import Cache  # noqa: E402

APP = "screener/app.py"

CONFIG_YAML = """\
report_date: null
exclude_st: true
exclude_bse: false
filters:
  - {field: roe, op: ">=", value: 10}
  - {field: total_mcap, op: ">=", value: 5.0e+9}
sort_by: roe
ascending: false
top: 50
output_fields: [code, name, industry, price, pe_ttm, total_mcap, roe]
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """建一个只含合成数据的库和配置，并让 app.py 指向它们。"""
    spot = pd.DataFrame(
        {
            "code": ["600000", "000001", "300750", "839999", "600519"],
            "name": ["浦发银行", "平安银行", "宁德时代", "ST某某", "贵州茅台"],
            "price": [8.0, 12.0, 200.0, 3.0, 1500.0],
            "pe_ttm": [5.0, 6.0, 30.0, -8.0, 28.0],
            "pb": [0.5, 0.7, 4.0, 1.1, 8.0],
            "total_mcap": [2.3e11, 2.5e11, 9.0e11, 1.0e9, 1.9e12],
        }
    )
    fin = pd.DataFrame(
        {
            "code": ["600000", "000001", "300750", "839999", "600519"],
            "name": ["浦发银行", "平安银行", "宁德时代", "ST某某", "贵州茅台"],
            "roe": [9.0, 11.0, 22.0, 1.0, 32.0],
            "net_profit_yoy": [-2.0, 6.0, 18.0, -30.0, 15.0],
            "revenue_yoy": [-2.0, 6.0, 18.0, -30.0, 15.0],
            "industry": ["银行", "银行", "电池", "其他", "白酒"],
            "report_date": ["20260331"] * 5,
        }
    )
    db = tmp_path / "t.db"
    cache = Cache(db)
    cache.save_spot(spot)
    cache.save_financials(fin)
    cache.close()

    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG_YAML, encoding="utf-8")
    monkeypatch.setenv("SCREENER_DB", str(db))
    monkeypatch.setenv("SCREENER_CONFIG", str(cfg))
    return tmp_path


def run_app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert not at.error, [e.value for e in at.error]
    return at


def hit_count(at: AppTest) -> int:
    return int(at.metric[0].value.split()[0])


def test_app_renders_and_matches_config(env):
    at = run_app()
    # roe>=10 且市值>=50亿：平安银行(11)、宁德(22)、茅台(32)；ST 已剔除
    assert hit_count(at) == 3
    assert set(at.dataframe[0].value["code"]) == {"000001", "300750", "600519"}
    # sort_by=roe 降序
    assert list(at.dataframe[0].value["roe"]) == [32.0, 22.0, 11.0]


def test_market_cap_shown_in_yi(env):
    """总市值列在表格里换算成亿，输入框同理。"""
    at = run_app()
    mcap = at.dataframe[0].value["total_mcap"]
    assert mcap.max() == pytest.approx(19000.0)  # 1.9e12 元 = 19000 亿
    assert at.number_input(key="val_1_num").value == pytest.approx(50.0)  # 5e9 元


def test_edit_threshold_updates_results(env):
    at = run_app()
    at.number_input(key="val_0_num").set_value(25.0).run()  # roe >= 25
    assert not at.exception
    assert hit_count(at) == 1
    assert list(at.dataframe[0].value["code"]) == ["600519"]


def test_add_and_remove_filter(env):
    at = run_app()
    n = len(at.session_state.filters)
    at.button(key="del_0").click().run()  # 去掉 roe 条件
    assert not at.exception
    assert len(at.session_state.filters) == n - 1
    assert hit_count(at) == 4  # 只剩市值条件，ST 仍被剔除

    add = [b for b in at.button if b.label.startswith("＋")][0]
    add.click().run()
    assert not at.exception
    assert len(at.session_state.filters) == n


def test_switch_numeric_field_to_text_field(env):
    """数值字段换成文本字段时控件类型改变，不应复用旧值报错。"""
    at = run_app()
    at.selectbox(key="field_0").set_value("industry").run()
    assert not at.exception
    assert at.selectbox(key="op_0").value == "contains"
    at.text_input(key="val_0_text").set_value("银行").run()
    assert not at.exception
    # 只剩「行业含银行」+「市值>=50亿」，两家银行都够市值
    assert set(at.dataframe[0].value["code"]) == {"000001", "600000"}


def test_between_operator_converts_yi_to_yuan(env):
    at = run_app()
    at.selectbox(key="op_1").set_value("between").run()
    at.number_input(key="lo_1").set_value(1000.0).run()
    at.number_input(key="hi_1").set_value(5000.0).run()
    assert not at.exception
    stored = [f for f in at.session_state.filters if f["_id"] == 1][0]["value"]
    assert stored == [1e11, 5e11]  # 亿 -> 元
    assert set(at.dataframe[0].value["code"]) == {"000001"}


def test_hit_count_is_pre_truncation(env):
    """top 截断显示，但命中数应是截断前的真实数量。"""
    at = run_app()
    at.number_input(key="val_0_num").set_value(0.0).run()  # roe >= 0
    at.number_input(key="top").set_value(1).run()
    assert not at.exception
    assert hit_count(at) == 4  # 真实命中 4 只
    assert len(at.dataframe[0].value) == 1  # 表格只显示 1 只


def test_stale_snapshot_warning(env, monkeypatch):
    from screener import fetch

    # 固定「最新已披露报告期」，免得财务过期提示混进来
    monkeypatch.setattr(fetch, "latest_published_report", lambda *a, **k: "20260331")

    at = run_app()
    assert not any("天前的数据" in w.value for w in at.warning)  # 刚写入的快照

    cache = Cache(str(env / "t.db"))
    old = (dt.datetime.now() - dt.timedelta(days=30)).isoformat(timespec="seconds")
    cache._set_meta("spot_fetched_at", old)
    cache.close()

    at = run_app()
    assert any("30 天前" in w.value for w in at.warning)


def test_empty_cache_shows_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENER_DB", str(tmp_path / "empty.db"))
    monkeypatch.setenv("SCREENER_CONFIG", str(tmp_path / "missing.yaml"))
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception
    assert at.warning and "缓存为空" in at.warning[0].value


def test_exported_yaml_is_loadable(env):
    """界面导出的 config 文本应能直接被 yaml 解析且不含内部字段。"""
    import yaml

    at = run_app()
    text = next(c.value for c in at.code if "filters" in c.value)
    cfg = yaml.safe_load(text)
    assert cfg["top"] == 50
    assert all("_id" not in f for f in cfg["filters"])
    assert {"field", "op", "value"} == set(cfg["filters"][0])


def test_ui_command_is_headless_and_local_only():
    """ui 子命令必须 headless（否则首次运行卡在 Email 引导提示）且默认只监听本机。"""
    from screener.cli import main, ui_command

    captured = {}

    def fake_call(cmd, env=None):
        captured["cmd"], captured["env"] = cmd, env
        return 0

    import subprocess

    real = subprocess.call
    subprocess.call = fake_call
    try:
        main(["--db", "/tmp/x.db", "ui", "-c", "/tmp/x.yaml", "--port", "8600", "--headless"])
    finally:
        subprocess.call = real

    cmd = captured["cmd"]
    assert "--server.headless" in cmd and cmd[cmd.index("--server.headless") + 1] == "true"
    assert cmd[cmd.index("--server.address") + 1] == "localhost"
    assert cmd[cmd.index("--server.port") + 1] == "8600"
    assert cmd[cmd.index("--browser.gatherUsageStats") + 1] == "false"
    # --db / -c 通过环境变量传给子进程
    assert captured["env"]["SCREENER_DB"] == "/tmp/x.db"
    assert captured["env"]["SCREENER_CONFIG"] == "/tmp/x.yaml"


def test_ui_command_respects_host_override():
    import argparse

    from screener.cli import ui_command

    args = argparse.Namespace(port=8501, host="0.0.0.0", headless=True)
    cmd = ui_command(args)
    assert cmd[cmd.index("--server.address") + 1] == "0.0.0.0"


def test_empty_columns_hidden_from_filters(env, monkeypatch):
    """备用数据源会返回整列为空的字段，不该出现在筛选项里。"""
    import pandas as pd

    from screener.cache import Cache

    cache = Cache(str(env / "t.db"))
    spot, _ = cache.load_spot()
    spot["pct_60d"] = pd.NA  # 模拟备用源返回的全空列
    cache.save_spot(spot)
    cache.close()

    at = run_app()
    assert any("60日涨跌幅" in i.value for i in at.info)
    # 字段下拉里不应出现它
    options = at.selectbox(key="field_0").options
    assert not any("pct_60d" in o for o in options)


def test_update_failure_shows_readable_error(env, monkeypatch):
    """拉取失败时页面给出可读错误，而不是抛栈，且不 rerun 把错误刷掉。"""
    from screener import fetch

    def boom():
        raise RuntimeError("接口挂了")

    monkeypatch.setattr(fetch, "fetch_spot", boom)

    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    [b for b in at.button if b.label == "刷新行情"][0].click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.error, "失败时应有 st.error"
    assert "接口挂了" in at.error[0].value
    assert "RuntimeError" in at.error[0].value


def test_fallback_notice_reaches_the_page(env, monkeypatch):
    """fetch_spot 切备用源时只 print，界面必须把它显示出来。"""
    import pandas as pd

    from screener import fetch

    def noisy():
        print("已切换到备用接口")
        return pd.DataFrame(
            {"code": ["600000"], "name": ["浦发银行"], "price": [8.0],
             "pe_ttm": [5.0], "total_mcap": [2.3e11]}
        )

    monkeypatch.setattr(fetch, "fetch_spot", noisy)

    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    [b for b in at.button if b.label == "刷新行情"][0].click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("已切换到备用接口" in w.value for w in at.warning)


def test_stale_financials_warning(env, monkeypatch):
    """财务数据落后一个报告期时要提示，只更新行情不够。"""
    from screener import fetch

    monkeypatch.setattr(fetch, "latest_published_report", lambda *a, **k: "20260630")
    at = run_app()  # fixture 里缓存的是 20260331
    assert any("财务数据还停在 20260331" in w.value for w in at.warning)
    assert any("20260630" in w.value for w in at.warning)


def test_no_stale_financials_warning_when_current(env, monkeypatch):
    from screener import fetch

    monkeypatch.setattr(fetch, "latest_published_report", lambda *a, **k: "20260331")
    at = run_app()
    assert not any("财务数据还停在" in w.value for w in at.warning)


def test_period_cumulative_notice_and_annual_roe_field(env, monkeypatch):
    """一季报要提示 roe 是 3 个月累计，且年化字段可选可筛。"""
    from screener import fetch

    monkeypatch.setattr(fetch, "latest_published_report", lambda *a, **k: "20260331")
    at = run_app()
    assert any("累计 3 个月" in i.value for i in at.info)

    options = at.selectbox(key="field_0").options
    assert any("roe_annual" in o for o in options)

    # 用年化字段筛：fixture 里 roe 11/22/32 一季报 ×4 => 44/88/128
    at.selectbox(key="field_0").set_value("roe_annual").run()
    at.number_input(key="val_0_num").set_value(100.0).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert hit_count(at) == 1  # 只有 roe=32 的茅台年化 128 过线
