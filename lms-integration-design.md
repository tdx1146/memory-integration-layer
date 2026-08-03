# LMS集成到OpenClaw方案设计

> 日期：2026-07-29
> 背景：LMS-PyTorch已部署测试通过，需要集成到OpenClaw中与沙漏双系统并行运行

---

## 一、方案评估与推荐

### 方案1：OpenClaw插件 + Python子进程通信  ← 推荐

**原理**：封装LMS为OpenClaw插件（CommonJS/Node.js），通过`spawn`启动LMS的Python交互子进程，通过stdin/stdout JSON-RPC通信。

**理由**：
1. ✅ **与现有架构一致**——沙漏注入插件(qingruyan-behavior-enforcer)和沙漏日志插件(sandglass-logger)都采用`spawn`调用Python的模式，有成熟参考
2. ✅ **进程隔离**——LMS运行在独立Python进程中，异常不会拖死OpenClaw
3. ✅ **资源可控**——LMS需要GPU/PyTorch，独立进程不干扰主进程
4. ✅ **可复用**——未来可拆为独立MCP服务
5. ✅ **无架构变更**——OpenClaw插件系统已有的`before_prompt_build`和`agent_end`钩子完美适配LMS的"记忆注入"和"记忆日志"两阶段

### 方案2：纯MCP服务（延迟实施）

**理由**：当前OpenClaw的MCP服务端（dandan-mcp-server.mjs + sandglass_mcp.py）运行正常，但LMS作为"记忆系统"需要实时注入到prompt（不是工具调用式的查询），MCP的`tools/list + tools/call`轮询模式不适合注入场景。未来可拆分出独立的LMS MCP服务供其他AI使用。

**计划安排**：先做插件方案，以后再拆MCP。

### 方案3：Python子进程直接接入（不推荐）

**理由**：缺少OpenClaw的生命周期管理、错误恢复、日志聚合。

---

## 二、集成架构

```
┌─────────────────────────────────────────────────────┐
│                   OpenClaw 进程                      │
│                                                      │
│  ┌─────────────────────┐    ┌────────────────────┐  │
│  │  qingruyan-behavior │    │  sandglass-logger  │  │
│  │  -enforcer (沙漏)    │    │  (沙漏日志)        │  │
│  └─────────┬───────────┘    └────────┬───────────┘  │
│            │ spawn(python3)          │ spawn        │
│            ▼                          ▼              │
│  ┌─────────────────────────────────────────────┐    │
│  │        lms-memory-plugin (新增)              │    │
│  │  ┌──────────┐   ┌────────────┐              │    │
│  │  │processor │   │ 记忆注入    │              │    │
│  │  │管理器    │   │ (injector) │              │    │
│  │  └────┬─────┘   └─────┬──────┘              │    │
│  └───────┼───────────────┼─────────────────────┘    │
│          │               │                           │
│          ▼               ▼                           │
│  ┌─────────────────────────────────────────────┐    │
│  │          lms_mcp_bridge.py (子进程)          │    │
│  │  stdin/stdout JSON-RPC                      │    │
│  │  ┌────────────┐   ┌──────────────────┐      │    │
│  │  │ MemoryPool  │   │ ProcessTurn      │      │    │
│  │  │ (状态池)    │   │ 接口             │      │    │
│  │  └────────────┘   └────────┬─────────┘      │    │
│  └────────────────────────────┼─────────────────┘    │
│                               │                       │
│                               ▼                       │
│  ┌─────────────────────────────────────────────┐    │
│  │       LivingMemoryLoop (海马体核心)           │    │
│  │  ┌──────────┐ ┌──────┐ ┌───────┐ ┌──────┐ │    │
│  │  │Attractor │ │Purpose│ │Memory │ │Cloud │ │    │
│  │  │Network   │ │Layer  │ │Manager│ │Embed │ │    │
│  │  └──────────┘ └──────┘ └───────┘ └──────┘ │    │
│  └─────────────────────────────────────────────┘    │
│                                                      │
│              embed服务(192.168.0.103:11435)          │
└─────────────────────────────────────────────────────┘
```

