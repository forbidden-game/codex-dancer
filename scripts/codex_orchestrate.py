#!/usr/bin/env python3
import argparse
import asyncio
import json
import sys
import tempfile
import textwrap
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

EVENT_SCHEMA_VERSION = "v1"


@dataclass
class Task:
    task_id: str
    title: str
    prompt: str
    scope: str


@dataclass
class EventSink:
    streams: List[Any]
    lock: asyncio.Lock
    run_id: Optional[str] = None

    async def emit(self, event: Dict[str, Any]) -> None:
        if not self.streams:
            return
        payload = dict(event)
        payload.setdefault("ts", time.time())
        payload.setdefault("schema_version", EVENT_SCHEMA_VERSION)
        if self.run_id:
            payload.setdefault("run_id", self.run_id)
        line = json.dumps(payload, ensure_ascii=False)
        async with self.lock:
            for stream in self.streams:
                stream.write(line + "\n")
                stream.flush()


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


def _build_main_plan_prompt(user_request: str) -> str:
    return textwrap.dedent(
        f"""
        You are the main orchestrator. Do not execute tasks directly.
        Phase: PLAN
        - Produce a JSON object with key "tasks".
        - Each task must include: id, title, prompt, scope.
        - scope must be one of "read-only" or "write".
        - Return ONLY valid JSON, no Markdown.
        User request:
        {user_request}
        """
    ).strip()


