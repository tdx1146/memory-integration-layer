"""感官层适配器：向量服务真实后端（OpenAI 兼容 HTTP /embeddings）。

将 ``real_backends`` 里对 ``http://...:11435/v1/embeddings`` 的调用
翻译成标准 ``SensorPort`` 的 embed/batch_embed/health/dim。

与旧实现差异：
  - 构造不连网；嵌入在调用时才发起，可注入 ``http_post`` 便于单测。
  - embed 失败抛 ``ConnectionError_``（可重试），不再静默返回 []——
    由上层决定降级策略（这是对旧版“吞错返回空向量”的修正）。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from interfaces.base import EMBEDDING_DIM, SensorPort
from interfaces.exceptions import CapacityError, ConnectionError_

logger = __import__("logging").getLogger(__name__)


class VectorAdapter(SensorPort):
    """基于 OpenAI 兼容 /embeddings 端点的向量真实后端适配器。"""

    def __init__(
        self,
        api_url: str = os.environ.get(
            "VECTOR_URL", "http://192.168.0.103:11435/v1/embeddings"
        ),
        model: str = "bge-m3",
        dim: int = EMBEDDING_DIM,
        timeout: float = 30.0,
        http_post: Optional[Any] = None,
    ):
        self.api_url = api_url
        self.model = model
        self._dim = dim
        self.timeout = timeout
        self._post = http_post or self._default_post

    # -- HTTP 底层 ---------------------------------------------------------
    def _default_post(self, url: str, json: Dict[str, Any]) -> Dict[str, Any]:
        try:
            import requests  # type: ignore

            resp = requests.post(url, json=json, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError, ConnectionError) as e:  # type: ignore
            raise ConnectionError_(f"向量服务不可达: {e}") from e
        except Exception as e:  # pragma: no cover - 依赖/格式异常
            raise ConnectionError_(f"向量服务请求失败: {e}") from e

    # -- SensorPort ---------------------------------------------------------
    def embed(self, text: str) -> List[float]:
        if not text or not text.strip():
            raise CapacityError("embed: 空文本")
        try:
            data = self._post(
                self.api_url, {"input": text, "model": self.model}
            )
        except (ConnectionError, OSError):
            raise ConnectionError_(f"向量服务不可达: {self.api_url}")
        try:
            vec = [float(x) for x in data["data"][0]["embedding"]]
        except (KeyError, IndexError, TypeError, ValueError) as e:  # pragma: no cover
            raise ConnectionError_(f"向量响应格式异常: {e}") from e
        if self._dim and len(vec) != self._dim:
            logger.warning("向量维度 %d != 期望 %d", len(vec), self._dim)
            self._dim = len(vec)
        return vec

    def batch_embed(self, texts: List[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]

    def health(self) -> bool:
        try:
            data = self._post(self.api_url, {"input": "health_check", "model": self.model})
            return bool(data.get("data"))
        except Exception:
            return False

    @property
    def dim(self) -> int:
        return self._dim
