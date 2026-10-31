# MiniMax × OceanBase：SDD 工程上下文模块

一个可复现的规格驱动开发（SDD）闭环，包含：

- **业务工程**：`sample_app/`，一个带测试的 Python 定价模块；
- **seekdb**：保存规格、代码、测试三类工程资料，上下文由真实 SQL 查询结果构造；
- **MiniMax**：真实调用 `MiniMax-M3.1-Flash`，根据上下文生成 `pricing.py` 的完整替换代码并落盘；
- **PowerContext**：把任务状态写入 Atomic Memory，进程重启后从同一条 PowerContext 记录读取最新状态；
- **测试闭环**：修改前目标测试失败、回归测试通过；修改后 6 条测试全部通过。

## 目录

```text
minimax/
  sample_app/
    specs/pricing.md          # 规格
    src/pricing.py            # 被修改的业务代码
    tests/test_pricing.py     # 目标测试 + 回归测试
  src/sdd_context/
    db.py                     # seekdb 初始化、导入、SQL 检索
    context_builder.py        # 由查询结果构造上下文
    minimax_client.py         # MiniMax API 调用 + 替换代码 + diff
    power_state.py            # PowerContext Atomic Memory 状态保存/读取
    cli.py                    # 统一入口
  tests/test_sdd_context.py   # 上下文/规格更新契约测试
  runs/                       # 运行日志、diff、MiniMax 记录、状态快照
  scripts/run_demo.sh
```

## 环境

要求：Python 3.11+、一个可连接的 seekdb（默认 `127.0.0.1:2898`，root 空密码）。

```bash
cd minimax
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export MINIMAX_API_KEY='sk-api-...'     # 不要提交到仓库
```

连接参数可用 `.env.example` 中的 `SEEKDB_*` 环境变量覆盖。数据库和表会自动创建。

## 一键复现

```bash
export PYTHONPATH=src
.venv/bin/python -m sdd_context.cli reset
.venv/bin/python -m sdd_context.cli init --reset
.venv/bin/python -m sdd_context.cli test --label before   # 2 failed, 4 passed
.venv/bin/python -m sdd_context.cli generate              # MiniMax 改代码，6 passed
.venv/bin/python -m sdd_context.cli state                 # 新进程读取 PowerContext 状态
```

也可以：

```bash
MINIMAX_API_KEY=... ./scripts/run_demo.sh
```

## 调用入口

`PYTHONPATH=src python -m sdd_context.cli <command>`

| 命令 | 作用 |
|---|---|
| `init [--reset]` | 创建数据库/表，导入 `specs`、`src`、`tests` |
| `context --task ...` | 用 seekdb SQL 选择资料，输出上下文并保存 `context_ready` 状态 |
| `generate --task ...` | 构造上下文 → 调 MiniMax → 替换 `pricing.py` → 跑测试 → 保存 `code_changed` 状态 |
| `test --label before/after` | 跑测试并记录退出码到 `runs/` |
| `state` | **新进程**读取最新 PowerContext 状态 |
| `history` | 读取所有历史状态 |
| `reset` | 从 `runs/before.py` 恢复修改前基线（用于 before 测试） |

## 验证样例

任务：给定价模块增加“数量 ≥ 10 的行打 9 折，折扣在税前列，少量行与原有税/舍入行为不变”。

- 修改前：`test_target_*` 2 条失败，`test_regression_*` 4 条通过；
- 修改后：6 条全部通过；
- MiniMax 记录：`runs/minimax_call.json`、`runs/minimax_response.txt`、`runs/minimax_change.diff`；
- 上下文：`runs/context.json`；
- 规格更新验证：`runs/spec-update-context.json` 含更新后的 marker，`runs/spec-restored-context.json` 恢复原规格；
- 状态复用：`runs/powercontext-state.json` 是 `state` 子进程在修改后读取到的最新 `code_changed` 记录。

## 已知限制

- 需要外部 seekdb 和 MiniMax API Key，均通过环境变量配置；
- `sample_app` 是演示业务工程，不是生产定价系统；
- 上下文选择使用关键词全文检索，适合本样例的小型资料集；更大工程可替换 `db.query_materials` 的排序/召回策略；
- PowerContext 状态使用其 SQLite 内置 profile，状态文件在 `.state/`，不提交仓库。
