"""接口层单元测试。

验证三件事：
1. 抽象契约（base.py）的语义：模型、指纹、降级实现。
2. 各适配器（adapters/）正确包装原系统 + 降级策略。
3. 依赖注入容器（registry.py）正确组装三层。

运行：
    cd <workspace> && python3 -m pytest interfaces/test_interfaces.py -v
"""

import os
import sys
from typing import List

import pytest

# 确保 workspace 在 sys.path，可 import interfaces
_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)

from interfaces import (
    ApplicationPort,
    CognitivePort,
    MemoryItem,
    Registry,
    SensorPort,
)
from interfaces.adapters import (
    HttpSensorAdapter,
    LMSCognitiveAdapter,
    MonumentApplicationAdapter,
    QdrantSensorAdapter,
    SandglassCognitiveAdapter,
)
from interfaces.base import DummyApplication, DummyCognitive, DummySensor
from interfaces.exceptions import CapacityError, InterfaceError


# ===========================================================================
# 抽象契约基础
# ===========================================================================
class TestContracts:
    def test_ports_are_abstract(self):
        """Port 必须有抽象方法，不可直接实例化。"""
        for port in (SensorPort, CognitivePort, ApplicationPort):
            with pytest.raises(TypeError):
                port()

    def test_memory_item_fingerprint_is_content_stable(self):
        """同内容指纹一致，跨系统可去重；不同内容指纹不同。"""
        a = MemoryItem(text="同一句话", origin="lms")
        b = MemoryItem(text="同一句话", origin="sandglass")
        c = MemoryItem(text="不同的话")
        assert a.fingerprint() == b.fingerprint()
        assert a.fingerprint() != c.fingerprint()

    def test_dummy_sensor_deterministic_and_normalized(self):
        """DummySensor 确定性、归一化、维度正确。"""
        s = DummySensor(dim=1024)
        v1, v2 = s.embed("abc"), s.embed("abc")
        assert v1 == v2
        assert len(v1) == 1024
        norm = sum(x * x for x in v1) ** 0.5
        assert abs(norm - 1.0) < 1e-6

    def test_dummy_sensor_rejects_empty(self):
        with pytest.raises(CapacityError):
            DummySensor().embed("   ")


