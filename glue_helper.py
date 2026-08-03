#!/usr/bin/env python3
"""胶水层薄 helper —— AgentOS 插件的统一记忆入口（B 方案接入载体）。

设计原则（dandan 铁律：永远不要临时方案）：
  - 插件（JS）无法直接 import Python 包，此薄脚本作为唯一桥接层。
  - 内部复用 glue_server.build_service() 的同一套装配（沙漏 txt 权威源 +
    LMS + 向量），确保读写走的是胶水层 IntegratedMemoryService 的**唯一确定链路**，
    不另起炉灶、不依赖脆弱 agent_end 事件。
  - 暴露三个子命令：recall / store / status。stdout 输出 JSON，供 Node 解析。

用法：
  python3 glue_helper.py status
  python3 glue_helper.py recall "<query>" [k]
  python3 glue_helper.py store "<text>" "<source>"
"""

from __future__ import annotations

import json
import sys

# 让 workspace 可被 import（glue_server 位于 workspace 根）
import os
_WORKSPACE = os.path.dirname(os.path.abspath(__file__))
if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)

from glue_server import build_service, ensure_service  # noqa: E402


def cmd_status() -> int:
    svc = ensure_service()
    try:
        st = svc.get_status()
    except Exception as e:  # 聚合状态，单项失败不应整体崩
        st = {"error": str(e)}
    print(json.dumps(st, ensure_ascii=False, default=str))
    return 0


def cmd_recall(query: str, k: int = 3) -> int:
    svc = ensure_service()
    try:
        results = svc.recall(query, k=k)
    except Exception as e:
        # fail-open：召回失败不阻塞注入，返回空并带 error 字段
        print(json.dumps({"error": str(e), "results": []}, ensure_ascii=False))
        return 0
    out = []
    for r in results:
        out.append({
            "text": getattr(r, "text", None),
            "created_at": getattr(r, "created_at", None),
            "origin": getattr(r, "origin", None),
            "system": getattr(r, "system", None),
            "entropy": getattr(r, "entropy", None),
            "surprise": getattr(r, "surprise", None),
            "score": getattr(r, "score", None),
        })
    print(json.dumps({"results": out}, ensure_ascii=False, default=str))
    return 0


def cmd_store(text: str, source: str = "agent") -> int:
    svc = ensure_service()
    try:
        item = svc.store(text, source=source)
    except Exception as e:
        # fail-open：存储失败仅记录，不抛给插件崩溃
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 0
    print(json.dumps({"ok": True, "id": getattr(item, "id", None),
                      "system": getattr(item, "system", None)}, ensure_ascii=False))
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    try:
        if cmd == "status":
            return cmd_status()
        if cmd == "recall":
            if len(argv) < 2:
                print(json.dumps({"error": "recall 需要 query"})); return 1
            k = int(argv[2]) if len(argv) > 2 else 3
            return cmd_recall(argv[1], k)
        if cmd == "store":
            if len(argv) < 2:
                print(json.dumps({"error": "store 需要 text"})); return 1
            source = argv[2] if len(argv) > 2 else "agent"
            return cmd_store(argv[1], source)
        print(json.dumps({"error": f"未知命令 {cmd}"}, ensure_ascii=False)); return 1
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False)); return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
