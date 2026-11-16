/**
 * BidGenie M1 - Python bridge.
 *
 * Resolves the bundled Python interpreter (DSH runtime first, then PATH),
 * spawns one whitelisted script from `libexec/`, enforces a hard timeout, and
 * returns a structured result. Nothing here touches business state: the
 * deterministic scripts are the single implementation of the operations.
 *
 * Contract: plugin/CONTRACT.md §4; PRD appendix H (script CLI) + §8.5 (exit codes).
 */
import { spawn } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { existsSync, mkdirSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import os from 'node:os';

/** Absolute path of the plugin package root (never depends on the Host cwd). */
export const PLUGIN_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
/** Directory holding the deterministic Python scripts (`bg_*.py`). */
export const SCRIPTS_DIR = join(PLUGIN_ROOT, 'libexec');
/** Hard per-call ceiling required by the M1 contract (120 s). */
export const DEFAULT_TIMEOUT_MS = 120_000;

const MAX_OUTPUT_CHARS = 4 * 1024 * 1024;

/** PRD 8.5 script exit code -> JSON `error` code (fallback when stdout carries no JSON). */
const EXIT_CODE_ERRORS = {
  1: 'E_USAGE',
  2: 'E_USAGE',
  3: 'E_NOT_FOUND',
  4: 'E_VALIDATE_FAILED',
  5: 'E_NOT_FOUND',
  6: 'E_TIMEOUT',
  7: 'E_LOCK_TIMEOUT',
  8: 'E_DEP_MISSING',
  9: 'E_USER_DECISION_REQUIRED',
};

function isFile(candidate) {
  try {
    return statSync(candidate).isFile();
  } catch {
    return false;
  }
}

/** `$DSH_HOME` when the Host exposes it, else the conventional `~/.dsh`. */
export function dshHome() {
  const fromEnv = process.env.DSH_HOME;
  if (typeof fromEnv === 'string' && fromEnv.trim() !== '') return resolve(fromEnv.trim());
  return join(os.homedir(), '.dsh');
}

/** Candidate interpreters, ordered by preference (documented for M1-00). */
export function pythonCandidates() {
  const candidates = [];
  const override = process.env.BIDGENIE_PYTHON ?? process.env.DSH_PYTHON;
  if (typeof override === 'string' && override.trim() !== '') {
    candidates.push({ exe: resolve(override.trim()), source: 'env' });
  }
  // 1) DSH bundled runtime: <dshHome>/dsh-runtimes/dsh-primary-runtime/dependencies/python
  const runtimeRoot = join(dshHome(), 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python');
  if (process.platform === 'win32') {
    candidates.push({ exe: join(runtimeRoot, 'python.exe'), source: 'dsh-runtime' });
    candidates.push({ exe: join(runtimeRoot, 'Scripts', 'python.exe'), source: 'dsh-runtime-scripts' });
  } else {
    candidates.push({ exe: join(runtimeRoot, 'bin', 'python3'), source: 'dsh-runtime' });
    candidates.push({ exe: join(runtimeRoot, 'bin', 'python'), source: 'dsh-runtime' });
  }
  // 2) PATH lookup (works when the bundled runtime layout changes).
  const separator = process.platform === 'win32' ? ';' : ':';
  const names = process.platform === 'win32' ? ['python.exe', 'python3.exe'] : ['python3', 'python'];
  for (const entry of String(process.env.PATH ?? '').split(separator)) {
    if (entry.trim() === '') continue;
    for (const name of names) candidates.push({ exe: join(entry, name), source: 'path' });
  }
  return candidates;
}

let cachedPython;
let cacheFilled = false;

/** Resolve and cache the interpreter. Returns `{exe, source}` or `null`. */
export function resolvePython() {
  if (cacheFilled) return cachedPython;
  cacheFilled = true;
  for (const candidate of pythonCandidates()) {
    if (isFile(candidate.exe)) {
      cachedPython = candidate;
      return cachedPython;
    }
  }
  cachedPython = null;
  return null;
}

/** Structured missing-dependency result, shaped like a script result. */
function dependencyFailure(code, message, hint) {
  return {
    exitCode: null,
    timedOut: false,
    stdout: '',
    stderr: '',
    json: null,
    python: null,
    error: { code, message, hint },
  };
}

/** Absolute path of one whitelisted script; rejects anything outside `libexec/`. */
export function scriptPath(scriptName) {
  if (typeof scriptName !== 'string' || !/^[A-Za-z0-9_.-]+\.py$/.test(scriptName)) {
    throw new Error(`invalid script name: ${String(scriptName)}`);
  }
  return join(SCRIPTS_DIR, scriptName);
}

/** Extract the single JSON object printed on stdout (`--json` contract). */
export function parseJsonOutput(stdout) {
  const trimmed = String(stdout ?? '').trim();
  if (trimmed === '') return null;
  try {
    return JSON.parse(trimmed);
  } catch {
    // Scripts normally print exactly one object; tolerate leading log lines.
    const start = trimmed.indexOf('{');
    const end = trimmed.lastIndexOf('}');
    if (start >= 0 && end > start) {
      try {
        return JSON.parse(trimmed.slice(start, end + 1));
      } catch {
        return null;
      }
    }
    return null;
  }
}

/**
 * Run one `libexec/*.py` script with the bundled interpreter.
 *
 * @returns {Promise<{exitCode:number|null,timedOut:boolean,stdout:string,stderr:string,
 *   json:unknown,python:string|null,error:{code:string,message:string,hint?:string}|null}>}
 */
function workspaceTempEnv(args) {
  const index = args.indexOf('--workspace');
  if (index < 0 || index + 1 >= args.length) return {};
  const workspace = resolve(String(args[index + 1]));
  try {
    const tempDir = join(workspace, 'dsh', 'tmp');
    mkdirSync(tempDir, { recursive: true });
    return { TMP: tempDir, TEMP: tempDir, TMPDIR: tempDir };
  } catch {
    return {};
  }
}

export function runScript(scriptName, args = [], options = {}) {
  const timeoutMs = Number.isFinite(options.timeoutMs) ? options.timeoutMs : DEFAULT_TIMEOUT_MS;
  const cwd = options.cwd ?? PLUGIN_ROOT;
  const python = resolvePython();
  if (python === null) {
    return Promise.resolve(
      dependencyFailure(
        'E_DEP_MISSING',
        '未找到可用的 Python 解释器',
        'DSH 运行时缺失时请安装 DSH 自带运行时，或设置 BIDGENIE_PYTHON 环境变量',
      ),
    );
  }
  let script;
  try {
    script = scriptPath(scriptName);
  } catch (error) {
    return Promise.resolve(dependencyFailure('E_DEP_MISSING', String(error.message), '脚本名不符合白名单'));
  }
  if (!isFile(script)) {
    return Promise.resolve(
      dependencyFailure('E_DEP_MISSING', `脚本不存在：libexec/${scriptName}`, '插件包缺少 libexec 脚本，请重新安装插件'),
    );
  }

  return new Promise((finish) => {
    let settled = false;
    let timedOut = false;
    let stdout = '';
    let stderr = '';
    let child;
    try {
      child = spawn(python.exe, ['-X', 'utf8', script, ...args], {
        cwd,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: {
          ...process.env,
          ...workspaceTempEnv(args),
          PYTHONIOENCODING: 'utf-8',
          PYTHONUTF8: '1',
          ...(options.env ?? {}),
        },
      });
    } catch (error) {
      finish({
        exitCode: null,
        timedOut: false,
        stdout,
        stderr,
        json: null,
        python: python.exe,
        error: { code: 'E_DEP_MISSING', message: String(error?.message ?? error) },
      });
      return;
    }

    const timer = setTimeout(() => {
      timedOut = true;
      try {
        child.kill();
      } catch {
        /* already gone */
      }
    }, timeoutMs);

    const append = (current, chunk) => {
      if (current.length >= MAX_OUTPUT_CHARS) return current;
      const next = current + chunk;
      return next.length > MAX_OUTPUT_CHARS ? next.slice(0, MAX_OUTPUT_CHARS) : next;
    };
    child.stdout?.setEncoding('utf8');
    child.stderr?.setEncoding('utf8');
    child.stdout?.on('data', (chunk) => {
      stdout = append(stdout, chunk);
    });
    child.stderr?.on('data', (chunk) => {
      stderr = append(stderr, chunk);
    });

    const settle = (payload) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      finish(payload);
    };

    child.on('error', (error) => {
      settle({
        exitCode: null,
        timedOut,
        stdout,
        stderr,
        json: null,
        python: python.exe,
        error: { code: 'E_DEP_MISSING', message: `Python 启动失败：${String(error?.message ?? error)}` },
      });
    });

    child.on('close', (code, signal) => {
      const json = parseJsonOutput(stdout);
      let error = null;
      if (timedOut) {
        error = { code: 'E_TIMEOUT', message: `脚本执行超时（>${timeoutMs}ms）`, hint: '可稍后重试；长任务应在 M2 起改用后台 job' };
      } else if (signal !== null && signal !== undefined) {
        error = { code: 'E_USAGE', message: `脚本被信号终止：${signal}` };
      } else if (json === null && (code ?? 0) !== 0) {
        const mapped = EXIT_CODE_ERRORS[code] ?? 'E_USAGE';
        error = {
          code: mapped,
          message: `脚本退出码 ${code}${stderr.trim() === '' ? '' : `：${stderr.trim().split(/\r?\n/).slice(-1)[0]}`}`,
          hint: '详见脚本 stderr，或查看 docs/devlog 中同类故障记录',
        };
      }
      settle({ exitCode: typeof code === 'number' ? code : null, timedOut, stdout, stderr, json, python: python.exe, error });
    });
  });
}

/**
 * Normalize a script result into the CONTRACT failure shape.
 * Returns `null` when the script succeeded with `{ok:true}` (or a plain JSON body).
 */
export function scriptFailure(result) {
  if (result?.error) return result.error;
  const json = result?.json;
  if (json !== null && typeof json === 'object' && json.ok === false) {
    return {
      code: typeof json.error === 'string' && json.error !== '' ? json.error : 'E_USAGE',
      message: typeof json.message === 'string' && json.message !== '' ? json.message : '脚本执行失败',
      hint: typeof json.hint === 'string' && json.hint !== '' ? json.hint : undefined,
      detail: json.detail,
    };
  }
  if (typeof result?.exitCode === 'number' && result.exitCode !== 0) {
    const mapped = EXIT_CODE_ERRORS[result.exitCode] ?? 'E_USAGE';
    return { code: mapped, message: result.stderr?.trim() || `脚本退出码 ${result.exitCode}` };
  }
  return null;
}

/** Write `value` to a throwaway JSON file, run `fn(file)`, then clean the directory. */
export async function withTempJson(prefix, value, fn) {
  const dir = await mkdtemp(join(os.tmpdir(), 'bidgenie-'));
  const file = join(dir, `${prefix}.json`);
  try {
    await writeFile(file, JSON.stringify(value, null, 2), 'utf8');
    return await fn(file);
  } finally {
    await rm(dir, { recursive: true, force: true }).catch(() => {});
  }
}

/** Convenience: returns true when the bundled runtime can actually run scripts. */
export function pythonAvailable() {
  const python = resolvePython();
  return python !== null && existsSync(SCRIPTS_DIR);
}
