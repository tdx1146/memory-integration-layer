# LMS-CLIENT — 活体记忆系统跨平台 SDK 接入文档

- 版本：0.1.0 ｜ 日期：2026-08-10 ｜ 依据：总体方案 §3.7（T2.7）
- 代码：`/vol2/1000/AI专用/memory-integration-layer/lms_client/`
- 服务对象：LMS 活体记忆系统（数据面 `:8190`，管理面 `:8191`）

一句话：**openclaw / codex / workbody 三平台通过同一套 `lms_client` SDK 接入
同一个记忆大脑** —— 你不需要知道 HTTP 细节，只需要会调 `recall` / `store` /
`status` / `dream`。

```
┌──────────┐   ┌──────────┐   ┌──────────┐
│ openclaw │   │  codex   │   │ workbody │
└────┬─────┘   └────┬─────┘   └────┬─────┘
     │ MCP 已接      │ MCP/CLI      │ OpenAPI/CLI
     ▼              ▼              ▼
┌──────────────────────────────────────────┐
│        lms_client SDK（本包）              │
│  http.py（LMSClient） · cli.py · mcp_bridge│
└────────────────┬─────────────────────────┘
                 │ REST（超时 10s 统一 / 重试 1 次）
                 ▼
┌───────────────────────────┐   ┌───────────────────────────┐
│ 数据面 :8190               │   │ 管理面 :8191（可选）        │
│ /recall /chat /status      │   │ /control/*（X-Control-Token│
│ /dream /snapshot /health   │   │  /register 注册接入方）     │
└───────────────────────────┘   └───────────────────────────┘
```

---

## 1. 安装（三选一）

```bash
# A. pip 安装（推荐，带 lms / lms-mcp 命令）
cd /vol2/1000/AI专用/memory-integration-layer
pip install -e .            # 任意 Python 3.9+ 环境；零强制依赖

# B. 免安装直接用（本包零依赖，httpx 有则用、无则回退 urllib）
python3 lms_client/cli.py recall "命名 毛毛" --k 2

# C. 仅 Python API（import 即用，无需安装）
import sys; sys.path.insert(0, "/vol2/1000/AI专用/memory-integration-layer")
from lms_client import LMSClient
```

> 依赖策略：`pyproject.toml` 声明 `dependencies = []`（httpx 为可选增强，
> `pip install -e '.[http]'` 可显式安装）。任何平台 clone 下来即跑。

---

## 2. 会话命名规范（跨平台映射，重要）

| 平台 | 会话 ID | 说明 |
|---|---|---|
| openclaw | `main` | 毛毛本体（主人主对话脑，113+ 条记忆所在） |
| 总线事件 | `bus` | 事件总线降噪脑（禁 LLM 自述） |
| codex | `codex-*` | 如 `codex-cli`、`codex-agent`，codex 独立脑 |
| workbody | `workbody-*` | 如 `workbody-main`，workbody 独立脑 |
| ~~default~~ | 废弃 | 旧默认脑，新客户端一律显式传 session_id |

规则：

- **openclaw 始终用 `main`**（保持与现有 lms-http MCP 一致，共享同一大脑）；
- **codex / workbody 用各自前缀**，彼此隔离、互不污染（同一平台多实例可用
  `codex-cli`、`codex-<项目名>` 细分）；
- 所有会话共享同一套记忆机制，只是内容分区——检索时指定对的 session_id。

在 :8191 注册接入方时，`client_id` 与上述会话 ID 对齐：
`openclaw-main` / `codex-cli` / `workbody-main`。

---

## 3. CLI 用法（三平台通用）

```bash
# 全局参数：--base-url / --token / --timeout / --retries / --json / --version
# 环境变量：LMS_BASE_URL / LMS_TOKEN / LMS_CLIENT_ID / LMS_TIMEOUT / LMS_RETRIES

# 只读检索（核心命令）
lms --base-url http://127.0.0.1:8190 recall "命名 毛毛" --k 3

# 存储一轮文本/对话（写入，推进轮次）
lms store "今天确认了跨平台 SDK 方案：三平台统一走 lms_client" --session codex-cli

# 查询会话状态
lms status --session main

# 触发做梦（记忆巩固；空闲时调用）
lms dream --session main --steps 20

# 存活探针
lms health

# 机器可读输出（供脚本/Agent 消费）
lms recall "毛毛" --k 2 --json
```

退出码：`0` 成功 ｜ `1` 业务/HTTP 错误 ｜ `3` 认证失败（401/403）｜ `4` 超时。
错误信息写 stderr，格式 `❌ …`，含 HTTP 状态码与服务端 detail。

---

## 4. Python API

```python
from lms_client import LMSClient, LmsError, TimeoutError, AuthError

client = LMSClient(base_url="http://127.0.0.1:8190")   # 默认即 8190

# 检索（只读，~1s）
result = client.recall("命名 毛毛", k=3, session_id="main")
for hit in result.results:
    print(hit.score, hit.text)

# 存储
client.store("一段重要的约定", session_id="codex-cli")

# 状态
st = client.status(session_id="main")
print(st.turn_count, st.episodic_buffer_size, st.llm_enabled)

# 做梦（默认 30s 超时，可覆盖）
client.dream(session_id="main", steps=20, full_cycle=False, timeout=60)

# 错误处理（分层捕获）
try:
    client.recall("", k=2)
except LmsError as e:
    print(type(e).__name__, e.status_code, e)
```

