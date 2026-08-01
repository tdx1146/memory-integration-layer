#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实后端适配器实现

将现有系统（沙漏、LMS、向量服务）接入到统一接口层。
"""

import sqlite3
import json
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime

import requests

from interfaces.base import SensorPort, CognitivePort, ApplicationPort
from interfaces.exceptions import AdapterError as BackendError
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


class IntegratedMemoryService:
    """整合记忆服务：统一入口"""

    def __init__(self,
                 sandglass_db: str = "/vol2/1000/AI专用/所有自动化/轻如烟/sandglass/sandglass.db",
                 lms_url: str = "http://localhost:8190",
                 vector_url: str = "http://192.168.0.103:11435/v1/embeddings",
                 vector_model: str = "bge-m3"):
        """初始化真实后端"""

        # 初始化三个适配器
        self.sandglass = RealSandglassAdapter(sandglass_db)
        self.lms = RealLMSAdapter(lms_url)
        self.vector = RealVectorAdapter(vector_url, vector_model)

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

    def recall(self, query: str, k: int = 10) -> List[MemoryItem]:
        """协同检索"""
        # 从沙漏检索
        sandglass_results = self.sandglass.recall(query, k)

        # 为结果补充向量
        for item in sandglass_results:
            if not item.vector:
                item.vector = self.vector.embed(item.text)

        return sandglass_results

    def get_status(self) -> Dict[str, Any]:
        """获取所有后端状态"""
        return {
            'sandglass': self.sandglass.get_state(),
            'lms': self.lms.get_state(),
            'vector': self.vector.health()
        }