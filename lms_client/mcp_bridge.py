#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lms_client.mcp_bridge — 把 lms_client 包装成 MCP 工具集（stdio 桥）。

供 openclaw / codex / workbody 等平台以标准 MCP 方式复用同一套 SDK：
    lms_recall(query, session_id, k)       只读检索记忆
    lms_store(text, session_id)            存储文本/对话进记忆
    lms_status(session_id)                 查询会话记忆状态
    lms_dream(session_id, steps, full_cycle) 触发做梦（记忆巩固）
    lms_snapshot(session_id)               保存会话快照
（别名 recall_memory / store_memory / get_status / dream 指向同一实现，
与任务规格及历史工具名对齐。）

实现：**零依赖最小 MCP stdio 服务器**（newline-delimited JSON-RPC 2.0）。
MCP stdio 传输 = stdin/stdout 各一行一个 JSON 消息（无 Content-Length 帧）；
实现 initialize / notifications/initialized / ping / tools/list / tools/call，
足以被任意标准 MCP 客户端（openclaw、codex CLI、claude 等）拉起。

用法：
    python3 lms_client/mcp_bridge.py [--base-url http://127.0.0.1:8190]
                                     [--token TOKEN] [--client-id lms-mcp]
    # codex 注册：
    codex mcp add lms -- python3 /vol2/1000/AI专用/memory-integration-layer/lms_client/mcp_bridge.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional

# 直接执行引导（必须在导入 lms_client 之前）：支持 `python3 lms_client/mcp_bridge.py`。
# 同时把脚本所在目录（包目录）移出 sys.path——否则包内 http.py 会以顶层
# 模块名 `http` 遮蔽 stdlib http 包（httpx/urllib 内部 import http.client 即炸）。
if __package__ in (None, ""):
    _pkg_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(_pkg_dir))
    sys.path = [p for p in sys.path if os.path.abspath(p) != _pkg_dir]

from lms_client.http import DEFAULT_BASE_URL, LMSClient, LmsError
from lms_client import __version__

__all__ = ["TOOL_SPECS", "handle_message", "serve_stdio", "main"]

SERVER_NAME = "lms-client-mcp"
SERVER_VERSION = __version__
PROTOCOL_VERSION = "2024-11-05"


# ─────────────────────────── 工具定义 ───────────────────────────

