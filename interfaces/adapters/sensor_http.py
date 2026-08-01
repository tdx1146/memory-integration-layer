"""感官层适配器：HTTP 嵌入服务（如 bge-m3 独立服务）。

当向量服务以 HTTP API 暴露时使用（POST {base}/embed 返回 {"vector": [...]},
POST {base}/batch_embed 返回 {"vectors": [[...]]}）。

构造不发起网络请求；所有连接在调用时才建立，便于注入 mock 与离线测试。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from interfaces.base import EMBEDDING_DIM, SensorPort


class HttpSensorAdapter(SensorPort):
    """基于 HTTP / JSON 的嵌入服务适配器。

    Args:
        base_url: 服务根地址，如 http://127.0.0.1:8000
        timeout: 请求超时（秒）
        dim: 期望向量维度
        http_get:  依赖注入用的 GET 函数（测试 mock 用）取 (url)->json
        http_post: 依赖注入用的 POST 函数（url, json)->json
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        timeout: float = 10.0,
        dim: int = EMBEDDING_DIM,
        http_get: Optional[Any] = None,
        http_post: Optional[Any] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._dim = dim
        self._get = http_get or self._default_get
        self._post = http_post or self._default_post

    # -- HTTP 底层 ---------------------------------------------------------
    def _session(self):
        try:
            import requests  # type: ignore

            return requests.Session()
        except Exception as e:
            from interfaces.exceptions import ConnectionError_

            raise ConnectionError_(f"HTTP 嵌入服务依赖缺失: {e}")

    def _default_get(self, url: str) -> Dict[str, Any]:
        return self._session().get(url, timeout=self.timeout).json()

    def _default_post(self, url: str, json: Dict[str, Any]) -> Dict[str, Any]:
        return self._session().post(url, json=json, timeout=self.timeout).json()

    # -- SensorPort ----------------------------------------------------------
    def embed(self, text: str) -> List[float]:
        if not text or not text.strip():
            from interfaces.exceptions import CapacityError

            raise CapacityError("embed: 空文本")
        try:
            resp = self._post(f"{self.base_url}/embed", {"text": text})
            vec = resp.get("vector") or resp.get("embedding")
        except Exception as e:
            from interfaces.exceptions import ConnectionError_

            raise ConnectionError_(f"HTTP 嵌入失败: {e}")
        return self._validate(vec)

    def batch_embed(self, texts: List[str]) -> List[List[float]]:
        try:
            resp = self._post(f"{self.base_url}/batch_embed", {"texts": texts})
            vecs = resp.get("vectors") or resp.get("embeddings")
        except Exception as e:
            from interfaces.exceptions import ConnectionError_

            raise ConnectionError_(f"HTTP 批量嵌入失败: {e}")
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            from interfaces.exceptions import InterfaceError

            raise InterfaceError("批量嵌入返回数量与原文本不一致")
        return [self._validate(v) for v in vecs]

    def health(self) -> bool:
        try:
            self._get(f"{self.base_url}/health")
            return True
        except Exception:
            return False

    @property
    def dim(self) -> int:
        return self._dim

    @staticmethod
    def _validate(vec: Any) -> List[float]:
        if not isinstance(vec, list) or not vec or not all(isinstance(x, (int, float)) for x in vec):
            from interfaces.exceptions import InterfaceError

            raise InterfaceError("嵌入返回非法向量")
        return [float(x) for x in vec]
