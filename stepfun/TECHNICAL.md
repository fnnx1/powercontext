# 技术说明（一页）

## 1. 模型与量化

- 模型：`Xenova/all-MiniLM-L6-v2`（ONNX 社区转换版，384 维，英文/多语短文本）
- 来源：Hugging Face `Xenova/all-MiniLM-L6-v2`
- 精度：INT8 动态量化（Transformers.js `dtype: 'q8'`），浏览器内 ONNX Runtime 执行
- 基线：同一模型的 FP32 ONNX 版本，可通过 `dtype: 'fp32'` 切换
- 体积：模型卡给出的 ONNX 文件约 90 MB（FP32）与约 23 MB（q8），压缩比约 **3.9×**。本次本地通过浏览器 resource timing 只确认加载了 Transformers.js（约 0.9 MB）和 ORT WASM（约 10 MB）；模型文件由 worker 下载，未在页面主线程 resource timing 中保留，因此不把 90/23 MB 写成实测值。

## 2. 端侧运行与降级

`public/app.js` 先检测 `navigator.gpu`：

- 有 WebGPU：`pipeline(..., { device: 'webgpu', dtype: 'q8' })`
- 无 WebGPU：自动使用 `{ device: 'wasm', dtype: 'q8' }`，页面 badge 显示 `WASM（WebGPU 不可用，已自动降级）`

文档与查询向量都在浏览器中计算，后端只接收向量，不调用任何云端推理 API。

## 3. seekdb 表与混合检索

```sql
CREATE TABLE docs (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  title VARCHAR(512) NOT NULL DEFAULT '',
  body MEDIUMTEXT NOT NULL,
  searchable_text MEDIUMTEXT NOT NULL,
  embedding VECTOR(384) NOT NULL,
  source VARCHAR(512) NOT NULL DEFAULT 'local',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE FULLTEXT INDEX ft_docs ON docs(searchable_text) WITH PARSER ik;
```

检索由 seekdb 的一条 SQL 完成：FTS 候选（`MATCH ... AGAINST`，按 score 排序取 topN）与 ANN 候选（`l2_distance(...) APPROXIMATE`，取 topN）先 `UNION ALL`，再由 seekdb 的 `GROUP BY` 聚合，按 `fts_score/(1+distance)` 排序返回。客户端不做两路结果拼合。

## 4. 增量写入与状态

- 每次导入是一批 `INSERT`，写入后即可被下一次 SQL 检索到；重复导入会追加。
- 页面维护当前知识库的文档数；切换/刷新后从 `/api/health` 读取 seekdb 中的实际条数。
- 本地测试记录：`POST /api/docs` 写入 2 篇，`POST /api/search` 返回 2 条，FTS score 与 distance 均由 seekdb 计算（见提交中的 `test/` 日志）。

## 5. 性能测量方法（可复现）

固定设备、Chrome/Edge 最新版、同一模型/精度/后端/批大小，记录：

1. 模型下载与首次加载（`performance.now()`，区分 cold/warm）
2. 文档向量化：批量大小 1/8/32，记录 tokens/s 与每篇耗时
3. 写入 + 索引刷新：从 `POST /api/docs` 到返回，及随后首次查询
4. 查询向量化与 seekdb 检索：分别计时 `/api/search` 的 embedding 与 SQL 时间
5. 端到端：从点击「搜索」到结果渲染

本次实际端到端运行（Edge 152 headless，WEBGPU，q8）：4 篇示例文档从点击导入到写入 seekdb **1.07 s**；查询「支持混合检索的数据库」从点击搜索到结果渲染 **1.01 s**；结果由 seekdb 的同一条 UNION ALL 混合检索 SQL 返回。该数据为 4 篇小样本，不作为大规模性能结论；正式对比应在至少 1,000 篇文档上重复 3 次取中位数。

## 6. 已知限制

- `@xenova/transformers` 首次运行需要从 Hugging Face/CDN 下载模型；这是模型获取，不是云端推理。
- 本地 seekdb 的 `seekdb-js` npm 包在比赛镜像中不可用，因此 `stepfun/seekdb-js` 是一个 Promise 化薄客户端；SQL 和检索全部由 seekdb 执行。
- 当前未建 ANN 索引，数据量增大时应增加向量索引；检索 SQL 已按 `APPROXIMATE` 编写，便于后续接入。
