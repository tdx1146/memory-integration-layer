# Memory Integration Layer — 统一记忆整合层

> AgentOS 的**胶水层**：在一条确定链路里整合沙漏（叙事记忆）、LMS（活体记忆）、向量（bge-m3）、丰碑（知识网络）。

> 🔗 **系统定位**（2026-08-10）：本模块是「胶水层 glue」。
> 上游依赖：LMS(:8190)、沙漏(:17333)、向量(:11435) ｜ 下游消费者：OpenClaw 插件（glue-memory-injector）、Agent OS 总线
> 外部接口：`:19000` /health、`/recall`、`storeTurn`；doubt_adapter（doubt_episode 账本，需 `DOUBT_BUS_FILE` 启用总线发布）
> 仓库：`https://github.com/tdx1146/memory-integration-layer`（master）
> 系统全图：**见 `tdx1146/agent-os` 仓库的 `TOPOLOGY.md`**（https://github.com/tdx1146/agent-os/blob/main/TOPOLOGY.md）

---

## 一、项目定位

**Memory Integration Layer = AgentOS 的统一记忆整合层（胶水层）**。

它不是又一个记忆系统，而是把 Agent 已有的四类记忆后端"粘"成一个统一入口：

- **沙漏（NexSandglass）** —— 显式叙事记忆（追加式 `sandglass.txt`，**权威源**）
- **LMS（living-memory-system）** —— 隐式状态（熵 / 惊讶度 / 激活节点）
- **向量服务** —— 语义嵌入（bge-m3，1024 维）
- **丰碑（Monument）** —— 跨 AI 知识贡献 / P2P 同步

所有读写走同一个 `IntegratedMemoryService`，实现**唯一确定链路**，不另起炉灶。

---

## 二、架构

### 前后端分离

| 层 | 目录 | 职责 |
|----|------|------|
| **前端（services/）** | `interfaces/services/` | 业务编排：跨 Port 聚合检索 / 写入 / 贡献，**不接触具体适配器** |
| **后端（adapters/）** | `interfaces/adapters/` | 每个真实系统一个适配器，只做"翻译" |
| **契约（base/）** | `interfaces/base.py` | 抽象 Port：`SensorPort` / `CognitivePort` / `ApplicationPort` |

```
┌──────────────────────────────────────────────────────────┐
│  glue_server.py / glue_helper.py   （胶水层 HTTP + 薄桥接）│
└───────────────────────┬──────────────────────────────────┘
                        ↓
┌──────────────────────────────────────────────────────────┐
│            IntegratedMemoryService (统一入口)             │
│      store() / recall() / contribute() / get_status()     │
└───────┬──────────────┬──────────────┬──────────────┬──────┘
        ↓              ↓              ↓              ↓
┌──────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐
│SandglassVault│ │ LMSAdapter │ │VectorAdapter│ │Monument(未接)│
│ (txt 权威源) │ │ (HTTP 8190)│ │(bge-m3嵌入) │ │ 丰碑 DHT    │
└──────────────┘ └────────────┘ └────────────┘ └────────────┘
```

### 胶水层接入运行链路（关键）

插件（JS）不能直接 import Python 包，因此用**薄桥接**接入运行链路：

```
OpenClaw plugin → before_prompt_build（每轮触发）
    ├─ 读: glue_helper.recall "<query>"  → IntegratedMemoryService
    │         ├─ SandglassVaultAdapter → sandglass.txt（叙事记忆, 权威源）
    │         ├─ LMSAdapter           → localhost:8190（活体记忆/J矩阵塑形）
    │         └─ VectorAdapter        → bge-m3（向量, 失败降级 fail-open）
    └─ 写: glue_helper.store "<text>" "<source>" → 追加txt + LMS + 向量（跨session沉积）
```

- `glue_helper.py`：薄桥接脚本（stdout 输出 JSON，供 Node/插件解析），**内部复用同一套装配**
- `glue_server.py`：HTTP 服务（默认 19000 端口），暴露统一 API
- 两者共用 `build_service()`，保证读写走**同一条** `IntegratedMemoryService` 链路

**三层记忆分工**：沙漏（显式叙事/日记）＋ LMS（隐式状态/突触塑形）＋ 胶水层（统一编排）。

---

## 三、数据源决策：为什么沙漏以 txt 为权威源

胶水层的沙漏记忆**以 `sandglass.txt`（约 3457+ 条）为权威源**，SQLite 辅助库 **仅作索引**（906 条）。

原因有二：

1. **落后严重**：SQLite 辅助库只有 906 条，而 txt 权威源有 3457+ 条，落后约 **2305 条**。
2. **写锁冲突**：SQLite 辅助库与 `sandglass_mcp` 存在**写锁冲突**，多进程并发写入会互相卡死。

