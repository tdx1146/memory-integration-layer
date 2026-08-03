"""自我怀疑账本适配器 —— doubt_episode 行为账本（SQLite 持久化）。

背景（规划：自我怀疑系统 P1.2）：
  为"自我怀疑系统"建立行为账本——每次怀疑闭环写一条记录，月度统计
  注入 persona（习惯奖励回路）。这是横切机制：L0 怀疑灯亮起 → 闭环
  后写账本 → 月末统计驱动阈值自适应。

设计要点（高内聚低耦合，对齐 SandglassAdapter 风格）：
  - 只做"SQLite 存取 + schema 校验"这一件事，不含业务逻辑。
  - 独立模块，不塞进现有 adapter；上层只调三个接口，不直接碰 SQLite：
      * store_doubt(episode_dict) -> bool   写入（fail-open，异常静默）
      * query_doubt(filter)      -> list[dict] 查询
      * monthly_stats()          -> dict     月度统计（供 persona 注入）
  - 构造可注入 ``db_path`` 便于测试；连接惰性建立（首次调用才 connect），
    不阻塞导入，也不在构造时强连。
  - 写路径不阻塞主流程：任何异常静默降级，返回 False（fail-open）。
  - 单写者：模块级共享实例 + 模块级便捷函数；``configure()`` 可重设
    （测试/运维注入隔离库）。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 默认账本路径（规划文件清单：沙漏数据目录）
DEFAULT_DB_PATH = "/vol1/@team/qh团队/QH/AI专用/所有自动化/轻如烟/sandglass/doubt.db"

# 枚举口径（与规划一致；SQLite CHECK 兜底把关）
TRIGGER_TYPES = ("conflict", "stakes", "fok", "surprise", "novelty", "user_correction")
USER_REACTIONS = ("corrected", "acknowledged", "silent", "rejected")

_DDL = """
CREATE TABLE IF NOT EXISTS doubt_episode (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    t REAL NOT NULL,
    trigger_type TEXT NOT NULL
        CHECK (trigger_type IN ('conflict','stakes','fok','surprise','novelty','user_correction')),
    suspicion TEXT,
    queried INTEGER NOT NULL DEFAULT 0,
    answer_changed INTEGER NOT NULL DEFAULT 0,
    overturn_evidence TEXT,
    user_reaction TEXT
        CHECK (user_reaction IN ('corrected','acknowledged','silent','rejected')),
    topic TEXT,
    confidence_after REAL
        CHECK (confidence_after IS NULL OR (confidence_after >= 0.0 AND confidence_after <= 1.0))
);
CREATE INDEX IF NOT EXISTS idx_doubt_t ON doubt_episode(t);
CREATE INDEX IF NOT EXISTS idx_doubt_trigger ON doubt_episode(trigger_type);
CREATE INDEX IF NOT EXISTS idx_doubt_topic ON doubt_episode(topic);
"""

# -- 字段规范化 --------------------------------------------------------------


def _to_bool(value: Any) -> int:
    """宽松布尔归一：truthy -> 1，其余 -> 0（存储为 INTEGER）。"""
    if isinstance(value, str):
        return 1 if value.strip().lower() in ("1", "true", "yes", "是", "对") else 0
    return 1 if value else 0


def _to_float(value: Any, default: float, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        f = max(lo, f)
    if hi is not None:
        f = min(hi, f)
    return f


def _norm_episode(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把调用方 dict 规范化为 schema 对齐的写入行（宽松容错）。

    未知/缺省字段给安全默认值；枚举合法性交给 SQLite CHECK 把关
    （不合法则写失败返回 False，fail-open 不抛异常）。
    """
    ep: Dict[str, Any] = dict(raw or {})
    ca_raw = ep.get("confidence_after")
    if ca_raw in (None, ""):
        confidence_after = None
    else:
        try:
            confidence_after = max(0.0, min(1.0, float(ca_raw)))
        except (TypeError, ValueError):
            confidence_after = None  # 无法解析 -> 存 NULL，不阻塞写
    return {
        "t": _to_float(ep.get("t"), time.time()),
        "trigger_type": str(ep.get("trigger_type") or "").strip(),
        "suspicion": str(ep.get("suspicion") or ""),
        "queried": _to_bool(ep.get("queried")),
        "answer_changed": _to_bool(ep.get("answer_changed")),
        "overturn_evidence": str(ep.get("overturn_evidence") or ""),
        "user_reaction": str(ep.get("user_reaction") or "").strip(),
        "topic": str(ep.get("topic") or ""),
        "confidence_after": confidence_after,
    }


