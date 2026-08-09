# -*- coding: utf-8 -*-
"""lms_client.models — 数据模型（dataclass）。

与 :8190 数据面响应结构一一对应：
    POST /recall        → RecallResult（含 RecallHit 列表）
    GET  /status/{sid}  → StatusInfo（status 为服务端状态字典 + 便捷属性）

全部为纯 dataclass，零依赖；`from_dict` 容忍服务端字段缺失/类型漂移
（fail-open 风格：取不到就取默认值，绝不因字段缺失抛异常）。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["RecallHit", "RecallResult", "StatusInfo"]


@dataclass
class RecallHit:
    """单条检索命中。"""

    text: str = ""
    score: float = 0.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RecallHit":
        return cls(
            text=str(data.get("text", "") or ""),
            score=float(data.get("score", 0.0) or 0.0),
        )


@dataclass
class RecallResult:
    """POST /recall 响应。"""

    session_id: str = "main"
    query: str = ""
    k: int = 5
    count: int = 0
    results: List[RecallHit] = field(default_factory=list)
    duration_ms: float = 0.0
    turn_count: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RecallResult":
        raw_results = data.get("results") or []
        return cls(
            session_id=str(data.get("session_id", "main") or "main"),
            query=str(data.get("query", "") or ""),
            k=int(data.get("k", 5) or 5),
            count=int(data.get("count", len(raw_results)) or 0),
            results=[RecallHit.from_dict(r) for r in raw_results],
            duration_ms=float(data.get("duration_ms", 0.0) or 0.0),
            turn_count=int(data.get("turn_count", 0) or 0),
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 JSON 友好 dict（MCP 桥 / --json 输出用）。"""
        return {
            "session_id": self.session_id,
            "query": self.query,
            "k": self.k,
            "count": self.count,
            "results": [
                {"text": h.text, "score": h.score} for h in self.results
            ],
            "duration_ms": self.duration_ms,
            "turn_count": self.turn_count,
        }


@dataclass
class StatusInfo:
    """GET /status/{sid} 响应。

    `status` 保留服务端原始字典（可继续用 .get() 取任意字段），
    常用字段提供便捷属性。
    """

    session_id: str = "main"
    status: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StatusInfo":
        return cls(
            session_id=str(data.get("session_id", "main") or "main"),
            status=dict(data.get("status") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
        }

    # ── 便捷属性（全部容错，缺字段返回 None/0/False） ──
    @property
    def turn_count(self) -> int:
        return int(self.status.get("turn_count", 0) or 0)

    @property
    def num_nodes(self) -> int:
        return int(self.status.get("num_nodes", 0) or 0)

    @property
    def input_dim(self) -> int:
        return int(self.status.get("input_dim", 0) or 0)

    @property
    def last_entropy(self) -> Optional[float]:
        v = self.status.get("last_entropy")
        return float(v) if v is not None else None

    @property
    def last_surprise(self) -> Optional[float]:
        v = self.status.get("last_surprise")
        return float(v) if v is not None else None

    @property
    def entropy_ratio(self) -> Optional[float]:
        v = self.status.get("entropy_ratio")
        return float(v) if v is not None else None

    @property
    def purpose_coherence(self) -> Optional[float]:
        v = self.status.get("purpose_coherence")
        return float(v) if v is not None else None

    @property
    def precision_mean(self) -> Optional[float]:
        v = self.status.get("precision_mean")
        return float(v) if v is not None else None

    @property
    def episodic_buffer_size(self) -> int:
        return int(self.status.get("episodic_buffer_size", 0) or 0)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.status.get("llm_enabled", False))

    @property
    def meta(self) -> Dict[str, Any]:
        return dict(self.status.get("meta") or {})

    def get(self, key: str, default: Any = None) -> Any:
        """访问原始状态字典任意字段（容错）。"""
        return self.status.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.status[key]
