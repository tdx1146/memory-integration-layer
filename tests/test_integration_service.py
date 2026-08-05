"""整合记忆服务（services 层前端）单元测试。

用假的 Port 替身验证业务编排逻辑：聚合写入、协同检索融合、知识贡献。
"""

import os
import sys

import pytest

_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)

from interfaces.adapters.monument_adapter import MonumentAdapter
from interfaces.adapters.sandglass_adapter import SandglassAdapter
from interfaces.base import CognitivePort, DummyCognitive
from interfaces.exceptions import AdapterError
from interfaces.models import CognitiveState, ContributionResult, MemoryItem
from interfaces.services.integration_service import IntegratedMemoryService


class _FakeVector:
    def __init__(self, dim=4):
        self._dim = dim
        self._v = 0

    def embed(self, text):
        self._v += 1
        # 确定性向量：内容 hash 归一化，便于余弦可比
        import hashlib

        h = hashlib.sha256(text.encode()).digest()
        vec = [(b / 255.0) - 0.5 for b in h[: self._dim]]
        norm = (sum(x * x for x in vec) ** 0.5) or 1.0
        return [x / norm for x in vec]

    def batch_embed(self, texts):
        return [self.embed(t) for t in texts]

    def health(self):
        return True

    @property
    def dim(self):
        return self._dim


class _FakeLMS:
    def __init__(self, context=""):
        self._ctx = context

    def store(self, memory):
        memory.entropy = 4.5
        return True

    def fetch_context(self, query):
        return {"memory_context": self._ctx}

    def get_state(self):
        return CognitiveState(stage="lms", memory_count=1)


class _FakeMonument:
    def __init__(self):
        self.contributed = []

    def contribute(self, knowledge, **kwargs):
        self.contributed.append(knowledge)
        return ContributionResult(ok=True, urn=f"urn:{len(self.contributed)}")

    def sync_monuments(self):
        return []

    def query_monument(self, topic, k=5):
        return [c for c in self.contributed if topic in c][:k]


@pytest.fixture
def svc(tmp_path):
    sg = SandglassAdapter(str(tmp_path / "svc.db"))
    return IntegratedMemoryService(
        sandglass=sg,
        lms=_FakeLMS(),
        vector=_FakeVector(),
        monument=_FakeMonument(),
    )


