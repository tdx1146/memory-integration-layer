"""认知层适配器：LMS（活体记忆系统）。

LMS 的职责在架构中是"隐式记忆 + 意识状态（熵/惊讶度）"，因此本适配器
将 LMS 包装成 CognitivePort 的 store/recall/get_state。

惰性加载：仅在首次调用时 `import lms_*`；若缺失则回退到 DummyCognitive，
抛 DegradedError 提示降级。构造时可注入真实客户端便于测试。
"""

from __future__ import annotations

from typing import List, Optional

from interfaces.base import DummyCognitive, CognitivePort
from interfaces.exceptions import DegradedError
from interfaces.models import CognitiveState, MemoryItem


class LMSCognitiveAdapter(CognitivePort):
    """LMS 认知适配器。state 语义来自 LMS 的熵/惊讶度动力学。"""

    def __init__(self, lms_client: Optional[object] = None, sid: str = "main"):
        self._client = lms_client
        self.sid = sid
        self._fallback = DummyCognitive()
        self._degraded = lms_client is None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        # 惰性连接真实 LMS（MCP/HTTP）；不可用则降级
        try:
            from lms.client import get_client  # type: ignore

            self._client = get_client(self.sid)
            self._degraded = False
        except Exception:
            self._degraded = True
        return self._client

    # -- CognitivePort -------------------------------------------------------
    def store(self, memory: MemoryItem) -> bool:
        c = self._ensure_client()
        if c is None:
            return self._fallback.store(memory)
        try:
            return c.store(memory)
        except Exception as e:
            self._degraded = True
            return self._fallback.store(memory)

    def recall(self, query: str, k: int = 5) -> List[MemoryItem]:
        if k < 1 or k > 100:
            from interfaces.exceptions import CapacityError

            raise CapacityError(f"recall: k 越界 {k}")
        c = self._ensure_client()
        if c is None:
            return self._fallback.recall(query, k)
        try:
            return c.recall(query, k)
        except Exception as e:
            self._degraded = True
            return self._fallback.recall(query, k)

    def get_state(self) -> CognitiveState:
        c = self._ensure_client()
        if c is None:
            return self._fallback.get_state()
        try:
            raw = c.get_state()
            return CognitiveState(
                entropy=raw.get("entropy", 0.0),
                surprise=raw.get("surprise", 0.0),
                purposefulness=raw.get("purposefulness", 0.0),
                memory_count=raw.get("memory_count", 0),
                active_nodes=list(raw.get("active_nodes", []) or []),
                stage=raw.get("stage", ""),
            )
        except Exception as e:
            self._degraded = True
            return self._fallback.get_state()

    @property
    def degraded(self) -> bool:
        return self._degraded