### 数据流

```
用户输入 ──→ before_prompt_build钩子
                │
                ├──→ 沙漏插件：注入沙漏记忆（现有）
                │
                └──→ LMS插件：调用MCP桥 → process_turn(text)
                         │
                         ├──→ LivingMemoryLoop.process_turn(user_input)
                         │       → 编码 → FEP推断 → 学习 → 记忆存储
                         │       → 记忆检索 → 解码 → 返回context
                         │
                         └──→ 返回 "记忆状态摘要" 注入prependSystemContext

LLM回复 ──→ agent_end钩子
                │
                └──→ LMS插件：调用MCP桥 → process_turn(text, llm_output)
                         → 编码LLM回复 → 更新海马体

后续对话 ──→ before_prompt_build钩子
                │
                └──→ LMS插件：返回包含"感知熵变化/记忆关联"的注入
```

---

## 三、文件结构

```
# OpenClaw插件目录
~/.openclaw/plugins/lms-memory-plugin/
├── openclaw.plugin.json          # 插件注册文件
├── index.js                      # 插件入口 (CommonJS)
├── package.json                  # (可选) npm依赖
└── README.md                     # 说明

# LMS仓库侧新增文件
/vol2/1000/AI专用/living-memory-system-cloud/
├── bridge/
│   └── mcp_server.py             # ← 新增：JSON-RPC stdio服务端
└── ... (原有文件不变)
```

### 不需要修改的已有文件

| 文件 | 原因 |
|------|------|
| `run_lms_pytorch.py` | 保留原样作为独立启动入口 |
| `runtime/loop.py` | LivingMemoryLoop核心无需修改 |
| `bridge/encoder.py` | 编码器保持通用 |
| `bridge/decoder.py` | 解码器保持通用 |
| `bridge/llm_bridge.py` | LLM桥接器保持通用 |
| `persistence/snapshot.py` | 快照逻辑保持不变 |

---

## 四、示例代码框架

### 4.1 LMS侧：MCP桥接服务 (mcp_server.py)

