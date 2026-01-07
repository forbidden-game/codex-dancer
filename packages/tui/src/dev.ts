import solidPlugin from "@opentui/solid/bun-plugin"
import path from "path"

const rootDir = path.resolve(import.meta.dir, "..")
process.chdir(rootDir)

const outDir = path.resolve(rootDir, "dist")
const entry = path.resolve(rootDir, "src/index.tsx")

const result = await Bun.build({
  entrypoints: [entry],
  outdir: outDir,
  target: "bun",
  plugins: [solidPlugin],
  tsconfig: path.resolve(rootDir, "tsconfig.json"),
  conditions: ["browser"],
  sourcemap: "inline",
})

if (!result.success) {
  for (const msg of result.logs) {
    console.error(msg)
  }
  process.exit(1)
}

await import(path.resolve(outDir, "index.js"))
