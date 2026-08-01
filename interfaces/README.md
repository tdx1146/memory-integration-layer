# Standard Interfaces 标准接口层

三层架构（钱学森系统论视角）的标准松耦合契约。每层只依赖抽象 Port，
具体系统通过适配器注入，彻底摆脱"接口混乱、各自为政"。

## 架构

```
┌──────────────────────────────────────────┐
│ 应用层  : ApplicationPort                 │  ← 丰碑网络
│  contribute() / sync_monuments() /        │     (知识贡献/P2P同步/检索)
│  query_monument()                         │
├──────────────────────────────────────────┤
│ 认知层  : CognitivePort                   │  ← LMS(隐式状态) + 沙漏(显式记忆)
│  store() / recall() / get_state()         │
├──────────────────────────────────────────┤
│ 感官层  : SensorPort                      │  ← 向量服务(bge-m3)
│  embed() / batch_embed() / health()       │
└──────────────────────────────────────────┘
```

## 目录结构

```
interfaces/
├── __init__.py                  # 公开 API 出口
├── base.py                      # ★ 抽象接口(Port) + 内存降级实现
├── models.py                    # 数据模型 (MemoryItem / CognitiveState / ContributionResult)
├── exceptions.py                # 异常体系 (统一降级)
├── registry.py                  # ★ 依赖注入容器
├── adapters/                    # 现有系统适配器
│   ├── sensor_qdrant.py         # Qdrant 向量服务
│   ├── sensor_http.py           # HTTP 嵌入服务 (bge-m3)
│   ├── cognitive_lms.py         # LMS (隐式记忆 + 状态)
│   ├── cognitive_sandglass.py   # 沙漏 (显式记忆)
│   └── application_monument.py  # 丰碑网络
└── test_interfaces.py           # 单元测试 (pytest)
```

## 快速上手

```python
from interfaces import Registry, MemoryItem

# 生产组装（惰性连接真实系统，失败自动降级）
reg = Registry.default()

# 感官层
vec = reg.sensor().embed("你好")          # -> [1024] float

# 认知层
reg.cognitive().store(MemoryItem(text="记忆", system="lms"))
hits  = reg.cognitive().recall("记忆", k=5)
state = reg.cognitive().get_state()       # 熵/惊讶度/目的性

# 应用层
reg.application().contribute("某条知识", tags=["architecture"])
urns = reg.application().sync_monuments()

# 测试/离线用全内存版
reg = Registry.in_memory()
```

## 设计原则

1. **面向接口，不面向实现**：调用方只 import `interfaces.base` 的 Port，
   永不直接 import `qdrant_client` / `lms` / `monument_core`。
2. **依赖注入**：所有适配器构造函数接受可注入的客户端/函数/URL，
   测试时传 mock，生产时传真实实现（见 `registry.py`）。
3. **统一数据模型**：跨层交换一律用 `MemoryItem` 等 dataclass，杜绝
   "同一对话存三份" 和字段名冲突。`MemoryItem.fingerprint()` 基于语义
   内容（排除 origin/id），可跨系统去重。
4. **统一异常与降级**：所有失败抛 `interfaces.exceptions` 下异常，
   适配器在底层不可用时降级到内存实现（不崩溃），可用 `degraded`
   属性观察降级状态。
5. **惰性连接**：构造不发起网络请求，import 依赖延迟到首次调用，
   保证包在任何环境都能加载、测试不依赖真实服务。

## 换取与迁移

- 新增系统 → 实现对应 Port 加一个适配器 + 注册到 `Registry`。
- 修改底层 → 只改该系统的适配器，业务代码零改动。
- 运行测试：

```bash
python3 -m pytest interfaces/test_interfaces.py -v
```

## 三层接口契约速览

| 层        | Port              | 方法                                       |
|-----------|-------------------|--------------------------------------------|
| 感官层    | `SensorPort`      | `embed` `batch_embed` `health` `dim`       |
| 认知层    | `CognitivePort`   | `store` `recall` `get_state`               |
| 应用层    | `ApplicationPort` | `contribute` `sync_monuments` `query_monument` |
