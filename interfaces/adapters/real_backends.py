#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实后端适配器实现

将现有系统（沙漏、LMS、向量服务）接入到统一接口层。
"""

import sqlite3
import json
import math
import re
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime

import requests

from interfaces.base import SensorPort, CognitivePort, ApplicationPort
from interfaces.exceptions import AdapterError as BackendError, CapacityError
from interfaces.models import ContributionResult
from memory_model import MemoryItem

logger = logging.getLogger(__name__)


class RealSandglassAdapter(CognitivePort):
    """沙漏真实后端适配器"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._check_connection()

    def _check_connection(self):
        """检查数据库连接"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.close()
            logger.info(f"沙漏数据库连接成功: {self.db_path}")
        except Exception as e:
            raise BackendError(f"沙漏数据库连接失败: {e}")

    def store(self, memory: MemoryItem) -> bool:
        """存储到沙漏"""
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()

            # 获取当前最大ID
            c.execute('SELECT MAX(id) FROM sandglass')
            max_id = c.fetchone()[0]
            new_id = (max_id or 0) + 1

            # 插入新记录
            c.execute('''
                INSERT INTO sandglass (id, timestamp, sender, text)
                VALUES (?, ?, ?, ?)
            ''', (
                new_id,  # INTEGER主键
                datetime.fromtimestamp(memory.timestamp).strftime('%Y-%m-%d %H:%M:%S'),
                memory.source,
                memory.text
            ))

            conn.commit()
            conn.close()
            logger.debug(f"沙漏存储成功: {new_id}")
            return True
        except Exception as e:
            logger.error(f"沙漏存储失败: {e}")
            return False

    def recall(self, query: str, k: int = 10) -> List[MemoryItem]:
        """从沙漏检索"""
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()

            # FTS5全文检索
            c.execute('''
                SELECT id, timestamp, sender, text
                FROM sandglass
                WHERE text LIKE ?
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (f'%{query}%', k))

            results = []
            for row in c.fetchall():
                try:
                    ts = datetime.strptime(row[1], '%Y-%m-%d %H:%M:%S').timestamp()
                except:
                    ts = 0.0

                results.append(MemoryItem(
                    id=str(row[0]),  # 转为字符串
                    text=row[3],
                    timestamp=ts,
                    source=row[2]
                ))

            conn.close()
            logger.debug(f"沙漏检索: {query} -> {len(results)}条")
            return results
        except Exception as e:
            logger.error(f"沙漏检索失败: {e}")
            return []

    def get_state(self) -> Dict[str, Any]:
        """获取沙漏状态"""
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM sandglass')
            count = c.fetchone()[0]
            conn.close()
            return {
                'backend': 'sandglass',
                'count': count,
                'healthy': True
            }
        except Exception as e:
            return {
                'backend': 'sandglass',
                'count': 0,
                'healthy': False,
                'error': str(e)
            }


class RealLMSAdapter(CognitivePort):
    """LMS真实后端适配器"""

    def __init__(self, api_url: str = "http://localhost:8190"):
        self.api_url = api_url.rstrip('/')
        self._check_connection()

    def _check_connection(self):
        """检查API连接"""
        try:
            resp = requests.get(f"{self.api_url}/health", timeout=5)
            if resp.status_code == 200:
                logger.info(f"LMS服务连接成功: {self.api_url}")
            else:
                raise BackendError(f"LMS服务状态异常: {resp.status_code}")
        except Exception as e:
            raise BackendError(f"LMS服务连接失败: {e}")

    def store(self, memory: MemoryItem) -> bool:
        """存储到LMS（通过/chat接口）"""
        try:
            resp = requests.post(
                f"{self.api_url}/chat",
                json={
                    'user_input': memory.text,
                    'session_id': memory.source or 'default'
                },
                timeout=30
            )

            if resp.status_code == 200:
                data = resp.json()
                # 提取LMS状态
                state = data.get('memory_state', {})
                memory.entropy = state.get('last_entropy')
                memory.surprise = state.get('last_surprise')
                memory.purposefulness = state.get('purpose_coherence')
                logger.debug(f"LMS存储成功: {memory.id}")
                return True
            else:
                logger.error(f"LMS存储失败: {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"LMS存储异常: {e}")
            return False

    def fetch_context(self, query: str, session_id: str = "main") -> Dict[str, Any]:
        """通过/chat接口获取LMS的memory_context（激活信息）"""
        try:
            resp = requests.post(
                f"{self.api_url}/chat",
                json={'user_input': query, 'session_id': session_id},
                timeout=30
            )
            if resp.status_code != 200:
                logger.error(f"LMS fetch_context失败: {resp.status_code}")
                return {}
            return resp.json()
        except Exception as e:
            logger.error(f"LMS fetch_context异常: {e}")
            return {}

    def recall(self, query: str, k: int = 10) -> List[MemoryItem]:
        """从LMS检索（返回激活的节点信息）"""
        # LMS的检索是通过/chat返回的memory_context
        # 这里返回空列表，实际检索通过store()的返回值获取
        return []

    def get_state(self) -> Dict[str, Any]:
        """获取LMS状态"""
        try:
            resp = requests.get(f"{self.api_url}/health", timeout=5)
            data = resp.json()
            return {
                'backend': 'lms',
                'status': data.get('status', 'unknown'),
                'active_sessions': data.get('active_sessions', 0),
                'healthy': True
            }
        except Exception as e:
            return {
                'backend': 'lms',
                'healthy': False,
                'error': str(e)
            }


class RealVectorAdapter(SensorPort):
    """向量服务真实适配器"""

    def __init__(self, api_url: str = "http://192.168.0.103:11435/v1/embeddings",
                 model: str = "bge-m3"):
        self.api_url = api_url
        self.model = model
        self._check_connection()

    def _check_connection(self):
        """检查向量服务连接"""
        try:
            resp = requests.post(
                self.api_url,
                json={'input': 'test', 'model': self.model},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                dim = len(data['data'][0]['embedding'])
                logger.info(f"向量服务连接成功: {self.api_url}, 维度={dim}")
            else:
                raise BackendError(f"向量服务状态异常: {resp.status_code}")
        except Exception as e:
            raise BackendError(f"向量服务连接失败: {e}")

    def embed(self, text: str) -> List[float]:
        """生成文本向量"""
        try:
            resp = requests.post(
                self.api_url,
                json={'input': text, 'model': self.model},
                timeout=30
            )

            if resp.status_code == 200:
                data = resp.json()
                return data['data'][0]['embedding']
            else:
                logger.error(f"向量生成失败: {resp.status_code}")
                return []
        except Exception as e:
            logger.error(f"向量生成异常: {e}")
            return []

    def batch_embed(self, texts: List[str]) -> List[List[float]]:
        """批量生成向量"""
        return [self.embed(text) for text in texts]

    def dim(self) -> int:
        """返回向量维度"""
        return 1024  # bge-m3固定1024维

    def health(self) -> Dict[str, Any]:
        """检查健康状态"""
        try:
            resp = requests.post(
                self.api_url,
                json={'input': 'health_check', 'model': self.model},
                timeout=10
            )
            if resp.status_code == 200:
                return {
                    'backend': 'vector',
                    'status': 'ok',
                    'healthy': True
                }
            else:
                return {
                    'backend': 'vector',
                    'healthy': False,
                    'status_code': resp.status_code
                }
        except Exception as e:
            return {
                'backend': 'vector',
                'healthy': False,
                'error': str(e)
            }


class RealMonumentAdapter(ApplicationPort):
    """丰碑真实后端适配器。

    将 workspace/monument/monument_core.MonumentCore 包装成 ApplicationPort。
    提供知识贡献（提候选 + 批准铸造）、P2P 同步、主题查询。
    丰碑核心不可用时，降级到 DummyApplication（不崩溃）。
    """

    def __init__(self, monument_dir: Optional[str] = None):
        self.monument_dir = monument_dir
        self._core = None
        self._dummy = None
        self._degraded = False
        self._lazy_load()

    # -- 底层访问 -----------------------------------------------------------
    def _lazy_load(self):
        """惰性加载丰碑核心；失败则降级到内存账本。"""
        try:
            from monument.monument_core import MonumentCore  # type: ignore

            self._core = MonumentCore(self.monument_dir) if self.monument_dir else MonumentCore()
            self._degraded = False
        except Exception as e:
            logger.warning(f"丰碑核心加载失败，降级到内存账本: {e}")
            self._core = None
            self._degraded = True

    def _fallback(self):
        if self._dummy is None:
            from interfaces.base import DummyApplication

            self._dummy = DummyApplication()
        return self._dummy

    # -- ApplicationPort：贡献知识 -------------------------------------------
    def contribute(self, knowledge: str, **kwargs) -> ContributionResult:
        """贡献一条知识到丰碑。

        kwargs 支持: title, tags/tag, contributor, source, references,
        conditions, approver, note。
        """
        if not knowledge or not knowledge.strip():
            return ContributionResult(ok=False, error="empty knowledge")
        if self._core is None:
            self._lazy_load()
        if self._core is None:
            return self._fallback().contribute(knowledge, **kwargs)
        try:
            tags = kwargs.get("tags")
            if isinstance(tags, str):
                tags = [tags]
            if not tags and kwargs.get("tag"):
                tags = [kwargs.get("tag")]
            tags = tags or ["standard"]

            candidate = self._core.create_candidate(
                title=kwargs.get("title") or (knowledge[:40] or "untitled"),
                body=knowledge,
                tags=tags,
                contributor=kwargs.get("contributor", "agent:unknown"),
                source=kwargs.get("source", {"type": "real_backends_adapter"}),
                references=kwargs.get("references"),
                conditions=kwargs.get("conditions"),
            )
            if not isinstance(candidate, dict):
                return ContributionResult(ok=False, error="create_candidate 返回格式异常")

            candidate_id = candidate.get("candidate_id")
            try:
                block = self._core.approve_candidate(
                    candidate_id=candidate_id,
                    approver=kwargs.get("approver", "system:interfaces"),
                    note=kwargs.get("note"),
                )
            except Exception:
                # 批准失败（如缺批准权限/链异常）时，候选仍在 candidates/ 留存，
                # 返回候选 ID 以便上层感知“已写入候选区”。
                return ContributionResult(
                    ok=False,
                    urn=f"candidate:{candidate_id}",
                    error="approve_candidate 失败，候选已留存",
                    extra={"candidate_id": candidate_id},
                )

            if isinstance(block, dict):
                content = block.get("content") or {}
                urn = content.get("urn") if isinstance(content, dict) else block.get("urn")
                return ContributionResult(
                    ok=True,
                    urn=urn or block.get("urn"),
                    tag=",".join(tags),
                    extra={"candidate_id": candidate_id, "block_index": block.get("header", {}).get("index")},
                )
            urn = block.urn if hasattr(block, "urn") else None
            return ContributionResult(ok=True, urn=urn, tag=",".join(tags))
        except Exception as e:
            self._degraded = True
            return ContributionResult(ok=False, error=str(e))

    # -- ApplicationPort：P2P 同步 -------------------------------------------
    def sync_monuments(self) -> List[str]:
        """与丰碑网络同步，返回当前可见条目的 urn 列表。"""
        if self._core is None:
            self._lazy_load()
        if self._core is None:
            return self._fallback().sync_monuments()
        try:
            if hasattr(self._core, "sync_nodes"):
                return [str(x) for x in (self._core.sync_nodes() or [])]
            items = self._core.read_all() if hasattr(self._core, "read_all") else []
            urns = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                content = it.get("content")
                urn = content.get("urn") if isinstance(content, dict) else it.get("urn")
                if urn:
                    urns.append(str(urn))
            return urns
        except Exception as e:
            self._degraded = True
            return self._fallback().sync_monuments()

    # -- ApplicationPort：主题查询 -------------------------------------------
    def query_monument(self, topic: str, k: int = 5) -> List[str]:
        """按主题在丰碑中检索，返回命中的知识文本。"""
        if not topic or not (1 <= k <= 100):
            raise CapacityError("query_monument: 参数越界")
        if self._core is None:
            self._lazy_load()
        if self._core is None:
            return self._fallback().query_monument(topic, k)
        try:
            t = topic.lower()
            hits: List[str] = []
            if hasattr(self._core, "read_by_tag"):
                try:
                    for d in self._core.read_by_tag(topic) or []:
                        text = self._block_text(d)
                        if t in text.lower():
                            hits.append(text.strip())
                except Exception:
                    pass
            if len(hits) < k and hasattr(self._core, "read_all"):
                try:
                    for d in self._core.read_all() or []:
                        text = self._block_text(d)
                        if t in text.lower():
                            hits.append(text.strip())
                except Exception:
                    pass
            seen, out = set(), []
            for h in hits:
                if h not in seen:
                    seen.add(h)
                    out.append(h)
            return out[:k]
        except Exception as e:
            self._degraded = True
            return self._fallback().query_monument(topic, k)

    @staticmethod
    def _block_text(block) -> str:
        """从丰碑区块/候选提取可搜索文本。"""
        if not isinstance(block, dict):
            return ""
        content = block.get("content")
        if isinstance(content, dict):
            title = content.get("title") or ""
            body = content.get("body") or ""
            tags = " ".join(content.get("tags") or [])
            return f"{title}\n{body}\n{tags}"
        return str(content or "")

    @property
    def degraded(self) -> bool:
        return self._degraded


class IntegratedMemoryService:
    """整合记忆服务：统一入口"""

    def __init__(self,
                 sandglass_db: str = "/vol2/1000/AI专用/所有自动化/轻如烟/sandglass/sandglass.db",
                 lms_url: str = "http://localhost:8190",
                 vector_url: str = "http://192.168.0.103:11435/v1/embeddings",
                 vector_model: str = "bge-m3",
                 monument_dir: Optional[str] = None):
        """初始化真实后端"""

        # 初始化三个适配器
        self.sandglass = RealSandglassAdapter(sandglass_db)
        self.lms = RealLMSAdapter(lms_url)
        self.vector = RealVectorAdapter(vector_url, vector_model)
        # 应用层：丰碑网络
        self.monument = RealMonumentAdapter(monument_dir)

        logger.info("整合记忆服务初始化完成")

    def store(self, text: str, source: str = "chat") -> MemoryItem:
        """存储到所有后端"""
        import time
        import uuid

        # 创建MemoryItem
        item = MemoryItem(
            id=uuid.uuid4().hex,
            text=text,
            timestamp=time.time(),
            source=source
        )

        # 生成向量
        item.vector = self.vector.embed(text)

        # 存储到沙漏
        self.sandglass.store(item)

        # 存储到LMS（同时获取状态）
        self.lms.store(item)

        logger.info(f"存储完成: {item.id}")
        return item

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        """余弦相似度"""
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    @staticmethod
    def _parse_lms_context(memory_context: str) -> Dict[str, Any]:
        """解析LMS memory_context字符串，提取激活信息。

        返回:
            {
                'active_nodes': List[Dict{'id','strength'}],
                'surprise': float,
                'entropy': float,
                'recalled': List[str]  # 相关记忆回忆列表
            }
        """
        result = {
            'active_nodes': [],
            'surprise': 0.0,
            'entropy': 0.0,
            'recalled': [],
        }
        if not memory_context:
            return result

        # 提取激活节点: 节点5(强:0.659), 节点36(中:0.593)...
        nodes = re.findall(
            r'节点(\d+)\((强|中|弱)?:?([0-9.]+)\)',
            memory_context
        )
        strength_map = {'强': 1.0, '中': 0.6, '弱': 0.3}
        for node_id, grade, score in nodes:
            try:
                s = float(score)
            except ValueError:
                s = strength_map.get(grade, 0.5)
            result['active_nodes'].append({'id': int(node_id), 'strength': s})

        # 提取惊讶度/熵: 熵:4.910, 惊讶度:9.963
        m_ent = re.search(r'熵:([0-9.]+)', memory_context)
        m_sur = re.search(r'惊讶度:([0-9.]+)', memory_context)
        if m_ent:
            result['entropy'] = float(m_ent.group(1))
        if m_sur:
            result['surprise'] = float(m_sur.group(1))

        # 提取相关记忆回忆 (从 "1. \"...\"" 结构)
        recalled = re.findall(
            r'^\s*\d+\.\s*[\"\u201c](.+?)[\"\u201d]\s*$',
            memory_context, re.MULTILINE
        )
        result['recalled'] = [r.strip() for r in recalled]

        return result

    @staticmethod
    def _text_score(query: str, text: str) -> float:
        """文本匹配分：基于查询关键词在结果中的出现比例（0~1）"""
        if not query or not text:
            return 0.0
        # 中文按字符切分，英文按词切分
        q_tokens = set()
        for tok in re.findall(r'[\u4e00-\u9fff]|[a-zA-Z0-9]+', query.lower()):
            q_tokens.add(tok)
        if not q_tokens:
            # 退化为子串匹配
            return 1.0 if query.lower() in text.lower() else 0.0
        text_l = text.lower()
        hit = sum(1 for t in q_tokens if t in text_l)
        return hit / len(q_tokens)

    def recall(self, query: str, k: int = 10) -> List[MemoryItem]:
        """协同检索：文本(0.3) + 向量(0.5) + LMS激活(0.2) 加权融合排序"""
        # ---------- 1. 沙漏文本检索 ----------
        sandglass_results = self.sandglass.recall(query, k)
        if not sandglass_results:
            return []

        # ---------- 2. 查询向量 ----------
        q_vec = self.vector.embed(query)

        # ---------- 3. LMS激活信息 ----------
        lms_data = {}
        try:
            lms_resp = self.lms.fetch_context(query)
            mc = lms_resp.get('memory_context', '') if lms_resp else ''
            lms_data = self._parse_lms_context(mc)

            # 补充memory_state里的数值（若解析失败则回退）
            if not lms_data['active_nodes'] and lms_resp:
                state = lms_resp.get('memory_state', {})
                lms_data['surprise'] = state.get('last_surprise', 0.0)
                lms_data['entropy'] = state.get('last_entropy', 0.0)
        except Exception as e:
            logger.warning(f"LMS激活信息获取失败: {e}")

        # ---------- 4. 对每条结果计算综合得分 ----------
        recalled_texts = lms_data.get('recalled', [])
        # LMS激活基线：惊讶度归一化（0~1），作为无显式命中的弱激活信号
        surprise_norm = min(lms_data.get('surprise', 0.0) / 20.0, 1.0)
        has_recall = bool(recalled_texts)

        scored = []
        for item in sandglass_results:
            # 向量相似度
            if not item.vector:
                item.vector = self.vector.embed(item.text)
            vec_score = self._cosine(q_vec, item.vector)

            # 文本匹配
            txt_score = self._text_score(query, item.text)

            # LMS激活：若结果文本命中LMS相关记忆回忆，给满分；否则用惊讶度弱激励
            lms_activation = 0.0
            if has_recall:
                for r in recalled_texts:
                    if r and (r in item.text or item.text in r):
                        lms_activation = 1.0
                        break
            if lms_activation == 0.0:
                lms_activation = surprise_norm * 0.5

            # 加权融合：文本0.3 + 向量0.5 + LMS激活0.2
            total = 0.3 * txt_score + 0.5 * vec_score + 0.2 * lms_activation

            # 附加激活信息，便于观察
            item.meta = item.meta or {}
            item.meta['scores'] = {
                'text': round(txt_score, 3),
                'vector': round(vec_score, 3),
                'lms_activation': round(lms_activation, 3),
                'total': round(total, 3),
            }
            item.meta['lms'] = {
                'surprise': round(lms_data.get('surprise', 0.0), 3),
                'entropy': round(lms_data.get('entropy', 0.0), 3),
                'active_node_count': len(lms_data.get('active_nodes', [])),
                'top_active_nodes': lms_data.get('active_nodes', [])[:5],
                'recalled_texts': recalled_texts,
            }

            scored.append((total, item))

        # ---------- 5. 按综合得分降序排序 ----------
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [item for _, item in scored[:k]]

        logger.debug(
            f"协同检索: {query} -> {len(results)}条 "
            f"(LMS激活节点{len(lms_data.get('active_nodes', []))}个, "
            f"惊讶度{lms_data.get('surprise', 0):.2f})"
        )
        return results

    def get_status(self) -> Dict[str, Any]:
        """获取所有后端状态"""
        return {
            'sandglass': self.sandglass.get_state(),
            'lms': self.lms.get_state(),
            'vector': self.vector.health(),
            'monument': {
                'degraded': self.monument.degraded,
                'block_count': len(self.monument.sync_monuments()),
            },
        }

    # -- 应用层：丰碑知识贡献 ------------------------------------------------
    def contribute(self, topic: str, k: int = 5, **kwargs) -> ContributionResult:
        """从沙漏检索结果中提炼知识，贡献到丰碑。

        1. 按 topic 从沙漏召回 k 条记忆；
        2. 若无显式正文 kwargs，将召回结果拼装成知识文本；
        3. 调用丰碑适配器 contribute() 写入 candidates/ 并铸造。

        kwargs 透传给丰碑适配器：title/tags/contributor/source/approver 等。
        """
        # 从沙漏检索结果提炼知识
        knowledge = kwargs.get("knowledge")
        if not knowledge:
            items = self.sandglass.recall(topic, k)
            if not items:
                # 沙漏无命中时用主题本身作为知识源
                knowledge = topic
            else:
                knowledge = "\n".join(f"- {it.text}" for it in items[:k])

        title = kwargs.pop("title", None) or f"{topic} 记忆提炼"
        tags = kwargs.pop("tags", None) or ["memory_refined", topic]
        return self.monument.contribute(
            knowledge=knowledge,
            title=title,
            tags=tags,
            **kwargs,
        )