```python
#!/usr/bin/env python3
"""
LMS MCP Bridge — 通过 stdin/stdout JSON-RPC 协议提供服务。

协议：
    → {"id": 1, "method": "ping"}
    ← {"id": 1, "result": "pong"}

    → {"id": 2, "method": "init", "params": {"snapshot_path": "..."}}
    ← {"id": 2, "result": {"status": "ok", "turn_count": 0}}

    → {"id": 3, "method": "process_turn", "params": {"text": "..."}}
    ← {"id": 3, "result": {"context": "...", "entropy": 0.45, "surprise": 0.12}}

    → {"id": 4, "method": "get_status"}
    ← {"id": 4, "result": {"turn_count": 5, "entropy": 0.33, ...}}

    → {"id": 5, "method": "save_snapshot", "params": {"path": "..."}}
    ← {"id": 5, "result": {"status": "ok"}}

    → {"id": 6, "method": "shutdown"}
    ← {"id": 6, "result": "bye"}
"""

import sys
import json
import logging
import traceback

logging.basicConfig(
    level=logging.INFO,
    format="[LMS-MCP] %(message)s",
)
logger = logging.getLogger("lms_mcp")


class LMSMCPServer:
    """LMS MCP 服务器，通过 stdin/stdout JSON-RPC 通信。"""

    def __init__(self):
        self.loop = None

    def handle_request(self, request: dict) -> dict:
        """处理单个JSON-RPC请求。"""
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        try:
            if method == "ping":
                return {"id": req_id, "result": "pong"}

            elif method == "init":
                return self._cmd_init(params)

            elif method == "process_turn":
                return self._cmd_process_turn(params)

            elif method == "get_status":
                return self._cmd_get_status()

            elif method == "save_snapshot":
                return self._cmd_save_snapshot(params)

            elif method == "shutdown":
                result = {"id": req_id, "result": "bye"}
                # 发送响应后进程退出
                print(json.dumps(result, ensure_ascii=False), flush=True)
                sys.exit(0)

            else:
                return {"id": req_id, "error": f"unknown method: {method}"}

        except Exception as e:
            logger.error(f"Error handling {method}: {e}\n{traceback.format_exc()}")
            return {"id": req_id, "error": str(e)}

    def _cmd_init(self, params: dict) -> dict:
        """初始化LivingMemoryLoop（首次调用时创建）。
        
        可传参：
            snapshot_path: 从快照恢复状态（可选）
        """
        if self.loop is not None:
            return {"id": None, "result": {"status": "already_initialized", "turn_count": self.loop.turn_count}}

        # 导入必要的模块
        sys.path.insert(0, "/vol2/1000/AI专用/living-memory-system-cloud")
        from runtime.loop import LivingMemoryLoop
        from runtime.config import default_config
        from core.sensory.cloud_embedder import CloudEmbedder

        # 构建配置
        config = default_config()
        config.update({
            'num_nodes': 1100,
            'input_dim': 1024,
        })

        # 创建CloudEmbedder
        embedder = CloudEmbedder(
            api_url=params.get('embed_url', 'http://192.168.0.103:11435/v1/embeddings'),
            dim=1024,
            model=None,
            cache_size=512,
        )
        config['embedder'] = embedder

        # 初始化循环
        self.loop = LivingMemoryLoop(config)

        # 可选：从快照恢复
        snapshot_path = params.get('snapshot_path')
        if snapshot_path:
            try:
                self.loop.load_state(snapshot_path)
                logger.info(f"从快照恢复: {snapshot_path}")
            except Exception as e:
                logger.warning(f"快照恢复失败，使用初始状态: {e}")

        logger.info(f"LMS初始化完成: nodes={config['num_nodes']}, dim={config['input_dim']}")
        return {"id": None, "result": {"status": "ok", "turn_count": self.loop.turn_count}}

    def _cmd_process_turn(self, params: dict) -> dict:
        """处理一轮对话，返回记忆context。"""
        if self.loop is None:
            return {"id": None, "error": "not initialized, call init first"}

        text = params.get("text", "")
        llm_output = params.get("llm_output", "")

        context = self.loop.process_turn(text, llm_output)
        status = self.loop.get_status()

        return {"id": None, "result": {
            "context": context,
            "entropy": status.get("last_entropy", 0),
            "surprise": status.get("last_surprise", 0),
            "turn_count": status.get("turn_count", 0),
            "precision_mean": status.get("precision_mean", 0),
        }}

    def _cmd_get_status(self) -> dict:
        """获取当前状态摘要。"""
        if self.loop is None:
            return {"id": None, "result": {"status": "not_initialized"}}

        status = self.loop.get_status()
        return {"id": None, "result": status}

    def _cmd_save_snapshot(self, params: dict) -> dict:
        """保存当前状态到快照。"""
        if self.loop is None:
            return {"id": None, "error": "not initialized"}

        path = params.get("path", "/vol2/1000/AI专用/living-memory-system-cloud/snapshots/latest.pt")
        self.loop.save_state(path)
        return {"id": None, "result": {"status": "ok", "path": path}}

    def run(self):
        """主循环：从stdin读取JSON-RPC请求，处理，输出到stdout。"""
        logger.info("LMS MCP Server started (stdin/stdout JSON-RPC)")
        # 输出就绪信号
        sys.stdout.write(json.dumps({"event": "ready"}) + "\n")
        sys.stdout.flush()

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON: {line[:100]}")
                continue

            response = self.handle_request(request)
            if response:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()


def main():
    server = LMSMCPServer()
    server.run()


if __name__ == "__main__":
    main()
```

### 4.2 OpenClaw插件入口 (index.js)