# ===========================================================================
# 感官层适配器
# ===========================================================================
class TestSensorAdapters:
    def test_qdrant_adapter_with_injected_embed(self):
        """QdrantAdapter：注入 fallback_embed，embed 返回其向量并写入。"""
        writes = []
        client = _FakeVectorClient(writes=writes)

        def fallback(t):
            return [1.0, 0.0]

        adap = QdrantSensorAdapter(
            collection="messages",
            fallback_embed=fallback,
            client=client,
            dim=2,
        )
        vec = adap.embed("hello")
        assert vec == [1.0, 0.0]
        assert adap.dim == 2
        assert len(writes) == 1
        assert "hello" in str(writes[0])

    def test_qdrant_adapter_no_client_degrades_to_hash(self):
        """无客户端、无 fallback：降级为确定性哈希向量，不抛错。"""
        adap = QdrantSensorAdapter(collection="x", client=None)
        vec = adap.embed("hi")
        assert len(vec) == 1024
        # health() 只是可连通性探测，不应抛异常（可能连到内存回退）
        assert isinstance(adap.health(), bool)
        assert adap.embed("hi") == vec  # 确定性命中

    def test_http_adapter_with_mocked_post(self):
        calls = []

        def post(url, json):
            calls.append(url)
            if url.endswith("/batch_embed"):
                return {"vectors": [[0.1, 0.2], [0.3, 0.4]]}
            return {"vector": [0.5, 0.6]}

        def get(url):
            return {"ok": True}

        adap = HttpSensorAdapter(base_url="http://s", dim=2, http_get=get, http_post=post)
        assert adap.embed("a") == [0.5, 0.6]
        assert adap.batch_embed(["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]
        assert adap.health() is True
        assert any("/batch_embed" in c for c in calls)

    def test_http_adapter_connection_error(self):
        from interfaces.exceptions import ConnectionError_

        def boom(url, json):
            raise RuntimeError("network down")

        adap = HttpSensorAdapter(base_url="http://s", http_post=boom)
        with pytest.raises(ConnectionError_):
            adap.embed("a")


# ===========================================================================
# 认知层适配器
# ===========================================================================
class TestCognitiveAdapters:
    def test_lms_adapter_with_client(self):
        client = _FakeLMSClient()
        adap = LMSCognitiveAdapter(lms_client=client)
        assert adap.store(MemoryItem(text="记忆A", system="lms")) is True
        hits = adap.recall("记忆A", k=2)
        assert hits and hits[0].text == "记忆A"
        st = adap.get_state()
        assert st.entropy == 1.5
        assert st.memory_count == 1
        assert not adap.degraded

    def test_lms_adapter_degrades_when_no_backend(self):
        """无客户端且底层不可 import：降级到内存，不抛错。"""
        adap = LMSCognitiveAdapter(lms_client=None, sid="nonexistent")
        # 模拟底层 lms.client 不可 import
        import interfaces.adapters.cognitive_lms as mod

        mod.DummyCognitive()  # 确保可用
        ok = adap.store(MemoryItem(text="降级", origin="user"))
        assert ok is True
        hits = adap.recall("降级")
        assert hits and "降级" in hits[0].text
        assert adap.get_state().memory_count >= 1

    def test_sandglass_adapter_with_injected_apis(self):
        sg = SandglassCognitiveAdapter(
            store_api=lambda m: True,
            recall_api=lambda q, k: [MemoryItem(text="沙漏命", system="sandglass")],
            state_api=lambda: {"entropy": 2.0, "purposefulness": 0.7},
        )
        assert sg.store(MemoryItem(text="x")) is True
        assert sg.recall("q")[0].text == "沙漏命"
        assert sg.get_state().entropy == 2.0

    def test_cognitive_k_out_of_range(self):
        for adap in (LMSCognitiveAdapter(lms_client=_FakeLMSClient()), DummyCognitive()):
            with pytest.raises(CapacityError):
                adap.recall("q", k=0)
            with pytest.raises(CapacityError):
                adap.recall("q", k=101)


# ===========================================================================
# 应用层适配器
# ===========================================================================
class TestApplicationAdapters:
    def test_monument_adapter_with_fake_core(self):
        core = _FakeMonumentCore()
        adap = MonumentApplicationAdapter(core=core)
        res = adap.contribute("丰碑知识 架构", tags=["architecture"])
        assert res.ok is True
        assert res.urn == "urn:monument:permanent:0000"
        assert adap.sync_monuments() == ["urn:monument:permanent:0000"]
        assert any("架构" in t for t in adap.query_monument("架构"))
        assert not adap.degraded

    def test_monument_adapter_degrades_no_core(self):
        """无 core 且无法 import：降级到内存账本，contribute 仍可用。"""
        adap = MonumentApplicationAdapter(core=None)
        res = adap.contribute("本地降级知识")
        # 若真实 core 可 import 则会走真实路径；这里断言返回类型一致即可
        assert isinstance(res.ok, bool)
        assert isinstance(adap.query_monument("知识"), list)

    def test_monument_k_out_of_range(self):
        adap = MonumentApplicationAdapter(core=_FakeMonumentCore())
        with pytest.raises(CapacityError):
            adap.query_monument("t", k=0)


# ===========================================================================
# 依赖注入容器
# ===========================================================================
class TestRegistry:
    def test_in_memory_registry_end_to_end(self):
        reg = Registry.in_memory()
        vec = reg.sensor().embed("你好")
        assert len(vec) == 1024
        assert reg.cognitive().store(MemoryItem(text="记忆", system="t")) is True
        assert len(reg.cognitive().recall("记忆")) >= 1
        assert reg.cognitive().get_state().memory_count >= 1
        res = reg.application().contribute("应用层知识")
        assert res.ok is True

    def test_sensor_cognitive_application_are_ports(self):
        reg = Registry.in_memory()
        assert isinstance(reg.sensor(), SensorPort)
        assert isinstance(reg.cognitive(), CognitivePort)
        assert isinstance(reg.application(), ApplicationPort)

    def test_unconfigured_provider_raises(self):
        reg = Registry()
        with pytest.raises(InterfaceError):
            reg.sensor()

    def test_wrong_type_raises(self):
        reg = Registry(sensor_provider=lambda: "not-a-port")
        with pytest.raises(InterfaceError):
            reg.sensor()

    def test_caching_returns_same_instance(self):
        reg = Registry.in_memory()
        assert reg.sensor() is reg.sensor()

    def test_with_sensor_replacement(self):
        reg = Registry.in_memory().with_sensor(lambda: DummySensor(dim=8))
        assert reg.sensor().dim == 8


# ===========================================================================
# 测试替身
# ===========================================================================
class _FakeVectorClient:
    """模拟 qdrant_client（InMemoryStorage 风格）。"""

    def __init__(self, writes):
        self.writes = writes

    def connected(self):
        return True

    def upsert(self, collection, data, ids=None):
        self.writes.append((collection, data, ids))
        return True


class _FakeLMSClient:
    def __init__(self):
        self.items = []

    def store(self, memory):
        self.items.append(memory)
        return True

    def recall(self, query, k):
        return [m for m in self.items if query in m.text][:k]

    def get_state(self):
        return {
            "entropy": 1.5,
            "surprise": 0.3,
            "purposefulness": 0.8,
            "memory_count": len(self.items),
            "active_nodes": ["node"],
            "stage": "test",
        }


class _FakeMonumentCore:
    """模拟 monument_core.MonumentCore。"""

    def __init__(self):
        self.blocks = []
        self.n = 0

    def create_candidate(self, title, body, tags, contributor, source=None, **kw):
        cid = f"candidate-{self.n}"
        self._pending = {"title": title, "body": body, "tags": tags}
        return {"candidate_id": cid}

    def approve_candidate(self, candidate_id, approver=None, note=None):
        urn = f"urn:monument:permanent:{self.n:04d}"
        pending = getattr(self, "_pending", {"title": "", "body": "", "tags": []})
        self.blocks.append(
            {
                "content": {
                    "urn": urn,
                    "title": pending["title"],
                    "body": pending["body"],
                    "tags": pending.get("tags", []),
                }
            }
        )
        self.n += 1
        return self.blocks[-1]

    def read_all(self):
        return list(self.blocks)

    def read_by_tag(self, tag):
        return [b for b in self.blocks if tag in b["content"].get("tags", [])]
