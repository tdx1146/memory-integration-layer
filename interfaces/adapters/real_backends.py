#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[Legacy 兼容层] 真实后端适配器 —— 别名转发。

历史背景：
  本模块早期把「沙漏 / LMS / 向量 / 丰碑」真实后端 + 「整合记忆服务」
  全部堆在单文件里（724 行），职责混杂。本次架构重构已将其拆分为
  高内聚独立模块：

      adapters/sandglass_adapter.py  -> SandglassAdapter
      adapters/lms_adapter.py        -> LMSAdapter / parse_lms_context
      adapters/vector_adapter.py     -> VectorAdapter
      adapters/monument_adapter.py   -> MonumentAdapter
      services/integration_service.py-> IntegratedMemoryService

  为保证「不破坏现有功能」，本文件保留旧的类名作为向后兼容别名，
  但不再包含任何实现（避免两套实现漂移）。新代码请直接 import 上述模块。
"""

from interfaces.adapters.lms_adapter import LMSAdapter as RealLMSAdapter
from interfaces.adapters.lms_adapter import parse_lms_context
from interfaces.adapters.monument_adapter import MonumentAdapter as RealMonumentAdapter
from interfaces.adapters.sandglass_adapter import SandglassAdapter as RealSandglassAdapter
from interfaces.adapters.vector_adapter import VectorAdapter as RealVectorAdapter
from interfaces.services.integration_service import IntegratedMemoryService

# 兼容旧引用
BackendError = __import__(
    "interfaces.exceptions", fromlist=["AdapterError"]
).AdapterError

__all__ = [
    "RealSandglassAdapter",
    "RealLMSAdapter",
    "RealVectorAdapter",
    "RealMonumentAdapter",
    "IntegratedMemoryService",
    "parse_lms_context",
    "BackendError",
]