```javascript
// LMS记忆插件 — 通过stdin/stdout JSON-RPC与LMS子进程通信
// 提供：before_prompt_build注入记忆context + agent_end记录对话

const { spawn } = require("child_process");
const path = require("path");

// ─── 配置 ───────────────────────────────────────────
const LMS_PROJECT = "/vol2/1000/AI专用/living-memory-system-cloud";
const LMS_MCP_SCRIPT = path.join(LMS_PROJECT, "bridge", "mcp_server.py");
const LMS_SNAPSHOT = path.join(LMS_PROJECT, "snapshots", "lms_latest.pt");
const LOCK_FILE = "/tmp/lms-memory-plugin.lock";  // 进程锁

// ─── MCP进程管理器 ──────────────────────────────────
class LMSProcessManager {
  constructor() {
    this.process = null;
    this.pending = new Map();     // id -> { resolve, reject, timer }
    this.buffer = "";
    this.nextId = 1;
    this.ready = false;
    this.readyQueue = [];         // 等待ready状态的任务
  }

  start() {
    if (this.process) return;

    this.process = spawn("python3", [LMS_MCP_SCRIPT], {
      cwd: LMS_PROJECT,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: "1",
      },
      stdio: ["pipe", "pipe", "pipe"],
    });

    // 解析stdout JSON行
    this.process.stdout.on("data", (data) => {
      this.buffer += data.toString();
      const lines = this.buffer.split("\n");
      this.buffer = lines.pop();  // 不完全行留在buffer

      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const msg = JSON.parse(line);

          // 就绪事件
          if (msg.event === "ready") {
            this.ready = true;
            this._drainReadyQueue();
            return;
          }

          // 响应（JSON-RPC）
          if (msg.id !== undefined) {
            const pending = this.pending.get(msg.id);
            if (pending) {
              clearTimeout(pending.timer);
              this.pending.delete(msg.id);
              if (msg.error) {
                pending.reject(new Error(msg.error));
              } else {
                pending.resolve(msg.result);
              }
            }
          }
        } catch (e) {
          // 忽略非JSON行（日志等）
        }
      }
    });

    // stderr → 日志
    this.process.stderr.on("data", (data) => {
      const text = data.toString();
      // 转发LMS日志（前缀标识来源）
      text.split("\n").filter(Boolean).forEach(line => {
        console.log(`[LMS-plugin] ${line}`);
      });
    });

    // 进程退出 → 自动重启
    this.process.on("exit", (code) => {
      console.log(`[LMS-plugin] Process exited (code=${code}), restarting in 1s...`);
      this.process = null;
      this.ready = false;
      setTimeout(() => this.start(), 1000);
    });

    this.process.on("error", (err) => {
      console.error(`[LMS-plugin] Process error: ${err.message}`);
    });
  }

  _drainReadyQueue() {
    const queue = this.readyQueue;
    this.readyQueue = [];
    for (const item of queue) {
      item.resolve();
    }
  }

  waitReady() {
    if (this.ready) return Promise.resolve();
    return new Promise((resolve) => {
      this.readyQueue.push({ resolve });
    });
  }

  async call(method, params = {}) {
    if (!this.process) {
      this.start();
    }

    await this.waitReady();

    const id = this.nextId++;
    const request = JSON.stringify({ id, method, params });

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`LMS RPC timeout: ${method}`));
      }, 15000);

      this.pending.set(id, { resolve, reject, timer });
      this.process.stdin.write(request + "\n");
    });
  }

  async init() {
    try {
      const result = await this.call("init", {
        embed_url: "http://192.168.0.103:11435/v1/embeddings",
        snapshot_path: LMS_SNAPSHOT,
      });
      return result;
    } catch (e) {
      // 快照不存在或恢复失败时可忽略（首次运行）
      console.log(`[LMS-plugin] Init warning: ${e.message}`);
      return { status: "fallback" };
    }
  }

  async processTurn(text, llmOutput = "") {
    return await this.call("process_turn", { text, llm_output: llmOutput });
  }

  async getStatus() {
    return await this.call("get_status");
  }

  async saveSnapshot(path) {
    return await this.call("save_snapshot", { path });
  }

  shutdown() {
    if (this.process) {
      this.call("shutdown").catch(() => {});
    }
  }
}

// ─── 全局单例 ───────────────────────────────────────
const lms = new LMSProcessManager();

// ─── 插件导出 ───────────────────────────────────────
module.exports = {
  id: "lms-memory-plugin",
  name: "LMS记忆插件",
  register(api) {
    // === 初始化 ===
    // 在插件加载阶段启动LMS子进程
    lms.start();
    lms.init().then(result => {
      console.log(`[LMS-plugin] Initialized: ${JSON.stringify(result)}`);
    }).catch(err => {
      console.error(`[LMS-plugin] Init failed: ${err.message}`);
    });

    // === before_prompt_build：注入记忆context ===
    api.on(
      "before_prompt_build",
      async (event) => {
        const prompt = event.prompt || "";

        // 跳过静默期
        if (prompt.includes("静默期")) return;

        try {
          // 将用户当前输入送入LMS，获取记忆context
          const result = await lms.processTurn(prompt);
          const context = result.context || "";

          // 构造注入块（只在新对话有显著记忆变化时注入）
          if (context && !context.includes("无显著激活")) {
            const entropy = result.entropy || 0;
            const surprise = result.surprise || 0;
            const turnCount = result.turn_count || 0;

            const injection = [
              "## 🧠 海马体记忆",
              "",
              `轮次: ${turnCount} | 熵: ${entropy.toFixed(3)} | 惊讶度: ${surprise.toFixed(3)}`,
              "",
              context,
            ].join("\n");

            return {
              prependSystemContext: injection,
            };
          }
        } catch (err) {
          console.error(`[LMS-plugin] before_prompt_build error: ${err.message}`);
        }
      },
      { priority: 90 },  // 沙漏插件是100，LMS设为90（在沙漏之后注入）
    );

    // === agent_end：将对话记录到LMS ===
    api.on(
      "agent_end",
      async (event) => {
        if (event.aborted || event.timedOut || event.externalAbort) return;

        const messages = event.messages || [];
        const lastUser = messages.filter(m => m.role === "user").pop();
        const lastAssistant = messages.filter(m => m.role === "assistant").pop();

        if (!lastUser && !lastAssistant) return;

        const userText = typeof lastUser?.content === "string"
          ? lastUser.content
          : (Array.isArray(lastUser?.content)
            ? lastUser.content.filter(c => c.type === "text").map(c => c.text).join("\n")
            : "");

        const assistantTexts = typeof lastAssistant?.content === "string"
          ? [lastAssistant.content]
          : (Array.isArray(lastAssistant?.content)
            ? lastAssistant.content.filter(c => c.type === "text").map(c => c.text)
            : []);

        try {
          // 将完整对话轮次送入LMS（用户输入 + LLM回复）
          if (userText && userText.trim()) {
            const llmOutput = assistantTexts.join("\n").substring(0, 2000);
            await lms.processTurn(userText.trim(), llmOutput);
          }

          // 每10轮自动保存快照
          const status = await lms.getStatus();
          if (status && status.turn_count && status.turn_count % 10 === 0) {
            lms.saveSnapshot(LMS_SNAPSHOT).catch(e => {
              console.error(`[LMS-plugin] Snapshot save failed: ${e.message}`);
            });
          }
        } catch (err) {
          console.error(`[LMS-plugin] agent_end error: ${err.message}`);
        }
      },
      { priority: 5 },  // 低优先级，在sandglass-logger(10)之后
    );

    // === 进程退出时清理 ===
    process.on("exit", () => {
      lms.shutdown();
    });
  },
};
```