def _spec(name: str, description: str,
          properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


# 会话命名约定（总体方案 §3.7）：main=毛毛本体 / bus=总线降噪 /
# openclaw-* / codex-* / workbody-* 各平台独立大脑。
_SID_DESC = "会话 ID（默认 main=毛毛本体；codex 用 codex-*、workbody 用 workbody-*）"

TOOL_SPECS: List[Dict[str, Any]] = [
    _spec(
        "lms_recall",
        "从 LMS 活体记忆系统检索记忆（只读，不产生新记忆、不调 LLM）。"
        "当你需要回忆与主人之前的对话/命名/约定时调用。",
        {
            "query": {"type": "string", "description": "检索查询文本"},
            "session_id": {"type": "string", "description": _SID_DESC,
                           "default": "main"},
            "k": {"type": "integer", "description": "返回条数 1-20",
                  "default": 5, "minimum": 1, "maximum": 20},
        },
        ["query"],
    ),
    _spec(
        "lms_store",
        "把一段文本/一轮对话存储进 LMS 记忆系统（写入路径，会推进该会话轮次）。"
        "重要对话、约定、事实请调用本工具落库。",
        {
            "text": {"type": "string", "description": "要存储的文本"},
            "session_id": {"type": "string", "description": _SID_DESC,
                           "default": "main"},
        },
        ["text"],
    ),
    _spec(
        "lms_status",
        "查询 LMS 会话记忆状态（轮次/熵/惊讶度/缓冲/LLM 开关等）。",
        {
            "session_id": {"type": "string", "description": _SID_DESC,
                           "default": "main"},
        },
        [],
    ),
    _spec(
        "lms_dream",
        "触发 LMS 做梦（记忆巩固/整合，空闲时调用；期间该会话对话会等待）。",
        {
            "session_id": {"type": "string", "description": _SID_DESC,
                           "default": "main"},
            "steps": {"type": "integer", "description": "做梦步数",
                      "default": 20, "minimum": 1},
            "full_cycle": {"type": "boolean",
                           "description": "是否执行完整七阶段做梦周期",
                           "default": False},
        },
        [],
    ),
    _spec(
        "lms_snapshot",
        "保存 LMS 会话快照到磁盘（持久化，建议重要节点后调用）。",
        {
            "session_id": {"type": "string", "description": _SID_DESC,
                           "default": "main"},
        },
        [],
    ),
]

# 任务规格别名（与历史 MCP 工具名对齐，指向同一实现）
_TOOL_ALIASES = {
    "recall_memory": "lms_recall",
    "store_memory": "lms_store",
    "get_status": "lms_status",
    "dream": "lms_dream",
}

ALL_TOOL_SPECS = list(TOOL_SPECS) + [
    _spec(alias, f"[别名] 同 {canonical}（跨平台工具名一致）",
          next(s["inputSchema"]["properties"]
               for s in TOOL_SPECS if s["name"] == canonical),
          next(s["inputSchema"]["required"]
               for s in TOOL_SPECS if s["name"] == canonical))
    for alias, canonical in _TOOL_ALIASES.items()
]


# ─────────────────────────── 工具实现 ───────────────────────────

def _h_recall(client: LMSClient, args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query", "") or "")
    if not query.strip():
        raise ValueError("query 不能为空")
    result = client.recall(
        query,
        k=int(args.get("k", 5) or 5),
        session_id=str(args.get("session_id", "main") or "main"),
    )
    return result.to_dict()


def _h_store(client: LMSClient, args: Dict[str, Any]) -> Dict[str, Any]:
    text = str(args.get("text", "") or "")
    if not text.strip():
        raise ValueError("text 不能为空")
    data = client.store(text, session_id=str(args.get("session_id", "main") or "main"))
    memory_state = data.get("memory_state") or {}
    return {
        "stored": True,
        "session_id": data.get("session_id", "main"),
        "turn_count": memory_state.get("turn_count", 0),
    }


def _h_status(client: LMSClient, args: Dict[str, Any]) -> Dict[str, Any]:
    return client.status(
        session_id=str(args.get("session_id", "main") or "main")).to_dict()


def _h_dream(client: LMSClient, args: Dict[str, Any]) -> Dict[str, Any]:
    return client.dream(
        session_id=str(args.get("session_id", "main") or "main"),
        steps=int(args.get("steps", 20) or 20),
        full_cycle=bool(args.get("full_cycle", False)),
    )


def _h_snapshot(client: LMSClient, args: Dict[str, Any]) -> Dict[str, Any]:
    return client.snapshot(
        session_id=str(args.get("session_id", "main") or "main"))


def _resolve_handler(name: str) -> Optional[Callable[[LMSClient, Dict[str, Any]], Any]]:
    canonical = _TOOL_ALIASES.get(name, name)
    return {
        "lms_recall": _h_recall,
        "lms_store": _h_store,
        "lms_status": _h_status,
        "lms_dream": _h_dream,
        "lms_snapshot": _h_snapshot,
    }.get(canonical)


# ─────────────────────────── JSON-RPC 处理 ───────────────────────────

def _send(msg: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle_message(client: LMSClient, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理单条 JSON-RPC 消息；通知类返回 None（无需响应）。"""
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        params = msg.get("params") or {}
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": (params.get("protocolVersion")
                                    or PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"tools": ALL_TOOL_SPECS}}
    if method == "tools/call":
        return _handle_tools_call(client, msg_id, msg.get("params") or {})
    return {
        "jsonrpc": "2.0", "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _handle_tools_call(client: LMSClient, msg_id: Any,
                       params: Dict[str, Any]) -> Dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    handler = _resolve_handler(name)
    if handler is None:
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32602, "message": f"Unknown tool: {name}"},
        }
    try:
        result = handler(client, args)
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "content": [
                    {"type": "text",
                     "text": json.dumps(result, ensure_ascii=False)},
                ],
                "isError": False,
            },
        }
    except ValueError as e:
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32602, "message": str(e)},
        }
    except LmsError as e:
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": f"[lms] {e}"}],
                "isError": True,
            },
        }
    except Exception as e:  # noqa: BLE001 — MCP 边界兜底
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32603, "message": f"Internal error: {e}"},
        }


def serve_stdio(client: LMSClient) -> int:
    """阻塞读 stdin，逐行处理 MCP 消息直到 EOF。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "Parse error"}})
            continue
        try:
            resp = handle_message(client, msg)
        except Exception as e:  # noqa: BLE001 — 单条消息异常不退出
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            _send(resp)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lms-mcp",
        description="LMS 活体记忆系统 MCP stdio 桥（零依赖）",
    )
    parser.add_argument("--base-url", default=os.environ.get("LMS_BASE_URL")
                        or DEFAULT_BASE_URL, help="LMS 服务地址")
    parser.add_argument("--token", default=os.environ.get("LMS_TOKEN"),
                        help="认证 token（可选）")
    parser.add_argument("--client-id", default="lms-mcp",
                        help="X-Client-Id 标识")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="统一超时秒数（默认 10）")
    args = parser.parse_args(argv)

    client = LMSClient(base_url=args.base_url, token=args.token,
                       timeout=args.timeout, client_id=args.client_id)
    return serve_stdio(client)


if __name__ == "__main__":
    sys.exit(main())
