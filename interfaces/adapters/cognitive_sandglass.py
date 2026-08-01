"""认知层适配器：沙漏系统（显式 / 时间序列记忆）。

沙漏在架构中负责"显式记忆 + 织线知识图谱"。本适配器把沙漏包装成
CognitivePort：store 写入显式记忆，recall 走沙漏检索语义，get_state
返回沙漏的阶段画像/回音折等状态。

惰性加载：通过 workspaces 的 sandglass MCP 工具或 HTTP 调用；无法连接时
退到内部 DummyCognitive（降级）。
"""

from __future__ import annotations

from typing import List, Optional

from interfaces.base import DummyCognitive, CognitivePort
from interfaces.models import CognitiveState, MemoryItem


class SandglassCognitiveAdapter(CognitivePort):
    """沙漏认知适配器。"""

    def __init__(self, client: Optional[object] = None, store_api=None, recall_api=None, state_api=None):
        self._client = client
        # 可注入的底层函数，便于测试与解耦
        self._store_api = store_api
        self._recall_api = recall_api
        self._state_api = state_api
        self._fallback = DummyCognitive()
        self._degraded = not (client or store_api or recall_api)

    # -- 底层访问 ----------------------------------------------------------
    def _real_store(self, memory: MemoryItem) -> bool:
        if self._store_api is not None:
            return bool(self._store_api(memory))
        if self._client is not None:
            return bool(self._client.store(memory))
        raise RuntimeError("no sandglass backend")

    def _real_recall(self, query: str, k: int) -> List[MemoryItem]:
        if self._recall_api is not None:
            items = self._recall_api(query, k)
        elif self._client is not None:
            items = self._client.recall(query, k)
        else:
            raise RuntimeError("no sandglass backend")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return [MemoryItem(**{k: v for k, v in it.items() if k in _MI_FIELDS}) for it in items]
        return list(items)

    def _real_state(self) -> CognitiveState:
        if self._state_api is not None:
            raw = self._state_api()
        elif self._client is not None:
            raw = self._client.get_state()
        else:
            raise RuntimeError("no sandglass backend")
        if isinstance(raw, CognitiveState):
            return raw
        return CognitiveState(
            entropy=raw.get("entropy", 0.0),
            surprise=raw.get("surprise", 0.0),
            purposefulness=raw.get("purposefulness", 0.0),
            stage=raw.get("stage", "") or raw.get("阶段", ""),
        )

    # -- CognitivePort --------------------------------------------------------
    def store(self, memory: MemoryItem) -> bool:
        try:
            return self._real_store(memory)
        except Exception:
            self._degraded = True
            return self._fallback.store(memory)

    def recall(self, query: str, k: int = 5) -> List[MemoryItem]:
        if k < 1 or k > 100:
            from interfaces.exceptions import CapacityError

            raise CapacityError(f"recall: k 越界 {k}")
        try:
            return self._real_recall(query, k)
        except Exception:
            self._degraded = True
            return self._fallback.recall(query, k)

    def get_state(self) -> CognitiveState:
        try:
            return self._real_state()
        except Exception:
            self._degraded = True
            return self._fallback.get_state()

    @property
    def degraded(self) -> bool:
        return self._degraded


_MI_FIELDS = {
    "id",
    "text",
    "content",
    "metadata",
    "created_at",
    "origin",
    "system",
    "entropy",
    "surprise",
    "intensity",
    "vector",
    "triple",
}
