# 复现与验收记录

环境：seekdb v1.4.0 (127.0.0.1:2898) + Python 3.14 venv + PowerContext 1.4.1 + MiniMax-M3.1-Flash。

| 检查 | 命令 | 结果 |
|---|---|---|
| seekdb 导入 | `PYTHONPATH=src python -m sdd_context.cli init --reset` | 导入 spec/code/test 三份资料 |
| 修改前测试 | `PYTHONPATH=src python -m sdd_context.cli test --label before` | exit 1；2 failed, 4 passed；目标测试因未实现折扣失败 |
| MiniMax 调用 | `PYTHONPATH=src python -m sdd_context.cli generate` | model=MiniMax-M3.1-Flash；request_id=0735111972069dac2e80337658c5960a；usage total_tokens=1150 |
| 修改后测试 | 同上，写入 `runs/test-after.log` | exit 0；6 passed |
| 上下文契约 | `PYTHONPATH=src python -m pytest -q tests/test_sdd_context.py` | 2 passed |
| 规格更新 | 修改 spec 后 `init` + `context` | `runs/spec-update-context.json` 含 `Updated requirement marker` |
| 规格恢复 | 恢复 spec 后 `init` + `context` | `runs/spec-restored-context.json` 不含 marker |
| 状态重启读取 | 新进程 `python -m sdd_context.cli state` | `status=code_changed`，`state_seq=7`，`tests.exit_code=0` |

关键文件：

- `runs/test-before.log`
- `runs/test-after.log`
- `runs/minimax_call.json`
- `runs/minimax_response.txt`
- `runs/minimax_change.diff`
- `runs/context.json`
- `runs/spec-update-context.json`
- `runs/spec-restored-context.json`
- `runs/powercontext-state.json`
- `runs/powercontext-history.json`
