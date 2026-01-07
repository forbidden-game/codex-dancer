# Codex Orchestrator

这是一个最小可用的 Self-Planning Orchestrator：
- 第一步用 Codex 生成任务清单
- 并行执行多个 worker
- 最后用 Codex 汇总

## 用法

### One-shot

```bash
python3 scripts/codex_orchestrate.py "Summarize this repo and propose improvements" \
  --max-tasks 4 \
  --max-workers 3
```

### REPL

```bash
./scripts/codex-orch --repl
```

REPL 内置命令：
- `exit` / `quit`：退出
- `/reset`：清空历史

### 常用参数

- `--model`：指定模型
- `--profile`：指定 Codex profile
- `--workdir`：指定工作目录（传给 `codex exec -C`）
- `--codex-arg`：额外透传参数，示例：`--codex-arg --full-auto`
- `--output`：写出最终汇总到文件
- `--history-turns`：带入上下文的历史轮数
- `--history-file`：把历史持久化到文件
