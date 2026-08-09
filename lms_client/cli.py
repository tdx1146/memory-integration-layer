#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lms_client.cli — LMS 活体记忆系统命令行客户端（跨平台统一入口）。

用法：
    lms [全局参数] recall "关键词" [--k 3] [--session main]
    lms [全局参数] store "要存储的文本" [--session main]
    lms [全局参数] status [--session main]
    lms [全局参数] dream [--session main] [--steps 20] [--full-cycle]
    lms [全局参数] health

全局参数：
    --base-url http://127.0.0.1:8190   （环境变量 LMS_BASE_URL）
    --token TOKEN                       （环境变量 LMS_TOKEN；:8191 管理面必需）
    --client-id lms-cli                 （环境变量 LMS_CLIENT_ID）
    --timeout 10                        （统一超时秒数，默认 10）
    --retries 1                         （失败重试次数，默认 1）
    --json                              机器可读 JSON 输出
    --version

退出码：0 成功 / 1 业务或 HTTP 错误 / 3 认证失败(401/403) / 4 超时。

示例：
    python3 cli.py recall "命名 毛毛" --k 2
    python3 cli.py store "今天和主人确认了跨平台 SDK 方案" --session codex-cli
    python3 cli.py status --session main
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

# 直接执行引导（必须在导入 lms_client 之前）：支持 `python3 cli.py` /
# `python3 lms_client/cli.py`（把仓库根目录加入 sys.path）。
# 同时把脚本所在目录（包目录）移出 sys.path——否则包内 http.py 会以顶层
# 模块名 `http` 遮蔽 stdlib http 包（httpx/urllib 内部 import http.client 即炸）。
if __package__ in (None, ""):
    _pkg_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(_pkg_dir))
    sys.path = [p for p in sys.path if os.path.abspath(p) != _pkg_dir]

from lms_client.http import (
    DEFAULT_BASE_URL,
    AuthError,
    HttpError,
    LMSClient,
    LmsError,
    TimeoutError,
)
from lms_client.models import RecallResult, StatusInfo
from lms_client import __version__

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH = 3
EXIT_TIMEOUT = 4

DEFAULT_SESSION = "main"


# ─────────────────────────── 输出 ───────────────────────────

def _emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _print_recall(r: RecallResult, json_out: bool) -> None:
    if json_out:
        _emit(r.to_dict())
        return
    print(f"会话: {r.session_id} | 查询: {r.query} | "
          f"命中 {r.count} 条 ({r.duration_ms}ms, turn {r.turn_count})")
    for i, hit in enumerate(r.results, 1):
        print(f"{i}. [{hit.score:.3f}] {hit.text}")


def _print_status(s: StatusInfo, json_out: bool) -> None:
    if json_out:
        _emit(s.to_dict())
        return
    meta = s.meta
    print(f"会话: {s.session_id} | turn: {s.turn_count} | "
          f"节点: {s.num_nodes} | 缓冲: {s.episodic_buffer_size} | "
          f"LLM: {'开' if s.llm_enabled else '关'}")
    print(f"熵: {s.last_entropy} | 惊讶度: {s.last_surprise} | "
          f"熵比: {s.entropy_ratio} | 目的一致性: {s.purpose_coherence} | "
          f"precision均值: {s.precision_mean}")
    print(f"meta: update_count={meta.get('update_count')} "
          f"collapse_count={meta.get('collapse_count')} "
          f"lr_multiplier={meta.get('lr_multiplier')} "
          f"surprise_history_len={meta.get('surprise_history_len')}")


def _print_store(data: Dict[str, Any], json_out: bool) -> None:
    memory_state = data.get("memory_state") or {}
    if json_out:
        _emit({
            "stored": True,
            "session_id": data.get("session_id", DEFAULT_SESSION),
            "turn_count": memory_state.get("turn_count", 0),
        })
        return
    print(f"已存储 → 会话: {data.get('session_id', DEFAULT_SESSION)} | "
          f"turn: {memory_state.get('turn_count', 0)}")


def _print_dream(data: Dict[str, Any], json_out: bool) -> None:
    if json_out:
        _emit(data)
        return
    result = data.get("result") or {}
    if isinstance(result, dict):
        summary = {k: v for k, v in result.items()
                   if not isinstance(v, (dict, list))}
        print(f"做梦完成 → 会话: {data.get('session_id', DEFAULT_SESSION)}")
        for k, v in summary.items():
            print(f"  {k}: {v}")
    else:
        print(f"做梦完成 → 会话: {data.get('session_id', DEFAULT_SESSION)} | "
              f"result: {result}")


def _print_health(data: Dict[str, Any], json_out: bool) -> None:
    if json_out:
        _emit(data)
        return
    print(f"健康: {data.get('status')} | 服务: {data.get('service')} | "
          f"版本: {data.get('version')} | 活跃会话: {data.get('active_sessions')} | "
          f"时间: {data.get('timestamp')}")


