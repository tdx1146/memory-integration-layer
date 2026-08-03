"""自我怀疑账本适配器（doubt_adapter）单元测试（独立、离线）。

运行：
    cd <胶水层目录> && /vol1/@apphome/trim.openclaw/data/home/agentos/living-memory-system/.venv/bin/python -m pytest tests/test_doubt_adapter.py -v
"""

import os
import sys
from datetime import datetime

import pytest

_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)

from interfaces.adapters.doubt_adapter import (
    DoubtAdapter,
    configure,
    monthly_stats,
    query_doubt,
    store_doubt,
)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "doubt_test.db")


@pytest.fixture
def adapter(db_path):
    return DoubtAdapter(db_path)


def _episode(**overrides):
    ep = {
        "t": datetime(2026, 8, 3, 12, 0, 0).timestamp(),
        "trigger_type": "conflict",
        "suspicion": "我之前说 X，现在上下文说 Y，矛盾了",
        "queried": True,
        "answer_changed": True,
        "overturn_evidence": "用户贴出的原文",
        "user_reaction": "corrected",
        "topic": "测试主题",
        "confidence_after": 0.7,
    }
    ep.update(overrides)
    return ep


class TestStoreDoubt:
    def test_store_roundtrip(self, adapter):
        """写入后能查回，字段映射/类型正确（bool 还原、时间戳保留）。"""
        ep = _episode()
        assert adapter.store_doubt(ep) is True
        rows = adapter.query_doubt()
        assert len(rows) == 1
        row = rows[0]
        assert row["trigger_type"] == "conflict"
        assert row["suspicion"] == ep["suspicion"]
        assert row["queried"] is True
        assert row["answer_changed"] is True
        assert row["overturn_evidence"] == "用户贴出的原文"
        assert row["user_reaction"] == "corrected"
        assert row["topic"] == "测试主题"
        assert row["confidence_after"] == pytest.approx(0.7)
        assert abs(row["t"] - ep["t"]) < 1e-6
        assert row["id"] == 1

    def test_default_timestamp_when_missing(self, adapter):
        """缺 t 时自动用当前时间，不阻塞写入。"""
        ep = _episode()
        del ep["t"]
        assert adapter.store_doubt(ep) is True
        row = adapter.query_doubt()[0]
        assert abs(row["t"] - datetime.now().timestamp()) < 60

    def test_fail_open_on_invalid_enum(self, adapter):
        """非法枚举被 SQLite CHECK 拦截：写失败返回 False，不抛异常。"""
        assert adapter.store_doubt(_episode(trigger_type="bogus")) is False
        assert adapter.store_doubt(_episode(user_reaction="angry")) is False
        assert adapter.query_doubt() == []  # 失败不留脏数据，也不崩

    def test_fail_open_on_invalid_input_types(self, adapter):
        """畸形输入不阻塞主流程：缺 trigger_type 写失败，坏 float 归一化。"""
        assert adapter.store_doubt(None) is False
        assert adapter.store_doubt({"suspicion": "没有 trigger_type"}) is False
        # confidence_after 非数字 -> 存 NULL，不崩
        assert adapter.store_doubt(_episode(confidence_after="abc")) is True
        assert adapter.query_doubt()[0]["confidence_after"] is None

    def test_confidence_clamped(self, adapter):
        """confidence_after 越界自动夹到 [0,1]。"""
        assert adapter.store_doubt(_episode(confidence_after=3.7)) is True
        assert adapter.store_doubt(_episode(confidence_after=-2.0)) is True
        vals = sorted(r["confidence_after"] for r in adapter.query_doubt())
        assert vals == [0.0, 1.0]

    def test_bool_coercion(self, adapter):
        """queried/answer_changed 宽松布尔归一。"""
        assert adapter.store_doubt(_episode(queried="yes", answer_changed=0)) is True
        row = adapter.query_doubt()[0]
        assert row["queried"] is True
        assert row["answer_changed"] is False