### 4.3 插件注册文件 (openclaw.plugin.json)

```json
{
  "id": "lms-memory-plugin",
  "name": "LMS记忆插件",
  "version": "1.0.0",
  "description": "将LMS(PyTorch)活体记忆系统集成到OpenClaw，提供海马体记忆context注入",
  "entry": "index.js",
  "configSchema": {
    "type": "object",
    "properties": {
      "snapshot_path": {
        "type": "string",
        "description": "LMS状态快照路径",
        "default": "/vol2/1000/AI专用/living-memory-system-cloud/snapshots/lms_latest.pt"
      },
      "embed_url": {
        "type": "string",
        "description": "嵌入服务URL",
        "default": "http://192.168.0.103:11435/v1/embeddings"
      },
      "auto_snapshot_interval": {
        "type": "integer",
        "description": "自动快照间隔（轮次）",
        "default": 10
      }
    },
    "additionalProperties": false
  },
  "contracts": {
    "hooks": {
      "allowPromptInjection": true,
      "allowConversationAccess": true
    }
  }
}
```

---

## 五、测试方法

### 5.1 单独测试MCP桥接服务

```bash
# 启动MCP服务（手动测试）
cd /vol2/1000/AI专用/living-memory-system-cloud
python3 bridge/mcp_server.py &

# 发送测试命令
echo '{"id":1,"method":"ping"}' | nc localhost 0  # stdin/stdio模式
```

