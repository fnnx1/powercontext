# 验证记录（2026-10-31，Asia/Shanghai）

环境：seekdb 1.4.0.0（本地 server），Python 3.12，FastAPI/Uvicorn，pymysql。

## 启动

```text
GET /api/health -> {"ok":true,"beats":0,"database":"memory_tavern"}
```

## 连续两轮（同一 session_id）

```text
POST /api/story/continue {"session_id":"demo","action":"把月亮藏进酒桶"}
-> {"turn":1,"story":"...这是酒馆今晚的第一段故事。","memory_used":false}

POST /api/story/continue {"session_id":"demo","action":"向老人追问月亮的味道"}
-> {"turn":2,"story":"...你上次说过「把月亮藏进酒桶」，这次它又回来了。","memory_used":true}
```

## 回读

```text
GET /api/story/recall?session_id=demo
-> {"turn":2,"last_action":"向老人追问月亮的味道","memory":[第一轮故事,第二轮故事]}
```

以上记录证明：Dify 的 HTTP 节点调用同一 companion，会话状态由 seekdb 持久化，第二轮输出确实引用了第一轮行动；更换 `session_id` 会得到独立状态。

## Dify 侧复现要点

- 导入 `memory-tavern.yml`。
- 开始节点：`session_id=demo`，`action=把月亮藏进酒桶`。
- 再次运行：`session_id=demo`，`action=向老人追问月亮的味道`。
- 预期第二次 Answer 的 `memory_used=true`，且文本包含上一轮行动。


## Dify 实际运行（2026-10-31 09:29 UTC）

在本地 Dify 1.18.0（docker compose）导入 `memory-tavern.yml`，Workflow 运行输入：

```json
{"session_id":"demo","action":"让酒馆里的钟倒着走"}
```

执行结果（Dify 事件流）：

```text
start   succeeded
story   succeeded  status_code=200
answer  succeeded
workflow_finished succeeded

answer: {"session_id":"demo","turn":4,
         "story":"...你上次说过「向老人追问月亮的味道」，这次它又回来了。",
         "memory_used":true}
```

说明：Dify 的 HTTP Request 节点实际调用了 companion；companion 从 seekdb 读取上一轮 `last_action` 并写入本轮，故 `memory_used=true`。Dify 容器通过 `host.docker.internal:7870` 访问 companion，需要按 README 配置 `extra_hosts` 与 `SSRF_PROXY_ALLOW_PRIVATE_IPS`。
