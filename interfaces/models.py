"""数据模型 —— 跨层传递的标准数据结构。

避免各系统各自为政的 dict（数据重复的根源之一）：所有跨层
交换必须使用这里定义的数据类，字段含义唯一。

注意：模型均为普通 dataclass，不绑定任何具体存储；序列化由
各适配器负责。为保持向后兼容，全部字段带默认值。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Intensity(Enum):
    """记忆强度 / 重要度分级（对齐 LMS 的激活强度口径）。"""

    WEAK = "weak"
    NORMAL = "normal"
    STRONG = "strong"
    CRITICAL = "critical"


@dataclass
class MemoryItem:
    """认知层往返的标准记忆单元。

    这是 store()/recall() 交换的统一载体，可同时被 LMS（隐式）与
    沙漏（显式）适配器消费。origin 标记来源，system 标记所属系统，
    便于跨系统去重（解决"同一对话存三份"问题）。
    """

    id: Optional[str] = None
    text: str = ""
    # 内容与元数据
    content: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    # 时间与来源
    created_at: float = field(default_factory=time.time)
    origin: str = "agent"  # agent | user | system | sync
    system: str = ""  # lms | sandglass | vector | monument
    # 认知状态
    entropy: Optional[float] = None
    surprise: Optional[float] = None
    intensity: Intensity = Intensity.NORMAL
    vector: Optional[List[float]] = None
    # 知识图谱三元组（可选，织线/丰碑消费）
    triple: Optional[Dict[str, Any]] = None

    def fingerprint(self) -> str:
        """跨系统去重指纹：基于语义内容而非 id 或 origin。

        刻意排除 origin/system/id：同一句话无论哪个系统、什么身份写入，
        都应得到同一指纹，才能用于跨系统去重。
        """
        import hashlib

        raw = f"{self.text}|{_stable(self.content)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass
class CognitiveState:
    """认知层状态快照（get_state() 返回值契约）。"""

    entropy: float = 0.0
    surprise: float = 0.0
    purposefulness: float = 0.0
    # 可选扩展
    memory_count: int = 0
    active_nodes: List[str] = field(default_factory=list)
    stage: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "entropy": self.entropy,
            "surprise": self.surprise,
            "purposefulness": self.purposefulness,
            "memory_count": self.memory_count,
            "active_nodes": list(self.active_nodes),
            "stage": self.stage,
        }
        if self.extra:
            d["extra"] = self.extra
        return d


@dataclass
class ContributionResult:
    """应用层贡献结果。"""

    ok: bool
    urn: Optional[str] = None
    tag: Optional[str] = None
    error: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.ok


# -- 指纹计算辅助 ------------------------------------------------------------


def _stable(obj: Any) -> str:
    """对 content 做稳定序列化，供指纹计算。"""
    import json

    def _norm(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: _norm(v) for k, v in sorted(o.items(), key=lambda kv: str(kv[0]))}
        if isinstance(o, (list, tuple)):
            return [_norm(i) for i in o]
        if isinstance(o, Enum):
            return o.value
    def _norm(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: _norm(v) for k, v in sorted(o.items(), key=lambda kv: str(kv[0]))}
        if isinstance(o, (list, tuple)):
            return [_norm(i) for i in o]
        if isinstance(o, Enum):
            return o.value
        try:
            json.dumps(o)
            return o
        except (TypeError, ValueError):
            return str(o)

    return json.dumps(_norm(obj), ensure_ascii=False, sort_keys=True)
