# Memory Integration Layer - 统一记忆整合层

> AgentOS胶水层：整合沙漏、LMS、向量服务、丰碑网络

---

## 简介

Memory Integration Layer是AgentOS的核心整合层，提供统一的记忆管理接口。

### 核心价值

- 🎯 **统一记忆管理**：一个接口管理沙漏、LMS、向量服务
- 🎯 **前后端分离**：adapters/（后端）+ services/（前端）
- 🎯 **高内聚低耦合**：每个模块独立，可替换
- 🎯 **完整测试覆盖**：87个测试全通过

---

## 架构

```
┌─────────────────────────────────────────────────┐
│     IntegratedMemoryService (统一入口)          │
│  store() / recall() / contribute() / status()   │
└─────────────────────────────────────────────────┘
                      ↓
        ┌─────────────┼─────────────┐
        ↓             ↓             ↓
┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│SandglassAdap│ │  LMSAdapter │ │VectorAdapter│
│  (沙漏DB)   │ │  (LMS API)  │ │ (向量服务)  │
└─────────────┘ └─────────────┘ └─────────────┘
        ↓             ↓             ↓
      文本存储      熵/惊讶度      1024维向量
      时间戳        目的性         相似度
      来源          激活节点       去重
        │             │             │
        └─────────────┴─────────────┘
                      ↓
         ┌──────────────────────────┐
         │   MonumentAdapter        │
         │   (丰碑网络知识贡献)      │
         └──────────────────────────┘
```

---

## 安装

### 快速安装

```bash
# 克隆仓库
git clone https://github.com/tdx1146/memory-integration-layer.git
cd memory-integration-layer

# 安装依赖
pip install -r requirements.txt

# 运行测试
pytest tests/ -v
```

### 验证

```python
from interfaces.services.integration_service import IntegratedMemoryService
from interfaces.adapters.sandglass_adapter import SandglassAdapter
from interfaces.adapters.lms_adapter import LMSAdapter
from interfaces.adapters.vector_adapter import VectorAdapter

# 初始化适配器
sandglass = SandglassAdapter("/path/to/sandglass.db")
lms = LMSAdapter("http://localhost:8190")
vector = VectorAdapter("http://192.168.0.103:11435/v1/embeddings", "bge-m3")

# 创建整合服务
service = IntegratedMemoryService(
    sandglass=sandglass,
    lms=lms,
    vector=vector
)

# 存储
item = service.store("测试消息", "chat")
print(f"ID: {item.id}, 熵: {item.entropy}")

# 检索
results = service.recall("测试", k=5)
print(f"找到 {len(results)} 条")
```

---

## 依赖系统

### 必需依赖

| 系统 | 说明 | 配置 |
|------|------|------|
| **沙漏DB** | 显式记忆（文本存储） | SQLite数据库路径 |
| **LMS** | 隐式记忆（熵/惊讶度） | http://localhost:8190 |
| **向量服务** | 语义嵌入（bge-m3） | http://192.168.0.103:11435 |

### 可选依赖

| 系统 | 说明 | 配置 |
|------|------|------|
| **丰碑网络** | 知识贡献 | MonumentAdapter |

---

## 使用指南

### 1. 存储记忆

```python
item = service.store("用户说了一句话", "chat")

# 自动完成：
# ✓ 沙漏DB：存储文本+时间戳+来源
# ✓ LMS API：获取熵+惊讶度+目的性
# ✓ 向量服务：生成1024维向量

print(f"ID: {item.id}")
print(f"熵: {item.entropy}")
print(f"惊讶度: {item.surprise}")
print(f"向量维度: {len(item.vector)}")
```

### 2. 检索记忆

```python
results = service.recall("关键词", k=10)

# 融合排序：
# - 文本匹配：30%
# - 向量相似度：50%
# - LMS激活：20%

for item in results:
    print(f"{item.text[:50]}... (相似度: {item.metadata.get('similarity')})")
```

### 3. 知识贡献

```python
result = service.contribute("主题", k=5)

# 自动完成：
# 1. 从沙漏检索相关记忆
# 2. 组装成知识体
# 3. 贡献到丰碑网络

print(f"成功: {result.ok}")
print(f"URN: {result.urn}")
```

### 4. 系统状态

```python
status = service.get_status()

# 返回：
{
  'sandglass': {'memory_count': 900, 'healthy': True},
  'lms': {'memory_count': 5, 'healthy': True},
  'vector': {'healthy': True}
}
```

---

## API文档

### IntegratedMemoryService

#### `__init__(sandglass, lms, vector, monument)`

构造函数，依赖注入后端适配器。

#### `store(text, source="chat") -> MemoryItem`

聚合写入：向量化 + 沙漏 + LMS。

#### `recall(query, k=10, weights=None) -> List[MemoryItem]`

协同检索：文本 + 向量 + LMS激活加权融合。

#### `contribute(topic, k=5, **kwargs) -> ContributionResult`

知识贡献：从沙漏提炼知识并贡献到丰碑。

#### `get_status() -> Dict[str, Any]`

各后端健康状态聚合。

---

## 数据模型

### MemoryItem

```python
@dataclass
class MemoryItem:
    id: str                    # UUID
    text: str                  # 文本内容
    created_at: float          # 时间戳
    origin: str                # 来源（agent/user/system）
    system: str                # 系统标识（lms/sandglass/vector）
    entropy: float             # 熵（LMS）
    surprise: float            # 惊讶度（LMS）
    vector: List[float]        # 向量（1024维）
    metadata: Dict[str, Any]   # 元数据
```

---

## 测试

### 运行测试

```bash
# 全部测试
pytest tests/ -v

# 单个测试
pytest tests/test_integration_service.py -v

# 覆盖率
pytest tests/ --cov=interfaces --cov-report=html
```

### 测试覆盖

- ✅ 87个测试全通过
- ✅ 单元测试：每个adapter独立测试
- ✅ 集成测试：IntegratedMemoryService端到端测试

---

## 架构设计

### 前后端分离

**后端层（adapters/）**：
- `sandglass_adapter.py`：沙漏DB适配器
- `lms_adapter.py`：LMS API适配器
- `vector_adapter.py`：向量服务适配器
- `monument_adapter.py`：丰碑网络适配器

**前端层（services/）**：
- `integration_service.py`：整合记忆服务

### 高内聚低耦合

**高内聚**：
- 每个adapter只负责单一系统
- 服务层只做业务编排

**低耦合**：
- 服务层依赖抽象Port，不依赖具体adapter
- 可替换任何后端实现

---

## 版本历史

| 版本 | 提交 | 说明 |
|------|------|------|
| v1.0 | 4f1b6f0 | MemoryItem自动生成UUID |
| v1.0 | 956c0fe | Registry契约修复 |
| v1.0 | b3cd70e | 初始版本 |

---

## 相关项目

| 项目 | 说明 |
|------|------|
| [living-memory-system](https://github.com/tdx1146/living-memory-system) | 活体记忆系统（LMS） |
| [agent-os-iso-sand](https://github.com/tdx1146/agent-os-iso-sand) | 同构沙盘 |
| [monument-network](https://github.com/tdx1146/monument-network) | 丰碑网络 |

---

## 许可证

MIT License

---

## 贡献

欢迎提交Issue和Pull Request！

---

_最后更新：2026-08-01_