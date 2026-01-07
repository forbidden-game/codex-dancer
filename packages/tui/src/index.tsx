/** @jsxImportSource @opentui/solid */
import { render, useTerminalDimensions } from "@opentui/solid"
import { RGBA, TextAttributes, TextareaRenderable } from "@opentui/core"
import { createSignal, For, Show, onMount } from "solid-js"
import path from "path"

type WorkerStatus = "pending" | "running" | "ok" | "error" | "timeout"

type WorkerItem = {
  id: string
  title: string
  scope: "read-only" | "write"
  status: WorkerStatus
}

type Message = {
  role: "user" | "assistant"
  text: string
}

type OrchestratorEvent = {
  type: string
  run_id?: string
  phase?: string
  worker_id?: string
  title?: string
  scope?: "read-only" | "write"
  status?: "ok" | "error" | "timeout"
  tasks?: Array<{ id: string; title: string; scope: "read-only" | "write" }>
}

const colors = {
  text: RGBA.fromInts(229, 231, 235),
  muted: RGBA.fromInts(156, 163, 175),
  success: RGBA.fromInts(34, 197, 94),
  error: RGBA.fromInts(239, 68, 68),
  warning: RGBA.fromInts(234, 179, 8),
  info: RGBA.fromInts(56, 189, 248),
  panel: RGBA.fromInts(17, 24, 39),
  border: RGBA.fromInts(30, 41, 59),
  userBg: RGBA.fromInts(30, 64, 175),
  assistantBg: RGBA.fromInts(55, 48, 163),
  panelHeaderBg: RGBA.fromInts(15, 23, 42),
  workerPendingBg: RGBA.fromInts(17, 24, 39),
  workerRunningBg: RGBA.fromInts(12, 74, 110),
  workerOkBg: RGBA.fromInts(20, 83, 45),
  workerErrorBg: RGBA.fromInts(127, 29, 29),
  workerTimeoutBg: RGBA.fromInts(120, 53, 15),
}

function statusColor(status: WorkerStatus) {
  if (status === "running") return colors.info
  if (status === "ok") return colors.success
  if (status === "error") return colors.error
  if (status === "timeout") return colors.warning
  return colors.muted
}

function statusSymbol(status: WorkerStatus) {
  if (status === "running") return "*"
  if (status === "ok") return "+"
  if (status === "error") return "x"
  if (status === "timeout") return "!"
  return "."
}

function statusBg(status: WorkerStatus) {
  if (status === "running") return colors.workerRunningBg
  if (status === "ok") return colors.workerOkBg
  if (status === "error") return colors.workerErrorBg
  if (status === "timeout") return colors.workerTimeoutBg
  return colors.workerPendingBg
}

async function readLines(stream: ReadableStream<Uint8Array>, onLine: (line: string) => void) {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split(/\r?\n/)
    buffer = parts.pop() ?? ""
    for (const part of parts) {
      if (part.trim().length === 0) continue
      onLine(part)
    }
  }
  if (buffer.trim().length > 0) onLine(buffer)
}

async function runOrchestrator(prompt: string, onEvent: (event: OrchestratorEvent) => void): Promise<string> {
  const rootDir = path.resolve(import.meta.dir, "../../..")
  const scriptPath = path.resolve(rootDir, "scripts/codex-orch")

  const proc = Bun.spawn({
    cmd: [
      scriptPath,
      prompt,
      "--events-stdout-only",
      "--session-file",
      path.resolve(rootDir, ".codex_orch_session.json"),
    ],
    cwd: rootDir,
    stdout: "pipe",
    stderr: "pipe",
  })

  if (!proc.stdout || !proc.stderr) {
    throw new Error("Failed to spawn orchestrator")
  }

  const stdoutTask = readLines(proc.stdout, (line) => {
    try {
      onEvent(JSON.parse(line))
    } catch {
      return
    }
  })

  const stderrPromise = new Response(proc.stderr).text()
  const exitCode = await proc.exited
  await stdoutTask
  const summary = (await stderrPromise).trim()

  if (exitCode !== 0) {
    return summary.length > 0 ? summary : `Orchestrator exited with code ${exitCode}`
  }

  return summary.length > 0 ? summary : "(no output)"
}

