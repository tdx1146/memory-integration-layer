#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
胶水层（整合层）HTTP 服务入口 —— 真正打通沙漏 + LMS + 向量 + 丰碑。

把四个真实后端接线进 ``IntegratedMemoryService``，暴露统一 HTTP API，
使插件 / 各系统通过单一入口做协同检索 / 聚合写入 / 状态聚合 / 知识贡献。

统一入口（对应 service 方法）：
    POST /recall        协同检索（文本0.3 + 向量0.5 + LMS激活0.2 加权融合）
    POST /store         聚合写入（向量化 + 沙漏 + LMS）
    POST /status        各后端健康 + 记忆数聚合
    POST /contribute    从沙漏召回提炼知识贡献到丰碑
    GET  /health        存活探针

启动：
    python3 glue_server.py [--port 19000] [--daemon]

依赖：interfaces（胶水层在本仓库根目录）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _load_dotenv(path: str = os.path.join(HERE, ".env")):
    """轻量 .env 加载器（零依赖，Phase 3 D4 新增）。

    只读 SANDGLASS_SOURCE / NEXSANDBASE_HOME / LMS_URL / VECTOR_URL 四项；
    已存在的环境变量优先（shell 显式导出 > .env），不覆盖。
    必须在 import 任何读取环境变量的模块之前调用。
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()  # 必须在下方模块导入之前执行

# 沙漏 txt 主数据目录（sandglass_vault 数据源）
from interfaces.adapters.sandglass_vault_adapter import SANDGLASS_HOME

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

# ────────────────────────── 真实后端路径配置 ──────────────────────────
# 沙漏用 sandglass_vault（txt 数据源），路径由 NEXSANDBASE_HOME 决定
# LMS 活体记忆 HTTP 服务
LMS_URL = os.environ.get("LMS_URL", "http://localhost:8190")
# 向量服务（bge-m3，OpenAI 兼容 /embeddings）
# Phase 3 D4：硬编码内网 IP 已改为读环境变量（.env / shell 均可覆盖）
VECTOR_URL = os.environ.get(
    "VECTOR_URL", "http://192.168.0.103:11435/v1/embeddings"
)

_service = None  # 全局单一 IntegratedMemoryService


def build_service():
    """装配胶水层：沙漏(sandglass_vault txt源) + LMS + 向量 → IntegratedMemoryService。"""
    from interfaces.adapters.sandglass_vault_adapter import SandglassVaultAdapter
    from interfaces.adapters.lms_adapter import LMSAdapter
    from interfaces.adapters.vector_adapter import VectorAdapter
    from interfaces.services.integration_service import IntegratedMemoryService

    # 沙漏走真实 txt 数据源（避免 SQLite 辅助库 + 写锁冲突），读到完整 3457 条
    sandglass = SandglassVaultAdapter()
    lms = LMSAdapter(api_url=LMS_URL, session_id="main")
    vector = VectorAdapter(api_url=VECTOR_URL, model="bge-m3")
    # 丰碑暂以 None 注入（contribute 会提示未接线）；需要时可接 MonumentAdapter
    return IntegratedMemoryService(
        sandglass=sandglass, lms=lms, vector=vector, monument=None
    )


def ensure_service():
    global _service
    if _service is None:
        _service = build_service()
    return _service


class GlueHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 精简日志
        sys.stderr.write(f"[glue] {self.log_date_time_string()} {fmt % args}\n")

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _send(self, code: int, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            svc = ensure_service()
            st = svc.get_status()
            self._send(200, {"status": "ok", "service": "glue-layer", "backends": st, "ts": time.time()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
        svc = ensure_service()
        try:
            if path == "/recall":
                query = body.get("query", "")
                k = int(body.get("k", 8))
                if not query:
                    self._send(400, {"error": "query 必填"})
                    return
                items = svc.recall(query, k=k)
                result = [
                    {
                        "id": it.id,
                        "text": it.text,
                        "origin": it.origin,
                        "system": it.system,
                        "scores": it.metadata.get("scores"),
                        "lms": it.metadata.get("lms"),
                    }
                    for it in items
                ]
                self._send(200, {"query": query, "count": len(result), "results": result})

            elif path == "/store":
                text = body.get("text", "")
                source = body.get("source", "chat")
                if not text:
                    self._send(400, {"error": "text 必填"})
                    return
                item = svc.store(text, source=source)
                self._send(200, {
                    "ok": True,
                    "id": item.id,
                    "vector": item.vector is not None,
                    "entropy": item.entropy,
                    "surprise": item.surprise,
                })

            elif path == "/status":
                st = svc.get_status()
                self._send(200, {"backends": st, "ts": time.time()})

            elif path == "/contribute":
                topic = body.get("topic", "")
                k = int(body.get("k", 5))
                knowledge = body.get("knowledge")
                try:
                    r = svc.contribute(topic=topic, k=k, knowledge=knowledge)
                    self._send(200, {"ok": r.ok, "urn": r.urn, "tag": r.tag, "error": r.error})
                except Exception as e:
                    self._send(200, {"ok": False, "error": f"丰碑未接线或失败: {e}"})

            else:
                self._send(404, {"error": f"unknown path {path}"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    ap = argparse.ArgumentParser(description="胶水层 HTTP 服务")
    ap.add_argument("--port", type=int, default=19000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--daemon", action="store_true", help="后台运行")
    args = ap.parse_args()

    if args.daemon:
        import subprocess
        pid = os.fork()
        if pid > 0:
            print(f"glue_server daemon pid={pid} port={args.port}")
            sys.exit(0)
        os.setsid()

    # 启动前先装配一次，尽早暴露接线错误
    svc = ensure_service()
    print(f"[glue] 胶水层已装配: {svc}")
    print(f"[glue] 后端: 沙漏({SANDGLASS_HOME}) | LMS({LMS_URL}) | 向量({VECTOR_URL})")

    server = HTTPServer((args.host, args.port), GlueHandler)
    print(f"[glue] 胶水层 HTTP 服务: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[glue] 停止")
        server.shutdown()


if __name__ == "__main__":
    main()
