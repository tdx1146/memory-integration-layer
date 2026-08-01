"""LMS 真实后端适配器 + memory_context 解析器单元测试。

通过注入 http_post / http_get 完全离线测试，不连接真实 LMS。
"""

import os
import sys

import pytest

_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)

from interfaces.adapters.lms_adapter import LMSAdapter, parse_lms_context
from interfaces.exceptions import CapacityError
from interfaces.models import MemoryItem


def _make_adapter(chat_response=None, health_response=None, fail=False):
    """注入存根 HTTP 层的 LMSAdapter。"""

    def post(url, json):
        if fail:
            raise RuntimeError("network down")
        if url.endswith("/chat"):
            return chat_response or {}
        return {}

    def get(url):
        if fail:
            raise RuntimeError("network down")
        return health_response or {}

    return LMSAdapter(api_url="http://lms.test", http_post=post, http_get=get)


class TestLMSAdapter:
    def test_store_backfills_entropy_surprise(self):
        """store 成功会从 memory_state 回填熵/惊讶度。"""
        adap = _make_adapter(
            chat_response={
                "memory_state": {
                    "last_entropy": 4.5,
                    "last_surprise": 8.0,
                    "purpose_coherence": 0.7,
                }
            }
        )
        m = MemoryItem(text="hello", origin="user")
        assert adap.store(m) is True
        assert m.entropy == 4.5
        assert m.surprise == 8.0
        assert m.metadata["purposefulness"] == 0.7

    def test_store_network_failure_degrades(self):
        adap = _make_adapter(fail=True)
        assert adap.store(MemoryItem(text="offline")) is False
        assert adap.degraded is True

    def test_fetch_context_returns_json(self):
        adap = _make_adapter(chat_response={"memory_context": "ctx"})
        assert adap.fetch_context("q") == {"memory_context": "ctx"}

    def test_recall_k_out_of_range(self):
        adap = _make_adapter()
        with pytest.raises(CapacityError):
            adap.recall("q", k=0)

    def test_get_state_uses_health(self):
        adap = _make_adapter(health_response={"status": "ok", "active_sessions": 3})
        st = adap.get_state()
        assert st.stage == "lms"
        assert st.memory_count == 3

    def test_get_state_network_fail_degrades(self):
        adap = _make_adapter(fail=True)
        st = adap.get_state()  # 降级到 DummyCognitive，不抛错
        assert isinstance(st.memory_count, int)


class TestParseLMSContext:
    def test_empty(self):
        assert parse_lms_context("") == {
            "active_nodes": [],
            "surprise": 0.0,
            "entropy": 0.0,
            "recalled": [],
        }

    def test_extracts_nodes_and_state(self):
        ctx = "熵:4.9 惊讶度:9.9 节点5(强:0.9)"
        out = parse_lms_context(ctx)
        assert out["entropy"] == 4.9
        assert out["surprise"] == 9.9
        assert out["active_nodes"] == [{"id": 5, "strength": 0.9}]

    def test_extracts_recalled_lines(self):
        ctx = '\n1. "回忆一"\n2. "回忆二"\n'
        out = parse_lms_context(ctx)
        assert out["recalled"] == ["回忆一", "回忆二"]

    def test_node_without_score_uses_grade(self):
        out = parse_lms_context("节点1(中)")
        assert out["active_nodes"] == [{"id": 1, "strength": 0.6}]
