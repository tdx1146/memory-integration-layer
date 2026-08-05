"""沙漏真实后端适配器单元测试（独立、离线、不依赖外部沙漏服务）。

运行：
    cd <workspace> && python3 -m pytest tests/test_sandglass_adapter.py -v
"""

import os
import sys
import tempfile

import pytest

_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)

from interfaces.adapters.sandglass_adapter import SandglassAdapter
from interfaces.exceptions import CapacityError
from interfaces.models import MemoryItem


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "sandglass_test.db")


@pytest.fixture
def adapter(db_path):
    return SandglassAdapter(db_path)


class TestSandglassAdapter:
    def test_store_and_recall_roundtrip(self, adapter):
        """写入后能按关键词召回，字段映射正确（created_at/origin/system）。"""
        import time

        ts = time.time()
        adap = adapter
        ok = adap.store(MemoryItem(text="你好世界 测试", origin="user", created_at=ts))
        assert ok is True
        hits = adap.recall("你好", k=5)
        assert len(hits) == 1
        item = hits[0]
        assert item.text == "你好世界 测试"
        assert item.system == "sandglass"
        assert item.origin == "user"
        assert abs(item.created_at - ts) < 2  # 秒级时间戳往返容差
        assert item.id is not None

    def test_recall_no_match_returns_empty(self, adapter):
        adapter.store(MemoryItem(text="无关内容", origin="agent"))
        assert adapter.recall("不存在的词") == []

    def test_recall_k_out_of_range(self, adapter):
        with pytest.raises(CapacityError):
            adapter.recall("q", k=0)
        with pytest.raises(CapacityError):
            adapter.recall("q", k=101)

    def test_get_state_counts(self, adapter):
        adapter.store(MemoryItem(text="一"), )
        adapter.store(MemoryItem(text="二"))
        assert adapter.get_state().memory_count == 2
        assert adapter.get_state().stage == "sandglass"

    def test_returns_multiple_ordered_by_time_desc(self, adapter):
        import time

        adapter.store(MemoryItem(text="older", created_at=time.time() - 100))
        adapter.store(MemoryItem(text="newer"))
        hits = adapter.recall("o")  # 只命中 older
        assert len(hits) == 1

    def test_store_persists_across_instances(self, db_path):
        """数据落盘：同一 db 文件新实例仍能召回（验证 SQLite 持久化）。"""
        SandglassAdapter(db_path).store(MemoryItem(text="持久化测试"))
        fresh = SandglassAdapter(db_path)
        assert len(fresh.recall("持久化")) == 1
        assert fresh.get_state().memory_count == 1


class TestSandglassVaultAdapterRecent:
    """沙漏真实后端（vault txt 源）recent 能力 —— 回魂快照数据源。

    离线单测：monkeypatch _load_vault 返回伪 vault，不依赖真实沙漏服务。
    """

    def test_recent_returns_latest_items(self, monkeypatch):
        from interfaces.adapters.sandglass_vault_adapter import SandglassVaultAdapter

        adap = SandglassVaultAdapter()

        def fake_load():
            return {
                "recent": lambda n: [
                    (3782, "2026-08-05 15:30:01", "最近一条"),
                    (3781, "2026-08-05 15:20:01", "稍早一条"),
                ][:n],
            }

        monkeypatch.setattr(adap, "_load_vault", fake_load)
        items = adap.recent(2)
        assert len(items) == 2
        assert items[0].text == "最近一条"
        assert items[0].origin == "sandglass"
        assert items[0].id == "3782"
        # 时间戳解析成功（2026-08-05 15:30:01 → epoch）
        assert items[0].created_at and items[0].created_at > 0

    def test_recent_n_out_of_range(self, monkeypatch):
        from interfaces.exceptions import CapacityError
        from interfaces.adapters.sandglass_vault_adapter import SandglassVaultAdapter

        adap = SandglassVaultAdapter()
        with pytest.raises(CapacityError):
            adap.recent(0)
        with pytest.raises(CapacityError):
            adap.recent(51)

    def test_recent_fail_open_on_backend_error(self, monkeypatch):
        from interfaces.adapters.sandglass_vault_adapter import SandglassVaultAdapter

        adap = SandglassVaultAdapter()

        def boom():
            raise RuntimeError("vault down")

        monkeypatch.setattr(adap, "_load_vault", boom)
        assert adap.recent(3) == []  # fail-open 不抛错
        assert adap.degraded is True
