"""认知层适配器：沙漏真实后端（SQLite 持久化）。

沙漏在架构中负责“显式 / 时间序列记忆”。本适配器相对
``cognitive_sandglass`` 的抽象版本，是不同的实现：它把固定的
SQLite ``sandglass.db`` 直接翻译成 ``CognitivePort`` 的语义。

设计要点（高内聚低耦合）：
  - 只做“SQLite 存取”这一件事，不含业务逻辑。
  - 构造可注入 ``db_path`` 便于测试；连接惰性建立（首次调用才 connect），
    不阻塞导入，也不在构造时强连。
  - 存储 / 检索返回统一 :class:`interfaces.models.MemoryItem`，契合 Port 契约。
  - 失败统一降级：写失败返回 False（由上层决定），读失败返回 []，
    状态失败返回空 CognitiveState —— 不抛跨层异常（fail-open）。
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from typing import List, Optional

from interfaces.base import DummyCognitive, CognitivePort
from interfaces.exceptions import CapacityError
from interfaces.models import CognitiveState, MemoryItem

logger = __import__("logging").getLogger(__name__)

# 沙漏表 schema（与真实沙漏库一致：id/ts/sender/text，ts 为 %Y-%m-%d %H:%M:%S）
_DDL = """
CREATE TABLE IF NOT EXISTS sandglass (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    sender TEXT,
    text TEXT
);
"""


class SandglassAdapter(CognitivePort):
    """基于 SQLite 的沙漏真实后端适配器。

    实现 ``CognitivePort`` 的 store/recall/get_state，数据落盘到固定的
    SQLite 文件。默认路径为 openclaw 沙漏库；可注入便于测试与隔离。
    """

    def __init__(self, db_path: str, fallback: Optional[CognitivePort] = None):
        self.db_path = db_path
        # 注入降级实现（默认内存版）；便于测试彻底离线时使用
        self._fallback = fallback or DummyCognitive()
        self._degraded = False

    # -- 底层访问（惰性建库） -----------------------------------------------
    def _connect(self) -> "sqlite3.Connection":
        """建立（并确保 schema 存在）的 SQLite 连接。失败抛 sqlite3.Error。"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(_DDL)
            return conn
        except Exception as e:  # pragma: no cover - 环境依赖
            self._degraded = True
            raise

    @staticmethod
    def _to_ts(value: Optional[str]) -> float:
        """把 SQLite 的 ``%Y-%m-%d %H:%M:%S`` 字符串还原成 unix 时间戳。"""
        if not value:
            return 0.0
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):  # pragma: no cover
            return 0.0

    @staticmethod
    def _from_ts(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    # -- CognitivePort ------------------------------------------------------
    def store(self, memory: MemoryItem) -> bool:
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sandglass (ts, sender, text) VALUES (?, ?, ?)",
                (
                    self._from_ts(memory.created_at or time.time()),
                    memory.origin or memory.system or "agent",
                    memory.text,
                ),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.warning("沙漏存储失败: %s", e)
            self._degraded = True
            return False

    def recall(self, query: str, k: int = 5) -> List[MemoryItem]:
        if k < 1 or k > 100:
            raise CapacityError(f"recall: k 越界 {k}")
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, ts, sender, text FROM sandglass
                WHERE text LIKE ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                (f"%{query}%", k),
            )
            rows = cur.fetchall()
            conn.close()
            out: List[MemoryItem] = []
            for row_id, ts, sender, text in rows:
                out.append(
                    MemoryItem(
                        id=str(row_id),
                        text=text or "",
                        created_at=self._to_ts(ts),
                        origin=sender or "agent",
                        system="sandglass",
                    )
                )
            return out
        except Exception as e:  # pragma: no cover - 环境依赖
            logger.warning("沙漏检索失败: %s", e)
            self._degraded = True
            return self._fallback.recall(query, k)

    def get_state(self) -> CognitiveState:
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM sandglass")
            count = cur.fetchone()[0]
            conn.close()
            return CognitiveState(memory_count=int(count), stage="sandglass")
        except Exception:  # pragma: no cover - 环境依赖
            self._degraded = True
            return self._fallback.get_state()

    @property
    def degraded(self) -> bool:
        return self._degraded
