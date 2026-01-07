# TUI MVP

This is a minimal TUI for codex-orch built with OpenTUI + Solid.

## Run

```bash
cd packages/tui
bun install
bun run dev
```

## Notes

- The dev script uses `@opentui/solid`'s Bun plugin to compile TSX.
- The TUI spawns `scripts/codex-orch` and consumes JSONL events from stdout.
- Summary output is read from stderr (using `--events-stdout-only`).
- Session state is persisted to `.codex_orch_session.json` in the repo root.