因此 `SandglassVaultAdapter` 直接复用沙漏原生 `sandglass_vault`（read 走三层检索 SearchRouter，write 走 txt 追加重建索引），保证读到的是沙漏**真实完整记忆**。SQLite 只留作检索索引，不再是权威。

---

## 四、快速开始 / 部署

### 1. 依赖系统

| 系统 | 说明 | 默认配置 |
|------|------|----------|
| **沙漏** | 叙事记忆权威源（txt） | `NEXSANDBASE_HOME` → `/vol2/1000/AI专用/所有自动化/轻如烟/sandglass` |
| **LMS** | 活体记忆 HTTP 服务 | `LMS_URL` → `http://localhost:8190` |
| **向量** | bge-m3 嵌入服务（OpenAI 兼容 `/embeddings`） | `VECTOR_URL` → `http://192.168.0.103:11435/v1/embeddings` |
| **丰碑** | 知识网络（可选，当前未接线） | `MonumentAdapter` |

### 2. 克隆 & 测试

```bash
git clone https://github.com/tdx1146/memory-integration-layer.git
cd memory-integration-layer

# 跑测试（当前 106 passed）
python3 -m pytest interfaces/test_interfaces.py tests/ -v
```

### 3. 接入插件（推荐用 glue_helper 桥接）

插件在 `before_prompt_build` 钩子里，用 shell 调 bridge helper（输出 JSON）：

```bash
# 检索（注入上下文）
python3 glue_helper.py recall "用户当前关注点" 3

# 写入（跨 session 沉积）
python3 glue_helper.py store "用户完成了XX任务" agent

# 健康状态
python3 glue_helper.py status
```

`glue_helper` 全部 **fail-open**：召回/写入失败不抛给插件崩溃，返回 `{"error": ...}`。

### 4. 或起 HTTP 服务

```bash
python3 glue_server.py --port 19000 --host 127.0.0.1
# POST /recall /soul /store /status /contribute ;  GET /health
```

---

## 五、API 速查

### IntegratedMemoryService（服务层统一入口）

| 方法 | 签名 | 作用 |
|------|------|------|
| `store` | `store(text, source="chat") -> MemoryItem` | 聚合写入：向量化 + 沙漏 + LMS |
| `recall` | `recall(query, k=10, weights=None) -> List[MemoryItem]` | 协同检索：文本 0.3 + 向量 0.5 + LMS 激活 0.2 加权融合 |
| `get_soul_snapshot` | `get_soul_snapshot(limit=5, recent_n=5) -> Dict[str, Any]` | 回魂快照：LMS 自述 + 状态指标 + 沙漏最近记忆（只读、fail-open、防循环） |
| `contribute` | `contribute(topic, k=5, **kwargs) -> ContributionResult` | 从沙漏召回提炼知识，贡献到丰碑 |
| `get_status` | `get_status() -> Dict[str, Any]` | 各后端健康状态聚合 |

### glue_helper（插件薄桥接，stdout JSON）

| 命令 | 用法 |
|------|------|
| `recall` | `python3 glue_helper.py recall "<query>" [k]` |
| `store` | `python3 glue_helper.py store "<text>" "<source>"` |
| `status` | `python3 glue_helper.py status` |

---

## 六、目录结构（整理后，方向A：只含纯胶水层代码）

```
memory-integration-layer/
├── glue_server.py                      # 胶水层 HTTP 装配 + 服务入口
├── glue_helper.py                      # 插件薄桥接脚本（stdout JSON）
├── interfaces/
│   ├── __init__.py                     # 公开 API 出口
│   ├── base.py                         # 抽象 Port + 内存降级实现
│   ├── models.py                       # 数据模型 (MemoryItem / CognitiveState / ContributionResult)
│   ├── exceptions.py                   # 统一异常/降级体系
│   ├── registry.py                     # 依赖注入容器
│   ├── adapters/                       # 后端层
│   │   ├── sandglass_vault_adapter.py  # ★ 沙漏真实后端（txt 权威源, 3457+条）
│   │   ├── sandglass_adapter.py        # 沙漏 SQLite 版（现存索引/兼容）
│   │   ├── lms_adapter.py              # LMS 真实后端 (HTTP) + parse_lms_context
│   │   ├── vector_adapter.py          # 向量真实后端 (OpenAI 兼容 /embeddings)
│   │   ├── cognitive_lms.py           # LMS 认知门面（组合/降级）
│   │   ├── cognitive_sandglass.py     # 沙漏认知门面（组合/降级）
│   │   ├── sensor_http.py / sensor_qdrant.py   # 感官层向量实现
│   │   ├── application_monument.py / monument_adapter.py  # 丰碑门面/真实后端
│   │   └── real_backends.py           # [Legacy 兼容别名层，仅转发]
│   ├── services/
│   │   └── integration_service.py     # IntegratedMemoryService（前端编排层）
│   └── test_interfaces.py             # 单元测试
├── tests/                              # 集成测试
├── PITFALLS.md / code_quality_report.md / memory_model.py
├── lms-integration-design.md / migration_plan.md / 系统整合全局规划-最终版.md
└── .gitignore                          # 188 行：AI记忆/独立子系统/运行时全部忽略
```

