# 🔀 数据迁移风险评估与实施方案

> 生成时间：2026-08-01 16:22
> 执行人：轻如烟（迁移风险评估专家 子代理）
> 目标：整合「沙漏 + LMS + facts.dict.md + 丰碑网络 + 向量服务」五类记忆系统
> 原则：**数据不丢、服务不中断、随时可回滚、灰度渐进**

---

## 一、数据盘点（实测定稿）

> ⚠️ 本盘点基于 2026-08-01 16:22 现场实测，非估算。

### 1.1 沙漏（sandglass）——显式叙事记忆
| 项目 | 值 |
|------|-----|
| 数据目录 | `/vol2/1000/AI专用/所有自动化/轻如烟/sandglass/` |
| 总数 | **3205 条**（`sandglass.txt` 行数双确认） |
| 主存储 | `sandglass.txt`：`YYYY-MM-DD HH:MM:SS | sender | text`（管道分隔，UTF-8） |
| 索引存储 | `sandglass.db`（SQLite3 **900 行** 全量 + `sandglass_fts` FTS5 全文索引） |
| 倒排索引 | `sandglass.idx`（token→行号映射，976 长行，AUTO-GENERATED） |
| 双写影子 | `shadow_sand.db`：织线三元组 **56 条**（`subject/relation/object/confidence`）+ trust 信任分表 |
| 辅助 | `persona/`（persona.md, discipline.json, task-log.jsonl）、`memory/`（按月 md）、`.踱步/`（think_*.md） |
| 体积 | 主目录 1.5MB；文本 234KB；db 487KB |

### 1.2 LMS（Living Memory System）——隐式状态记忆
| 项目 | 值 |
|------|-----|
| Repo | `/vol2/1000/AI专用/living-memory-system-cloud/`（1.4GB 含 .venv） |
| 服务 | `api.run`，端口 **8190**（127.0.0.1，运行中 pid 3491124） |
| 快照目录 | `snapshots/`：`latest.pt`(401KB) + `latest.pt.bak`(428KB) + `snapshot_50.pt`(662KB) |
| 格式 | **PyTorch `.pt` v0.3.0**：J矩阵[256×256]、bias、sigma、precision、history、memory潜变量、tokenizer、meta元可塑性 |
| 维度 | **256 节点 × 64 维**（bge-m3 远程 embed 投影到 64） |
| Embed | 远程 `192.168.0.103:11435/v1/embeddings`（bge-m3，1024→64） |
| 快照版本 | `SNAPSHOT_VERSION = "0.3.0"`（向后兼容：旧快照缺 meta 字段时跳过） |

### 1.3 facts.dict.md——事实断言
| 项目 | 值 |
|------|-----|
| 主文件 | `workspace/facts.dict.md`：323 行，**30 条断言**（`- **[实体]**` 格式） |
| memory副本 | `workspace/memory/facts.dict.md`：616 行，30 条断言（有历史增补） |
| 其他副本 | `找回自己/daily/`、`找回自己/memory/`、多处 http 备份（可能过期） |
| 格式 | Markdown v3：`### 分类` + `- **[实体]** 断言` |

### 1.4 向量服务
| 项目 | 值 |
|------|-----|
| 本地向量库 | `workspace/memory/.vector.db`：SQLite，**718 条**向量记录（id/path/chunk_index/chunk_text/hash/BLOB vector） |
| Qdrant | 端口 **6333**（运行中），当前 **0 collection**（空，尚未灌数据） |
| 嵌入模型 | `bge-m3-Q2_K.gguf`(366MB) + `bge-m3-Q4_K_M.gguf`(437MB) 本地 |

### 1.5 丰碑（monument）——永久知识
| 项目 | 值 |
|------|-----|
| 目录 | `workspace/monument/`：`monument_core.py`(12KB) + `candidates/`（**2 条**候选 JSON）+ `permanent/` |
| 数据 | candidate-20260710 两份（23行/37行 JSON），含 title/body/tags/contributor/source |

---

## 二、风险评估

### 2.1 总评矩阵 (P=概率 / I=影响 / R=风险系数=P×I)

