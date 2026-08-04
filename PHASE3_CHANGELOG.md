# PHASE3_CHANGELOG — 胶水层通电（D4）+ 总线 handler 宿主化

> 日期：2026-08-04（Phase 3）
> 工程：Agent OS 总线改良工程 Phase 3（D4 启动 glue_server + D6 深化：胶水层作 hexagon handler 宿主，shell 降级为一种 handler）
> 本文件覆盖：`memory-integration-layer`（胶水层）；总线侧改动见 `iso-sand/PHASE3_CHANGELOG.md`

---

## 一、任务 1：胶水层配置 + 启动 glue_server ✅

### 改动
1. **新增 `.env`**（本仓库根目录）：
   - `SANDGLASS_SOURCE=/vol2/1000/AI专用/所有自动化/轻如烟/sandglass_source`
   - `NEXSANDBASE_HOME=/vol2/1000/AI专用/所有自动化/轻如烟/sandglass`
   - `LMS_URL=http://localhost:8190`
   - `VECTOR_URL=http://192.168.0.103:11435/v1/embeddings`
2. **`glue_server.py`**：
   - 顶部新增轻量 `.env` 加载器 `_load_dotenv()`（零依赖，已存在的环境变量优先，不覆盖），必须在 import 读取环境的模块之前执行 —— 解决此前必须手工 source .env 才能启动的问题（与 LMS MCP 的 `set -a; . ./.env` 等效但内建）
   - 向量 URL 硬编码改为读环境变量（注释标注 Phase 3 D4）
3. **`interfaces/adapters/vector_adapter.py`**：构造默认 `api_url` 改为 `os.environ.get("VECTOR_URL", "http://192.168.0.103:11435/v1/embeddings")`（显式传参仍优先，向后兼容）

### 启动（setsid 防会话清理）
```bash
cd /vol2/1000/AI专用/memory-integration-layer
setsid bash -c 'exec python3 glue_server.py --host 127.0.0.1 --port 19000' \
  > /tmp/glue-server.log 2>&1 < /dev/null &
```
- PID：`72720`（首次 71558，中途为 fail-open 测试停过一次后重启）
- 日志：`/tmp/glue-server.log`

### 验证（curl 实测）
| 项 | 结果 |
|----|------|
| `GET /health` | `{"status":"ok","service":"glue-layer",...}` |
| `POST /store {"text":"Phase3 胶水层通电测试","source":"test"}` | `{"ok":true,"id":"c680750c...","vector":true,"entropy":5.32,"surprise":0.11}` |
| `POST /recall {"query":"胶水层","k":3}` | count=3，加权分 total≈0.608（text 0.3 + vector 0.614 + lms 0.003 融合可见） |
| `python3 glue_helper.py status` | sandglass 3668 / LMS 2 / vector healthy 全绿 |
| 沙漏 txt 追加 | ✅ `2026-08-04 17:26:19 | test | Phase3 胶水层通电测试`（3667→3668 行） |
| LMS 8190 收到 | ✅ /chat memory_state.turn_count 递增，memory_context 可召回该文本 |
| 向量服务被调用 | ✅ /recall 结果带 vector 分（0.614/0.582），证明 bge-m3 嵌入生效 |

---

## 二、任务 3：Dummy 降级验证（可替换性守卫）✅

实测（fail-open 语义全部符合设计）：

1. **停 glue_server**（kill 71558）→ 注入 `interfaces.store` 事件
   - 死信记录：`死信: handler=interfaces.store 异常: RuntimeError: glue /store 失败: URLError: Connection refused`（含 original_event 完整可追溯）
   - 总线不崩：消费者进程存活，轮询继续
2. **重启 glue_server**（PID 72720）→ 再注入 → 恢复工作
   - operation_log：`interfaces.store | OK | 胶水层 store 成功...`
   - 死信总数保持 2 条（仅 fail-open 测试那 2 条），无新增
3. **结论**：胶水后端可替换性保持 —— handler 层不感知后端故障，Dummy/降级路径（registry 异常隔离 → 死信 → 继续）工作正常

---

## 三、测试回归

- `pytest interfaces/test_interfaces.py tests/` → **80 passed, 1 failed**（与基线 80/81 完全一致）
- 唯一失败：`tests/test_vector_adapter.py::test_embed_connection_error_is_retryable` —— 原因是 `.venv` 未装 `requests`（测试模块级 `import requests`），**为既有问题，与本次改动无关**
- 手工验证：`VECTOR_URL` 环境变量可覆盖默认值；显式构造参数仍优先（向后兼容）

---

## 四、测试数据清理（安全红线）

沙漏 `sandglass.txt` 为追加式权威源，测试 store 追加了 4 行测试文本（17:26/17:28/17:29 三条 + 重启验证一条）。**已清理**：
- `head -n -4` 截断删除（4 行均为本次测试所加，位于文件尾部，不触碰真实记忆）
- `sandglass_vault.rebuild_index()` 重建索引，count 恢复 3667
- glue_server `/health` 复查 sandglass memory_count=3667 ✅

**遗留说明**：LMS（8190）无法经 HTTP 精确删除单条记忆，测试文本已以低价值条目留存于 LMS（`turn_count` 已计入），将随 LMS 自然衰减/做梦整合消化；如需彻底清除需 dandan 决定是否重置 session 或接受自然衰减。

---

## 五、遗留问题 / 观察项

1. **glue 与总线轮询竞态**：总线消费者 3s 轮询，事件注入到 handler 执行存在 ≤3s 延迟；若未来要求低延迟可缩短 poll_interval（当前无需）。
2. **recall 结果重复**：`/recall` 偶发返回同文本两条（沙漏 search 与融合排序的既有行为，非本次引入），后续可在 integration_service 按 fingerprint 去重。
3. **LMS 测试文本残留**：见第四节，观察 LMS 衰减即可。
4. **心跳噪音**：总线心跳 `sandglass.heartbeat` 每 5 分钟一条，纯观测不统合（Phase 2 既定），非本次引入。
5. **glue_server 日志缓冲**：setsid 重定向 stdout 后 Python 缓冲导致日志延迟落盘（进程存活时几乎看不到行），如需实时日志可加 `-u` 启动参数（不影响功能）。

---

## 六、安全红线遵守

- ✅ 只改 `memory-integration-layer`（+ `iso-sand`，见另一 changelog）；未碰丰碑/玄鉴/LMS 核心代码（LMS 仅经 8190 HTTP）
- ✅ 未做任何 git 操作；未打印任何 token/密钥
- ✅ 沙漏数据只追加（测试条目已按规则删除并重建索引）；真实记忆零改动
- ✅ glue_server 以 setsid 启动，防会话清理
