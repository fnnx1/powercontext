# SeekDB × StepFun：端侧迷你知识库

演示「浏览器本地向量化 → seekdb 存储 → seekdb 向量/全文混合检索」的完整流程，无云端推理 API。

## 快速启动

1. 启动 seekdb（本地 server 模式），例如：
   ```bash
   ./seekdb --nodaemon --base-dir=/tmp/seekdb-stepfun --port=2881
   ```
2. 安装并启动 Web 应用：
   ```bash
   cd stepfun
   npm install
   SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 SEEKDB_USER=root npm start
   ```
3. 打开 `http://127.0.0.1:7860`，点击「填入示例」→「导入并向量化」，再输入查询。

## 技术栈

- 端侧推理：`@xenova/transformers`（Transformers.js），模型 `Xenova/all-MiniLM-L6-v2`，384 维，INT8（`dtype: 'q8'`）。
- 后端：Node.js + `mysql2`，`seekdb-js/index.js` 提供 Promise 化 seekdb 客户端。
- 数据库：seekdb，表 `docs` 保存原文、可检索文本和 `VECTOR(384)`。
- 混合检索：单条 seekdb SQL 用 `MATCH ... AGAINST` 与 `l2_distance(...)` 两个候选分支 `UNION ALL`，再由 seekdb 聚合排序。

详细设计、量化说明、性能测量方法和限制见 [TECHNICAL.md](./TECHNICAL.md)。
