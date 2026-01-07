#!/usr/bin/env python3
import argparse
import asyncio
import json
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Task:
    task_id: str
    title: str
    prompt: str
    scope: str


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            return "\n".join(lines[1:-1]).strip()
    return text


def _load_json(text: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _normalize_tasks(payload: Any, fallback_prompt: str, max_tasks: int) -> List[Task]:
    if isinstance(payload, list):
        raw_tasks = payload
    elif isinstance(payload, dict):
        raw_tasks = payload.get("tasks", [])
    else:
        raw_tasks = []
    tasks: List[Task] = []
    for idx, item in enumerate(raw_tasks, start=1):
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("id") or idx)
        title = str(item.get("title") or f"Task {idx}")
        prompt = str(item.get("prompt") or title)
        scope = str(item.get("scope") or "read-only")
        tasks.append(Task(task_id=task_id, title=title, prompt=prompt, scope=scope))
        if len(tasks) >= max_tasks:
            break
    if not tasks:
        tasks.append(Task(task_id="1", title="Main", prompt=fallback_prompt, scope="read-only"))
    return tasks


def _build_planner_prompt(user_request: str, max_tasks: int) -> str:
    return textwrap.dedent(
        f"""
        You are a planner. Produce a JSON object with a single key \"tasks\".
        Each task must be an object with: id, title, prompt, scope.
        - id: short string
        - title: short summary
        - prompt: instructions for a worker agent
        - scope: one of \"read-only\" or \"write\"
        Constraints:
        - Max tasks: {max_tasks}
        - Keep prompts concise and actionable
        - Return ONLY valid JSON, no Markdown
        User request:
        {user_request}
        """
    ).strip()


def _build_worker_prompt(task: Task, user_request: str) -> str:
    return textwrap.dedent(
        f"""
        CONTEXT: WORKER
        ROLE: You are a sub-agent run by the ORCHESTRATOR. Do only the assigned task.
        RULES: No extra scope, no other workers.
        Your final output will be provided back to the ORCHESTRATOR.
        TASK: {task.prompt}
        SCOPE: {task.scope}
        USER_REQUEST: {user_request}
        """
    ).strip()


def _build_summarizer_prompt(user_request: str, worker_outputs: List[str]) -> str:
    joined = "\n\n".join(
        f"[Worker {idx+1}]\n{output}" for idx, output in enumerate(worker_outputs)
    )
    return textwrap.dedent(
        f"""
        You are the orchestrator. Synthesize the workers' outputs into a single response.
        Requirements:
        - Start with a direct conclusion
        - List key reasoning briefly
        - Provide concrete next steps
        - Resolve conflicts between workers if any
        User request:
        {user_request}
        Worker outputs:
        {joined}
        """
    ).strip()


def _format_history(history: List[Dict[str, str]], max_turns: int) -> str:
    if max_turns <= 0:
        return ""
    trimmed = history[-max_turns * 2 :]
    lines: List[str] = []
    for item in trimmed:
        role = item.get("role", "user")
        content = item.get("content", "")
        lines.append(f"{role.upper()}: {content}")
    return "\n".join(lines).strip()


def _compose_request(user_input: str, history: List[Dict[str, str]], max_turns: int) -> str:
    history_text = _format_history(history, max_turns)
    if not history_text:
        return user_input
    return textwrap.dedent(
        f"""
        Conversation history:
        {history_text}

        Current user request:
        {user_input}
        """
    ).strip()


def _build_codex_cmd(
    prompt: str,
    output_path: Path,
    model: Optional[str],
    profile: Optional[str],
    codex_args: List[str],
    workdir: Optional[Path],
) -> List[str]:
    cmd = ["codex", "exec", "--output-last-message", str(output_path), "--skip-git-repo-check"]
    if model:
        cmd += ["-m", model]
    if profile:
        cmd += ["-p", profile]
    if workdir:
        cmd += ["-C", str(workdir)]
    if codex_args:
        cmd += codex_args
    cmd.append(prompt)
    return cmd


async def _run_worker(
    task: Task,
    user_request: str,
    output_path: Path,
    model: Optional[str],
    profile: Optional[str],
    codex_args: List[str],
    workdir: Optional[Path],
    timeout_s: Optional[int],
    semaphore: asyncio.Semaphore,
) -> None:
    prompt = _build_worker_prompt(task, user_request)
    cmd = _build_codex_cmd(prompt, output_path, model, profile, codex_args, workdir)
    async with semaphore:
        proc = await asyncio.create_subprocess_exec(*cmd)
        try:
            if timeout_s is None:
                await proc.wait()
            else:
                await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


