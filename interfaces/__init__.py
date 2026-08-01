"""标准接口层 —— 三层架构的松耦合契约。

架构背景（钱学森系统论）：
    ┌─────────────────────────────┐
    │  应用层 : ApplicationPort    │  ← 丰碑网络（知识贡献 / P2P 同步）
    │  认知层 : CognitivePort      │  ← LMS + 沙漏（隐式/显式记忆）
    │  感官层 : SensorPort         │  ← 向量服务（bge-m3 嵌入）
    └─────────────────────────────┘

每层只依赖抽象接口（base.py），具体实现通过适配器（adapters/）
注入，从而实现松耦合。调用方永远不直接 import 具体系统。

用法：
    from interfaces.registry import Registry
    reg = Registry.default()          # 组装所有真实适配器
    v = reg.sensor.embed("你好")     # sensor/cognitive/application 为 @property，直接返回 Port
    st = reg.cognitive.get_state()
    reg.application.contribute("知识")
"""

from interfaces.base import (
    SensorPort,
    CognitivePort,
    ApplicationPort,
    EMBEDDING_DIM,
    DummySensor,
    DummyCognitive,
    DummyApplication,
)
from interfaces.models import (
    MemoryItem,
    CognitiveState,
    ContributionResult,
    Intensity,
)
from interfaces.registry import Registry

__all__ = [
    "SensorPort",
    "CognitivePort",
    "ApplicationPort",
    "MemoryItem",
    "CognitiveState",
    "ContributionResult",
    "Intensity",
    "EMBEDDING_DIM",
    "DummySensor",
    "DummyCognitive",
    "DummyApplication",
    "Registry",
]