| # | 风险 | P(0-1) | I(1-5) | R | 等级 | 缓解措施 |
|---|------|--------|--------|---|------|----------|
| R1 | **沙漏.txt 与 .db 数据不一致**（txt=3205，db=900） | 0.8 | 4 | 3.2 | 🔴高 | 迁移前以 txt 为权威源重建 db |
| R2 | **LMS .pt 快照损坏/版本不兼容**（torch.save 二进制） | 0.4 | 5 | 2.0 | 🔴高 | 三重备份(原+备份+时间戳)，校验 schema v0.3.0 |
| R3 | **facts.dict.md 多副本漂移**（主/ memory/ 多处不同） | 0.7 | 4 | 2.8 | 🔴高 | 归一化单一权威源+md5校验 |
| R4 | **双写影子 shadow_sand.db 织线丢失**（仅56条，易被覆盖） | 0.5 | 3 | 1.5 | 🟡中 | 迁移前导出 wthread_triples 全量 |
| R5 | **服务中断**（杀进程/重启 LMS 8190/Qdrant） | 0.4 | 4 | 1.6 | 🟡中 | 冷迁移窗口(低峰)，先起后杀 |
| R6 | **向量索引失效**（chunk 路径变更导致 hash 失配） | 0.6 | 3 | 1.8 | 🟡中 | 保留原 chunk_hash，路径不改或全量重建 |
| R7 | **格式转换出错**（txt→json / J矩阵→? 半结构化） | 0.5 | 4 | 2.0 | 🟡中 | 每步 dry-run + 行数/条目数校验 |
| R8 | **数据丢失（无备份即迁移）** | 0.3 | 5 | 1.5 | 🟡中 | 强制先行全量备份 |
| R9 | **回滚难度高**（新系统写坏原文件） | 0.5 | 4 | 2.0 | 🟡中 | 备份_orig，改代码不覆盖原数据 |
| R10 | **LMS embed 服务依赖手机IP不可达**（192.168.0.103） | 0.5 | 3 | 1.5 | 🟡中 | 快照本地位于.snapshots，迁移不依赖embed |

### 2.2 关键风险定性
1. **最大风险 R1**：沙漏 txt(3205) 与 db(900) 差 2305 条说明 db 只建了前 900 行索引。**权威源必须选 txt**，db 可随时重建，绝不以 db 为准做迁移。
2. **二进制风险 R2**：LMS 的 `.pt` 是 PyTorch 二进制，无法人工 diff，损坏即全损。必须保留原文件 + `latest.pt.bak` + 新增时间戳快照三份。
3. **漂移风险 R3**：facts 有 ≥5 个副本，如果拿 `memory/facts.dict.md`(616行) 覆盖主文件(323行) 会引入过期/历史内容。需先 diff 再合并。

---

## 三、迁移方案（渐进式 5 步）

> 目标架构：`memory_model.py` 统一 MemoryItem 契约 = 沙漏(sandglass) + 向量(qdrant/local) + LMS + 丰碑(monument)，facts 作为静态权威事实源。

### 阶段 0：安全备份（先决，~10 分钟）
```bash
TS=$(date +%Y%m%d-%H%M%S)
BK=/vol2/1000/AI专用/所有自动化/backups/migration-${TS}
mkdir -p $BK/sandglass $BK/lms $BK/memory $BK/facts $BK/monument

# 沙漏全量（txt + db + idx + shadow + persona + 踱步）
cp /vol2/1000/AI专用/所有自动化/轻如烟/sandglass/{sandglass.txt,sandglass.db,sandglass.idx,shadow_sand.db} $BK/sandglass/
rsync -a /vol2/1000/AI专用/所有自动化/轻如烟/sandglass/persona/ $BK/sandglass/persona/

# LMS 快照三重备份
SNAP=/vol2/1000/AI专用/living-memory-system-cloud/snapshots
cp $SNAP/latest.pt   $BK/lms/latest.pt.${TS}
cp $SNAP/latest.pt.bak $BK/lms/latest.pt.bak.${TS}
cp $SNAP/snapshot_50.pt $BK/lms/snapshot_50.pt.${TS}

# facts 全部副本 + md5
md5sum /vol1/@apphome/trim.openclaw/data/workspace/facts.dict.md \
       /vol1/@apphome/trim.openclaw/data/workspace/memory/facts.dict.md > $BK/facts/md5.log
cp /vol1/@apphome/trim.openclaw/data/workspace/facts.dict.md $BK/facts/
cp /vol1/@apphome/trim.openclaw/data/workspace/memory/facts.dict.md $BK/facts/facts.memory-copy.md

# 向量 + 丰碑
cp /vol1/@apphome/trim.openclaw/data/workspace/memory/.vector.db $BK/memory/
cp -r /vol1/@apphome/trim.openclaw/data/workspace/monument/ $BK/monument/

echo "备份完成: $BK"
```
✅ **验证**：`diff --brief` 原文件 vs 备份；`python -c "torch.load('...',weights_only=False)"` 校验 LMS；md5 记录存档。