# ─────────────────────────── 命令分发 ───────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lms",
        description="LMS 活体记忆系统命令行客户端"
                    "（openclaw / codex / workbody 统一接入）",
    )
    parser.add_argument("--base-url",
                        default=os.environ.get("LMS_BASE_URL")
                        or DEFAULT_BASE_URL,
                        help=f"LMS 服务地址（默认 {DEFAULT_BASE_URL}）")
    parser.add_argument("--token", default=os.environ.get("LMS_TOKEN"),
                        help="认证 token（可选；:8191 管理面必需）")
    parser.add_argument("--client-id", default="lms-cli",
                        help="X-Client-Id 标识")
    parser.add_argument("--timeout", type=float,
                        default=float(os.environ.get("LMS_TIMEOUT", "10")),
                        help="统一超时秒数（默认 10）")
    parser.add_argument("--retries", type=int,
                        default=int(os.environ.get("LMS_RETRIES", "1")),
                        help="失败重试次数（默认 1）")
    parser.add_argument("--json", action="store_true", default=None,
                        help="输出 JSON（机器可读；也可放在子命令后）")
    parser.add_argument("--version", action="version",
                        version=f"lms-client {__version__}")

    # 子命令也接受 --json（放在命令后）；SUPPRESS 保证未指定时不覆盖全局值
    _json_parent = argparse.ArgumentParser(add_help=False)
    _json_parent.add_argument("--json", action="store_true",
                              default=argparse.SUPPRESS, dest="json")

    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="{recall,store,status,dream,health}")

    rc = sub.add_parser("recall", help="只读检索记忆", parents=[_json_parent])
    rc.add_argument("query", help="检索关键词")
    rc.add_argument("--k", type=int, default=5, help="返回条数 1-20（默认 5）")
    rc.add_argument("--session", dest="session_id", default=DEFAULT_SESSION,
                    help=f"会话 ID（默认 {DEFAULT_SESSION}）")

    st = sub.add_parser("store", help="存储文本/对话进记忆",
                        parents=[_json_parent])
    st.add_argument("text", help="要存储的文本")
    st.add_argument("--session", dest="session_id", default=DEFAULT_SESSION,
                    help=f"会话 ID（默认 {DEFAULT_SESSION}）")

    ss = sub.add_parser("status", help="查询会话记忆状态",
                        parents=[_json_parent])
    ss.add_argument("--session", dest="session_id", default=DEFAULT_SESSION,
                    help=f"会话 ID（默认 {DEFAULT_SESSION}）")

    dr = sub.add_parser("dream", help="触发做梦（记忆巩固）",
                        parents=[_json_parent])
    dr.add_argument("--session", dest="session_id", default=DEFAULT_SESSION,
                    help=f"会话 ID（默认 {DEFAULT_SESSION}）")
    dr.add_argument("--steps", type=int, default=20, help="做梦步数（默认 20）")
    dr.add_argument("--full-cycle", action="store_true",
                    help="执行完整七阶段做梦周期")

    h = sub.add_parser("health", help="存活探针", parents=[_json_parent])
    return parser


def _run(args: argparse.Namespace) -> int:
    args.json = bool(getattr(args, "json", False))  # 全局/子命令任一位置给定即真
    client = LMSClient(base_url=args.base_url, token=args.token,
                       timeout=args.timeout, retries=args.retries,
                       client_id=args.client_id)
    cmd = args.command
    if cmd == "recall":
        _print_recall(client.recall(args.query, k=args.k,
                                    session_id=args.session_id),
                      json_out=args.json)
    elif cmd == "store":
        _print_store(client.store(args.text, session_id=args.session_id),
                     json_out=args.json)
    elif cmd == "status":
        _print_status(client.status(session_id=args.session_id),
                      json_out=args.json)
    elif cmd == "dream":
        _print_dream(client.dream(session_id=args.session_id,
                                  steps=args.steps,
                                  full_cycle=args.full_cycle),
                     json_out=args.json)
    elif cmd == "health":
        _print_health(client.health(), json_out=args.json)
    else:  # pragma: no cover — argparse required=True 已拦截
        parser = _build_parser()
        parser.error(f"未知命令: {cmd}")
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _run(args)
    except TimeoutError as e:
        print(f"❌ 超时: {e}", file=sys.stderr)
        return EXIT_TIMEOUT
    except AuthError as e:
        print(f"❌ 认证失败: {e}", file=sys.stderr)
        return EXIT_AUTH
    except HttpError as e:
        if e.status_code == 404:
            print(f"❌ 不存在: {e}", file=sys.stderr)
        else:
            print(f"❌ {e}", file=sys.stderr)
        return EXIT_ERROR
    except LmsError as e:
        print(f"❌ {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
