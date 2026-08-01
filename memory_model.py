#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一记忆数据模型 - memory_model.py

面向多个记忆系统的统一数据抽象层。当前生态包含三类记忆系统：

1. **沙漏** (sandglass, 显式记忆) —— 用户可感知、可编辑的叙事记忆
2. **LMS** (Living Memory System, 隐式记忆) —— 自动整合、带熵/惊讶度/目的性的状态记忆
3. **向量服务** (Qdrant / bge-m3 embed) —— 语义向量，用于相似度检索
4. **丰碑** (monument) —— 用户批准后的永久知识洞见

本模块不直接替换任何一个系统，而是提供一个统一的 :class:`MemoryItem`
数据契约 + 三个正交能力接口（store / recall / contribute），让各系统
可以被"插拔式"地接入，同时保证向后兼容——每个后端都可用独立调用。

用法示例::

    from memory_model import MemoryItem, MemoryService

    service = MemoryService()  # 会自动探测可用后端

    # 写入：同时进沙漏和LMS
    item = MemoryItem(
        text="用户确认了编辑页面渲染问题已修复",
        source="chat",
        tags=["edit-web", "fix"],
    )
    result = service.store(item)

    # 检索：协同召回（向量 + LMS 语义 + 关键词）
    hits = service.recall("编辑页面渲染")

    # 贡献：写入丰碑（需先 approved）
    service.contribute({"title": "...", "body": "...", "tags": [...]})

设计目标（钱学森系统论视角）：
    - 统一数据契约，消灭重复定义
    - 正交能力接口，各后端解耦
    - 任一后端故障不影响其它后端（fail-open）
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

# 尝试使用 pydantic（更严格的运行时校验），否则退化为 dataclass
try:  # pragma: no cover - 值判断
    from pydantic import BaseModel, Field, ConfigDict
    _Pydantic = BaseModel  # type: ignore

    class MemoryItemPure(BaseModel):  # type: ignore[misc]
        """pydantic 版 MemoryItem（用于校验与序列化）"""

        model_config = ConfigDict(
            extra="forbid",              # 拒绝未知字段，避免数据漂移
            populate_by_name=True,       # 允许别名反填
        )

        id: str = Field(default_factory=lambda: uuid.uuid4().hex)
        text: str = Field(..., min_length=1, description="记忆正文")
        timestamp: float = Field(
            default_factory=lambda: time.time(), description="Unix 时间戳（秒）"
        )
        source: str = Field("unknown", description="来源标记: chat/memory/pulse/digest...")
        vector: Optional[List[float]] = Field(
            None, description="语义向量（由向量服务生成）"
        )
        entropy: Optional[float] = Field(None, ge=0, description="LMS 熵（高=新颖/混乱）")
        surprise: Optional[float] = Field(None, ge=0, description="LMS 惊讶度")
        purposefulness: Optional[float] = Field(
            None, description="LMS 目的性（0-1）"
        )
        tags: List[str] = Field(default_factory=list, description="标签")
        meta: Dict[str, Any] = Field(default_factory=dict, description="扩展元数据")

    USES_PYDANTIC = True
except Exception:  # pylint: disable=broad-except
    _Pydantic = object  # type: ignore
    USES_PYDANTIC = False

    @dataclass
    class MemoryItemPure:
        """dataclass 版 MemoryItem（pydantic 不可用时的降级实现）"""

        id: str = field(default_factory=lambda: uuid.uuid4().hex)
        text: str = ""
        timestamp: float = field(default_factory=lambda: time.time())
        source: str = "unknown"
        vector: Optional[List[float]] = None
        entropy: Optional[float] = None
        surprise: Optional[float] = None
        purposefulness: Optional[float] = None
        tags: List[str] = field(default_factory=list)
        meta: Dict[str, Any] = field(default_factory=dict)

logger = logging.getLogger(__name__)


# ============================================================
# 1. MemoryItem —— 统一数据契约
# ============================================================

