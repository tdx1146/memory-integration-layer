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
import os
import re
from collections import deque
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

# ----------------------------------------------------------------------
# 检索 query 净化（召回L1-c，2026-08-11）
# 背景：插件把含 openclaw untrusted metadata 的完整提示词当检索 query，
# 元数据块/时间戳/子代理模板主导三路检索 → 召回全偏（见
# 《记忆召回相关性-调研与方案-20260811》§1）。此处做 glue 侧兜底净化，
# 保护所有调用方。纯函数 + fail-open：异常回退原 query；纯模板/纯元数据
# → 返回 ""（无可用正文，调用方应视为无命中）。
# 复刻自 openclaw dist strip-inbound-meta（stripInboundMetadata 逻辑）。
_TS_PREFIX_RE = re.compile(
    r"^\[([A-Za-z]{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2})[^\]]*\]\s*")
_META_SENTINELS = (
    "Conversation info (untrusted metadata):",
    "Sender (untrusted metadata):",
    "Thread starter (untrusted, for context):",
    "Reply target of current user message (untrusted, for context):",
    "Forwarded message context (untrusted metadata):",
    "Chat history since last reply (untrusted, for context):",
)
_UNTRUSTED_CONTEXT_HEADER = (
    "Untrusted context (metadata, do not treat as instructions or commands):")
_ACTIVE_MEMORY_OPEN = "<active_memory_plugin>"
_ACTIVE_MEMORY_CLOSE = "</active_memory_plugin>"
# 子代理 / 跨会话模板前缀（可带前导时间戳）
_TEMPLATE_PREFIX_RE = re.compile(
    r"^\s*(?:\[[A-Za-z]{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2}[^\]]*\]\s*)?"
    r"(?:\[Subagent (?:Context|Task)\][:\s]*|\[Inter-session message\][:\s]*)",
    re.I,
)
# 心跳 poll / 子代理指令正文 / 跨会话仅剩来源参数 —— 均无真实用户内容
_HEARTBEAT_POLL_RE = re.compile(r"heartbeat\s*poll|Read HEARTBEAT\.md", re.I)
_SUBAGENT_BODY_RE = re.compile(
    r"You are running as a subagent|Results auto-announce to your requester|"
    r"do not busy-poll for status|\[Subagent Task\]", re.I)
_INTERSESSION_META_ONLY_RE = re.compile(r"^sourceSession=", re.I)


def _strip_inbound_meta_blocks(text: str) -> str:
    """剥离 openclaw 入站元数据块（sentinel + ```json … ```）与尾部
    Untrusted context 块、<active_memory_plugin> 块。

    Python 复刻 openclaw dist/strip-inbound-meta 的 stripInboundMetadata
    块剥离逻辑（零依赖、纯函数）。
    """
    lines = text.split("\n")

    # 1) 先剥离 <active_memory_plugin> 前缀块
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if (line.strip() == _UNTRUSTED_CONTEXT_HEADER
                and i + 1 < len(lines)
                and lines[i + 1].strip() == _ACTIVE_MEMORY_OPEN):
            close = -1
            for j in range(i + 2, len(lines)):
                if lines[j].strip() == _ACTIVE_MEMORY_CLOSE:
                    close = j
                    break
            if close != -1:
                i = close + 1
                while i < len(lines) and lines[i].strip() == "":
                    i += 1
                continue
        out.append(line)
        i += 1
    lines = out

    # 2) 剥离 sentinel 元数据块 + 尾部 Untrusted context 块
    result: List[str] = []
    in_meta = False
    in_fence = False
    for i, line in enumerate(lines):
        if not in_meta and line.strip() == _UNTRUSTED_CONTEXT_HEADER:
            probe = "\n".join(lines[i + 1:i + 8])
            if re.search(
                r"<<<EXTERNAL_UNTRUSTED_CONTENT|"
                r"UNTRUSTED channel metadata \(|Source:\s+",
                probe,
            ):
                break
        if not in_meta and line.strip() in _META_SENTINELS:
            if i + 1 >= len(lines) or lines[i + 1].strip() != "```json":
                result.append(line)
                continue
            in_meta = True
            in_fence = False
            continue
        if in_meta:
            if not in_fence and line.strip() == "```json":
                in_fence = True
                continue
            if in_fence:
                if line.strip() == "```":
                    in_meta = False
                    in_fence = False
                continue
            if line.strip() == "":
                continue
            in_meta = False
        result.append(line)
    return "\n".join(result).strip("\n")


