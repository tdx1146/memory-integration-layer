"""服务层（前端 / 业务编排）。

按“前后端分离”原则：
  - ``adapters/``（后端）：把各系统翻译成标准 Port，只做单点翻译。
  - ``services/``（前端）：编排多个 Port，实现跨系统业务逻辑（协同检索、
    聚合存储、知识提炼贡献），不含任何具体系统细节。

业务代码只依赖 ``interfaces.base`` 的抽象 Port 与 ``interfaces.models``，
不直接 import 任何具体适配器 —— 通过构造注入，便于测试替换。
"""

from interfaces.services.integration_service import IntegratedMemoryService

__all__ = ["IntegratedMemoryService"]