# -- 适配器 -----------------------------------------------------------------


class DoubtAdapter:
    """自我怀疑账本适配器（SQLite 持久化，fail-open）。

    三个对外接口：``store_doubt`` / ``query_doubt`` / ``monthly_stats``。
    上层永远通过这三个接口读写，不直接操作 SQLite。
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._degraded = False

    # -- 底层访问（惰性建库） ------------------------------------------------
    def _connect(self) -> "sqlite3.Connection":
        """建立（并确保 schema 存在）的 SQLite 连接。失败抛 sqlite3.Error。"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            conn.executescript(_DDL)
            return conn
        except Exception as e:  # pragma: no cover - 环境依赖
            self._degraded = True
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            raise

    @staticmethod
    def _close(conn: Optional["sqlite3.Connection"]) -> None:
        """安全关闭连接：先回滚未决事务（释放写锁），再 close。"""
        if conn is None:
            return
        try:
            conn.rollback()  # 失败写入后残留的 RESERVED 锁必须显式释放
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    # -- 对外接口 ------------------------------------------------------------
    def store_doubt(self, episode: Dict[str, Any]) -> bool:
        """写一条怀疑闭环记录。任何异常静默降级（fail-open），返回 bool。

        单条写入即提交，保证单写者语义下记录不丢；写失败不影响主流程。
        无论成功失败，连接都会在 finally 中安全关闭（避免残留写锁）。
        """
        conn = None
        try:
            row = _norm_episode(episode)
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO doubt_episode
                    (t, trigger_type, suspicion, queried, answer_changed,
                     overturn_evidence, user_reaction, topic, confidence_after)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["t"],
                    row["trigger_type"],
                    row["suspicion"],
                    row["queried"],
                    row["answer_changed"],
                    row["overturn_evidence"],
                    row["user_reaction"],
                    row["topic"],
                    row["confidence_after"],
                ),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.warning("doubt 账本写入失败（fail-open）: %s", e)
            self._degraded = True
            return False
        finally:
            self._close(conn)

    def query_doubt(self, filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """按条件查询怀疑闭环记录，返回 dict 列表（含 id）。

        filter 支持键（均可选）：
          - trigger_type: str | list[str]  触发类型（多值取并集）
          - topic: str                      主题模糊匹配（LIKE）
          - user_reaction: str              用户反应
          - month: str                      "YYYY-MM"（本地时区整月窗口）
          - t_from / t_to: float            时间戳区间
          - queried: bool                   是否已查询过
          - limit: int                      返回条数上限（默认 50，最大 500）
        默认按时间倒序（最新在前）。读失败返回 []（fail-open）。
        """
        f = dict(filter or {})
        where: List[str] = []
        args: List[Any] = []

        tt = f.get("trigger_type")
        if tt:
            values = tt if isinstance(tt, (list, tuple)) else [tt]
            if values:
                marks = ",".join("?" for _ in values)
                where.append(f"trigger_type IN ({marks})")
                args.extend(str(v) for v in values)

        topic = f.get("topic")
        if topic:
            where.append("topic LIKE ?")
            args.append(f"%{topic}%")

        ur = f.get("user_reaction")
        if ur:
            where.append("user_reaction = ?")
            args.append(str(ur))

        month = f.get("month")
        if month:
            start, end = _month_window(str(month))
            where.append("t >= ? AND t < ?")
            args.extend((start, end))

        if f.get("t_from") is not None:
            where.append("t >= ?")
            args.append(float(f["t_from"]))
        if f.get("t_to") is not None:
            where.append("t < ?")
            args.append(float(f["t_to"]))

        if f.get("queried") is not None:
            where.append("queried = ?")
            args.append(_to_bool(f["queried"]))

        limit = int(f.get("limit", 50))
        if limit < 1:
            limit = 1
        if limit > 500:
            limit = 500

        sql = "SELECT id, t, trigger_type, suspicion, queried, answer_changed, overturn_evidence, user_reaction, topic, confidence_after FROM doubt_episode"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY t DESC LIMIT ?"
        args.append(limit)

        try:
            conn = self._connect()
            rows = conn.execute(sql, args).fetchall()
            return [_row_to_dict(r) for r in rows]
        except Exception as e:  # pragma: no cover - 环境依赖
            logger.warning("doubt 账本查询失败（fail-open）: %s", e)
            self._degraded = True
            return []
        finally:
            self._close(conn)

    def monthly_stats(self) -> Dict[str, Any]:
        """月度统计（本地时区），供 persona 习惯奖励回路注入。

        返回形如::

            {
              "total": 12,
              "months": {
                "2026-08": {
                  "total": 8,
                  "by_trigger": {"conflict": 3, "user_correction": 2, ...},
                  "by_reaction": {"corrected": 2, "silent": 4, ...},
                  "queried": 5,
                  "answer_changed": 3,
                  "overturned": 2,          # answer_changed 且 reaction=corrected
                  "avg_confidence_after": 0.72,
                },
                ...
              },
              "latest_month": "2026-08",
              "latest": {...}               # latest_month 的明细
            }

        读失败返回 {}（fail-open，不阻塞主流程）。
        """
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT t, trigger_type, queried, answer_changed, user_reaction, confidence_after FROM doubt_episode"
            ).fetchall()
        except Exception as e:  # pragma: no cover - 环境依赖
            logger.warning("doubt 账本统计失败（fail-open）: %s", e)
            self._degraded = True
            return {}
        finally:
            self._close(conn)

        months: Dict[str, Dict[str, Any]] = {}
        total = 0
        for t, tt, queried, ac, ur, ca in rows:
            total += 1
            m = datetime.fromtimestamp(t).strftime("%Y-%m")
            b = months.setdefault(
                m,
                {
                    "total": 0,
                    "by_trigger": {},
                    "by_reaction": {},
                    "queried": 0,
                    "answer_changed": 0,
                    "overturned": 0,
                    "conf_sum": 0.0,
                    "conf_n": 0,
                },
            )
            b["total"] += 1
            b["by_trigger"][tt] = b["by_trigger"].get(tt, 0) + 1
            b["by_reaction"][ur or "unknown"] = b["by_reaction"].get(ur or "unknown", 0) + 1
            if queried:
                b["queried"] += 1
            if ac:
                b["answer_changed"] += 1
            if ac and ur == "corrected":
                b["overturned"] += 1
            if ca is not None:
                b["conf_sum"] += ca
                b["conf_n"] += 1

        out_months: Dict[str, Dict[str, Any]] = {}
        for m, b in months.items():
            out_months[m] = {
                "total": b["total"],
                "by_trigger": b["by_trigger"],
                "by_reaction": b["by_reaction"],
                "queried": b["queried"],
                "answer_changed": b["answer_changed"],
                "overturned": b["overturned"],
                "avg_confidence_after": round(b["conf_sum"] / b["conf_n"], 4) if b["conf_n"] else None,
            }

        latest = max(out_months) if out_months else None
        result: Dict[str, Any] = {
            "total": total,
            "months": out_months,
            "latest_month": latest,
        }
        if latest:
            result["latest"] = out_months[latest]
        return result

    @property
    def degraded(self) -> bool:
        return self._degraded


