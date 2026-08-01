"""应用层适配器：丰碑真实后端（MonumentCore 包装）。

丰碑负责“知识贡献 + P2P 同步 + 检索”。本适配器把
``workspace/monument/monument_core.MonumentCore`` 包装成 ``ApplicationPort``。

与 ``application_monument.MonumentApplicationAdapter`` 的关系：
  这里是同一 Port 契约下的另一实现 —— 直接解耦 ``real_backends`` 旧逻辑，
  便于独立单测（可注入 ``core`` 而无需真实丰碑）。

设计要点（高内聚低耦合）：
  - 只做“翻译”，专注 contribute / sync_monuments / query_monument。
  - 惰性加载 + 降级：core 不可用时回退到 DummyApplication（不崩溃）。
  - 候选被批准失败时，仍返回候选区 urn，便于上层感知“已留存候选”。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from interfaces.base import DummyApplication, ApplicationPort
from interfaces.exceptions import CapacityError
from interfaces.models import ContributionResult

logger = __import__("logging").getLogger(__name__)


class MonumentAdapter(ApplicationPort):
    """丰碑真实后端适配器（MonumentCore 包装）。"""

    def __init__(
        self,
        core: Optional[object] = None,
        monument_dir: Optional[str] = None,
        fallback: Optional[ApplicationPort] = None,
    ):
        self._core = core
        self._monument_dir = monument_dir
        self._fallback = fallback or DummyApplication()
        self._degraded = core is None

    # -- 底层访问（惰性加载） ----------------------------------------------
    def _ensure_core(self):
        if self._core is not None:
            return self._core
        try:
            from monument.monument_core import MonumentCore  # type: ignore

            self._core = (
                MonumentCore(self._monument_dir)
                if self._monument_dir
                else MonumentCore()
            )
            if hasattr(self._core, "is_ready"):
                self._degraded = not bool(self._core.is_ready())
            else:
                self._degraded = False
        except Exception as e:
            logger.warning("丰碑核心加载失败，降级内存账本: %s", e)
            self._core = None
        return self._core

    # -- ApplicationPort ----------------------------------------------------
    def contribute(self, knowledge: str, **kwargs) -> ContributionResult:
        if not knowledge or not knowledge.strip():
            return ContributionResult(ok=False, error="empty knowledge")
        core = self._ensure_core()
        if core is None:
            return self._fallback.contribute(knowledge, **kwargs)

        try:
            tags = kwargs.get("tags")
            if isinstance(tags, str):
                tags = [tags]
            if not tags and kwargs.get("tag"):
                tags = [kwargs.get("tag")]
            tags = tags or ["standard"]

            candidate = core.create_candidate(
                title=kwargs.get("title") or (knowledge[:40] or "untitled"),
                body=knowledge,
                tags=tags,
                contributor=kwargs.get("contributor", "agent:unknown"),
                source=kwargs.get("source", {"type": "monument_adapter"}),
                references=kwargs.get("references"),
                conditions=kwargs.get("conditions"),
            )
            candidate_id = (
                candidate.get("candidate_id")
                if isinstance(candidate, dict)
                else str(candidate)
            )
            try:
                block = core.approve_candidate(
                    candidate_id=candidate_id,
                    approver=kwargs.get("approver", "system:interfaces"),
                    note=kwargs.get("note"),
                )
            except Exception:
                # 批准失败：候选仍留存 candidates/，返回候选 urn 以便上层感知
                return ContributionResult(
                    ok=False,
                    urn=f"candidate:{candidate_id}",
                    error="approve_candidate 失败，候选已留存",
                    extra={"candidate_id": candidate_id},
                )

            if isinstance(block, dict):
                content = block.get("content") or {}
                urn = content.get("urn") if isinstance(content, dict) else block.get("urn")
                return ContributionResult(
                    ok=True,
                    urn=urn or block.get("urn"),
                    tag=",".join(tags),
                    extra={"candidate_id": candidate_id},
                )
            return ContributionResult(ok=True, urn=block.urn if hasattr(block, "urn") else None, tag=",".join(tags))
        except Exception as e:
            self._degraded = True
            return ContributionResult(ok=False, error=str(e))

    def sync_monuments(self) -> List[str]:
        core = self._ensure_core()
        if core is None:
            return self._fallback.sync_monuments()
        try:
            if hasattr(core, "sync_nodes"):
                return [str(x) for x in (core.sync_nodes() or [])]
            items = core.read_all() if hasattr(core, "read_all") else []
            urns: List[str] = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                content = it.get("content")
                urn = content.get("urn") if isinstance(content, dict) else it.get("urn")
                if urn:
                    urns.append(str(urn))
            return urns
        except Exception as e:
            self._degraded = True
            logger.warning("丰碑同步失败: %s", e)
            return self._fallback.sync_monuments()

    def query_monument(self, topic: str, k: int = 5) -> List[str]:
        if not topic or not (1 <= k <= 100):
            raise CapacityError("query_monument: 参数越界")
        core = self._ensure_core()
        if core is None:
            return self._fallback.query_monument(topic, k)
        try:
            t = topic.lower()
            hits: List[str] = []
            if hasattr(core, "read_by_tag"):
                try:
                    for d in core.read_by_tag(topic) or []:
                        text = _block_text(d)
                        if t in (text or "").lower():
                            hits.append(text.strip())
                except Exception:
                    pass
            if len(hits) < k and hasattr(core, "read_all"):
                for d in core.read_all() or []:
                    text = _block_text(d)
                    if t in (text or "").lower():
                        hits.append(text.strip())
            seen, out = set(), []
            for h in hits:
                if h not in seen:
                    seen.add(h)
                    out.append(h)
            return out[:k]
        except Exception as e:
            self._degraded = True
            logger.warning("丰碑查询失败: %s", e)
            return self._fallback.query_monument(topic, k)

    @property
    def degraded(self) -> bool:
        return self._degraded


def _block_text(block: object) -> str:
    """从丰碑区块 / 候选提取可搜索文本。"""
    if not isinstance(block, dict):
        return ""
    content = block.get("content")
    if isinstance(content, dict):
        title = content.get("title") or ""
        body = content.get("body") or ""
        tags = " ".join(content.get("tags") or [])
        return f"{title}\n{body}\n{tags}"
    return str(content or "")
