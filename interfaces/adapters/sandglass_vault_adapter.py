"""认知层适配器：沙漏真实后端（sandglass_vault — sandglass.txt 数据源）。

沙漏（NexSandglass）的真正数据层是追加式 ``sandglass.txt`` + ``.idx`` 索引，
由 ``sandglass_vault.py`` 维护（read layer 走 SearchRouter 三层检索 +
    write layer 走 txt 追加）。

本适配器直接复用沙漏原生 ``sandglass_vault``，确保胶水层读到的是
沙漏**真实完整记忆**（约 3457 条），而非 SQLite 辅助库（906 条、且与
sandglass_mcp 存在写锁冲突）。

实现 ``CognitivePort`` 的 store/recall/get_state：
  - recall   -> 调 ``sandglass_vault.search()``（三层路由：影子沙→投石问路→mmap）
  - get_state-> 调 ``sandglass_vault.count()``
  - store    -> 追加一行到 ``sandglass.txt``（沙漏标准写入），并触发索引重建
失败统一 fail-open（读失败返回 []，写失败返回 False）。
"""

from __future__ import annotations

import os
import time
from typing import List, Optional

from interfaces.base import DummyCognitive, CognitivePort
from interfaces.exceptions import CapacityError
from interfaces.models import CognitiveState, MemoryItem

logger = __import__("logging").getLogger(__name__)

# 沙漏源码目录 —— sandglass_vault 所在
SANDGLASS_SOURCE = os.environ.get(
    "SANDGLASS_SOURCE", "/vol2/1000/AI专用/所有自动化/轻如烟/sandglass_source"
)
SANDGLASS_HOME = os.environ.get(
    "NEXSANDBASE_HOME", "/vol2/1000/AI专用/所有自动化/轻如烟/sandglass"
)


class SandglassVaultAdapter(CognitivePort):
    """基于 sandglass_vault（sandglass.txt 数据源）的沙漏真实后端适配器。"""

    def __init__(self, fallback: Optional[CognitivePort] = None):
        self._fallback = fallback or DummyCognitive()
        self._degraded = False
        self._vault = None  # 惰性导入

    # -- 惰性加载 vault -----------------------------------------------------
    def _load_vault(self):
        """惰性 import sandglass_vault；设置 NEXSANDBASE_HOME 后即可用。"""
        if self._vault is not None:
            return self._vault
        import sys

        if SANDGLASS_SOURCE not in sys.path:
            sys.path.insert(0, SANDGLASS_SOURCE)
        if "NEXSANDBASE_HOME" not in os.environ:
            os.environ["NEXSANDBASE_HOME"] = SANDGLASS_HOME
        from sandglass_vault import search, recent, count, rebuild_index

        self._vault = {"search": search, "recent": recent, "count": count, "rebuild_index": rebuild_index}
        return self._vault

    # -- CognitivePort ------------------------------------------------------
    def store(self, memory: MemoryItem) -> bool:
        """追加一行到 sandglass.txt（沙漏标准写入），然后重建索引。"""
        try:
            vault = self._load_vault()
            txt_path = os.path.join(SANDGLASS_HOME, "sandglass.txt")
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(memory.created_at or time.time()))
            sender = memory.origin or memory.system or "agent"
            line = f"{ts} | {sender} | {memory.text}"
            # 追加（避免多进程并发冲突，用 append + flush）
            with open(txt_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
            # 触发索引增量同步/重建（尽力而为）
            try:
                vault["rebuild_index"]()
            except Exception as e:  # pragma: no cover
                logger.warning("沙漏索引重建失败（不影响写入）: %s", e)
            return True
        except Exception as e:
            logger.warning("沙漏(vault)存储失败: %s", e)
            self._degraded = True
            return False

    def recall(self, query: str, k: int = 5) -> List[MemoryItem]:
        if k < 1 or k > 100:
            raise CapacityError(f"recall: k 越界 {k}")
        try:
            vault = self._load_vault()
            results = vault["search"](query, limit=k)
            out: List[MemoryItem] = []
            for line_no, ts, text in results:
                out.append(
                    MemoryItem(
                        id=str(line_no),
                        text=text or "",
                        created_at=self._parse_ts(ts),
                        origin="sandglass",
                        system="sandglass",
                    )
                )
            return out[:k]
        except Exception as e:
            logger.warning("沙漏(vault)检索失败: %s", e)
            self._degraded = True
            return self._fallback.recall(query, k)

    def get_state(self) -> CognitiveState:
        try:
            vault = self._load_vault()
            count = int(vault["count"]() or 0)
            return CognitiveState(memory_count=count, stage="sandglass")
        except Exception:
            self._degraded = True
            return self._fallback.get_state()

    @staticmethod
    def _parse_ts(ts: str) -> float:
        if not ts:
            return 0.0
        try:
            from datetime import datetime

            return datetime.strptime(ts.strip(), "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):  # pragma: no cover
            return 0.0

    @property
    def degraded(self) -> bool:
        return self._degraded
