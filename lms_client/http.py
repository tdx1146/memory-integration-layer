# -*- coding: utf-8 -*-
"""lms_client.http — LMS 活体记忆系统 HTTP 客户端（SDK 核心）。

数据面 :8190（/recall /chat /status /dream /snapshot /health）与管理面 :8191
（/control/*，X-Control-Token / Authorization: Bearer 认证）的统一客户端。

设计要点（总体方案 §3.7 对齐）：
  - 传输层：httpx 优先（更完整超时/连接语义），缺失时自动回退 stdlib urllib
    ——零强制依赖，openclaw / codex / workbody 任何环境可直接运行；
  - 超时：统一默认 10s（构造参数可覆盖）；按 §3.7 协议规范，单操作默认值
    recall/status 5s、store 10s、dream 30s（均可经 timeout 参数覆盖）；
  - 重试：默认 1 次——仅网络层失败 / 429 / 5xx 触发（幂等安全），
    4xx（含 401/403）不重试；退避 0.5s→1.0s→1.5s；
  - 幂等标识：每个请求携带 X-Request-Id（uuid4 hex）；
  - 异常：LmsError 基类，TimeoutError（超时）/ AuthError（401/403）/
    HttpError（其余 HTTP 错误，携带 status_code 与服务端 detail）。
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

from .models import RecallResult, StatusInfo

try:  # httpx 可用时优先（更完整超时/连接语义）
    import httpx  # type: ignore
    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover — 依赖缺失路径
    httpx = None
    _HAVE_HTTPX = False

__all__ = [
    "LMSClient",
    "LmsError",
    "TimeoutError",
    "AuthError",
    "HttpError",
    "DEFAULT_BASE_URL",
]

DEFAULT_BASE_URL = "http://127.0.0.1:8190"
_VERSION = "0.1.0"
_USER_AGENT = f"lms-client/{_VERSION}"


class LmsError(Exception):
    """LMS 客户端错误基类。"""

    def __init__(self, message: str,
                 status_code: Optional[int] = None,
                 detail: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class TimeoutError(LmsError):
    """请求超时（连接/读取超过 timeout 秒）。

    注意：本模块内遮蔽内置 TimeoutError（语义一致：超时即失败）。
    传输层捕获内置超时（socket.timeout / httpx.TimeoutException）后
    统一归一为本类型抛出；调用方 `except lms_client.http.TimeoutError`
    即可精确捕获超时，与内置超时互不干扰。
    """


class AuthError(LmsError):
    """认证失败（HTTP 401/403）：token 缺失、无效或无权限。"""


class HttpError(LmsError):
    """其他 HTTP 错误（4xx/5xx），携带 status_code 与服务端 detail。"""


def _backoff(attempt: int) -> float:
    """重试退避：0.5s → 1.0s → 1.5s（封顶）。"""
    return min(0.5 * attempt, 1.5)


class LMSClient:
    """LMS 数据面/管理面统一 HTTP 客户端。

    参数:
        base_url:  服务地址（默认 http://127.0.0.1:8190；可用环境变量 LMS_BASE_URL）
        token:     认证 token（可选）。:8191 管理面必需；数据面 :8190 当前无需。
                   可用环境变量 LMS_TOKEN。设置后同时发送 `Authorization: Bearer`
                   与 `X-Control-Token` 两个请求头（兼容两套认证，§3.7 统一协议）。
        timeout:   统一超时秒数（默认 10.0；可用环境变量 LMS_TIMEOUT）
        retries:   失败重试次数（默认 1；仅网络层失败/429/5xx 触发）
        client_id: X-Client-Id 标识（默认 lms-client；可用环境变量 LMS_CLIENT_ID）
    """

    def __init__(self,
                 base_url: Optional[str] = None,
                 token: Optional[str] = None,
                 timeout: Optional[float] = None,
                 retries: int = 1,
                 client_id: Optional[str] = None) -> None:
        self.base_url = (base_url or os.environ.get("LMS_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.token = token if token is not None else os.environ.get("LMS_TOKEN")
        # 统一超时：显式传入或环境变量 LMS_TIMEOUT 才生效；
        # 未设置时走 §3.7 单操作默认值（见 _op_timeout）
        _raw_timeout = (timeout if timeout is not None
                        else os.environ.get("LMS_TIMEOUT"))
        self.timeout: Optional[float] = (
            float(_raw_timeout) if _raw_timeout not in (None, "") else None)
        self.retries = max(0, int(retries if retries is not None
                                   else os.environ.get("LMS_RETRIES", "1")))
        self.client_id = (client_id or os.environ.get("LMS_CLIENT_ID")
                          or "lms-client")

    def _op_timeout(self, explicit: Optional[float],
                    default: float) -> float:
        """解析单操作超时：显式参数 > 客户端统一 timeout > 操作默认值。"""
        if explicit is not None:
            return float(explicit)
        if self.timeout is not None:
            return float(self.timeout)
        return float(default)

    # ─────────────────────────── 公共 API ───────────────────────────

    def health(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """GET /health — 存活探针（默认 3s）。"""
        return self._request("GET", "/health",
                             timeout=self._op_timeout(timeout, 3.0))

    def recall(self, query: str, k: int = 5, session_id: str = "main",
               timeout: Optional[float] = None) -> RecallResult:
        """POST /recall — 只读情景检索（不 process_turn、不调 LLM）。

        session_id 会话不存在时服务端惰性创建空脑（不产生记忆写入）。
        """
        if not query or not str(query).strip():
            raise LmsError("query 不能为空", status_code=400)
        body = {"session_id": session_id, "query": str(query), "k": int(k)}
        data = self._request("POST", "/recall", json_body=body,
                             timeout=self._op_timeout(timeout, 5.0))
        return RecallResult.from_dict(data)

    def store(self, text: str, session_id: str = "main",
              timeout: Optional[float] = None) -> Dict[str, Any]:
        """POST /chat — 存储一轮文本/对话进记忆（写入路径）。

        映射说明：数据面当前无独立 /store 端点，写路径为 POST /chat 且
        llm_output=""——服务端 process_turn(user_input, llm_output="") 将
        文本塑形进记忆（不产 LLM 回复，与 lms_http_mcp.lms_store 同语义）。
        返回完整 ChatResponse（response/memory_context/memory_state/session_id）。
        """
        if not text or not str(text).strip():
            raise LmsError("text 不能为空", status_code=400)
        body = {"session_id": session_id,
                "user_input": str(text),
                "llm_output": ""}
        return self._request("POST", "/chat", json_body=body,
                             timeout=self._op_timeout(timeout, 10.0))

    def status(self, session_id: str = "main",
               timeout: Optional[float] = None) -> StatusInfo:
        """GET /status/{sid} — 查询会话记忆状态。

        会话不存在 → HttpError(404)（detail 含服务端提示）。
        """
        data = self._request("GET", f"/status/{quote(session_id, safe='')}",
                             timeout=self._op_timeout(timeout, 5.0))
        return StatusInfo.from_dict(data)

    def dream(self, session_id: str = "main", steps: int = 20,
              full_cycle: bool = False,
              timeout: Optional[float] = None) -> Dict[str, Any]:
        """POST /dream/{sid} — 触发做梦（记忆巩固/整合）。

        做梦可能耗时较长，默认超时 30s（§3.7 做梦规范）；完整七阶段周期
        （full_cycle=True）建议显式给更大 timeout。
        会话不存在时服务端自动创建。
        """
        body = {"steps": int(steps), "full_cycle": bool(full_cycle)}
        return self._request("POST", f"/dream/{quote(session_id, safe='')}",
                             json_body=body,
                             timeout=self._op_timeout(timeout, 30.0))

    def snapshot(self, session_id: str = "main",
                 timeout: Optional[float] = None) -> Dict[str, Any]:
        """POST /snapshot/{sid} — 保存会话快照（落盘）。"""
        return self._request(
            "POST", f"/snapshot/{quote(session_id, safe='')}",
            json_body={}, timeout=self._op_timeout(timeout, 10.0))

    # ─────────────────────────── 请求内核 ───────────────────────────

    def _headers(self, has_body: bool) -> Dict[str, str]:
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "X-Client-Id": self.client_id,
            "X-Request-Id": uuid.uuid4().hex,
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        if self.token:
            # 双头兼容：:8191 管理面认 X-Control-Token 与 Bearer 二选一
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-Control-Token"] = self.token
        return headers

    def _request(self, method: str, path: str, *,
                 json_body: Optional[Dict[str, Any]] = None,
                 timeout: Optional[float] = None,
                 retries: Optional[int] = None) -> Dict[str, Any]:
        """带重试策略的请求外壳。返回 JSON dict。

        重试仅针对：网络层失败（连接拒绝/DNS/超时）与 429/5xx；
        4xx（含 401/403）立即抛错不重试。
        """
        timeout = self.timeout if timeout is None else float(timeout)
        max_retries = self.retries if retries is None else max(0, int(retries))
        url = f"{self.base_url}{path}"
        headers = self._headers(has_body=json_body is not None)

        attempt = 0
        while True:
            attempt += 1
            try:
                status, payload = self._request_once(
                    method, url, headers, json_body, timeout)
            except LmsError as e:
                # 网络层失败 / 超时：可重试（幂等安全）
                if attempt <= max_retries:
                    time.sleep(_backoff(attempt))
                    continue
                raise

            if status < 400:
                return payload

            detail = self._extract_detail(payload, status)
            if status in (401, 403):
                raise AuthError(
                    f"认证失败（HTTP {status}）: {detail}",
                    status_code=status, detail=detail)
            if status == 429 or status >= 500:
                # 服务端过载/暂不可用：重试一次（尊重 Retry-After 若有）
                if attempt <= max_retries:
                    time.sleep(_backoff(attempt))
                    continue
                raise HttpError(
                    f"服务端错误（HTTP {status}）: {detail}",
                    status_code=status, detail=detail)
            raise HttpError(
                f"请求失败（HTTP {status}）: {detail}",
                status_code=status, detail=detail)

    def _request_once(self, method: str, url: str, headers: Dict[str, str],
                      json_body: Optional[Dict[str, Any]],
                      timeout: float) -> Tuple[int, Dict[str, Any]]:
        """单次 HTTP 往返。返回 (status_code, payload_dict)。

        网络层/超时错误抛 LmsError 子类（TimeoutError / LmsError）；
        HTTP 状态码一律返回由 _request 统一处理（不在此抛）。
        """
        if _HAVE_HTTPX:
            return self._request_httpx(method, url, headers, json_body, timeout)
        return self._request_urllib(method, url, headers, json_body, timeout)

    def _request_httpx(self, method, url, headers, json_body, timeout):
        with httpx.Client(timeout=timeout) as client:
            try:
                resp = client.request(
                    method, url, headers=headers,
                    json=json_body if json_body is not None else None)
            except httpx.TimeoutException as e:
                raise TimeoutError(
                    f"请求超时（>{timeout}s）: {method} {url} — {e}") from e
            except httpx.HTTPError as e:
                raise LmsError(f"网络错误: {method} {url} — {e}") from e
            return resp.status_code, self._parse_body(resp.text)

    def _request_urllib(self, method, url, headers, json_body, timeout):
        import urllib.error
        import urllib.request

        data = None
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, self._parse_body(raw)
        except socket.timeout as e:
            raise TimeoutError(
                f"请求超时（>{timeout}s）: {method} {url} — {e}") from e
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            return e.code, self._parse_body(raw)
        except urllib.error.URLError as e:
            raise LmsError(f"网络错误: {method} {url} — {e.reason}") from e

    @staticmethod
    def _parse_body(raw: str) -> Dict[str, Any]:
        """解析响应体；非 JSON 时返回空 dict（fail-open，不阻塞调用方）。"""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"_raw": parsed}
        except json.JSONDecodeError:
            return {"_raw": raw[:500]}

    @staticmethod
    def _extract_detail(payload: Dict[str, Any], status: int) -> str:
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if detail is not None:
                return str(detail)
        raw = payload.get("_raw") if isinstance(payload, dict) else None
        if raw:
            return str(raw)
        return f"（服务端无 detail，HTTP {status}）"
