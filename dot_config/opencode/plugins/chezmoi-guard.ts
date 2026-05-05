// chezmoi-guard: hard-block tool calls that would edit chezmoi-managed files.
//
// Why: chezmoi is the source of truth for tracked dotfiles. Direct edits to
// the live file get reverted on the next `chezmoi apply` (chezmoi re-add
// silently skips templates), and edits made through opencode's Edit/Write
// tools bypass the source-of-truth entirely. This plugin intercepts those
// tool calls before they execute and throws an error that points the agent
// at `chezmoi edit --apply <path>`.
//
// Scope:
//   - Blocks: edit, write, apply_patch, multiedit
//   - Does NOT block: bash (intentional — agent can `chezmoi edit --apply`)
//   - Does NOT cover: GUI editors, apps writing their own configs
//     (those are handled by Tier C drift detection in starship.toml)
//
// Cache: `chezmoi managed --path-style absolute` is invoked at plugin load
// and refreshed on a 5-minute TTL. Newly-tracked files become blocked
// within 5 minutes; force-refresh by restarting the opencode session.

import type { Plugin } from "@opencode-ai/plugin"
import { execSync } from "node:child_process"

const TTL_MS = 5 * 60 * 1000
let managed = new Set<string>()
let lastLoad = 0

function refresh(): void {
  if (Date.now() - lastLoad < TTL_MS && managed.size > 0) return
  try {
    const out = execSync("chezmoi managed --path-style absolute", {
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "ignore"],
      timeout: 3000,
    })
    managed = new Set(out.trim().split("\n").filter(Boolean))
    lastLoad = Date.now()
  } catch {
    // Stale cache is better than no cache. Plugin must never crash opencode.
  }
}

const BLOCKED_TOOLS = new Set(["edit", "write", "apply_patch", "multiedit"])

function pathsFromArgs(tool: string, args: any): string[] {
  if (!args) return []
  if (tool === "apply_patch" && typeof args.patchText === "string") {
    const re = /\*\*\* (?:Add|Update|Move to|Delete) File: (.+)/g
    const out: string[] = []
    let m: RegExpExecArray | null
    while ((m = re.exec(args.patchText)) !== null) out.push(m[1].trim())
    return out
  }
  if (typeof args.filePath === "string") return [args.filePath]
  if (typeof args.path === "string") return [args.path]
  return []
}

export const ChezmoiGuard: Plugin = async () => {
  refresh()
  return {
    "tool.execute.before": async (input, output) => {
      if (!BLOCKED_TOOLS.has(input.tool)) return
      refresh()
      const paths = pathsFromArgs(input.tool, output.args)
      for (const p of paths) {
        if (managed.has(p)) {
          throw new Error(
            `[chezmoi-guard] ${p} is chezmoi-managed.\n` +
              `Edit the source instead:\n` +
              `  chezmoi edit --apply ${p}\n` +
              `or open the source file directly:\n` +
              `  $(chezmoi source-path ${p})`,
          )
        }
      }
    },
  }
}
