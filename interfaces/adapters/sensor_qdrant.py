"""感官层适配器：Qdrant 向量服务。

包住 workspace 根下的 qdrant_client（QdrantClient / InMemoryStorage），
把"文本 → 向量 → 写入"流程翻译成标准 SensorPort.embed/batch_embed。

为保持模块可导入，这里惰性加载原系统；构造时可注入客户端或降级嵌入函数。
"""

from __future__ import annotations

from typing import Callable, List, Optional

from interfaces.base import EMBEDDING_DIM, DummySensor, SensorPort


class QdrantSensorAdapter(SensorPort):
    """基于 Qdrant 的嵌入 / 向量写入适配器。

    embed 语义：
      1. 若提供 fallback_embed（如 bge-m3 的 HTTP 嵌入），用其产生真实向量；
      2. 否则退化到确定性 DummySensor（离线降级）；
      3. 得到向量后，尽力写入 client（失败不阻塞 embed 返回语义）。
    """

    def __init__(
        self,
        collection: str = "messages",
        fallback_embed: Optional[Callable[[str], List[float]]] = None,
        client: Optional[object] = None,
        dim: int = EMBEDDING_DIM,
    ):
        self.collection = collection
        self._fallback_embed = fallback_embed
        self._client = client
        self._dim = dim

    # -- 底层访问 --------------------------------------------------------
    def _lazy_client(self):
        if self._client is not None:
            return self._client
        try:
            import qdrant_client  # type: ignore

            self._client = qdrant_client.create_client()
        except Exception as e:
            from interfaces.exceptions import ConnectionError_

            raise ConnectionError_(f"Qdrant 不可用: {e}")
        return self._client

    # -- SensorPort 实现 --------------------------------------------------
    def embed(self, text: str) -> List[float]:
        if not text or not text.strip():
            from interfaces.exceptions import CapacityError

            raise CapacityError("embed: 空文本")
        vector = (
            self._fallback_embed(text)
            if self._fallback_embed is not None
            else self._hash(text)
        )
        self._try_write(text, vector)
        return vector

    def batch_embed(self, texts: List[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]

    def health(self) -> bool:
        try:
            c = self._lazy_client()
            return bool(getattr(c, "connected", lambda: True)())
        except Exception:
            return False

    @property
    def dim(self) -> int:
        return self._dim

    # -- 内部辅助 ---------------------------------------------------------
    def _hash(self, text: str) -> List[float]:
        return DummySensor(dim=self._dim).embed(text)

    def _try_write(self, text: str, vector: List[float]) -> None:
        """尽力写入底层向量库；失败不抛，由调用方降级。"""
        try:
            c = self._lazy_client()
        except Exception:
            return
        fingerprint = self._fingerprint(text)
        try:
            # QdrantClient.upsert 契约：data 为 dict，ids 可选
            c.upsert(
                self.collection,
                {"id": fingerprint, "vector": vector, "payload": {"text": text}},
            )
        except TypeError:
            # InMemoryStorage.upsert 契约：data 为记录对象
            try:
                c.upsert(
                    self.collection,
                    self._record(fingerprint, text, vector),
                )
            except Exception:
                pass
        except Exception:
            pass

    @staticmethod
    def _fingerprint(text: str) -> str:
        from interfaces.models import MemoryItem

        return MemoryItem(text=text, origin="vector", system="vector").fingerprint()

    @staticmethod
    def _record(record_id: str, text: str, vector: List[float]):
        try:
            from qdrant_client import InMemoryRecord  # type: ignore

            return InMemoryRecord(id=record_id, payload={"text": text, "vector": vector})
        except Exception:  # pragma: no cover
            return {"id": record_id, "payload": {"text": text, "vector": vector}}