if USES_PYDANTIC:

    class MemoryItem(MemoryItemPure):
        """统一记忆条目。

        同时携带：文本、向量、时间戳、来源、LMS 状态三要素。
        全部字段有默认值，天然向后兼容——旧的裸文本也能构造。
        """

        def dump(self) -> Dict[str, Any]:
            """导出为可序列化字典（供 JSON / 各后端写入）。"""
            return self.model_dump()

        @classmethod
        def load(cls, data: Union[Dict[str, Any], "MemoryItem"]) -> "MemoryItem":
            """从字典或已有实例恢复 MemoryItem。"""
            if isinstance(data, MemoryItem):
                return data
            return cls(**data)

else:

    class MemoryItem(MemoryItemPure):
        """统一记忆条目（纯 dataclass 降级实现）。

        字段契约与 pydantic 版完全一致。
        """

        def dump(self) -> Dict[str, Any]:
            return asdict(self)

        @classmethod
        def load(cls, data: Union[Dict[str, Any], "MemoryItem"]) -> "MemoryItem":
            if isinstance(data, MemoryItem):
                return data
            return cls(**data)


# ============================================================
# 2. 后端适配器抽象（正交能力）
# ============================================================

class MemoryBackendError(Exception):
    """记忆后端操作失败（存储/检索/贡献阶段）。"""


class _Backend:
    """后端能力抽象基类。子类可按需实现开放接口。"""

    def check(self) -> bool:
        """健康检查：返回该后端当前是否可用。"""
        return False

    def store(self, item: MemoryItem) -> Optional[str]:
        """写入一条记忆。返回后端生成的 ID 或 None。"""
        raise NotImplementedError

    def close(self) -> None:
        """释放后端资源（连接、句柄）。"""


class _RecallBackend(_Backend):
    """可检索后端（向量 / LMS / 关键词）。"""

    def recall(
        self,
        query: str,
        limit: int = 5,
        **kwargs: Any,
    ) -> List[MemoryItem]:
        """按 query 检索，返回相似记忆列表（按相关度降序）。"""
        raise NotImplementedError


class _ContributeBackend(_Backend):
    """可贡献后端（丰碑）。"""

    def contribute(self, knowledge: Dict[str, Any]) -> Dict[str, Any]:
        """把一个知识纲要写入永久层。返回后端结果字典。"""
        raise NotImplementedError


# ── 沙漏后端 ────────────────────────────────────────────
class SandglassBackend(_Backend):
    """沙漏（显式叙事记忆）后端适配。

    真实场景中沙漏由 OpenClaw 插件 (sandglass_*) 承载，其写入通道
    在进程内表现为一个可调用对象 ``writer``。这里预留了扩展点：
    ``writer(text=..., source=...) -> dict``。
    """

    def __init__(self, writer: Optional[Any] = None) -> None:
        self._writer = writer

    def check(self) -> bool:
        return self._writer is not None and callable(self._writer)

    def store(self, item: MemoryItem) -> Optional[str]:
        if not self.check():
            logger.warning("沙漏后端不可用，跳过写入")
            return None
        try:
            result = self._writer(
                text=item.text,
                source=item.source or "memory_model",
            )
            rid = None
            if isinstance(result, dict):
                rid = result.get("id") or result.get("memory_id")
            return rid or item.id
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("沙漏写入失败")
            raise MemoryBackendError(f"沙漏写入失败: {exc}") from exc


