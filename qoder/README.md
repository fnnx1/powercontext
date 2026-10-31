# Memory Tavern · Qoder Sites

一个用 Qoder Sites 发布的互动说书小游戏：玩家对酒馆说一句话，故事会根据 seekdb 中保存的上一轮行动继续；换一个旅人代号就是全新的记忆。

## 文件

- `site/`：Qoder Sites 发布的静态站点（HTML/CSS/JS）
- `../dify/companion/`：本地 FastAPI + seekdb 记忆服务（本作品通过公开隧道调用）
- 线上 API：`https://yes-efforts-factors-tutorials.trycloudflare.com`（本地 companion 的临时公网地址）

## 技术结合

- **Qoder Sites**：站点通过 `qoder sites prepare_site` / `publish_site` 发布。
- **seekdb**：companion 把每个 `session_id` 的 `turn`、`last_action` 与 `story_beats` 写入 seekdb；第二次输入时从 `story_state` 读回上一轮行动，故事里会引用它。这是应用核心功能，不是展示用的连接测试。
- **PowerContext**：可选的记忆/上下文层；本作品直接使用 seekdb 持久化，符合“至少一种”的要求。

## 体验

打开 Qoder Sites 地址，输入 `demo` 和「把月亮藏进酒桶」，再输入「向老人追问月亮的味道」。第二段会出现“你上次说过……”，`记忆账本` 也会追加两条。

## 本地复现

```bash
cd ../dify/companion
SEEKDB_PORT=2881 .venv/bin/uvicorn app:app --host 0.0.0.0 --port 7870
python3 -m http.server 8080 -d site
```

修改 `site/app.js` 的 `API` 为你的 companion 公网地址，然后用 Qoder CLI 发布：

```bash
qoder sites prepare_site --input '{"actionId":"<uuid>","projectRoot":"qoder","webDirectory":"site","displayName":"Memory Tavern","slug":"memory-tavern"}'
qoder sites publish_site --input '{"actionId":"<same-uuid>"}'
```