class TestQueryDoubt:
    def _seed(self, adapter):
        adapter.store_doubt(_episode(t=datetime(2026, 8, 1).timestamp(), trigger_type="conflict", topic="甲"))
        adapter.store_doubt(_episode(t=datetime(2026, 8, 15).timestamp(), trigger_type="fok", topic="甲"))
        adapter.store_doubt(_episode(t=datetime(2026, 7, 20).timestamp(), trigger_type="user_correction", topic="乙"))

    def test_filter_trigger_type(self, adapter):
        self._seed(adapter)
        rows = adapter.query_doubt({"trigger_type": "fok"})
        assert [r["topic"] for r in rows] == ["甲"]
        assert len(rows) == 1

    def test_filter_trigger_type_list(self, adapter):
        self._seed(adapter)
        rows = adapter.query_doubt({"trigger_type": ["conflict", "user_correction"]})
        assert {r["topic"] for r in rows} == {"甲", "乙"}

    def test_filter_topic_like(self, adapter):
        self._seed(adapter)
        rows = adapter.query_doubt({"topic": "甲"})
        assert len(rows) == 2

    def test_filter_month_local_window(self, adapter):
        self._seed(adapter)
        rows = adapter.query_doubt({"month": "2026-08"})
        assert {r["topic"] for r in rows} == {"甲"}
        assert len(rows) == 2

    def test_filter_time_range(self, adapter):
        self._seed(adapter)
        t_from = datetime(2026, 8, 1).timestamp()
        rows = adapter.query_doubt({"t_from": t_from})
        assert len(rows) == 2  # 8/1 和 8/15

    def test_filter_queried(self, adapter):
        adapter.store_doubt(_episode(queried=True, topic="q1"))
        adapter.store_doubt(_episode(queried=False, topic="q2"))
        assert [r["topic"] for r in adapter.query_doubt({"queried": True})] == ["q1"]

    def test_limit_and_order_desc(self, adapter):
        for i in range(5):
            adapter.store_doubt(_episode(t=datetime(2026, 8, 1).timestamp() + i, topic=f"t{i}"))
        rows = adapter.query_doubt({"limit": 2})
        assert len(rows) == 2
        assert rows[0]["topic"] == "t4"  # 最新在前

    def test_no_filter_returns_all(self, adapter):
        self._seed(adapter)
        assert len(adapter.query_doubt()) == 3


class TestMonthlyStats:
    def test_monthly_breakdown(self, adapter):
        adapter.store_doubt(_episode(t=datetime(2026, 8, 3).timestamp(), trigger_type="conflict", queried=True, answer_changed=True, user_reaction="corrected", confidence_after=0.7))
        adapter.store_doubt(_episode(t=datetime(2026, 8, 10).timestamp(), trigger_type="fok", queried=False, answer_changed=False, user_reaction="silent", confidence_after=0.5))
        adapter.store_doubt(_episode(t=datetime(2026, 7, 25).timestamp(), trigger_type="surprise", queried=True, answer_changed=False, user_reaction="acknowledged", confidence_after=0.9))

        stats = adapter.monthly_stats()
        assert stats["total"] == 3
        assert set(stats["months"].keys()) == {"2026-07", "2026-08"}
        assert stats["latest_month"] == "2026-08"

        aug = stats["months"]["2026-08"]
        assert aug["total"] == 2
        assert aug["by_trigger"] == {"conflict": 1, "fok": 1}
        assert aug["by_reaction"] == {"corrected": 1, "silent": 1}
        assert aug["queried"] == 1
        assert aug["answer_changed"] == 1
        assert aug["overturned"] == 1
        assert aug["avg_confidence_after"] == pytest.approx(0.6)

        jul = stats["months"]["2026-07"]
        assert jul["overturned"] == 0
        assert jul["avg_confidence_after"] == pytest.approx(0.9)

    def test_stats_empty_db(self, adapter):
        stats = adapter.monthly_stats()
        assert stats == {"total": 0, "months": {}, "latest_month": None}


class TestPersistenceAndModuleLevel:
    def test_persists_across_instances(self, db_path):
        """数据落盘：同一 db 文件新实例仍可查询。"""
        DoubtAdapter(db_path).store_doubt(_episode())
        fresh = DoubtAdapter(db_path)
        assert len(fresh.query_doubt()) == 1
        assert fresh.monthly_stats()["total"] == 1

    def test_module_level_functions_with_configure(self, tmp_path):
        """模块级便捷入口（单写者）：configure 隔离库后 store/query/stats 走同一实例。"""
        db = str(tmp_path / "doubt_module.db")
        configure(db)
        assert store_doubt(_episode()) is True
        assert len(query_doubt()) == 1
        assert monthly_stats()["total"] == 1

    def test_shared_instance_is_singleton(self, tmp_path):
        configure(str(tmp_path / "doubt_single.db"))
        a = store_doubt  # 模块函数绑定的共享实例
        assert a(_episode(topic="s")) is True
        assert len(query_doubt({"topic": "s"})) == 1