# ── 向量后端 ────────────────────────────────────────────
class VectorBackend(_RecallBackend, _Backend):
    """向量服务（Qdrant + bge-m3）后端适配。

    ``embed_fn``: ``text -> List[float]``
    ``search_fn``: ``(vector, top_k, payload_filter) -> List[Dict]``
    """

    def __init__(
        self,
        embed_fn: Optional[Any] = None,
        search_fn: Optional[Any] = None,
        store_fn: Optional[Any] = None,
        collection: str = "memory_items",
    ) -> None:
        self._embed = embed_fn
        self._search = search_fn
        self._store = store_fn
        self.collection = collection

    def check(self) -> bool:
        return self._embed is not None and self._search is not None

    def embed(self, text: str) -> Optional[List[float]]:
        """调用向量服务生成 text 的语义向量。失败返回 None。"""
        if self._embed is None:
            return None
        try:
            vec = self._embed(text)
            if vec and isinstance(vec, (list, tuple)):
                return [float(v) for v in vec]
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("向量化失败: %s", exc)
        return None

    def store(self, item: MemoryItem) -> Optional[str]:
        if not item.vector:
            item.vector = self.embed(item.text)
        if self._store is None or not item.vector:
            logger.warning("向量后端未配置 store_fn 或向量为空，跳过")
            return None
        try:
            self._store(
                collection=self.collection,
                point_id=item.id,
                vector=item.vector,
                payload={"text": item.text, "timestamp": item.timestamp,
                         "source": item.source, "tags": item.tags},
            )
            return item.id
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("向量写入失败")
            raise MemoryBackendError(f"向量写入失败: {exc}") from exc

    def recall(self, query: str, limit: int = 5, **kwargs: Any) -> List[MemoryItem]:
        if not self.check():
            return []
        vec = self.embed(query)
        if vec is None:
            return []
        try:
            raw = self._search(vec, top_k=limit, **kwargs) or []
            items: List[MemoryItem] = []
            for row in raw:
                payload = row.get("payload", {}) if isinstance(row, dict) else getattr(row, "payload", {})
                items.append(
                    MemoryItem(
                        id=row.get("id") if isinstance(row, dict) else str(getattr(row, "id", "")),
                        text=payload.get("text", ""),
                        timestamp=payload.get("timestamp") or time.time(),
                        source=payload.get("source", "vector"),
                        vector=vec,
                        tags=payload.get("tags", []),
                    )
                )
            return items
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("向量检索失败: %s", exc)
            return []


# ── LMS 后端 ────────────────────────────────────────────
class LMSBackend(_RecallBackend, _Backend):
    """LMS 活体记忆后端适配。

    ``recall_fn``: ``(user_input, sid) -> dict``（含 RetrievedMemories 等）
    ``state_fn``:  可选，读取状态（熵/惊讶度）。
    """

    def __init__(
        self,
        recall_fn: Optional[Any] = None,
        state_fn: Optional[Any] = None,
        store_fn: Optional[Any] = None,
        sid: str = "main",
    ) -> None:
        self._recall = recall_fn
        self._state = state_fn
        self._store = store_fn
        self.sid = sid

    def check(self) -> bool:
        return self._recall is not None

    def store(self, item: MemoryItem) -> Optional[str]:
        """写入 LMS：喂一段对话（user=外部，assistant=文本）。"""
        if self._store is None:
            logger.warning("LMS 未配置 store_fn，跳过副作用写入")
            return None
        try:
            self._store(user_input=item.text, llm_output=item.meta.get("llm_output", ""), sid=self.sid)
            return item.id
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("LMS 写入失败")
            raise MemoryBackendError(f"LMS 写入失败: {exc}") from exc

    def recall(self, query: str, limit: int = 5, **kwargs: Any) -> List[MemoryItem]:
        if not self.check():
            return []
        try:
            ctx = self._recall(user_input=query, sid=kwargs.get("sid", self.sid)) or {}
            raw = ctx.get("retrieved") or ctx.get("RetrievedMemories") or ctx.get("context") or []
            raw = raw if isinstance(raw, list) else [raw]
            items: List[MemoryItem] = []
            for row in raw[:limit]:
                if isinstance(row, str):
                    items.append(MemoryItem(text=row, source="lms"))
                    continue
                if not isinstance(row, dict):
                    continue
                _meta = row.get("metadata", row.get("meta", {})) or {}
                items.append(
                    MemoryItem(
                        id=row.get("id", uuid.uuid4().hex),
                        text=row.get("text", row.get("content", "")),
                        source=row.get("source", "lms"),
                        entropy=row.get("entropy", _meta.get("entropy")),
                        surprise=row.get("surprise", _meta.get("surprise")),
                        purposefulness=row.get(
                            "purposefulness", _meta.get("purposefulness")
                        ),
                        meta=_meta,
                    )
                )
            return items
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("LMS 检索失败: %s", exc)
            return []


# ── 丰碑后端 ────────────────────────────────────────────
class MonumentBackend(_ContributeBackend):
    """丰碑（永久知识）后端适配。

    ``contribute_fn``: 接收 knowledge dict，调用 MonumentCore.create_candidate 等。
    """

    def __init__(
        self,
        contribute_fn: Optional[Any] = None,
        core: Optional[Any] = None,
    ) -> None:
        self._fn = contribute_fn
        self._core = core

    def check(self) -> bool:
        return self._fn is not None or self._core is not None

    def contribute(self, knowledge: Dict[str, Any]) -> Dict[str, Any]:
        if self._fn:
            return self._fn(knowledge)
        if self._core:
            return self._core.create_candidate(
                title=knowledge.get("title", "未命名洞见"),
                body=knowledge.get("body", knowledge.get("text", "")),
                tags=knowledge.get("tags", []),
                contributor=knowledge.get("contributor", "memory_model"),
                source=knowledge.get("source", {}),
            )
        raise MemoryBackendError("丰碑后端不可用")


