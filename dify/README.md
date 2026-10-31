# 记忆酒馆 · Dify × seekdb

一个“会记住你上次胡闹”的互动说书小游戏。玩家每次对酒馆说一句行动，Dify 负责流程编排，调用本地 companion 服务；companion 从 **seekdb** 读取上一轮故事状态并写回，所以下一次输入会看到角色对上次选择的反应。

## 作品怎么玩

1. 输入一个会话 ID（例如 `demo`）和一句行动，例如「把月亮藏进酒桶」。
2. Dify 的 HTTP 节点调用 companion，返回本轮故事与 `memory_used`。
3. 再用同一会话输入「向老人追问月亮的味道」，故事会引用上一轮的行动。
4. 换一个会话 ID，故事从第一段重新开始，互不串线。

## 目录

- `memory-tavern.yml`：Dify Workflow DSL（Dify 1.x，`version: 0.1.5`）
- `companion/`：FastAPI + seekdb 的故事状态服务
- `data/`：验证用的初始化/示例数据说明
- `VERIFY.md`：实际运行记录与复现命令

## 启动

### 1. seekdb

```bash
# 任选已有的 seekdb 实例；companion 只需要 MySQL 协议
export SEEKDB_HOST=127.0.0.1
export SEEKDB_PORT=2881
export SEEKDB_USER=root
export SEEKDB_PASSWORD=
```

### 2. companion

```bash
cd companion
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 7870
```

验证：

```bash
curl -s http://127.0.0.1:7870/api/health
curl -s -X POST http://127.0.0.1:7870/api/story/continue \
  -H 'content-type: application/json' \
  -d '{"session_id":"demo","action":"把月亮藏进酒桶"}'
```

### 3. Dify

1. 启动 Dify 1.x（自托管或云端均可），在「工作室 → 导入 DSL」选择 `memory-tavern.yml`。
2. 如果 Dify 跑在 Docker 中，`host.docker.internal` 需要指向宿主机；Linux 上给 Dify 的 `api`、`worker`、`ssrf_proxy`、`agent_ssrf_proxy` 容器加：
   ```yaml
   extra_hosts:
     - "host.docker.internal:host-gateway"
   ```
   并让 SSRF 代理允许宿主机网段（Dify `.env`）：
   ```bash
   SSRF_PROXY_ALLOW_PRIVATE_IPS=172.17.0.0/16
   ```
   如果仍不可达，把 DSL 中 `http://host.docker.internal:7870` 改成 Dify 容器能访问的宿主机地址。
3. 该 Workflow **不需要模型服务**：所有叙事由 companion 的确定性模板生成，Dify 只负责输入、HTTP 调用和输出编排；因此没有 API Key 也能完整运行。
4. 打开应用，输入会话 ID 和行动即可。

## Dify 与 seekdb 的分工

- **Dify**：Workflow 入口、参数校验、HTTP Request 节点、输出 Answer；是应用的核心编排层。
- **seekdb**：`story_state` 保存每个会话的轮次与上一句行动；`story_beats` 保存历史故事，下一次输入由 companion 读取并注入叙事；同时为 `story` 建了全文索引，便于扩展为“按关键词回忆旧故事”。

## 已知限制

- companion 的叙事是模板生成，不是大模型；这是为了让没有模型 Key 的评审也能复现“记忆影响下一次输出”的核心体验。若配置了模型，可把 companion 换成一个 LLM 调用，Dify 流程不变。
- `story_beats` 的全文索引是可选的；首次启动若已存在会忽略重复创建错误。