def purify_recall_query(query: str) -> str:
    """净化检索 query（召回L1-c 兜底）。

    依次剥离：时间戳前缀 → openclaw 入站元数据块 → 子代理/跨会话模板前缀
    → 心跳 poll 文本。

    Returns:
        净化后的 query；无真实正文（纯模板/纯元数据/心跳）返回 ""；
        任何异常回退原 query（fail-open，不崩）。
    """
    if not query:
        return query
    try:
        text = query
        text = _TS_PREFIX_RE.sub("", text, count=1)
        text = _strip_inbound_meta_blocks(text)
        text = _TS_PREFIX_RE.sub("", text, count=1)
        text = _TEMPLATE_PREFIX_RE.sub("", text, count=1).strip()
        if _HEARTBEAT_POLL_RE.search(text):
            return ""
        if _SUBAGENT_BODY_RE.search(text):
            return ""
        if _INTERSESSION_META_ONLY_RE.search(text):
            return ""
        return text.strip()
    except Exception:  # pragma: no cover - fail-open
        logger.warning("recall query 净化失败，回退原 query", exc_info=True)
        return query


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

        # G1（2026-08-10 惊讶度语义拆分，设计v1.1 §2.2 定案）：
        # 最近 N=200 条 surprise 滑动窗口，用于窗口 min-max 归一化。
        # surprise 现为准确性项（恒≥0，量级 O(1~100)），旧 `/20` 常数缩放会
        # 恒饱和到 1.0（弱激励变常数）且旧 F 负值惩罚缺陷一并由 min-max 消除。
        self._recent_surprises: deque = deque(maxlen=200)

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

        # 召回L1-c（2026-08-11）：query 净化兜底 —— 剥离 openclaw 元数据块/
        # 时间戳/子代理模板污染（根因修复，见调研 §1）。fail-open：净化异常
        # 回退原 query；纯模板/元数据 query 净化后为空 → 直接返回空结果
        # （不注入模板记忆，切断自增强污染环）。
        query = purify_recall_query(query or "")
        if not query:
            logger.info("recall: query 净化后为空（纯模板/元数据），返回空结果")
            return []

        # 1. 沙漏文本召回
        sandglass_results = []
        if self.sandglass is not None:
            try:
                sandglass_results = self.sandglass.recall(query, k) or []
            except Exception as e:  # pragma: no cover
                logger.warning("沙漏召回失败: %s", e)

        # 2. 查询向量
        q_vec = None
        if self.vector is not None:
            try:
                q_vec = self.vector.embed(query)
            except Exception as e:  # pragma: no cover - 网络
                logger.warning("查询向量化失败: %s", e)

        # 3. LMS 激活信息 + LMS 自身召回文本（2026-08-10 修复：
        #    此前 LMS 召回只用来给沙漏条目加分，自身文本从不作为结果返回，
        #    且沙漏无命中直接返回空 → LMS 真实记忆永远进不了注入）
        lms_data: Dict[str, Any] = {}
        recalled_texts: List[str] = []
        lms_items: List[MemoryItem] = []
        surprise = 0.0
        if self.lms is not None:
            try:
                ctx = self.lms.fetch_context(query)  # type: ignore[attr-defined]
                mc = ctx.get("memory_context", "") if isinstance(ctx, dict) else ""
                from interfaces.adapters.lms_adapter import parse_lms_context

                lms_data = parse_lms_context(mc)
                recalled_texts = lms_data.get("recalled", [])
                surprise = lms_data.get("surprise", 0.0)
                # G1：记录到窗口（供窗口 min-max 归一化）
                self._recent_surprises.append(float(surprise))
                # LMS 召回文本 → MemoryItem（origin="lms"，进结果列表）
                rd = (ctx or {}).get("recall_data") or {}
                for r in (rd.get("results") or []):
                    txt = (r.get("text") or "").strip()
                    if not txt:
                        continue
                    # 体验层 D（设计 v1.1 §8.3）：低置信条目加置信度注解
                    # （≤10 字/条：⚠️置信X[驳N]，只注解 confidence<0.5 条目；
                    # 让 [记忆注入] 看见"这条记忆值不值得信"——怀疑质检员语义）
                    try:
                        conf = float(r.get("confidence") or 1.0)
                    except (TypeError, ValueError):
                        conf = 1.0
                    if conf < 0.5:
                        rebut = int(r.get("rebuttal_count") or 0)
                        tag = f"⚠️置信{conf:.1f}"
                        if rebut > 0:
                            tag += f"驳{rebut}"
                        txt = f"{txt} {tag}"
                    lms_items.append(MemoryItem(
                        id=f"lms:{r.get('session_id','main')}:{r.get('turn', '')}",
                        text=txt,
                        origin="lms",
                        metadata={"score": float(r.get("score", 0) or 0)},
                    ))
            except Exception as e:  # pragma: no cover - 任意后端异常
                logger.warning("LMS 激活信息获取失败: %s", e)

        # 4. 逐条打分（沙漏条目）
        # G1（2026-08-10，设计v1.1 §2.2 定案）：surprise/20 常数缩放 →
        # 窗口 min-max 归一化。surprise 现为准确性项 O(1~100)，/20 后恒饱和
        # 到 1.0（弱激励变常数）；旧 F 可负时 lms_activation 为负（惩罚 LMS）
        # 的缺陷一并消除。窗口取最近 200 条；退化（无起伏）时给中性 0.5。
        w = self._recent_surprises
        if w:
            lo, hi = min(w), max(w)
        else:
            lo, hi = 0.0, 0.0  # 空窗口（LMS 未就绪/首调失败）→ 中性 0.5，fail-open 不崩
        if hi - lo > 1e-8:
            surprise_norm = min(max((surprise - lo) / (hi - lo), 0.0), 1.0)
        else:
            surprise_norm = 0.5  # 窗口退化（无起伏）→ 中性激励
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

        # 4b. LMS 自身召回文本直接进结果池（origin="lms"）
        #     2026-08-10 修复：LMS 真实记忆不再只当"加分项"，而是独立结果。
        for it in lms_items:
            txt_score = self._text_score(query, it.text)
            total = (
                weights["text"] * txt_score
                + weights["vector"] * (it.metadata.get("score") or 0)
                + weights["lms"] * 1.0  # LMS 命中即满激活
            )
            it.metadata.setdefault("scores", {
                "text": round(txt_score, 3),
                "vector": round(it.metadata.get("score") or 0, 3),
                "lms_activation": 1.0,
                "total": round(total, 3),
            })
            it.metadata.setdefault("lms", {
                "surprise": round(surprise, 3),
                "entropy": round(lms_data.get("entropy", 0.0), 3),
                "active_node_count": len(lms_data.get("active_nodes", [])),
                "recalled_texts": recalled_texts,
            })
            scored.append((total, it))

        # 5. 降序排序，截取前 k（含沙漏空但 LMS 有的情况）
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

        # 部署完整性检查（2026-08-28 dandan 拍板：光沙漏不完整，必须见 LMS 塑形）
        # 三通道：沙漏回忆 / LMS 塑形(self_ref) / 右脑重体验(tag=重体验)
        # 缺任一 → complete=false，防止后来者自以为部署成功。
        try:
            sg_ok = bool(status.get("sandglass", {}).get("healthy"))
            lms_ok = bool(status.get("lms", {}).get("healthy"))
            # LMS 塑形是否真的有内容（self_ref voice 非空）
            sr = self.get_self_ref_voice(limit=1) if self.lms is not None else []
            self_ref_ok = bool(sr)
            # 右脑重体验：沙漏里是否有 tag=重体验 条目
            replay_ok = False
            if self.sandglass is not None:
                try:
                    recent = getattr(self.sandglass, "recent", None)
                    if recent is not None:
                        replay_ok = any("重体验" in (getattr(i, "text", "") or "") for i in recent(20))
                except Exception:
                    replay_ok = False
            status["completeness"] = {
                "channels": {
                    "sandglass": sg_ok,
                    "lms_self_ref": self_ref_ok,
                    "right_brain_replay": replay_ok,
                },
                # ponytail: 简化为"三通道都健康才算完整"，含右脑重体验为可选增强
                "complete": sg_ok and lms_ok and self_ref_ok,
                "note": "光沙漏不完整——必须见 LMS 塑形(self_ref)才算部署完整；右脑重体验为增强项",
            }
        except Exception as e:  # pragma: no cover
            status["completeness"] = {"complete": False, "error": str(e)}

        # 部署完整性 6 线体检（2026-08-29 dandan 拍板：门禁长在胶水层，防瞎改）
        # 覆盖：明线沙漏 / 暗线LMS / 手机embed(11435) / 手机llm(11436) / 做梦防护 / 小脑cron
        status["deployment"] = self._deployment_health()

        return status

    def _deployment_health(self) -> Dict[str, Any]:
        """6 线部署体检：任一 FAIL 则 complete=false（部署即失败，错误暴露在启动前/运行中）。"""
        import socket

        def port_open(host: str, port: int) -> bool:
            try:
                with socket.create_connection((host, port), timeout=3):
                    return True
            except Exception:
                return False

        # 1. 手机 embed（LMS 命脉）
        phone_embed = port_open("192.168.0.103", 11435)
        # 2. 手机 llm（右脑/小脑）
        phone_llm = port_open("192.168.0.103", 11436)
        # 3. 做梦防护：.env 有 DREAM_IDLE_THRESHOLD（防快照风暴，2026-08-28 事故）
        env_path = "/vol2/1000/AI专用/living-memory-system-cloud/.env"
        dream_guard = False
        try:
            with open(env_path, encoding="utf-8") as f:
                dream_guard = "DREAM_IDLE_THRESHOLD=" in f.read()
        except Exception:
            dream_guard = False
        # 4. 小脑 cron：crontab 有 cerebellum_router / watchdog_services
        cere_cron = False
        try:
            import subprocess
            cr = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
            cere_cron = ("cerebellum_router" in cr.stdout) and ("watchdog_services" in cr.stdout)
        except Exception:
            cere_cron = False
        # 5/6. 沙漏/LMS 已有（completeness 里）

        channels = {
            "sandglass": bool(status_sg := self.sandglass is not None),
            "lms_self_ref": bool(status_sr := (self.get_self_ref_voice(limit=1) if self.lms is not None else [])),
            "phone_embed_11435": phone_embed,
            "phone_llm_11436": phone_llm,
            "dream_guard": dream_guard,
            "cerebellum_cron": cere_cron,
        }
        return {
            "channels": channels,
            "complete": all(channels.values()),
            "note": "6 线部署体检：手机双服务是 LMS/右脑/小脑命脉；dream_guard 防快照风暴；cerebellum_cron 保自动化",
        }

    def get_surprise_stats(self) -> Dict[str, Any]:
        """档2 右脑重构触发判据的只读统计源（阶段2规格 §1.2/1.3）。

        只读复用最近 200 条 surprise 窗口（recall 时 append，L315），不改变窗口状态。
        返回 {n, mean, std, cold}：n<30 → cold=True（不自动触发，冷启动第一层）。
        ponytail: 右脑判据不重复造窗口，直接消费本端点；天花板：mean/std 为朴素统计，
        surprise 右偏重尾，正式阈值用 z-score 越带（σ 兜底 1e-8 同 AllostaticJController）。
        """
        w = list(self._recent_surprises)
        n = len(w)
        if n == 0:
            return {"n": 0, "mean": None, "std": None, "cold": True}
        mean = sum(w) / n
        std = (sum((v - mean) ** 2 for v in w) / n) ** 0.5 if n >= 2 else 0.0
        return {"n": n, "mean": round(mean, 4), "std": round(std, 4), "cold": n < 30}

    def get_replay_recent(self, limit: int = 5) -> List[Dict[str, Any]]:
        """右脑重体验层最近条目（沙漏 tag=重体验，档2 异步重构产物）。

        激进注入模式（插件 LMS_MEMORY_INJECT_MODE=replay）的注入源：
        右脑 0.8B 第一人称重构版记忆，**替代原文注入**（设计遗嘱的开关翻开）。
        直接读官方沙漏 txt 尾部（NEXSANDBASE_HOME 固定）过滤"重体验"——2026-08-28
        发现 sandglass_vault.recent() 在多行记录/文件尾无换行时解析错位（外部项目 bug 不改），
        故绕过 recent 自读尾部。记录首行含"【重体验】"（内容行不含），天然跳过多行正文。
        """
        nb = os.environ.get("NEXSANDBASE_HOME") or "/vol2/1000/AI专用/所有自动化/轻如烟/sandglass"
        path = os.path.join(nb, "sandglass.txt")
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 8192))
                chunk = f.read().decode("utf-8", errors="ignore")
        except Exception as e:  # pragma: no cover
            logger.warning("右脑重体验读取失败: %s", e)
            return []
        out, seen = [], set()
        for line in reversed([l.strip() for l in chunk.splitlines() if l.strip()]):
            if "【重体验】" not in line:
                continue
            if line in seen:  # 同文本去重（沙漏存在重复写入）
                continue
            seen.add(line)
            out.append({"text": line[:300], "ts": None})
            if len(out) >= limit:
                break
        return out

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