function App() {
  const [messages, setMessages] = createSignal<Message[]>([])
  const [workers, setWorkers] = createSignal<WorkerItem[]>([])
  const [workerLog, setWorkerLog] = createSignal<string[]>([])
  const [phase, setPhase] = createSignal<string>("idle")
  const [running, setRunning] = createSignal(false)
  const dimensions = useTerminalDimensions()
  let input: TextareaRenderable

  const updateWorker = (id: string, updater: (prev: WorkerItem) => WorkerItem) => {
    setWorkers((prev) => prev.map((item) => (item.id === id ? updater(item) : item)))
  }

  const appendLog = (line: string) => {
    setWorkerLog((prev) => {
      const next = [...prev, line]
      return next.slice(-200)
    })
  }

  const handleEvent = (event: OrchestratorEvent) => {
    if (event.type === "run_start") {
      setPhase("plan")
      setWorkers([])
      setWorkerLog([])
      appendLog("[main] start")
      return
    }
    if (event.type === "main_start" && event.phase) {
      setPhase(event.phase)
      appendLog(`[main] ${event.phase}`)
      return
    }
    if (event.type === "tasks_planned" && event.tasks) {
      setWorkers(event.tasks.map((task) => ({ id: task.id, title: task.title, scope: task.scope, status: "pending" })))
      appendLog(`[main] planned ${event.tasks.length} worker(s)`)
      return
    }
    if (event.type === "worker_start" && event.worker_id) {
      updateWorker(event.worker_id, (item) => ({ ...item, status: "running" }))
      appendLog(`[worker ${event.worker_id}] start`)
      return
    }
    if (event.type === "worker_end" && event.worker_id && event.status) {
      updateWorker(event.worker_id, (item) => ({ ...item, status: event.status ?? item.status }))
      appendLog(`[worker ${event.worker_id}] ${event.status}`)
      return
    }
    if (event.type === "run_end") {
      setPhase("idle")
      appendLog("[main] done")
      return
    }
  }

  const submit = async () => {
    if (running()) return
    const prompt = input?.plainText?.trim() ?? ""
    if (!prompt) return
    input.clear()
    setMessages((prev) => [...prev, { role: "user", text: prompt }])
    setRunning(true)
    try {
      const summary = await runOrchestrator(prompt, handleEvent)
      setMessages((prev) => [...prev, { role: "assistant", text: summary }])
    } finally {
      setRunning(false)
      setPhase("idle")
    }
  }

  onMount(() => {
    setTimeout(() => {
      input?.focus()
    }, 1)
  })

  return (
    <box flexDirection="column" width={dimensions().width} height={dimensions().height}>
      <box
        paddingLeft={2}
        paddingRight={2}
        paddingTop={1}
        paddingBottom={1}
        backgroundColor={colors.panelHeaderBg}
        flexShrink={0}
      >
        <text fg={colors.text} attributes={TextAttributes.BOLD}>
          codex-orch
        </text>
        <Show when={running()}>
          <text fg={colors.muted}>  * {phase()}</text>
        </Show>
      </box>
      <box flexDirection="row" flexGrow={1}>
        <box flexDirection="column" flexGrow={1} paddingLeft={2} paddingRight={2}>
          <box paddingTop={1} paddingBottom={1} flexShrink={0}>
            <text fg={colors.muted}>main</text>
            <Show when={running()}>
              <text fg={colors.muted}>  {phase()}</text>
            </Show>
          </box>
          <scrollbox flexGrow={1}>
            <For each={messages()}>
              {(msg) => (
                <box
                  flexDirection="row"
                  gap={1}
                  marginBottom={1}
                  paddingLeft={1}
                  paddingRight={1}
                  backgroundColor={msg.role === "user" ? colors.userBg : colors.assistantBg}
                >
                  <text fg={colors.text} attributes={TextAttributes.BOLD}>
                    {msg.role === "user" ? "Xiezhao" : "codex-dancer"}
                  </text>
                  <text fg={colors.text} wrapMode="word">
                    {msg.text}
                  </text>
                </box>
              )}
            </For>
          </scrollbox>
          <box paddingTop={1} paddingBottom={1} flexShrink={0}>
            <textarea
              height={3}
              placeholder={running() ? "Running..." : "Type a prompt and press Enter"}
              textColor={colors.text}
              focusedTextColor={colors.text}
              cursorColor={colors.text}
              onSubmit={submit}
              keyBindings={[{ name: "return", action: "submit" }]}
              ref={(val: TextareaRenderable) => (input = val)}
            />
          </box>
        </box>
        <box
          width={36}
          flexDirection="column"
          paddingLeft={2}
          paddingRight={2}
          border={["left"]}
          borderColor={colors.border}
        >
          <box paddingTop={1} paddingBottom={1} flexShrink={0} backgroundColor={colors.panelHeaderBg}>
            <text fg={colors.text} attributes={TextAttributes.BOLD}>
              workers
            </text>
          </box>
          <scrollbox flexGrow={1}>
            <Show when={workers().length > 0} fallback={<text fg={colors.muted}>no workers</text>}>
              <For each={workers()}>
                {(worker) => (
                  <box flexDirection="row" gap={1} marginBottom={1} paddingLeft={1} backgroundColor={statusBg(worker.status)}>
                    <text fg={statusColor(worker.status)}>{statusSymbol(worker.status)}</text>
                    <text fg={colors.text} wrapMode="none">
                      {worker.title}
                    </text>
                  </box>
                )}
              </For>
            </Show>
            <Show when={workerLog().length > 0}>
              <box paddingTop={1} paddingBottom={1} flexShrink={0}>
                <text fg={colors.muted}>activity</text>
              </box>
              <For each={workerLog()}>
                {(line) => (
                  <text fg={colors.muted} wrapMode="none">
                    {line}
                  </text>
                )}
              </For>
            </Show>
          </scrollbox>
        </box>
      </box>
    </box>
  )
}

render(() => <App />, {
  exitOnCtrlC: true,
  targetFps: 60,
})
