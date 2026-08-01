"""丰碑真实后端适配器单元测试（注入 core，离线）。"""

import os
import sys

import pytest

_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)

from interfaces.adapters.monument_adapter import MonumentAdapter
from interfaces.exceptions import CapacityError


class _FakeMonumentCore:
    """最小可用丰碑核心替身。"""

    def __init__(self):
        self.blocks = []
        self.n = 0
        self._pending = None

    def create_candidate(self, title, body, tags, contributor, source=None, **kwargs):
        cid = f"candidate-{self.n}"
        self._pending = {"title": title, "body": body, "tags": tags}
        return {"candidate_id": cid}

    def approve_candidate(self, candidate_id, approver=None, note=None):
        urn = f"urn:monument:permanent:{self.n:04d}"
        pending = self._pending or {"title": "", "body": "", "tags": []}
        self.blocks.append({
            "content": {
                "urn": urn,
                "title": pending["title"],
                "body": pending["body"],
                "tags": pending.get("tags", []),
            }
        })
        self.n += 1
        return self.blocks[-1]

    def read_all(self):
        return list(self.blocks)

    def read_by_tag(self, tag):
        return [b for b in self.blocks if tag in b["content"].get("tags", [])]


class _FailingApproveCore(_FakeMonumentCore):
    def approve_candidate(self, candidate_id, approver=None, note=None):
        raise RuntimeError("no permission")


@pytest.fixture
def adapter():
    return MonumentAdapter(core=_FakeMonumentCore())


class TestMonumentAdapter:
    def test_contribute_ok(self, adapter):
        res = adapter.contribute("架构知识", title="T", tags=["arch"])
        assert res.ok is True
        assert res.urn == "urn:monument:permanent:0000"
        assert "arch" in res.tag

    def test_contribute_empty_rejected(self, adapter):
        res = adapter.contribute("   ")
        assert res.ok is False
        assert "empty" in (res.error or "")

    def test_sync_monuments_lists_urns(self, adapter):
        adapter.contribute("知识A", tags=["x"])
        urns = adapter.sync_monuments()
        assert urns == ["urn:monument:permanent:0000"]

    def test_query_monument_finds_by_content(self, adapter):
        adapter.contribute("丰碑 架构 设计", tags=["arch"])
        hits = adapter.query_monument("架构")
        assert any("架构" in h for h in hits)

    def test_query_monument_k_out_of_range(self, adapter):
        with pytest.raises(CapacityError):
            adapter.query_monument("t", k=0)
        with pytest.raises(CapacityError):
            adapter.query_monument("t", k=101)

    def test_approve_failure_returns_candidate_urn(self):
        adap = MonumentAdapter(core=_FailingApproveCore())
        res = adap.contribute("知识", tags=["t"])
        assert res.ok is False
        assert res.urn and res.urn.startswith("candidate:")

    def test_degrades_when_no_core(self):
        """无 core 且无法 import 时降级到内存账本，仍可用。"""
        adap = MonumentAdapter(core=None)
        res = adap.contribute("本地降级知识")
        assert isinstance(res.ok, bool)
        assert isinstance(adap.query_monument("知识"), list)
