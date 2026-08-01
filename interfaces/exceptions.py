"""异常体系 —— 让错误处理可以分层降级。

所有适配器一律抛这里的异常，契约面（base.py）只承诺抛
InterfaceError 及其子类，调用方据此统一降级。
"""

from typing import Optional


class InterfaceError(Exception):
    """标准接口层基础异常。"""

    def __init__(self, message: str, reason: str = "unknown", retryable: bool = False):
        super().__init__(message)
        self.reason = reason
        self.retryable = retryable


class AdapterError(InterfaceError):
    """适配器初始化/配置错误。"""

    def __init__(self, message: str, reason: str = "adapter_config"):
        super().__init__(message, reason=reason)


class ConnectionError_(InterfaceError):
    """底层连接失败（感官层/向量服务不可达）。"""

    def __init__(self, message: str, reason: str = "connection"):
        super().__init__(message, reason=reason, retryable=True)


class DegradedError(InterfaceError):
    """降级策略触发：主路径失败，已回退到备用/缓存。"""

    def __init__(self, message: str, fallback: Optional[str] = None):
        super().__init__(message, reason="degraded")
        self.fallback = fallback


class CapacityError(InterfaceError):
    """超限/超预算（如向量维度不符、召回数为负）。"""

    def __init__(self, message: str, reason: str = "capacity"):
        super().__init__(message, reason=reason)
