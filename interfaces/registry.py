"""依赖注入容器 (Registry) —— 组装三层 Port 与其适配器。

调用方通过 Registry 获取抽象 Port，永远不直接接触具体系统。
这样：
  - 测试时可用 mocks 构造 Registry，
  - 生产时用真实适配器构造 Registry，
  - 更换底层系统只需换适配器，不动业务代码。

用法：
    registry = Registry.default()            # 真实适配器（惰性连接）
    cognition = registry.cognitive           # CognitivePort（@property 直接取实例）
    state = cognition.get_state()

    # 全内存版（测试 / 离线）
    registry = Registry.in_memory()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

from interfaces.base import (
    ApplicationPort,
    CognitivePort,
    SensorPort,
)
from interfaces.exceptions import InterfaceError


Provider = Callable[[], object]


@dataclass
class _ProviderGroup:
    # 每个字段是一个 0 参可调用，返回对应 Port 实例
    sensor: Optional[Provider] = None
    cognitive: Optional[Provider] = None
    application: Optional[Provider] = None


@dataclass
class Registry:
    """三层 Port 的组装容器。"""

    sensor_provider: Optional[Provider] = None
    cognitive_provider: Optional[Provider] = None
    application_provider: Optional[Provider] = None

    _cache: Dict[str, object] = field(default_factory=dict, repr=False)

    # -- 访问器（懒加载 + 缓存）--------------------------------------------
    @property
    def sensor(self) -> SensorPort:
        return self._get("sensor", self.sensor_provider, SensorPort)

    @property
    def cognitive(self) -> CognitivePort:
        return self._get("cognitive", self.cognitive_provider, CognitivePort)

    @property
    def application(self) -> ApplicationPort:
        return self._get("application", self.application_provider, ApplicationPort)

    def _get(self, key: str, provider: Optional[Provider], expected_type) -> object:
        if key in self._cache:
            return self._cache[key]
        if provider is None:
            raise InterfaceError(f"Registry: '{key}' 未配置提供者")
        try:
            instance = provider()
        except Exception as e:
            raise InterfaceError(f"Registry: 无法创建 '{key}': {e}") from e
        if not isinstance(instance, expected_type):
            raise InterfaceError(
                f"Registry: '{key}' 提供者返回 {type(instance).__name__}，"
                f"期望 {expected_type.__name__}"
            )
        self._cache[key] = instance
        return instance

    # -- 便捷构造 ------------------------------------------------------------
    @classmethod
    def in_memory(cls) -> "Registry":
        """全内存实现：不依赖任何外部服务，适合测试 / 离线 / 演示。"""
        from interfaces.base import DummyApplication, DummyCognitive, DummySensor

        return cls(
            sensor_provider=lambda: DummySensor(),
            cognitive_provider=lambda: DummyCognitive(),
            application_provider=lambda: DummyApplication(),
        )

    @classmethod
    def default(cls, **overrides) -> "Registry":
        """生产组装：尝试真实适配器（惰性连接，失败自动降级）。"""
        from interfaces.adapters.sensor_qdrant import QdrantSensorAdapter
        from interfaces.adapters.cognitive_lms import LMSCognitiveAdapter
        from interfaces.adapters.cognitive_sandglass import SandglassCognitiveAdapter
        from interfaces.adapters.application_monument import MonumentApplicationAdapter

        sensor_provider = overrides.get("sensor_provider") or QdrantSensorAdapter
        cognitive_provider = overrides.get("cognitive_provider") or _chain_cognitive
        application_provider = overrides.get("application_provider") or MonumentApplicationAdapter
        return cls(
            sensor_provider=lambda: sensor_provider(),
            cognitive_provider=lambda: cognitive_provider(),
            application_provider=lambda: application_provider(),
        )

    # -- with 支持（便于临时替换某一个 provider）-----------------------------
    def with_sensor(self, provider: Provider) -> "Registry":
        return Registry(provider, self.cognitive_provider, self.application_provider)

    def with_cognitive(self, provider: Provider) -> "Registry":
        return Registry(self.sensor_provider, provider, self.application_provider)

    def with_application(self, provider: Provider) -> "Registry":
        return Registry(self.sensor_provider, self.cognitive_provider, provider)


def _chain_cognitive() -> CognitivePort:
    """认知层组合：LMS（状态权威）优先，沙漏作为显式记忆补充。

    返回一个组合 Port：store/recall 写沙漏（显式），get_state 读 LMS（隐式状态）。
    """
    from interfaces.base import DummyCognitive
    from interfaces.adapters.cognitive_lms import LMSCognitiveAdapter
    from interfaces.adapters.cognitive_sandglass import SandglassCognitiveAdapter

    return _CompositeCognitive(
        state_source=LMSCognitiveAdapter(),
        memory_source=SandglassCognitiveAdapter(),
        fallback=DummyCognitive(),
    )


class _CompositeCognitive(CognitivePort):
    """组合认知适配器：状态来自 LMS，显式记忆来自沙漏。

    2026-08-10 修复（毛毛，dandan 质问"LMS 活了吗"）：
    此前 recall 只走沙漏（memory_source），LMS 的 recall 从未被调用——
    注入给主 AI 的全是沙漏 cron 噪音，LMS 116 轮真实对话一条都没回到对话。
    现改为融合：LMS 召回（隐式记忆，语义权重高）+ 沙漏召回（显式记忆）合并，
    去重后按分排序。LMS 不可达时自动只走沙漏（fail-open）。
    """

    def __init__(self, state_source, memory_source, fallback):
        self._state = state_source
        self._memory = memory_source
        self._fallback = fallback

    def store(self, memory) -> bool:
        try:
            return bool(self._memory.store(memory))
        except Exception:
            return bool(self._fallback.store(memory))

    def recall(self, query, k=5) -> list:
        """融合召回：LMS（活体隐式记忆）+ 沙漏（显式记忆），按分排序去重。"""
        items = []
        # 1) LMS 召回（活体记忆——真实对话痕迹；失败静默降级）
        try:
            items.extend(self._state.recall(query, k))  # state_source 即 LMSCognitiveAdapter
        except Exception:
            pass
        # 2) 沙漏召回（显式记忆）
        try:
            items.extend(self._memory.recall(query, k))
        except Exception:
            pass
        # 3) 全失败才回退 Dummy
        if not items:
            try:
                return list(self._fallback.recall(query, k))
            except Exception:
                return []
        # 去重（按 id/text 指纹）+ 按 score 降序
        seen = set()
        dedup = []
        for it in items:
            key = getattr(it, "id", None) or hash(str(getattr(it, "text", ""))[:64])
            if key in seen:
                continue
            seen.add(key)
            dedup.append(it)
        scored = [
            (it, float((it.metadata or {}).get("score", 0) or 0)) for it in dedup
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [it for it, _ in scored[:k]]

    def get_state(self):
        try:
            return self._state.get_state()
        except Exception:
            return self._fallback.get_state()
