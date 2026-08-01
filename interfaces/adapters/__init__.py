"""适配器包 —— 把现有系统包装成标准 Port。

每个现有系统一个适配器，实现 interfaces.base 中对应 Port，
内部调用该系统原有 API。适配器只做"翻译"，不含业务逻辑。

导入策略：{system}_api 模块采用惰性导入（运行时才 import 原系统），
这样本包在依赖缺失时仍能加载，且便于测试用内存实现替换。

两类适配器：
  - ``*_adapter`` 系列（real_backends 拆分而来）：真实 HTTP/SQLite/Monument 后端。
  - ``sensor_* / cognitive_* / application_*`` 系列：组合/降级门面。
"""

from interfaces.adapters.sensor_qdrant import QdrantSensorAdapter
from interfaces.adapters.sensor_http import HttpSensorAdapter
from interfaces.adapters.cognitive_lms import LMSCognitiveAdapter
from interfaces.adapters.cognitive_sandglass import SandglassCognitiveAdapter
from interfaces.adapters.application_monument import MonumentApplicationAdapter

# 真实后端适配器（惰性连接，无副作用）
from interfaces.adapters.sandglass_adapter import SandglassAdapter
from interfaces.adapters.lms_adapter import LMSAdapter, parse_lms_context
from interfaces.adapters.vector_adapter import VectorAdapter
from interfaces.adapters.monument_adapter import MonumentAdapter

__all__ = [
    "QdrantSensorAdapter",
    "HttpSensorAdapter",
    "LMSCognitiveAdapter",
    "SandglassCognitiveAdapter",
    "MonumentApplicationAdapter",
    # 真实后端
    "SandglassAdapter",
    "LMSAdapter",
    "parse_lms_context",
    "VectorAdapter",
    "MonumentAdapter",
]