class TestIntegratedMemoryService:
    def test_store_aggregates_and_returns_item(self, svc):
        item = svc.store("用户确认修复完成", source="chat")
        assert item.text == "用户确认修复完成"
        assert item.origin == "chat"
        assert item.vector is not None and len(item.vector) == 4
        # 已写入沙漏：可由 recall 召回
        assert len(svc.recall("确认修复")) >= 1

    def test_store_self_ref_source_skips_lms(self, svc):
        """自述回写防御：source=self_ref 的条目不得回注 LMS。

        否则每次入库都会经 LMS /chat 触发新一轮反思 → 再蒸馏 →
        再入库 → 死循环。自述只落沙漏/向量，绝不喂回 LMS。
        """
        item = svc.store("我同时唤醒多条记忆，但关注方向稳定。",
                         source="self_ref")
        assert item.origin == "self_ref"
        # 仍写入沙漏（持久可检索）
        assert len(svc.recall("关注方向稳定")) >= 1
        # 未回注 LMS：_FakeLMS.store 会回填 entropy，未调用则保持 None
        assert item.entropy is None
        # 对照：普通 chat 来源正常回注 LMS（entropy 被回填）
        item2 = svc.store("普通对话内容", source="chat")
        assert item2.entropy == 4.5

    def test_recall_weighted_merge(self, svc):
        svc.store("架构 设计 高内聚", source="agent")
        hits = svc.recall("架构", k=5)
        assert len(hits) == 1
        # 附加了融合得分
        assert hits[0].metadata.get("scores", {}).get("total", 0) > 0

    def test_recall_empty_when_no_sandglass_hit(self, svc):
        assert svc.recall("完全不存在的关键词xyz") == []

    def test_recall_lms_activation_boosts(self, svc):
        """命中 LMS 回忆文本的条目获得满分激活。"""
        sandglass = SandglassAdapter(str(svc.sandglass.db_path))
        sandglass.store(MemoryItem(text="回忆一", origin="user"))
        # 重建服务：LMS 上下文包含该回忆
        svc2 = IntegratedMemoryService(
            sandglass=sandglass,
            lms=_FakeLMS(context='\n1. "回忆一"\n'),
            vector=_FakeVector(),
            monument=_FakeMonument(),
        )
        hits = svc2.recall("回忆", k=5)
        assert hits
        assert hits[0].metadata["scores"]["lms_activation"] == 1.0

    def test_contribute_refines_from_sandglass(self, svc):
        svc.store("架构 设计 原则", source="agent")
        svc.store("高内聚 低耦合", source="agent")
        res = svc.contribute("架构")
        assert res.ok is True
        assert isinstance(svc.monument.contributed[0], str)  # type: ignore[attr-defined]
        assert "架构" in svc.monument.contributed[0]  # type: ignore[attr-defined]

    def test_contribute_uses_explicit_knowledge(self, svc):
        res = svc.contribute("主题", knowledge="显式知识正文", tags=["manual"])
        assert res.ok is True
        assert svc.monument.contributed[0] == "显式知识正文"  # type: ignore[attr-defined]

    def test_get_status_reports_backends(self, svc):
        st = svc.get_status()
        assert st["vector"]["healthy"] is True
        assert st["sandglass"]["healthy"] is True
        assert st["lms"]["memory_count"] == 1
        assert "monument" in st

    def test_requires_at_least_one_backend(self):
        with pytest.raises(AdapterError):
            IntegratedMemoryService()

    def test_get_soul_snapshot_composes_all_sources(self, svc):
        """回魂快照：自述 + 状态 + 最近记忆三源聚合。"""
        # 给假 LMS 补 fetch_status / fetch_self_ref（回魂专用只读方法）
        svc.lms.fetch_status = lambda: {  # type: ignore[attr-defined]
            "entropy_ratio": 0.947,
            "last_surprise": 0.113,
            "purpose_coherence": 0.92,
        }
        svc.lms.fetch_self_ref = lambda limit=5: ["我正同时唤起多个记忆，方向稳定。"]  # type: ignore[attr-defined]
        # 给假沙漏补 recent
        svc.sandglass.recent = lambda n: [  # type: ignore[attr-defined]
            MemoryItem(text=f"最近记忆{i}", origin="sandglass", created_at=1700000000 + i)
            for i in range(2)
        ]

        snap = svc.get_soul_snapshot(limit=3, recent_n=2)
        assert snap["ok"] is True
        assert snap["session_id"] == "main"
        assert snap["lms_voice"] == ["我正同时唤起多个记忆，方向稳定。"]
        assert snap["lms_state"]["entropy_ratio"] == 0.947
        assert snap["lms_state"]["purpose_coherence"] == 0.92
        assert len(snap["recent"]) == 2
        assert snap["recent"][0]["text"] == "最近记忆0"
        assert snap["recent"][0]["origin"] == "sandglass"
        assert snap["recent"][0]["ts"] == 1700000000

    def test_get_soul_snapshot_fail_open(self, svc):
        """fail-open：后端缺方法/抛异常 → 对应字段为空，不抛错。"""
        # 假 LMS 无 fetch_status / fetch_self_ref，假沙漏无 recent
        snap = svc.get_soul_snapshot()
        assert snap["ok"] is True
        assert snap["lms_voice"] == []
        assert snap["lms_state"] == {}
        assert snap["recent"] == []

        # 后端方法抛异常同样 fail-open
        def boom(*a, **k):
            raise RuntimeError("boom")

        svc.lms.fetch_status = boom  # type: ignore[attr-defined]
        svc.lms.fetch_self_ref = boom  # type: ignore[attr-defined]
        svc.sandglass.recent = boom  # type: ignore[attr-defined]
        snap2 = svc.get_soul_snapshot()
        assert snap2["ok"] is True
        assert snap2["lms_voice"] == []
        assert snap2["lms_state"] == {}
        assert snap2["recent"] == []

    def test_cosine_pure_function(self):
        assert IntegratedMemoryService._cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
        assert IntegratedMemoryService._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
        assert IntegratedMemoryService._cosine([], [1.0]) == 0.0

    def test_text_score_pure_function(self):
        assert IntegratedMemoryService._text_score("架构", "架构设计") == 1.0
        assert IntegratedMemoryService._text_score("架构", "无关内容") == 0.0