# ============================================================
# 3. MemoryService —— 统一门面 / 编排
# ============================================================

class MemoryService:
    """统一记忆服务：对外暴露三个正交操作。

    - :meth:`store`       —— 并行写入沙漏（+可选向量、LMS）
    - :meth:`recall`      —— 协同检索（向量 + LMS + 关键词，加权融合）
    - :meth:`contribute`  —— 贡献永久知识到丰碑

    关键设计：**fail-open**。任一后端故障只记 warning，不中断主流程；
    后端未启用时静默跳过，保证向后兼容（不配置任何后端也能跑）。
    """

    def __init__(
        self,
        sandglass: Optional[SandglassBackend] = None,
        vector: Optional[VectorBackend] = None,
        lms: Optional[LMSBackend] = None,
        monument: Optional[MonumentBackend] = None,
    ) -> None:
        self.sandglass = sandglass
        self.vector = vector
        self.lms = lms
        self.monument = monument
        self._backends: List[_Backend] = [
            b for b in (sandglass, vector, lms, monument) if b is not None
        ]

    # ── 写入 ─────────────────────────────────────────────
    def store(
        self,
        item: Union[MemoryItem, Dict[str, Any], str],
        targets: Sequence[str] = ("sandglass", "vector", "lms"),
    ) -> Dict[str, Optional[str]]:
        """写入记忆到指定后端。

        Args:
            item: MemoryItem / dict / str（裸文本自动包装）。
            targets: 待写入的后端名集合（沙漏, 向量, LMS 默认全开）。

        Returns:
            ``{后端名: 后端返回ID}`` 的字典，跳过或失败项为 None。
        """
        item = self._coerce(item)
        results: Dict[str, Optional[str]] = {}
        backend_map = {
            "sandglass": self.sandglass,
            "vector": self.vector,
            "lms": self.lms,
        }
        for name in targets:
            backend = backend_map.get(name)
            if backend is None or not getattr(backend, "check", lambda: False)():
                results[name] = None
                continue
            try:
                results[name] = backend.store(item)
            except MemoryBackendError as exc:
                logger.warning("%s 写入跳过: %s", name, exc)
                results[name] = None
        return results

    # ── 协同检索 ─────────────────────────────────────────
    def recall(
        self,
        query: str,
        limit: int = 5,
        weights: Optional[Dict[str, float]] = None,
        **kwargs: Any,
    ) -> List[MemoryItem]:
        """跨后端协同检索。

        融合策略：按来源归组，每个来源各取 ``limit`` 条，再按来源权重加权；
        最后按综合相关分降序，去重（id / text hash）后截取前 ``limit`` 条。

        Args:
            query: 检索问题文本。
            limit: 最终返回条数。
            weights: 各来源权重，如 ``{"vector": 0.6, "lms": 0.3, "keyword": 0.1}``。

        Returns:
            按综合相关度降序的记忆列表。
        """
        weights = weights or {"vector": 0.6, "lms": 0.3, "keyword": 0.1}
        collected: List[tuple] = []  # (score, item)

        if self.vector is not None and weights.get("vector", 0) > 0:
            for item in self.vector.recall(query, limit=limit, **kwargs):
                collected.append((weights["vector"], item))

        if self.lms is not None and weights.get("lms", 0) > 0:
            for item in self.lms.recall(query, limit=limit, **kwargs):
                collected.append((weights["lms"], item))

        if weights.get("keyword", 0) > 0:
            for item in self._keyword_recall(query, limit=limit):
                collected.append((weights["keyword"], item))

        # 去重：按 text 哈希，保留最高分
        seen: Dict[str, float] = {}
        best: Dict[str, MemoryItem] = {}
        for score, item in collected:
            key = self._dedup_key(item)
            if key not in seen or score > seen[key]:
                seen[key] = score
                best[key] = item

        ranked = sorted(best.items(), key=lambda kv: seen[kv[0]], reverse=True)
        return [item for _, item in ranked[:limit]]

    def _keyword_recall(self, query: str, limit: int = 5) -> List[MemoryItem]:
        """关键词级降级检索：在所有已 store 于本进程内的条目中做子串匹配。

        这是内存级兜底；真实系统应由各自后端完成。此处仅为协同融合补位。
        """
        q = query.lower()
        hits: List[MemoryItem] = []
        for item in getattr(self, "_local_items", []):
            if q in item.text.lower() or any(q in t.lower() for t in item.tags):
                hits.append(item)
        return hits[:limit]

    # ── 贡献到丰碑 ───────────────────────────────────────
    def contribute(self, knowledge: Dict[str, Any]) -> Dict[str, Any]:
        """把一条经核准的知识纲要写入丰碑永久层。

        Args:
            knowledge: 含 ``title`` / ``body`` / ``tags`` / ``contributor`` 的字典。

        Returns:
            丰碑后台返回的候选 / 区块字典。
        """
        if self.monument is None or not self.monument.check():
            raise MemoryBackendError("丰碑后端不可用，无法贡献")
        return self.monument.contribute(knowledge)

    # ── 工具方法 ─────────────────────────────────────────
    def _coerce(self, item: Union[MemoryItem, Dict[str, Any], str]) -> MemoryItem:
        if isinstance(item, str):
            item = MemoryItem(text=item)
        elif isinstance(item, dict):
            item = MemoryItem.load(item)
        elif not isinstance(item, MemoryItem):
            raise TypeError(f"不支持的记忆类型: {type(item)!r}")
        # 记录本地样本，供 keyword 兜底
        self._local_items = getattr(self, "_local_items", None) or []
        self._local_items.append(item)  # type: ignore
        return item

    @staticmethod
    def _dedup_key(item: MemoryItem) -> str:
        return item.id or hashlib.sha1(item.text.encode("utf-8")).hexdigest()

    def close(self) -> None:
        """关闭所有后端资源。"""
        for backend in self._backends:
            try:
                backend.close()
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("关闭后端失败: %s", exc)