> 说明：AI 记忆（`memory/`）、独立子系统（`ai_group_chat/`、`monument/`、`hunyuan/`、`元丰碑/`、`找回自己/`、`轻如烟/`）及运行时脚本均已 `.gitignore`。git 目前只跟踪 **39 个纯胶水层代码文件**。

---

## 七、配置说明

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| 沙漏数据源目录 | `SANDGLASS_SOURCE` | `/vol2/1000/AI专用/所有自动化/轻如烟/sandglass_source` | 沙漏源码（sandglass_vault 所在） |
| 沙漏主数据目录 | `NEXSANDBASE_HOME` | `/vol2/1000/AI专用/所有自动化/轻如烟/sandglass` | txt 权威源所在 |
| LMS URL | `LMS_URL` | `http://localhost:8190` | 活体记忆 HTTP |
| 向量 URL | `VECTOR_URL` | `http://192.168.0.103:11435/v1/embeddings` | bge-m3 嵌入 |

均可用环境变量覆盖；构造时**惰性连接**，失败自动降级（`degraded` 属性可观察）。

---

## 八、已知问题 / 遗留

- **丰碑 DHT 未启动**：9000 端口 UDP 未监听，contribute 会产生 sync warning，是 `degraded` 的唯一来源。丰碑 `balance=0` 的贡献沉淀问题已修复。
- **沙漏 SQLite 写锁**：已用 **txt 权威源** 规避（见第三节）；SQLite 仅作索引，不作为写入权威。
- **记忆读写稳定性**：胶水层已接入运行链路，建议观察 3-7 天确认稳定。

---

## 九、设计原则

1. **面向接口不面向实现**：调用方只 import `interfaces.base` 的 Port，永不直接碰具体客户端。
2. **依赖注入**：适配器构造接受可注入的客户端/URL，测试传 mock、生产传真实实现。
3. **统一数据模型**：跨层一律用 `MemoryItem` 等 dataclass，`fingerprint()` 可跨系统去重。
4. **统一异常与降级**：所有失败抛 `interfaces.exceptions` 下异常，底层不可用时降级到内存实现（不崩溃）。
5. **惰性连接**：构造不发网络请求，import 依赖延迟到首次调用。
6. **fail-open 接入**：胶水层读写不阻塞插件主链路（`glue_helper` 失败返回 `{"error"}`）。

---

## 测试

```bash
# 全部测试（当前 106 passed）
python3 -m pytest interfaces/test_interfaces.py tests/ -v

# 覆盖率
python3 -m pytest interfaces/test_interfaces.py tests/ --cov=interfaces --cov-report=html
```

---

## 自我怀疑账本（doubt_adapter）

胶水层内置**持续自我怀疑**（AgentOS 聪明机制）：`interfaces/adapters/doubt_adapter.py`
把每次怀疑闭环写入 `doubt.db`（trigger: conflict/stakes/fok/surprise/novelty/user_correction），
月度统计注入 persona。

**总线接入（Phase 6）**：设置环境变量 `DOUBT_BUS_FILE`（指向 iso-sand 权威总线）后，
`store_doubt()` 写入成功即发布 `doubt.episode` 事件（v1.1 契约）→ 消费者 lms.feed
→ LMS /feed 塑形（自我怀疑喂潜意识）。默认不发布，fail-open。
详细接入见 `Agent OS/DOUBT-SYSTEM.md`。

## 相关项目

| 项目 | 说明 |
|------|------|
| [living-memory-system](https://github.com/tdx1146/living-memory-system) | 活体记忆系统（LMS） |
| [agent-os-iso-sand](https://github.com/tdx1146/agent-os-iso-sand) | 同构沙盘 |
| [monument-network](https://github.com/tdx1146/monument-network) | 丰碑网络 |

---

## 执照

MIT License

---

_最后更新：2026-08-04（胶水层统一接入 + 沙漏 txt 权威源 + 方向A整理）_