def _build_main_synthesis_prompt(user_request: str, worker_outputs: List[str]) -> str:
    joined = "\n\n".join(
        f"[Worker {idx+1}]\n{output}" for idx, output in enumerate(worker_outputs)
    )
    return textwrap.dedent(
        f"""
        You are the main orchestrator. Use the workers' outputs to answer the user.
        Phase: SYNTHESIZE
        Requirements:
        - Start with a direct conclusion.
        - Provide brief reasoning.
        - Provide concrete next steps if applicable.
        User request:
        {user_request}
        Worker outputs:
        {joined}
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


def _build_codex_resume_cmd(
    session_id: Optional[str],
    prompt: str,
    output_path: Path,
    model: Optional[str],
    codex_args: List[str],
) -> List[str]:
    cmd = [
        "codex",
        "exec",
        "resume",
        "--output-last-message",
        str(output_path),
        "--skip-git-repo-check",
        "--json",
    ]
    if model:
        cmd += ["-m", model]
    if codex_args:
        cmd += codex_args
    if session_id:
        cmd.append(session_id)
    else:
        cmd.append("--last")
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
    event_sink: EventSink,
) -> None:
    prompt = _build_worker_prompt(task, user_request)
    cmd = _build_codex_cmd(prompt, output_path, model, profile, codex_args, workdir)
    async with semaphore:
        await event_sink.emit(
            {
                "type": "worker_start",
                "worker_id": task.task_id,
                "title": task.title,
                "scope": task.scope,
            }
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            if timeout_s is None:
                await proc.wait()
            else:
                await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            await event_sink.emit(
                {
                    "type": "worker_end",
                    "worker_id": task.task_id,
                    "status": "timeout",
                }
            )
            return
        status = "ok" if proc.returncode == 0 else "error"
        await event_sink.emit(
            {
                "type": "worker_end",
                "worker_id": task.task_id,
                "status": status,
                "exit_code": proc.returncode,
            }
        )


def _read_output(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _extract_session_id(events: Iterable[Dict[str, Any]]) -> Optional[str]:
    def walk(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"session_id", "sessionId"} and isinstance(item, str):
                    return item
                found = walk(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return None

    for event in events:
        found = walk(event)
        if found:
            return found
    return None


async def _run_codex_json(cmd: List[str]) -> Tuple[int, List[Dict[str, Any]], str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    events: List[Dict[str, Any]] = []
    if proc.stdout:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    stderr_text = ""
    if proc.stderr:
        stderr_text = (await proc.stderr.read()).decode("utf-8", errors="replace")
    code = await proc.wait()
    if code != 0:
        raise RuntimeError(f"codex exec failed with exit code {code}. {stderr_text.strip()}")
    return code, events, stderr_text


async def _run_main(
    args: argparse.Namespace,
    prompt: str,
    session_id: Optional[str],
    event_sink: EventSink,
    phase: str,
) -> Tuple[str, Optional[str]]:
    with tempfile.TemporaryDirectory(prefix="codex_orchestrate_") as tmpdir:
        tmp_path = Path(tmpdir)
        output_path = tmp_path / "main.txt"
        await event_sink.emit({"type": "main_start", "phase": phase})
        if session_id is None and args.use_last_session:
            cmd = _build_codex_resume_cmd(
                None,
                prompt,
                output_path,
                args.model,
                args.codex_arg,
            )
            _, events, _ = await _run_codex_json(cmd)
            response = _read_output(output_path)
            session = _extract_session_id(events)
            await event_sink.emit(
                {
                    "type": "main_end",
                    "phase": phase,
                    "session_id": session,
                }
            )
            return response, session

        if session_id:
            cmd = _build_codex_resume_cmd(
                session_id,
                prompt,
                output_path,
                args.model,
                args.codex_arg,
            )
            _, events, _ = await _run_codex_json(cmd)
            response = _read_output(output_path)
            session = _extract_session_id(events) or session_id
            await event_sink.emit(
                {
                    "type": "main_end",
                    "phase": phase,
                    "session_id": session,
                }
            )
            return response, session

        workdir = Path(args.workdir).resolve() if args.workdir else None
        cmd = _build_codex_cmd(
            prompt,
            output_path,
            args.model,
            args.profile,
            args.codex_arg,
            workdir,
        )
        cmd.insert(2, "--json")
        _, events, _ = await _run_codex_json(cmd)
        response = _read_output(output_path)
        session = _extract_session_id(events)
        await event_sink.emit(
            {
                "type": "main_end",
                "phase": phase,
                "session_id": session,
            }
        )
        return response, session


def _load_session_id(path: Optional[Path]) -> Optional[str]:
    if not path or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        value = payload.get("session_id")
        if isinstance(value, str):
            return value
    return None


def _save_session_id(path: Optional[Path], session_id: Optional[str]) -> None:
    if not path or not session_id:
        return
    path.write_text(json.dumps({"session_id": session_id}), encoding="utf-8")


async def _run_once(
    args: argparse.Namespace,
    user_request: str,
    session_id: Optional[str],
    event_sink: EventSink,
) -> Tuple[str, Optional[str]]:
    run_id = str(uuid.uuid4())
    event_sink.run_id = run_id
    await event_sink.emit({"type": "run_start", "request": user_request})
    plan_prompt = _build_main_plan_prompt(user_request)
    plan_text, session_id = await _run_main(args, plan_prompt, session_id, event_sink, "plan")

    plan_text = _strip_code_fences(plan_text)
    payload = _load_json(plan_text)
    if payload is None:
        repair_prompt = textwrap.dedent(
            f"""
            Your previous response was invalid JSON.
            Return ONLY valid JSON with key "tasks". Do not include Markdown.
            User request:
            {user_request}
            """
        ).strip()
        plan_text, session_id = await _run_main(args, repair_prompt, session_id, event_sink, "plan_repair")
        plan_text = _strip_code_fences(plan_text)
        payload = _load_json(plan_text) or {}

    tasks = _normalize_tasks(payload, user_request, args.max_tasks)
    await event_sink.emit(
        {
            "type": "tasks_planned",
            "count": len(tasks),
            "tasks": [{"id": t.task_id, "title": t.title, "scope": t.scope} for t in tasks],
        }
    )

    semaphore = asyncio.Semaphore(max(1, args.max_workers))
    worker_jobs = []
    worker_outputs: List[Path] = []
    with tempfile.TemporaryDirectory(prefix="codex_orchestrate_workers_") as tmpdir:
        tmp_path = Path(tmpdir)
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
                    Path(args.workdir).resolve() if args.workdir else None,
                    args.timeout,
                    semaphore,
                    event_sink,
                )
            )
        await asyncio.gather(*worker_jobs)
        worker_texts = [_read_output(p) for p in worker_outputs]

    synthesis_prompt = _build_main_synthesis_prompt(user_request, worker_texts)
    final_text, session_id = await _run_main(args, synthesis_prompt, session_id, event_sink, "synthesize")
    await event_sink.emit(
        {
            "type": "run_end",
            "session_id": session_id,
        }
    )
    event_sink.run_id = None
    return final_text, session_id


async def _run_all(args: argparse.Namespace) -> int:
    session_path = Path(args.session_file).resolve() if args.session_file else None
    session_id = _load_session_id(session_path)
    if args.events_stdout_only:
        args.events_stdout = True
    event_streams: List[Any] = []
    event_fp = None
    if args.events_file:
        events_path = Path(args.events_file).resolve()
        mode = "a" if args.events_append else "w"
        event_fp = events_path.open(mode, encoding="utf-8")
        event_streams.append(event_fp)
    if args.events_stdout:
        event_streams.append(sys.stdout)
    event_sink = EventSink(streams=event_streams, lock=asyncio.Lock())

    if not args.repl:
        if not args.prompt:
            raise ValueError("prompt is required unless --repl is set")
        summary_text, session_id = await _run_once(args, args.prompt, session_id, event_sink)
        if args.output:
            Path(args.output).write_text(summary_text, encoding="utf-8")
        output_stream = sys.stderr if args.events_stdout_only else sys.stdout
        output_stream.write(summary_text + "\n")
        _save_session_id(session_path, session_id)
        if event_fp:
            event_fp.close()
        return 0

    while True:
        try:
            user_input = input("codex-orch> ").strip()
        except EOFError:
            break
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            break
        if user_input.lower() == "/reset-session":
            session_id = None
            if session_path and session_path.exists():
                session_path.unlink()
            continue

        summary_text, session_id = await _run_once(args, user_input, session_id, event_sink)
        output_stream = sys.stderr if args.events_stdout_only else sys.stdout
        output_stream.write(summary_text + "\n")
        _save_session_id(session_path, session_id)

    if event_fp:
        event_fp.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Self-planning Codex orchestrator")
    parser.add_argument("prompt", nargs="?", default="", help="User request to orchestrate")
    parser.add_argument("--max-tasks", type=int, default=4, help="Maximum number of worker tasks")
    parser.add_argument("--max-workers", type=int, default=3, help="Maximum parallel workers")
    parser.add_argument("--timeout", type=int, default=None, help="Timeout seconds per worker")
    parser.add_argument("--repl", action="store_true", help="Run in REPL mode")
    parser.add_argument("--session-file", default=".codex_orch_session.json", help="Persist main session id")
    parser.add_argument("--use-last-session", action="store_true", help="Use codex exec resume --last")
    parser.add_argument("--events-file", default=None, help="Write JSONL events to file")
    parser.add_argument("--events-append", action="store_true", help="Append events instead of truncating")
    parser.add_argument("--events-stdout", action="store_true", help="Write JSONL events to stdout")
    parser.add_argument("--events-stdout-only", action="store_true", help="When set, write summary to stderr")
    parser.add_argument("--model", default=None, help="Codex model override")
    parser.add_argument("--profile", default=None, help="Codex profile override")
    parser.add_argument("--workdir", default=None, help="Working directory for Codex runs")
    parser.add_argument("--codex-arg", action="append", default=[], help="Extra arg for codex exec")
    parser.add_argument("--output", default=None, help="Write final summary to file")
    args = parser.parse_args()

    return asyncio.run(_run_all(args))


if __name__ == "__main__":
    raise SystemExit(main())
