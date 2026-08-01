"""应用层适配器：丰碑网络。

丰碑负责"知识贡献 + P2P 同步 + 检索"。本适配器把
workspace/monument/monument_core.MonumentCore 包装成 ApplicationPort。

惰性加载 + 降级：无法连接时退到本地内存账本（DummyApplication），
保证上层不因丰碑故障而崩溃。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from interfaces.base import DummyApplication, ApplicationPort
from interfaces.models import ContributionResult


class MonumentApplicationAdapter(ApplicationPort):
    """丰碑应用适配器。"""

    def __init__(self, core: Optional[object] = None, monument_dir: Optional[str] = None):
        self._core = core
        self._monument_dir = monument_dir
        self._fallback = DummyApplication()
        self._degraded = core is None

    # -- 底层访问 -----------------------------------------------------------
    def _ensure_core(self):
        if self._core is not None:
            return self._core
        try:
            from monument.monument_core import MonumentCore  # type: ignore

            self._core = (
                MonumentCore(self._monument_dir) if self._monument_dir else MonumentCore()
            )
            self._degraded = False
        except Exception:
            self._core = None
        return self._core

    # -- ApplicationPort ----------------------------------------------------
    def contribute(self, knowledge: str, **kwargs) -> ContributionResult:
        core = self._ensure_core()
        if core is None:
            return self._fallback.contribute(knowledge, **kwargs)
        try:
            # 丰碑核心 API：create_candidate(title, body, tags, contributor, source)
            tags = kwargs.get("tags")
            if isinstance(tags, str):
                tags = [tags]
            if not tags and kwargs.get("tag"):
                tags = [kwargs.get("tag")]
            tags = tags or ["standard"]
            candidate = core.create_candidate(
                title=kwargs.get("title", knowledge[:40] or "untitled"),
                body=knowledge,
                tags=tags,
                contributor=kwargs.get("contributor", "agent:unknown"),
                source=kwargs.get("source", {"type": "interface_adapter"}),
                references=kwargs.get("references"),
                conditions=kwargs.get("conditions"),
            )
            candidate_id = (
                candidate.get("candidate_id") if isinstance(candidate, dict) else str(candidate)
            )
            block = core.approve_candidate(
                candidate_id=candidate_id,
                approver=kwargs.get("approver", "system:interfaces"),
                note=kwargs.get("note"),
            )
            if isinstance(block, dict):
                # urn 在 block['content']['urn']
                content = block.get("content") or {}
                urn = content.get("urn") if isinstance(content, dict) else None
                return ContributionResult(ok=True, urn=urn or block.get("urn"))
            return ContributionResult(ok=True, urn=block.urn if hasattr(block, "urn") else None)
        except Exception as e:
            self._degraded = True
            return ContributionResult(ok=False, error=str(e))

    def sync_monuments(self) -> List[str]:
        core = self._ensure_core()
        if core is None:
            return self._fallback.sync_monuments()
        try:
            # 尝试调用网络同步；若核心无此方法，则枚举本地块作为"同步源"
            if hasattr(core, "sync_nodes"):
                return [str(x) for x in (core.sync_nodes() or [])]
            items = core.read_all() if hasattr(core, "read_all") else []
            urns = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                content = it.get("content")
                urn = (
                    content.get("urn")
                    if isinstance(content, dict)
                    else it.get("urn")
                )
                if urn:
                    urns.append(str(urn))
            return urns
        except Exception:
            self._degraded = True
            return self._fallback.sync_monuments()

    def query_monument(self, topic: str, k: int = 5) -> List[str]:
        if not topic or not (1 <= k <= 100):
            from interfaces.exceptions import CapacityError

            raise CapacityError("query_monument: 参数越界")
        core = self._ensure_core()
        if core is None:
            return self._fallback.query_monument(topic, k)
        try:
            # 先按标签精确检索；若命中不足，再按正文模糊搜索
            docs = []
            if hasattr(core, "read_by_tag") and docs is not None:
                try:
                    docs = core.read_by_tag(topic) or []
                except Exception:
                    docs = []
            hits = []
            for d in docs:
                text = _block_text(d)
                if topic.lower() in text.lower():
                    hits.append(text.strip())
            if len(hits) < k and hasattr(core, "read_all"):
                for d in core.read_all() or []:
                    text = _block_text(d)
                    if topic.lower() in text.lower():
                        hits.append(text.strip())
            # 去重保序，截断
            seen, out = set(), []
            for h in hits:
                if h not in seen:
                    seen.add(h)
                    out.append(h)
            return out[:k]
        except Exception:
            self._degraded = True
            return self._fallback.query_monument(topic, k)

    @property
    def degraded(self) -> bool:
        return self._degraded


def _block_text(block) -> str:
    """从丰碑区块/候选提取可搜索文本。"""
    if not isinstance(block, dict):
        return ""
    content = block.get("content")
    if isinstance(content, dict):
        title = content.get("title") or ""
        body = content.get("body") or ""
        tags = " ".join(content.get("tags") or [])
        return f"{title}\n{body}\n{tags}"
    return str(content or "")