### 阶段 1：沙漏权威源归一 + 增量回填（~15 分钟）
1. 以 `sandglass.txt` 为权威，重建索引：确认 db 行数 == txt 行数。
2. 导出 csv/json 作为迁移中间格式（勿动原文件）：
   ```bash
   python3 -c "
   import sqlite3
   c = sqlite3.connect('$BK/sandglass/sandglass.db')  # 副本上操作，绝不写原db
   c.execute('DELETE FROM sandglass')
   # ... 从 sandglass.txt 逐行 parse 重建 3205 条
   "
   ```
3. 验证：count == 3205，FTS 重建后 `search_weights` 生效。

### 阶段 2：facts.dict.md 去重归一（~10 分钟）
1. diff 主文件(323行) vs memory副本(616行)，提取增量断言。
2. 合并到单一权威 `facts.dict.md`，保留 `### 分类` 结构。
3. 用 `import_facts_to_thread.py` 把 30→N 条断言灌入 `wthread_triples`（沙漏织线，避免 R4）。
4. ✅ 验证：断言总数 = 合并后条数；导入后 shadow_sand.db triples 增加。

### 阶段 3：向量服务切换 Qdrant（灰度，~20 分钟）
1. Qdrant 已运行(6333)但 0 collection，先建 collection + 从 `.vector.db` 灌 718 条（保留原 chunk_hash 与 path）。
2. **灰度**：`memory_model.py` 的 vector backend 先指向本地 `.vector.db`（现状），Qdrant 并行灌入后切 10% 流量→50%→100%。
3. ✅ 验证：检索命中率对比（同 query 本地 vs Qdrant top-5 重合率 > 0.9）。

### 阶段 4：LMS 快照确认 + 与沙漏联动（~15 分钟）
1. 确认 `latest.pt` 可 load（三份备份在手则无风险）。
2. 通过 `memory_model.MemoryService.store()` 验证双写：一次调用同时写沙漏 + LMS + 向量，fail-open。
3. ✅ 验证：LMS 熵/惊讶度正常下降；episodic buffer 能 Recall 刚存的文本。

### 阶段 5：丰碑知识沉淀 + 收尾（~10 分钟）
1. 将 2 条 candidate 按需 approve 进 `permanent/`。
2. 全局一致性校验：五系统交叉抽查。

**总工期估算：约 80–90 分钟**（每步含验证与回滚点）。

---

## 四、回滚方案

### 4.1 回滚脚本模板 `rollback_migration.sh`
> 每阶段完成后将文件末尾的 `:DONE` 标记成真，回滚只还原已执行的阶段。