# ============================================================
# 便捷工厂函数
# ============================================================

def default_service() -> MemoryService:
    """构造一个可用的 MemoryService。

    默认不绑定任何真实后端，只开启存内 keyword 兜底，保证能安全跑。
    真实接入时，通过显式注入各后端适配器即可。**此函数不依赖网络**。
    """
    return MemoryService()


# ============================================================
# 辅助工具
# ============================================================

def memory_id_from(
    text: str, salt: str = "", timestamp: Optional[float] = None
) -> str:
    """根据文本内容生成稳定的内容寻址 ID（相同文本→相同 ID，便于去重）。"""
    ts = f"{timestamp or time.time():.0f}" if timestamp else ""
    raw = f"{salt}:{ts}:{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def utc_iso(timestamp: Optional[float] = None) -> str:
    """把 unix 时间戳转成 ISO8601 UTC 字符串（便于持久化/跨系统对齐）。"""
    ts = datetime.fromtimestamp(timestamp or time.time(), tz=timezone.utc)
    return ts.isoformat()


# ============================================================
# 自测（python memory_model.py）
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    svc = default_service()

    # MemoryItem 构造 / 序列化
    item = MemoryItem(
        text="编辑页面渲染问题已修复，用户确认通过。",
        source="chat",
        tags=["edit-web", "fix"],
        entropy=0.42,
        surprise=0.8,
        purposefulness=0.7,
    )
    print("item.id       =", item.id)
    print("item.dump()   =", item.dump())
    restored = MemoryItem.load(item.dump())
    assert restored.text == item.text

    # store（无后端时后端项为 None，不应抛错）
    res = svc.store(item)
    print("store()       =", res)

    # recall（内存 keyword 兜底）
    hits = svc.recall("编辑页面", limit=3)
    print("recall() hits =", len(hits))

    # 丰碑（未配置 → 抛 MemoryBackendError）
    try:
        svc.contribute({"title": "t", "body": "b", "tags": ["x"]})
    except MemoryBackendError as exc:
        print("contribute()  =", exc)

    print("\n[OK] memory_model 自测通过")
