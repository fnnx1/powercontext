# 一页技术说明

## 结构

```text
specs/pricing.md ─┐
src/pricing.py   ├─ seekdb.sdd_documents ──SQL 检索──> context_builder
tests/test_pricing.py ─┘                                      │
                                                               ▼
                                              MiniMax-M3.1-Flash
                                                               │
                                            完整替换 pricing.py + diff
                                                               │
                                      pytest 目标测试 + 回归测试
                                                               │
                                      PowerContext Atomic Memory
```

## 分工

| 技术 | 实际作用 |
|---|---|
| seekdb / OceanBase | 保存规格、源码、测试三份工程资料；`context_builder` 通过 `MATCH ... AGAINST` / LIKE 查询选出的真实行构造上下文；引用为 `seekdb://sdd_documents/...` |
| MiniMax | `minimax_client.generate()` 把任务、规格、源码、测试发送给 `MiniMax-M3.1-Flash`，返回完整 `pricing.py`，模块落盘并生成 unified diff；记录 request id、token usage、响应哈希 |
| PowerContext | 每次 `context`/`generate` 后向 Atomic Memory 写入一条 `sdd_task_state`；`state` 子进程重新打开同一 scope 并取最大 `state_seq`，用于恢复任务状态 |
| pytest | 修改前目标测试失败、回归通过；修改后全部通过；日志含命令和退出码 |

## 上下文选择

1. `init` 把三份资料导入 `sdd_documents`；
2. `build_context(task)` 用任务文本查询 seekdb；
3. 若全文检索不可用则回退到 `LIKE`；
4. 输出必须包含 spec/code/test 三种真实路径，缺失则报错而不是编造引用；
5. 规格文件更新后重新 `init`，查询结果会返回新内容（见 `runs/spec-update-context.json`）。

## 状态复用

- PowerContext SQLite 数据库：`.state/powercontext.db`
- scope 标题：`SDD Engineering Context`
- 每条状态包含 `state_seq`，重启后 `state` 命令取最大序号；
- 最新状态为 `code_changed`，包含 MiniMax request id、diff 路径、测试 exit code 和 6 passed。

## 限制

- 演示工程很小，关键词检索足够；大工程应替换召回/排序策略；
- seekdb 和 MiniMax Key 通过环境变量提供，不提交凭据；
- MiniMax 只生成一次完整文件替换，不做多轮自主修复；测试失败会返回非零退出码。