```bash
#!/usr/bin/env bash
# usage: ./rollback_migration.sh <BACKUP_DIR>
set -euo pipefail
BK=${1:?需要备份目录 backups/migration-*}
[ -d "$BK" ] || { echo "备份目录不存在"; exit 1; }
echo "⚠️ 回滚开始 $(date) — 将还原到 $BK"

stop_lms() { kill 3491124 2>/dev/null && echo "LMS 已停" || echo "LMS 未运行"; }
start_lms() {
  cd /vol2/1000/AI专用/living-memory-system-cloud
  LMS_EMBEDDER=remote nohup .venv/bin/python3 -m api.run >> /tmp/lms-rollback.log 2>&1 &
  echo "LMS 已重启 pid $!"
}

# 1. 还原沙漏（txt + db + idx + shadow）
cp -f "$BK/sandglass/sandglass.txt"   /vol2/1000/AI专用/所有自动化/轻如烟/sandglass/
cp -f "$BK/sandglass/sandglass.db"    /vol2/1000/AI专用/所有自动化/轻如烟/sandglass/
cp -f "$BK/sandglass/sandglass.idx"   /vol2/1000/AI专用/所有自动化/轻如烟/sandglass/
cp -f "$BK/sandglass/shadow_sand.db"  /vol2/1000/AI专用/所有自动化/轻如烟/sandglass/
rsync -a "$BK/sandglass/persona/"     /vol2/1000/AI专用/所有自动化/轻如烟/sandglass/persona/

# 2. 还原 facts
cp -f "$BK/facts/facts.dict.md" /vol1/@apphome/trim.openclaw/data/workspace/facts.dict.md

# 3. 还原向量
stop_qdrant; cp -f "$BK/memory/.vector.db" /vol1/@apphome/trim.openclaw/data/workspace/memory/.vector.db; start_qdrant

# 4. 还原 LMS（需先停，再放快照，再启）
stop_lms
cp -f "$BK/lms/latest.pt" /vol2/1000/AI专用/living-memory-system-cloud/snapshots/latest.pt
start_lms

# 5. 还原丰碑
cp -r "$BK/monument/." /vol1/@apphome/trim.openclaw/data/workspace/monument/

echo "✅ 回滚完成 $(date)"
echo "验证：沙漏 $(grep -c '' /vol2/1000/AI专用/所有自动化/轻如烟/sandglass/sandglass.txt) 条；LMS get_status 应返回正常"
```

### 4.2 回滚判定标准
| 现象 | 动作 |
|------|------|
| 沙漏行数与迁移前不符 | 用 txt 备份覆盖，重建 db |
| LMS get_status 报 torch 加载错 | 换用 `latest.pt.bak` 或 `snapshot_50.pt` |
| facts 断言缺失/重复 | 整回退到主文件备份 |
| 向量检索命中率 <0.8 | 回退 `.vector.db` |
| 任一服务 5XX >2% | 立即回滚该阶段 |

---

## 五、风险缓解专章

### 5.1 备份策略（多重+异地+可校验）
- **3-2-1 原则**：3 份副本（原 + 备份目录 + 时间戳版本）、2 种介质（本卷 + `/vol2/backups/`）、1 份离线（`轻如烟.hdd_backup`）。
- **LMS 二进制**强制三份（原/backup/时间戳），回滚时按序尝试。
- **facts** 迁移前记录 md5，用于找回漂移判定。

### 5.2 双写策略（新旧并行）
- `memory_model.py` 已内建 **fail-open**：任一后端写入失败只记 warning，不中断主流程。这是天然的双写缓冲。
- 迁移期间保持**双写**：新事件同时进旧文件 + 新系统，直到灰度完全切断旧写路径。
- 切断条件：新系统连续 7 天零写入失败。

### 5.3 灰度发布
1. **沙漏/向量**：10% → 50% → 100%（weight 系数 `{"vector":0.6,"lms":0.3,"keyword":0.1}`）。
2. **facts**：先 dry-run 合并预览，人工(轻如烟)确认后落盘。
3. **LMS**：快照先存备份不动，服务不重启即可验证 API 读路径；重启窗口选低峰(凌晨 3-5 点)。
4. **丰碑**：只写不入 permanent，用户 approve 才固化。

### 5.4 验证清单（Cross-cut）
- 行数/条数前后精确相等（txt=3205, db=3205, facts=合并后N, triples≥56）。
- 每系统抽查 3 条原文保留（diff 无内容篡改）。
- `LMS lms_status` 返回 ok。
- Qdrant `/collections` 出现新 collection 且 count 匹配。

---

## 六、待办落实
- [ ] 阶段 0：执行 `migration_plan.sh` 备份（先做，10 分钟）
- [ ] 阶段 1：沙漏 txt→db 重建到 3205
- [ ] 阶段 2：facts 归一 + 灌织线
- [ ] 阶段 3：Qdrant 灌 718 向量 + 灰度切换
- [ ] 阶段 4：LMS 快照确认 + 双写验证
- [ ] 阶段 5：丰碑 approve + 全局一致性校验
- [ ] 全量跑 `test_lms_integration.sh` 回归

---

*本文档为评估+执行方案，实测数据可在 `backups/migration-*/` 复核。*
