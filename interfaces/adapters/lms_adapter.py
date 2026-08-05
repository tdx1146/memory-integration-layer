"""认知层适配器：LMS 真实后端（HTTP 服务）。

LMS（活体记忆系统）在架构中负责“隐式记忆 + 意识状态（熵/惊讶度）”。
本适配器把 ``real_backends`` 里基于 HTTP 的 LMS 调用翻译成
``CognitivePort``，但与旧实现不同处在于：
  - 构造不发起任何网络请求（懒连接），不在 `__init__` 抛异常；
  - 依赖注入 ``http_post`` / ``http_get`` 便于离线单测；
  - store 后可回填 LMS 返回的熵/惊讶度到 memory，供上层观察；
  - 失败统一降级为内存实现（fail-open），不打挂调用方。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from interfaces.base import DummyCognitive, CognitivePort
from interfaces.exceptions import CapacityError
from interfaces.models import CognitiveState, MemoryItem

logger = __import__("logging").getLogger(__name__)


class LMSAdapter(CognitivePort):
    """基于 HTTP 的 LMS 真实后端适配器。

    Args:
        api_url: LMS 服务根地址（如 http://localhost:8190）。
        session_id: 默认会话 ID，调用 /chat 时携带。
        timeout: 请求超时（秒）。
        http_post: 依赖注入用 POST 函数 ``(url, json) -> dict``，便于 mock。
        http_get:  依赖注入用 GET 函数 ``(url) -> dict``，便于 mock。
        fallback:  降级用的 CognitivePort（缺省为 DummyCognitive）。
    """

    def __init__(
        self,
        api_url: str = "http://localhost:8190",
        session_id: str = "main",
        timeout: float = 30.0,
        http_post: Optional[Any] = None,
        http_get: Optional[Any] = None,
        fallback: Optional[CognitivePort] = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.session_id = session_id
        self.timeout = timeout
        self._post = http_post or self._default_post
        self._get = http_get or self._default_get
        self._fallback = fallback or DummyCognitive()
        self._degraded = False

    # -- HTTP 底层 ---------------------------------------------------------
    def _session(self):
        try:
            import requests  # type: ignore

            return requests.Session()
        except Exception as e:  # pragma: no cover - 依赖缺失
            raise RuntimeError(f"requests 依赖缺失: {e}")

    def _default_post(self, url: str, json: Dict[str, Any]) -> Dict[str, Any]:
        return self._session().post(url, json=json, timeout=self.timeout).json()

    def _default_get(self, url: str) -> Dict[str, Any]:
        return self._session().get(url, timeout=self.timeout).json()

    # -- 私有工具 -----------------------------------------------------------
    def _submit(self, user_input: str) -> Dict[str, Any]:
        """调用 LMS /chat，返回解析后的 JSON（可能含 memory_state / memory_context）。"""
        return self._post(
            f"{self.api_url}/chat",
            {"user_input": user_input, "session_id": self.session_id},
        )

    # -- CognitivePort ------------------------------------------------------
    def store(self, memory: MemoryItem) -> bool:
        try:
            data = self._submit(memory.text)
            state = data.get("memory_state", {}) if isinstance(data, dict) else {}
            # 回填 LMS 意识状态，供上层观察
            if state.get("last_entropy") is not None:
                memory.entropy = state.get("last_entropy")
            if state.get("last_surprise") is not None:
                memory.surprise = state.get("last_surprise")
            if state.get("purpose_coherence") is not None:
                memory.metadata["purposefulness"] = state.get("purpose_coherence")
            return True
        except Exception as e:  # pragma: no cover - 网络/依赖
            logger.warning("LMS 存储失败: %s", e)
            self._degraded = True
            return False

    def fetch_context(self, query: str) -> Dict[str, Any]:
        """获取 LMS /chat 完整响应（含 memory_context 激活信息）。失败返回 {}。"""
        try:
            return self._submit(query) or {}
        except Exception as e:  # pragma: no cover - 网络
            logger.warning("LMS fetch_context 失败: %s", e)
            return {}

    def fetch_self_ref(self, limit: int = 5) -> List[str]:
        """读取 LMS 自指回路最近自述（反思产物，反思回流）。失败返回 []（fail-open）。"""
        try:
            data = self._get(
                f"{self.api_url}/self-ref/voice?session_id={self.session_id}&limit={limit}")
            voices = data.get("voices", []) if isinstance(data, dict) else []
            return [v for v in voices if isinstance(v, str) and v.strip()]
        except Exception as e:  # pragma: no cover - 网络/依赖
            logger.warning("LMS self_ref 读取失败: %s", e)
            return []

    def recall(self, query: str, k: int = 5) -> List[MemoryItem]:
        if k < 1 or k > 100:
            raise CapacityError(f"recall: k 越界 {k}")
        # LMS 的显式检索经 /chat 的 memory_context 返回；此处提供降级实现。
        # 深层的语义召回由服务层（integration_service）基于 fetch_context 融合。
        try:
            data = self._submit(query)
            ctx = data.get("memory_context", "") if isinstance(data, dict) else ""
            items = _parse_context(ctx)
            return items[:k]
        except Exception as e:  # pragma: no cover - 网络
            logger.warning("LMS 检索失败: %s", e)
            self._degraded = True
            return self._fallback.recall(query, k)

    def get_state(self) -> CognitiveState:
        try:
            data = self._get(f"{self.api_url}/health")
            return CognitiveState(
                stage="lms",
                memory_count=int(data.get("active_sessions", 0) or 0),
                extra={"status": data.get("status", "unknown")},
            )
        except Exception:  # pragma: no cover - 网络
            self._degraded = True
            return self._fallback.get_state()

    @property
    def degraded(self) -> bool:
        return self._degraded


# ---------------------------------------------------------------------------
# LMS memory_context 解析（从旧的 IntegratedMemoryService._parse_lms_context 迁移，
# 保留算法，纯函数化便于单测）
# ---------------------------------------------------------------------------

import re  # noqa: E402

_NODE_RE = re.compile(r"节点(\d+)\((强|中|弱)?:?([0-9.]*)\)")
_ENTROPY_RE = re.compile(r"熵:([0-9.]+)")
_SURPRISE_RE = re.compile(r"惊讶度:([0-9.]+)")
_RECALL_RE = re.compile(r"^\s*\d+\.\s*[\"\u201c](.+?)[\"\u201d]\s*$", re.MULTILINE)
_STRENGTH = {"强": 1.0, "中": 0.6, "弱": 0.3}


def parse_lms_context(memory_context: str) -> Dict[str, Any]:
    """解析 LMS ``memory_context`` 字符串，提取激活信息。纯函数。

    Returns:
        ``{"active_nodes": [{id, strength}], "surprise": float,
          "entropy": float, "recalled": [str]}``
    """
    result: Dict[str, Any] = {"active_nodes": [], "surprise": 0.0, "entropy": 0.0, "recalled": []}
    if not memory_context:
        return result

    for node_id, grade, score in _NODE_RE.findall(memory_context):
        try:
            s = float(score)
        except ValueError:
            s = _STRENGTH.get(grade, 0.5)
        result["active_nodes"].append({"id": int(node_id), "strength": s})

    m_ent = _ENTROPY_RE.search(memory_context)
    m_sur = _SURPRISE_RE.search(memory_context)
    if m_ent:
        result["entropy"] = float(m_ent.group(1))
    if m_sur:
        result["surprise"] = float(m_sur.group(1))

    result["recalled"] = [r.strip() for r in _RECALL_RE.findall(memory_context)]
    return result


def _parse_context(memory_context: str) -> List[MemoryItem]:
    """把解析出的 recalled 文本转成 MemoryItem 列表。"""
    return [
        MemoryItem(text=r, origin="lms", system="lms")
        for r in parse_lms_context(memory_context)["recalled"]
    ]