或者编写测试脚本：

```python
# test_lms_mcp.py
import subprocess
import json

proc = subprocess.Popen(
    ["python3", "bridge/mcp_server.py"],
    cwd="/vol2/1000/AI专用/living-memory-system-cloud",
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)

# 等待ready
line = proc.stdout.readline()
assert json.loads(line)["event"] == "ready"
print("✅ MCP Server ready")

# 测试init
proc.stdin.write(json.dumps({"id": 1, "method": "init"}) + "\n")
proc.stdin.flush()
line = proc.stdout.readline()
result = json.loads(line)
assert result["result"]["status"] == "ok"
print(f"✅ Init OK: {result['result']}")

# 测试process_turn
proc.stdin.write(json.dumps({
    "id": 2, "method": "process_turn",
    "params": {"text": "你好，今天天气真好"}
}) + "\n")
proc.stdin.flush()
line = proc.stdout.readline()
result = json.loads(line)
assert "context" in result["result"]
print(f"✅ ProcessTurn: entropy={result['result']['entropy']:.3f}")

# 测试shutdown
proc.stdin.write(json.dumps({"id": 3, "method": "shutdown"}) + "\n")
proc.stdin.flush()
proc.wait()
print("✅ Shutdown OK")
```

### 5.2 插件加载测试

```bash
# 在OpenClaw配置中启用插件
# plugins.entries.lms-memory-plugin: enabled: true

# 检查插件日志
tail -f /var/log/openclaw/plugin-lms.log

# 确认进程存在
ps aux | grep mcp_server.py

# 检查注入内容
cat /tmp/plugin-lms-injection.txt  # (插件内可设调试文件)
```

### 5.3 端到端测试

```bash
# 启动LMS MCP服务（独立运行）
cd /vol2/1000/AI专用/living-memory-system-cloud
python3 -c "
import sys; sys.path.insert(0, '.')
from bridge.mcp_server import LMSMCPServer
server = LMSMCPServer()
server.run()
" &

# 然后通过标准JSON-RPC测试
python3 -c "
import json, sys
# ... 完整的集成测试
"
```

---

## 六、与沙漏系统的共存策略

| 维度 | 沙漏系统 | LMS系统 |
|------|---------|---------|
| 记忆类型 | 对话日志（文本稠密） | 吸引子网络（向量稀疏） |
| 注入方式 | before_prompt_build注入画像+历史 | before_prompt_build注入海马体context |
| 写入方式 | agent_end写入sandglass.txt | agent_end写入吸引子网络+情景缓冲区 |
| 进程模型 | spawn python3 CLI | spawn python3 MCP |
| 优先级 | 100（最高） | 90（沙漏之后） |
| 数据持久化 | sandglass.txt文件 | .pt快照文件 |
| 嵌入服务 | 内置TF-IDF | CloudEmbedder (11435) |