# -- 工具 --------------------------------------------------------------------


def _row_to_dict(row: tuple) -> Dict[str, Any]:
    return {
        "id": row[0],
        "t": row[1],
        "trigger_type": row[2],
        "suspicion": row[3],
        "queried": bool(row[4]),
        "answer_changed": bool(row[5]),
        "overturn_evidence": row[6],
        "user_reaction": row[7],
        "topic": row[8],
        "confidence_after": row[9],
    }


def _month_window(month: str) -> tuple:
    """把 'YYYY-MM' 转成本地时区 [月初, 下月初) 的 unix 时间戳区间。"""
    try:
        start = datetime.strptime(month, "%Y-%m")
    except ValueError:
        now = datetime.now()
        start = datetime(now.year, now.month, 1)
    if start.month == 12:
        end = datetime(start.year + 1, 1, 1)
    else:
        end = datetime(start.year, start.month + 1, 1)
    return start.timestamp(), end.timestamp()


# -- 模块级便捷入口（单写者共享实例） ----------------------------------------


_shared: Optional[DoubtAdapter] = None


def _shared_adapter() -> DoubtAdapter:
    global _shared
    if _shared is None:
        _shared = DoubtAdapter()
    return _shared


def store_doubt(episode_dict: Dict[str, Any]) -> bool:
    """写一条怀疑闭环记录（共享实例，fail-open）。"""
    return _shared_adapter().store_doubt(episode_dict)


def query_doubt(filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """按条件查询怀疑闭环记录（共享实例）。"""
    return _shared_adapter().query_doubt(filter)


def monthly_stats() -> Dict[str, Any]:
    """月度统计（共享实例），供 persona 注入。"""
    return _shared_adapter().monthly_stats()


def configure(db_path: Optional[str] = None) -> DoubtAdapter:
    """重设共享实例（测试/运维注入隔离库）。返回新实例。"""
    global _shared
    _shared = DoubtAdapter(db_path)
    return _shared