def _read_output(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


async def _run_codex(cmd: List[str]) -> int:
    proc = await asyncio.create_subprocess_exec(*cmd)
    code = await proc.wait()
    if code != 0:
        raise RuntimeError(f"codex exec failed with exit code {code}")
    return code


async def _run_once(args: argparse.Namespace, user_request: str) -> str:
    workdir = Path(args.workdir).resolve() if args.workdir else None

    with tempfile.TemporaryDirectory(prefix="codex_orchestrate_") as tmpdir:
        tmp_path = Path(tmpdir)

        planner_out = tmp_path / "planner.json"
        planner_prompt = _build_planner_prompt(user_request, args.max_tasks)
        planner_cmd = _build_codex_cmd(
            planner_prompt,
            planner_out,
            args.model,
            args.profile,
            args.codex_arg,
            workdir,
        )
        await _run_codex(planner_cmd)

        planner_raw = _read_output(planner_out)
        planner_raw = _strip_code_fences(planner_raw)
        payload = _load_json(planner_raw) or {}
        tasks = _normalize_tasks(payload, user_request, args.max_tasks)

        semaphore = asyncio.Semaphore(max(1, args.max_workers))
        worker_jobs = []
        worker_outputs: List[Path] = []
        for idx, task in enumerate(tasks, start=1):
            out_path = tmp_path / f"worker_{idx}.txt"
            worker_outputs.append(out_path)
            worker_jobs.append(
                _run_worker(
                    task,
                    user_request,
                    out_path,
                    args.model,
                    args.profile,
                    args.codex_arg,
                    workdir,
                    args.timeout,
                    semaphore,
                )
            )

        await asyncio.gather(*worker_jobs)

        worker_texts = [_read_output(p) for p in worker_outputs]

        summary_out = tmp_path / "summary.txt"
        summary_prompt = _build_summarizer_prompt(user_request, worker_texts)
        summary_cmd = _build_codex_cmd(
            summary_prompt,
            summary_out,
            args.model,
            args.profile,
            args.codex_arg,
            workdir,
        )
        await _run_codex(summary_cmd)

        summary_text = _read_output(summary_out)
        return summary_text


async def _run_all(args: argparse.Namespace) -> int:
    if not args.repl:
        if not args.prompt:
            raise ValueError("prompt is required unless --repl is set")
        summary_text = await _run_once(args, args.prompt)
        if args.output:
            Path(args.output).write_text(summary_text, encoding="utf-8")
        sys.stdout.write(summary_text + "\n")
        return 0

    history: List[Dict[str, str]] = []
    if args.history_file:
        history_path = Path(args.history_file)
        if history_path.exists():
            try:
                history = json.loads(history_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                history = []

    while True:
        try:
            user_input = input("codex-orch> ").strip()
        except EOFError:
            break
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            break
        if user_input.lower() == "/reset":
            history = []
            continue

        history.append({"role": "user", "content": user_input})
        composed = _compose_request(user_input, history[:-1], args.history_turns)
        summary_text = await _run_once(args, composed)
        sys.stdout.write(summary_text + "\n")
        history.append({"role": "assistant", "content": summary_text})

        if args.history_file:
            Path(args.history_file).write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Self-planning Codex orchestrator")
    parser.add_argument("prompt", nargs="?", default="", help="User request to orchestrate")
    parser.add_argument("--max-tasks", type=int, default=4, help="Maximum number of worker tasks")
    parser.add_argument("--max-workers", type=int, default=3, help="Maximum parallel workers")
    parser.add_argument("--timeout", type=int, default=None, help="Timeout seconds per worker")
    parser.add_argument("--repl", action="store_true", help="Run in REPL mode")
    parser.add_argument("--history-turns", type=int, default=6, help="Number of prior turns to include")
    parser.add_argument("--history-file", default=None, help="Persist conversation history to file")
    parser.add_argument("--model", default=None, help="Codex model override")
    parser.add_argument("--profile", default=None, help="Codex profile override")
    parser.add_argument("--workdir", default=None, help="Working directory for Codex runs")
    parser.add_argument("--codex-arg", action="append", default=[], help="Extra arg for codex exec")
    parser.add_argument("--output", default=None, help="Write final summary to file")
    args = parser.parse_args()

    return asyncio.run(_run_all(args))


if __name__ == "__main__":
    raise SystemExit(main())