超时规范（对齐总体方案 §3.7）：recall/status 5s、store 10s、dream 30s、
health 3s；构造参数 `timeout=` 可统一覆盖，单方法 `timeout=` 可单独覆盖。
重试：默认 1 次，仅网络层失败/429/5xx 触发（4xx 不重试）。

---

## 5. MCP 桥（mcp_bridge.py）

零依赖最小 MCP stdio 服务器（newline-delimited JSON-RPC 2.0），任何标准
MCP 客户端可直接拉起。工具集：

| 工具 | 参数 | 说明 |
|---|---|---|
| `lms_recall` | query, session_id, k | 只读检索记忆 |
| `lms_store` | text, session_id | 存储文本进记忆 |
| `lms_status` | session_id | 查询会话状态 |
| `lms_dream` | session_id, steps, full_cycle | 触发做梦 |
| `lms_snapshot` | session_id | 保存快照 |

别名（与历史工具名对齐，同一实现）：`recall_memory` / `store_memory` /
`get_status` / `dream`。

```bash
python3 lms_client/mcp_bridge.py [--base-url http://127.0.0.1:8190] [--token TOKEN]
```

自测握手（模拟 MCP 客户端）：

```bash
printf '%s\n' \
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
'{"jsonrpc":"2.0","method":"notifications/initialized"}' \
'{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"lms_recall","arguments":{"query":"命名 毛毛","k":2,"session_id":"main"}}}' \
| python3 lms_client/mcp_bridge.py
```

---

## 6. 三平台接入步骤

### 6.1 OpenClaw（已接，无需新动作）

- 现状：`lms-http` / `lms-memory` 两个 MCP 已注册，走 :8190 数据面，
  session_id=`main`（毛毛本体）。
- 可选增强：把插件/工具换成本 SDK 的 `mcp_bridge.py`（工具名与 codex/workbody
  完全一致），或直接用 CLI：
  ```bash
  lms recall "毛毛 命名" --k 3
  ```

### 6.2 Codex

```bash
# 方式 A：注册 MCP（推荐，Agent 内直接调 lms_recall / lms_store / ...）
codex mcp add lms -- python3 /vol2/1000/AI专用/memory-integration-layer/lms_client/mcp_bridge.py
# 或使用 venv 解释器 + 安装后的 lms-mcp 命令：
codex mcp add lms -- /path/to/venv/bin/lms-mcp

# 方式 B：直接用 CLI（不注册 MCP 时）
lms recall "命名 毛毛" --k 2 --session codex-cli
lms store "codex 记录的约定" --session codex-cli

# 可选：在 :8191 注册接入方（拿到 client_token，后续可审计/鉴权）
curl -s -X POST -H "X-Control-Token: $LMS_CONTROL_TOKEN" -H "Content-Type: application/json" \
     -d '{"client_id":"codex-cli","purpose":"codex 接入","platform":"codex"}' \
     http://127.0.0.1:8191/control/register
```

在 `~/.codex/AGENTS.md` 写：**「需要回忆与主人的对话/命名/约定时，
调用 lms_recall（session_id=codex-*）；重要事实用 lms_store 落库。」**

### 6.3 Workbody

```bash
# 方式 A：OpenAPI 客户端（:8191 管理面自动生成，import 即得类型化客户端）
curl -s http://127.0.0.1:8191/openapi.json > lms-openapi.json
# → 用 workbody 的 OpenAPI 生成器导入，配置 base_url=http://127.0.0.1:8191 + token

# 方式 B：MCP（与 codex 同款命令）
codex mcp add lms -- python3 /vol2/1000/AI专用/memory-integration-layer/lms_client/mcp_bridge.py

# 方式 C：CLI（最省事）
lms recall "毛毛" --k 3 --session workbody-main
lms store "workbody 会话纪要" --session workbody-main
```

---

## 7. 失败场景速查

| 场景 | 现象 | 处理 |
|---|---|---|
| 服务未启动 / URL 写错 | `❌ 网络错误: POST http://127.0.0.1:9999/recall — ... Connection refused` | 检查 :8190 是否在跑、`--base-url` 是否写对 |
| token 缺失/无效（:8191） | `❌ 认证失败（HTTP 401）: 无效或缺失的控制令牌…` 退出码 3 | 配 `--token` 或环境变量 `LMS_TOKEN` |
| 请求超时 | `❌ 超时: 请求超时（>10s）…` 退出码 4 | 服务忙（如做梦期间）；加大 `--timeout` 或稍后重试 |
| 会话不存在（status） | `❌ 不存在: 请求失败（HTTP 404）: 会话 'xxx' 不存在` | 首次使用会在 recall/store 时自动创建 |
| 做梦期间写操作 | `❌ 服务端错误（HTTP 503）: 系统正在做梦…` | 等做梦结束重试（SDK 自动重试 1 次） |

## 8. 路线图（后续版本）

- 数据面 `/v1/*` 统一协议端点落地后，`LMSClient` 增加 `/v1/recall` 等快路径
  （当前直连现有端点，向前兼容）；
- 管理面 `:8191` 的 `/control/*` 专用客户端（register/config/diagnose）；
- 官方 `mcp` Python SDK 适配层（当前零依赖最小实现，接口已对齐，
  换 SDK 只需替换 `serve_stdio` 内部）。

---

_文档与代码同源：lms_client 0.1.0 ｜ 工程：LMS 稳定化 T2.7 跨平台 SDK_
