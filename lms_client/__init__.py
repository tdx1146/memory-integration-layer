# -*- coding: utf-8 -*-
"""lms_client — LMS 活体记忆系统跨平台 SDK（openclaw / codex / workbody 统一接入）。

组成：
    http.py        LMSClient（HTTP 客户端：recall/store/status/dream/snapshot/health）
    models.py      RecallResult / StatusInfo dataclass
    mcp_bridge.py  零依赖 MCP stdio 桥（lms_recall/lms_store/lms_status/lms_dream/lms_snapshot）
    cli.py         `lms` 命令行客户端

版本：0.1.0（2026-08-10，总体方案 §3.7 T2.7）
"""

__version__ = "0.1.0"

from .http import (  # noqa: F401
    DEFAULT_BASE_URL,
    AuthError,
    HttpError,
    LMSClient,
    LmsError,
    TimeoutError,
)
from .models import RecallHit, RecallResult, StatusInfo  # noqa: F401

__all__ = [
    "__version__",
    "LMSClient",
    "LmsError",
    "TimeoutError",
    "AuthError",
    "HttpError",
    "DEFAULT_BASE_URL",
    "RecallHit",
    "RecallResult",
    "StatusInfo",
]
