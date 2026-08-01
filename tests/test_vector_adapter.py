"""向量真实后端适配器单元测试（离线，注入 http_post）。"""

import os
import sys

import pytest

_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)

from interfaces.adapters.vector_adapter import VectorAdapter
from interfaces.exceptions import CapacityError, ConnectionError_


def _make(vector_response=None, fail=False, dim=3):
    def post(url, json):
        if fail:
            import requests

            raise requests.exceptions.ConnectionError("refused")
        body = vector_response or {"data": [{"embedding": [1.0, 2.0, 3.0]}], "model": "bge-m3"}
        # 按请求 input 数量生成（单条/批量走同端点）
        if isinstance(json.get("input"), str):
            return body
        return body

    return VectorAdapter(api_url="http://vec.test/v1/embeddings", dim=dim, http_post=post)


class TestVectorAdapter:
    def test_embed_returns_vector(self):
        adap = _make()
        assert adap.embed("你好") == [1.0, 2.0, 3.0]
        assert adap.dim == 3

    def test_batch_embed_returns_vectors(self):
        adap = _make()
        out = adap.batch_embed(["a", "b"])
        assert out == [[1.0, 2.0, 3.0], [1.0, 2.0, 3.0]]

    def test_embed_rejects_empty(self):
        adap = _make()
        with pytest.raises(CapacityError):
            adap.embed("   ")

    def test_embed_connection_error_is_retryable(self):
        adap = _make(fail=True)
        with pytest.raises(ConnectionError_) as exc:
            adap.embed("a")
        assert exc.value.retryable is True

    def test_health_when_up(self):
        assert _make().health() is True

    def test_health_when_down(self):
        assert _make(fail=True).health() is False

    def test_updates_dim_when_mismatch(self):
        adap = _make(vector_response={"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]}, dim=3)
        assert adap.embed("x") == [0.1, 0.2, 0.3, 0.4]
        assert adap.dim == 4  # 动态修正

    def test_bad_format_raises_connection_error(self):
        adap = _make(vector_response={"unexpected": True})
        with pytest.raises(ConnectionError_):
            adap.embed("x")
