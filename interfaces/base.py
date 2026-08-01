"""抽象接口定义 (Port) —— 三层架构的契约面。

设计原则：
1. 每层一个 Port（抽象基类 + ABC），调用方只依赖 Port。
2. 默认提供 *memory 实现和 *stub 用法，便于测试与降级。
3. 方法签名即契约：参数类型、返回值类型、异常类型都写清楚。
4. 语义化错误：任何实现都以抛 InterfaceError 子类表示失败，
   不吞异常、不返回 None 代表成功。

依赖注入：实现类通过构造函数接收底层依赖（如 vector client、
LMS 客户端），构造由 Registry 统一完成，Port 本身不负责实例化。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from interfaces.exceptions import CapacityError, InterfaceError
from interfaces.models import CognitiveState, ContributionResult, MemoryItem

# 向量维度（bge-m3 的输出维度）
EMBEDDING_DIM = 1024


# --------------------------------------------------------------------------
# 感官层 (Sensor) —— 把文本变成向量
# --------------------------------------------------------------------------
class SensorPort(ABC):
    """感官层接口：文本 → 向量。向量服务（bge-m3）实现本接口。"""

    @abstractmethod
    def embed(self, text: str) -> List[float]:
        """单条文本嵌入。返回定长向量（默认 1024 维）。

        Raises:
            ConnectionError_: 向量服务不可达（调用方可降级/重试）。
            CapacityError:   输入为空 / 维度异常。
        """
        raise NotImplementedError

    @abstractmethod
    def batch_embed(self, texts: List[str]) -> List[List[float]]:
        """批量嵌入，返回与 texts 等长的向量列表（顺序一致）。"""
        raise NotImplementedError

    # -- 可选能力探测 ----------------------------------------------------
    @abstractmethod
    def health(self) -> bool:
        """健康检查：服务是否可连通。"""
        raise NotImplementedError

    @property
    @abstractmethod
    def dim(self) -> int:
        """向量维度。"""
        raise NotImplementedError


class DummySensor(SensorPort):
    """内存降级实现：不依赖外部服务，用确定性哈希模拟向量。

    用于：测试、离线降级。注意：没有语义信息，仅供联调。
    """

    def __init__(self, dim: int = EMBEDDING_DIM, seed: int = 0):
        self._dim = dim
        self._seed = seed

    def embed(self, text: str) -> List[float]:
        if not text or not text.strip():
            raise CapacityError("embed: 空文本")
        return self._hash_vec(text)

    def batch_embed(self, texts: List[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]

    def health(self) -> bool:
        return True

    @property
    def dim(self) -> int:
        return self._dim

    def _hash_vec(self, text: str) -> List[float]:
        import hashlib

        h = hashlib.sha256(f"{self._seed}|{text}".encode("utf-8")).digest()
        out = [0.0] * self._dim
        for i, b in enumerate(h * ((self._dim // 32) + 1)):
            if i >= self._dim:
                break
            out[i] = (b / 255.0) - 0.5  # [-0.5, 0.5)
        # 归一化，保证余弦可比
        norm = (sum(v * v for v in out) ** 0.5) or 1.0
        return [v / norm for v in out]


# --------------------------------------------------------------------------
# 认知层 (Cognitive) —— 记忆的存取与状态
# --------------------------------------------------------------------------
class CognitivePort(ABC):
    """认知层接口：LMS（隐式）+ 沙漏（显式）的统一记忆契约。"""

    @abstractmethod
    def store(self, memory: MemoryItem) -> bool:
        """持久化一条记忆。返回是否写入成功。

        Raises:
            InterfaceError: 底层存储失败。
        """
        raise NotImplementedError

    @abstractmethod
    def recall(self, query: str, k: int = 5) -> List[MemoryItem]:
        """按语义/关键词召回 k 条记忆。

        Raises:
            CapacityError: k 不在 [1, 100]。
        """
        raise NotImplementedError

    @abstractmethod
    def get_state(self) -> CognitiveState:
        """返回认知状态：熵、惊讶度、目的性等。"""
        raise NotImplementedError


class DummyCognitive(CognitivePort):
    """内存降级实现：内存 list 存记忆，简单关键词召回。

    用于测试与离线降级；提供确定性状态。
    """

    def __init__(self):
        self._store: List[MemoryItem] = []
        self._entropy: float = 3.5
        self._surprise: float = 1.0
        self._purpose: float = 0.5

    def store(self, memory: MemoryItem) -> bool:
        memory.id = memory.id or f"dummy-{len(self._store)}"
        memory.system = memory.system or "dummy"
        self._store.append(memory)
        return True

    def recall(self, query: str, k: int = 5) -> List[MemoryItem]:
        if not 1 <= k <= 100:
            raise CapacityError(f"recall: k 越界 {k}")
        if not query:
            return []
        scored = sorted(
            ((self._score(m, query), m) for m in self._store),
            key=lambda t: t[0],
            reverse=True,
        )
        return [m for _, m in scored[:k]]

    def get_state(self) -> CognitiveState:
        return CognitiveState(
            entropy=self._entropy,
            surprise=self._surprise,
            purposefulness=self._purpose,
            memory_count=len(self._store),
        )

    @staticmethod
    def _score(m: MemoryItem, query: str) -> float:
        q = query.lower()
        text = m.text.lower()
        return sum(1.0 for tok in q.split() if tok in text)


# --------------------------------------------------------------------------
# 应用层 (Application) —— 知识贡献与丰碑同步
# --------------------------------------------------------------------------
class ApplicationPort(ABC):
    """应用层接口：丰碑网络（知识贡献 / P2P 同步 / 检索）。"""

    @abstractmethod
    def contribute(self, knowledge: str, **kwargs) -> ContributionResult:
        """向丰碑网络贡献一条知识。成功返回含 urn 的结果。"""
        raise NotImplementedError

    @abstractmethod
    def sync_monuments(self) -> List[str]:
        """与网络中其他丰碑节点同步，返回新拉取条目的 urn/id 列表。"""
        raise NotImplementedError

    @abstractmethod
    def query_monument(self, topic: str, k: int = 5) -> List[str]:
        """按主题在丰碑中检索，返回命中和知识文本/urn 列表。"""
        raise NotImplementedError


class DummyApplication(ApplicationPort):
    """内存降级实现：本地 list 模拟丰碑账本。"""

    def __init__(self):
        self._blocks: List[Dict] = []
        self._counter = 0

    def contribute(self, knowledge: str, **kwargs) -> ContributionResult:
        if not knowledge or not knowledge.strip():
            return ContributionResult(ok=False, error="empty knowledge")
        self._counter += 1
        urn = f"dummy-{self._counter}"
        self._blocks.append({"urn": urn, "knowledge": knowledge, "tag": kwargs.get("tag")})
        return ContributionResult(ok=True, urn=urn, tag=kwargs.get("tag"))

    def sync_monuments(self) -> List[str]:
        # 模拟同步：返回当前块索引作为"新拉取"
        return [b["urn"] for b in self._blocks]

    def query_monument(self, topic: str, k: int = 5) -> List[str]:
        if not topic or not 1 <= k <= 100:
            raise CapacityError("query_monument: 参数越界")
        t = topic.lower()
        return [
            b["knowledge"]
            for b in self._blocks
            if t in b["knowledge"].lower()
        ][:k]