**双系统注入示例效果**：

```
[沙漏脉冲] 画像: 决策平稳 | 状态: 省钱(+0%) | 🎭平稳
[沙漏召回] 相关记忆: [2026-07-29] ...
[海马体记忆] 轮次: 47 | 熵: 0.423 | 惊讶度: 0.087
  激活节点: 节点42(中:0.312), 节点107(强:0.678), ...
  长时记忆检索: 强度2.341, 活跃维度: ...
  历史记忆: "1. "关于LMS连接... " | "2. "丰碑网络..."
```

---

## 七、进程管理策略

### 7.1 启动策略
- 插件加载时立即启动LMS子进程（`lms.start()`）
- 首次启动时自动init（创建LivingMemoryLoop实例）
- 尝试从最新快照恢复（首次运行无快照时友好降级）

### 7.2 重启策略
- 进程退出时自动重启（1秒延迟）
- 重启后重新init
- 连续3次重启失败则在锁定时间内不再重试（防止快速循环）

### 7.3 超时处理
- RPC调用默认15秒超时
- `process_turn`如果耗时过长不会阻塞主线程
- 超时后跳过本轮注入，LMS继续在后台处理

### 7.4 快照策略
- `agent_end`钩子每10轮自动保存快照
- 快照保存异步执行，不阻塞对话
- 保存失败记录日志，不影响正常运行

---

## 八、错误处理清单

```javascript
// 场景1：LMS子进程初始化失败
// → 跳过注入，记录警告，进程中不会崩溃
// → 后续agent_end依然尝试写入（LMS可能已恢复）

// 场景2：process_turn超时
// → 返回空context，不注入
// → 主插件流程继续

// 场景3：embed服务(11435)不可达
// → LivingMemoryLoop.process_turn内部会处理
// → 返回"嵌入服务不可用"的降级context
// → 插件不需要感知

// 场景4：快照恢复失败
// → 使用初始状态（空海马体）
// → 新系统从零开始习得记忆

// 场景5：快照保存失败
// → 记录日志，忽略
// → 下次agent_end再次尝试保存
```

---

## 九、部署步骤

```bash
# 1. 创建插件目录
mkdir -p ~/.openclaw/plugins/lms-memory-plugin

# 2. 复制插件文件
cp index.js ~/.openclaw/plugins/lms-memory-plugin/
cp openclaw.plugin.json ~/.openclaw/plugins/lms-memory-plugin/

# 3. 创建LMS MCP桥接文件
cp bridge/mcp_server.py /vol2/1000/AI专用/living-memory-system-cloud/bridge/

# 4. 确保快照目录存在
mkdir -p /vol2/1000/AI专用/living-memory-system-cloud/snapshots/

# 5. 在OpenClaw配置中启用插件
# 编辑 ~/.openclaw/config.yaml 或通过API添加：
# plugins:
#   entries:
#     lms-memory-plugin:
#       enabled: true

# 6. 重启OpenClaw加载插件
openclaw gateway restart

# 7. 验证插件加载
openclaw gateway status | grep lms-memory
# 应输出: Plugin: lms-memory-plugin (enabled)

# 8. 验证LMS子进程
ps aux | grep mcp_server.py
# 应看到python3 bridge/mcp_server.py进程

# 9. 测试一轮对话
# 在任意通道发送消息，确认系统提示包含 "海马体记忆" 块
```

---

## 十、后续优化方向

1. **分离为MCP服务**：将来把LMS MCP拆为HTTP服务（参考丰碑网络`mcp-api-server.py`），使其他AI可直接调用
2. **双系统记忆关联**：让沙漏的文本记忆和LMS的向量记忆可以互相检索
3. **性能优化**：LMS初始化耗时约2-3秒（加载模型+创建网络），考虑预启动
4. **监控面板**：在OpenClaw dashboard中显示LMS状态（熵值曲线、记忆强度等）
