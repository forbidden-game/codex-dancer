# Codex Orchestrator

这是一个最小可用的 Self-Planning Orchestrator：
- 主会话负责规划与整合
- 并行执行多个 worker
- worker 输出回灌主会话得到最终答复

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
- `/reset-session`：清空主会话并重新开始

### 常用参数

- `--model`：指定模型
- `--main-model`：主会话（plan/synthesize）模型
- `--worker-model`：worker 默认模型
- `--agent-model`：指定 agent 的模型，格式 `agent=model`，可重复
- `--profile`：指定 Codex profile
- `--workdir`：指定工作目录（传给 `codex exec -C`）
- `--codex-arg`：额外透传参数，示例：`--codex-arg --full-auto`
- `--output`：写出最终汇总到文件
- `--session-file`：持久化主会话 ID（默认 `.codex_orch_session.json`）
- `--use-last-session`：使用 `codex exec resume --last`
- `--events-file`：输出 JSONL 事件流到文件
- `--events-append`：事件流追加写入
- `--events-stdout`：输出 JSONL 事件流到 stdout
- `--events-stdout-only`：事件流输出到 stdout，summary 输出到 stderr

## Agent 说明

Plan 阶段输出的每个 task 现在包含 `agent` 字段，用于选择不同的 worker 行为与模型。

可用 agent：
- `general`：默认执行/分析
- `explore`：内部代码搜索与模式发现
- `librarian`：外部文档与标准参考
- `oracle`：复杂推理与架构权衡
- `frontend`：UI/UX 与视觉相关
- `writer`：文档与对外文字

示例：为不同 agent 指定不同模型

```bash
python3 scripts/codex_orchestrate.py "Review this repo and propose improvements" \
  --main-model gpt-5.2 \
  --worker-model gpt-5.1 \
  --agent-model explore=gpt-5.1 \
  --agent-model librarian=gpt-5.1 \
  --agent-model oracle=gpt-5.2
```

## 事件流（JSONL）

事件 Schema：`schemas/codex_orch_events.schema.json`

启用 `--events-file` 或 `--events-stdout` 后会输出以下事件类型（每行一条 JSON）：
- `run_start` / `run_end`
- `main_start` / `main_end`（phase: plan | plan_repair | synthesize）
- `tasks_planned`
- `worker_start` / `worker_end`

示例（仅展示结构）：
```json
{"type":"worker_start","schema_version":"v1","run_id":"...","worker_id":"1","title":"Task 1","scope":"read-only","agent":"explore","model":"gpt-5.1","ts":1700000000.0}
{"type":"worker_end","schema_version":"v1","run_id":"...","worker_id":"1","status":"ok","exit_code":0,"agent":"explore","model":"gpt-5.1","ts":1700000001.2}
```
