"""整合记忆服务（services 层前端）。

在三个 Port（感官 - 向量 / 认知 - 沙漏 + LMS / 应用 - 丰碑）之上，
提供面向业务的整合能力：

  - :meth:`store`   —— 聚合写入：向量化 + 沙漏 + LMS，返回统一 MemoryItem。
  - :meth:`recall`  —— 协同检索：文本(0.3) + 向量(0.5) + LMS激活(0.2) 加权融合。
  - :meth:`contribute` —— 从沙漏召回提炼知识并贡献到丰碑。
  - :meth:`get_status` —— 各后端健康状态聚合。

设计（高内聚低耦合）：
  - 本类不 import 具体适配器，只依赖构造注入的抽象 Port。
  - 通过 ``Registry`` 或手工传入 SandglassAdapter/LMSAdapter/VectorAdapter/
    MonumentAdapter 组装。
  - 业务排序逻辑（融合/打分）集中在服务层，便于单测。
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

from interfaces.base import CognitivePort, SensorPort, ApplicationPort
from interfaces.exceptions import AdapterError
from interfaces.models import ContributionResult, MemoryItem

logger = __import__("logging").getLogger(__name__)

# 检索融合权重（可覆盖）
DEFAULT_WEIGHTS = {"text": 0.3, "vector": 0.5, "lms": 0.2}

# 回魂快照：巡检/心跳噪音行（沙漏自治巡检 self_pulse 等，无信息量）
_SOUL_NOISE_RE = re.compile(
    r"self_pulse|守夜|巡检|画像漂移|heartbeat|无待办时", re.I)


def _dedupe_preserve_order(seq: List[str]) -> List[str]:
    """按首次出现顺序去重（字符串列表）。"""
    seen = set()
    out = []
    for s in seq:
        s = (s or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _filter_soul_noise(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """过滤巡检/心跳噪音行并去重（保序）。"""
    seen = set()
    out = []
    for it in items:
        text = (it.get("text") or "").strip()
        if not text or text in seen:
            continue
        if _SOUL_NOISE_RE.search(text):
            continue
        seen.add(text)
        out.append(it)
    return out


class IntegratedMemoryService:
    """整合记忆服务：统一入口（前端编排层）。"""

    def __init__(
        self,
        sandglass: Optional[CognitivePort] = None,
        lms: Optional[CognitivePort] = None,
        vector: Optional[SensorPort] = None,
        monument: Optional[ApplicationPort] = None,
    ):
        self.sandglass = sandglass
        self.lms = lms
        self.vector = vector
        self.monument = monument

        # 对构造缺省项做安全校验，方便 TDD 尽早暴露装配错误
        if not any(
            p is not None for p in (self.sandglass, self.lms, self.vector, self.monument)
        ):
            raise AdapterError("IntegratedMemoryService: 未注入任何后端 Port")

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def store(self, text: str, source: str = "chat") -> MemoryItem:
        """聚合写入：向量化 + 沙漏 + LMS。

        Returns:
            携带向量与（若 LMS 回填）熵/惊讶度的 MemoryItem。
        """
        item = MemoryItem(text=text, origin=source, system="integration")
        if not item.id:
            import uuid
            item.id = uuid.uuid4().hex

        if self.vector is not None:
            try:
                item.vector = self.vector.embed(text)
            except Exception as e:  # pragma: no cover - 网络
                logger.warning("向量化失败，继续其它后端: %s", e)

        ok_sandglass = True
        if self.sandglass is not None:
            ok_sandglass = bool(self.sandglass.store(item))
        # 自述回写防御（反思回流，2026-08-05）：
        # source=self_ref 的条目（自述入库）不得回注 LMS —— 否则每次入库都会
        # 经 LMS /chat 触发新一轮反思 → 再蒸馏 → 再入库 → 死循环。
        # 自述只落沙漏/向量（持久可检索），绝不喂回 LMS 塑形。
        if self.lms is not None and item.origin != "self_ref":
            self.lms.store(item)

        if not ok_sandglass and self.vector is None and self.lms is None:
            raise AdapterError("store: 所有后端写入均失败")
        logger.debug("store 完成: %s", item.id)
        return item

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def recall(
        self, query: str, k: int = 10, weights: Optional[Dict[str, float]] = None
    ) -> List[MemoryItem]:
        """协同检索：文本(0.3) + 向量(0.5) + LMS激活(0.2) 加权融合排序。

        Returns:
            按综合得分降序的记忆列表。
        """
        weights = {**DEFAULT_WEIGHTS, **(weights or {})}

        # 1. 沙漏文本召回（无命中直接返回空）
        if self.sandglass is None:
            return []
        sandglass_results = self.sandglass.recall(query, k)
        if not sandglass_results:
            return []

        # 2. 查询向量
        q_vec = None
        if self.vector is not None:
            try:
                q_vec = self.vector.embed(query)
            except Exception as e:  # pragma: no cover - 网络
                logger.warning("查询向量化失败: %s", e)

        # 3. LMS 激活信息
        lms_data: Dict[str, Any] = {}
        recalled_texts: List[str] = []
        surprise = 0.0
        if self.lms is not None:
            try:
                ctx = self.lms.fetch_context(query)  # type: ignore[attr-defined]
                mc = ctx.get("memory_context", "") if isinstance(ctx, dict) else ""
                from interfaces.adapters.lms_adapter import parse_lms_context

                lms_data = parse_lms_context(mc)
                recalled_texts = lms_data.get("recalled", [])
                surprise = lms_data.get("surprise", 0.0)
            except Exception as e:  # pragma: no cover - 任意后端异常
                logger.warning("LMS 激活信息获取失败: %s", e)

        # 4. 逐条打分
        surprise_norm = min(surprise / 20.0, 1.0)
        has_recall = bool(recalled_texts)
        scored = []
        for item in sandglass_results:
            vec_score = 0.0
            if q_vec is not None:
                if not item.vector and self.vector is not None:
                    try:
                        item.vector = self.vector.embed(item.text)
                    except Exception:  # pragma: no cover
                        item.vector = None
                if item.vector:
                    vec_score = max(0.0, self._cosine(q_vec, item.vector))
            txt_score = self._text_score(query, item.text)

            # LMS 激活：命中回忆给满分，否则用惊讶度弱激励
            lms_activation = 0.0
            if has_recall:
                for r in recalled_texts:
                    if r and (r in item.text or item.text in r):
                        lms_activation = 1.0
                        break
            if lms_activation == 0.0:
                lms_activation = surprise_norm * 0.5

            total = (
                weights["text"] * txt_score
                + weights["vector"] * vec_score
                + weights["lms"] * lms_activation
            )

            item.metadata.setdefault("scores", {
                "text": round(txt_score, 3),
                "vector": round(vec_score, 3),
                "lms_activation": round(lms_activation, 3),
                "total": round(total, 3),
            })
            item.metadata.setdefault("lms", {
                "surprise": round(surprise, 3),
                "entropy": round(lms_data.get("entropy", 0.0), 3),
                "active_node_count": len(lms_data.get("active_nodes", [])),
                "recalled_texts": recalled_texts,
            })
            scored.append((total, item))

        # 5. 降序排序，截取前 k
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [item for _, item in scored[:k]]
        logger.debug(
            "协同检索: %s -> %d条 (LMS激活节点%d, 惊讶度%.2f)",
            query, len(results), len(lms_data.get("active_nodes", [])), surprise,
        )
        return results

    def get_self_ref_voice(self, limit: int = 5) -> List[str]:
        """读取 LMS 自指回路最近自述（反思回流）。失败返回 []（fail-open）。"""
        if self.lms is None:
            return []
        try:
            return self.lms.fetch_self_ref(limit=limit)  # type: ignore[attr-defined]
        except Exception as e:  # pragma: no cover - 任意后端异常
            logger.warning("self_ref 读取失败: %s", e)
            return []

    # ------------------------------------------------------------------
    # 回魂快照（会话醒来自我状态，2026-08-05）
    # ------------------------------------------------------------------
    def get_soul_snapshot(
        self, limit: int = 5, recent_n: int = 5
    ) -> Dict[str, Any]:
        """回魂快照：LMS 自述 + 记忆状态指标 + 沙漏最近记忆。

        只读、fail-open、防循环：
          - 仅 GET（LMS /self-ref/voice、/status/{sid}；沙漏 txt 尾部读）
          - 绝不调用 LMS /chat（不触发反思/回注/新记忆产生）
          - 任一后端失败只缺字段，不抛异常

        Returns:
            ``{"ok": True, "ts": float, "session_id": str,
              "lms_voice": [str], "lms_state": {…}, "recent": [{text,ts}]}``
        """
        import time as _time

        snap: Dict[str, Any] = {"ok": True, "ts": _time.time()}
        snap["session_id"] = getattr(self.lms, "session_id", "main") if self.lms else "main"

        # 1. LMS 最近自述（复用 /self-ref/voice）
        snap["lms_voice"] = self.get_self_ref_voice(limit=limit)
        # 去重（蒸馏缓存会产出连续重复自述，保留顺序取首现）
        snap["lms_voice"] = _dedupe_preserve_order(snap["lms_voice"])[:limit]

        # 2. LMS 状态指标（复用 /status/{sid}，只读 GET）
        snap["lms_state"] = {}
        if self.lms is not None:
            fetcher = getattr(self.lms, "fetch_status", None)
            if callable(fetcher):
                try:
                    snap["lms_state"] = fetcher() or {}
                except Exception as e:  # pragma: no cover - 任意后端异常
                    logger.warning("回魂 lms_state 读取失败: %s", e)
                    snap["lms_state"] = {}

        # 3. 沙漏最近记忆（沙漏原生 recent，按时间倒序）
        snap["recent"] = []
        if self.sandglass is not None:
            rec = getattr(self.sandglass, "recent", None)
            if callable(rec):
                try:
                    items = rec(recent_n) or []
                    snap["recent"] = [
                        {
                            "text": getattr(it, "text", "") or "",
                            "ts": getattr(it, "created_at", None),
                            "origin": getattr(it, "origin", "") or "",
                        }
                        for it in items[:recent_n]
                    ]
                    # 过滤巡检/心跳噪音 + 去重（沙漏自治巡检每 5min 一条，会淹没回魂段）
                    snap["recent"] = _filter_soul_noise(snap["recent"])[:recent_n]
                except Exception as e:  # pragma: no cover - 任意后端异常
                    logger.warning("回魂 recent 读取失败: %s", e)
                    snap["recent"] = []

        return snap


    # ------------------------------------------------------------------
    # 应用层：知识贡献
    # ------------------------------------------------------------------
    def contribute(self, topic: str, k: int = 5, **kwargs) -> ContributionResult:
        """从沙漏召回结果中提炼知识，贡献到丰碑。

        1. 按 topic 从沙漏召回 k 条记忆；
        2. 无显式正文 kwargs 时，将召回结果拼装成知识文本；
        3. 调用丰碑 Port.contribute() 写入 candidates/ 并铸造。
        """
        if self.monument is None:
            raise AdapterError("contribute: 未注入丰碑后端")

        knowledge = kwargs.pop("knowledge", None)
        if not knowledge:
            items = self.sandglass.recall(topic, k) if self.sandglass else []
            if not items:
                knowledge = topic
            else:
                knowledge = "\n".join(f"- {it.text}" for it in items[:k])

        title = kwargs.pop("title", None) or f"{topic} 记忆提炼"
        tags = kwargs.pop("tags", None) or ["memory_refined", topic]
        return self.monument.contribute(knowledge=knowledge, title=title, tags=tags, **kwargs)

    # ------------------------------------------------------------------
    # 状态聚合
    # ------------------------------------------------------------------
    def get_status(self) -> Dict[str, Any]:
        """聚合各后端健康状态。"""
        status: Dict[str, Any] = {}

        if self.sandglass is not None:
            try:
                st = self.sandglass.get_state()
                status["sandglass"] = {"memory_count": st.memory_count, "healthy": True}
            except Exception as e:  # pragma: no cover
                status["sandglass"] = {"healthy": False, "error": str(e)}

        if self.lms is not None:
            try:
                st = self.lms.get_state()
                status["lms"] = {"memory_count": st.memory_count, "healthy": True}
            except Exception as e:  # pragma: no cover
                status["lms"] = {"healthy": False, "error": str(e)}

        if self.vector is not None:
            status["vector"] = {"healthy": bool(self.vector.health())}

        if self.monument is not None:
            degraded = bool(getattr(self.monument, "degraded", False))
            status["monument"] = {"degraded": degraded}

        return status

    # ------------------------------------------------------------------
    # 工具方法（纯函数，便于单测）
    # ------------------------------------------------------------------
    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        """余弦相似度（空向量返回 0）。"""
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    @staticmethod
    def _text_score(query: str, text: str) -> float:
        """文本匹配分：查询关键词命中结果正文的比例（0~1）。"""
        if not query or not text:
            return 0.0
        tokens = set(re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+", query.lower()))
        if not tokens:
            return 1.0 if query.lower() in text.lower() else 0.0
        text_l = text.lower()
        hit = sum(1 for t in tokens if t in text_l)
        return hit / len(tokens)
