#!/usr/bin/env node
/**
 * npm prepack：清理运行时生成的 Python 缓存，避免被打进 npm 包。
 *
 * 背景：release 仓库是 DSH 当前安装目录（link:），插件运行脚本时会在
 * libexec/ 下生成 __pycache__；package.json 的 files 白名单会把它一起打包。
 * npm 文档规定 files 存在时 .npmignore 不生效，所以用 prepack 主动清理。
 */
import { readdirSync, rmSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(fileURLToPath(new URL('.', import.meta.url)))
let removedDirs = 0
let removedFiles = 0

function walk(directory) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === '.git') continue
    const full = join(directory, entry.name)
    if (entry.isDirectory()) {
      if (entry.name === '__pycache__') {
        rmSync(full, { recursive: true, force: true })
        removedDirs += 1
        continue
      }
      walk(full)
      continue
    }
    if (/\.(pyc|pyo)$/iu.test(entry.name)) {
      rmSync(full, { force: true })
      removedFiles += 1
    }
  }
}

if (statSync(root).isDirectory()) walk(root)
console.log(`[prepack] cleaned __pycache__ dirs=${removedDirs} pyc/pyo files=${removedFiles}`)
