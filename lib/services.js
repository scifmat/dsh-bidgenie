/**
 * BidGenie M1 - Host module functions ("services" without a Cordis service).
 *
 * Decision (PRD 6.6.3 / CONTRACT §2): no custom Host service is registered.
 * The same plain functions are called by the HTTP routes (panel) and by the
 * model tools, so every operation has exactly one implementation.
 *
 * Rules enforced here:
 *  - workspace resolution only through `workspaceRegistry.resolveByPath`
 *    (never Host `process.cwd()` / `DSH_*` env; PRD 2.7 GAP-14);
 *  - deterministic writes only through the bundled `libexec/*.py` scripts;
 *  - no cached business state (the filesystem is the single source of truth).
 *
 * Frozen contract: plugin/CONTRACT.md §2/§3/§4/§5.
 */
import { existsSync, readFileSync } from 'node:fs';
import { mkdir, readFile, readdir, rename, stat, writeFile } from 'node:fs/promises';
import { dirname, basename, isAbsolute, join, resolve, sep } from 'node:path';
import {
  PLUGIN_ROOT,
  SCRIPTS_DIR,
  resolvePython,
  runScript,
  scriptFailure,
  withTempJson,
} from './python.js';

/**
 * 面板路由 = 用户动作：把 actor/source 通过环境变量传给脚本（`_lib/logging.py`
 * 读取后写 `actor=user` / `detail.source=panel`，backend P1 task-106 落地）。
 * 脚本侧未就绪时事件仍为 actor=script；投标人 meta.source 与事件 detail.source
 * 已由 `--data` 参数标记为 panel，可作为过渡证据。
 */
const PANEL_ACTOR_ENV = Object.freeze({ BIDGENIE_ACTOR: 'user', BIDGENIE_SOURCE: 'panel' });

/* ----------------------------------------------------------------------------- */
/* Errors                                                                        */
/* ----------------------------------------------------------------------------- */

/**
 * Business error carrying one of the PRD §8.5 codes.
 * `E_INTERNAL` is used only for unclassified Host failures (HTTP 500) and is not
 * a business error code.
 */
export class BgError extends Error {
  constructor(code, message, hint) {
    super(message);
    this.name = 'BgError';
    this.code = code;
    if (hint !== undefined) this.hint = hint;
  }
}

export function bgError(code, message, hint) {
  return new BgError(code, message, hint);
}

export function isBgError(error) {
  return error instanceof BgError;
}

const HTTP_STATUS_BY_CODE = {
  E_USAGE: 400,
  E_SCHEMA_INVALID: 400,
  E_VALIDATE_FAILED: 400,
  E_USER_DECISION_REQUIRED: 409,
  E_NOT_FOUND: 404,
  E_ILLEGAL_TRANSITION: 409,
  E_ALREADY_IN_STAGE: 409,
  E_GUARD_FAILED: 409,
  E_LOCKED: 409,
  E_LOCK_TIMEOUT: 409,
  E_STATE_CORRUPT: 500,
  E_TIMEOUT: 504,
  E_DEP_MISSING: 503,
  E_NETWORK: 502,
  E_CONVERT_FAILED: 500,
  E_EXPORT_FAILED: 500,
  E_REPAIR_FAILED: 500,
  E_BUSY: 409,
  E_INTERNAL: 500,
};

export function httpStatusFor(error) {
  if (!isBgError(error)) return 500;
  return HTTP_STATUS_BY_CODE[error.code] ?? 500;
}

/** CONTRACT §3 error envelope: `{ok:false,error,message,hint?}`. */
export function errorPayload(error) {
  if (isBgError(error)) {
    return {
      ok: false,
      error: error.code,
      message: error.message,
      ...(error.hint === undefined ? {} : { hint: error.hint }),
    };
  }
  return {
    ok: false,
    error: 'E_INTERNAL',
    message: `宿主内部错误：${String(error?.message ?? error)}`,
  };
}

/* ----------------------------------------------------------------------------- */
/* Versions / constants                                                          */
/* ----------------------------------------------------------------------------- */

const PACKAGE_JSON = (() => {
  try {
    return JSON.parse(readFileSync(join(PLUGIN_ROOT, 'package.json'), 'utf8').replace(/^\uFEFF/, ''));
  } catch {
    return {};
  }
})();

export const PLUGIN_VERSION = typeof PACKAGE_JSON.version === 'string' ? PACKAGE_JSON.version : '0.1.0';

function readPackageVersion(packageFile) {
  try {
    if (!existsSync(packageFile)) return '';
    const document = JSON.parse(readFileSync(packageFile, 'utf8').replace(/^\uFEFF/, ''));
    return typeof document.version === 'string' ? document.version.trim() : '';
  } catch {
    return '';
  }
}

/** 尽量从 DSH 安装包/环境变量读取宿主版本（0.2.x 基线比较用）；取不到返回空。 */
export function detectDshHostVersion() {
  for (const name of ['BIDGENIE_HOST_VERSION', 'DSH_HOST_VERSION']) {
    const value = typeof process.env[name] === 'string' ? process.env[name].trim() : '';
    if (value !== '') return value;
  }
  const candidates = [];
  if (typeof process.resourcesPath === 'string' && process.resourcesPath !== '') {
    candidates.push(join(process.resourcesPath, 'app.asar', 'package.json'));
    candidates.push(join(process.resourcesPath, 'app.asar', 'dsh', 'package.json'));
    candidates.push(join(process.resourcesPath, 'app.asar.unpacked', 'package.json'));
    candidates.push(join(process.resourcesPath, 'app', 'package.json'));
  }
  if (typeof process.execPath === 'string' && process.execPath !== '') {
    candidates.push(join(dirname(process.execPath), 'resources', 'app.asar', 'package.json'));
  }
  if (typeof process.env.DSH_APP_PATH === 'string' && process.env.DSH_APP_PATH !== '') {
    candidates.push(join(process.env.DSH_APP_PATH, 'package.json'));
  }
  for (const candidate of candidates) {
    const version = readPackageVersion(candidate);
    if (/^\d+\.\d+\.\d+/.test(version)) return version;
  }
  return '';
}

/** 宿主版本：优先 DSH 包版本，取不到为空（env-check 会 skipped 并提示配置）。 */
export const HOST_VERSION = detectDshHostVersion();
/** 面板展示用：未获取时给出配置说明而不是空/未运行。 */
export const HOST_VERSION_LABEL = HOST_VERSION || '未获取（可配置 BIDGENIE_HOST_VERSION）';

/** Default checklist size required by the M1 env-check stub (CONTRACT §3). */
export const REQUIRED_CHECKS = 8;
/** Whole environment check may take up to 10 minutes (CONTRACT §8.2 / PRD F2.2). */
export const ENV_CHECK_TIMEOUT_MS = 600_000;
/** 环境修复：plan 30s、apply 20min、verify 30s（task-160 契约）。 */
const ENV_REPAIR_PLAN_TIMEOUT_MS = 30_000;
const ENV_REPAIR_APPLY_TIMEOUT_MS = 1_200_000;
const ENV_REPAIR_VERIFY_TIMEOUT_MS = 30_000;
/** 同一工作区只允许一个环境修复任务（Host 层写锁；脚本内另有 env-repair.lock）。 */
const ENV_REPAIR_LOCKS = new Map();

/* ----------------------------------------------------------------------------- */
/* Small filesystem helpers                                                      */
/* ----------------------------------------------------------------------------- */

const BOM = /^\uFEFF/;

export async function readJsonFile(filePath) {
  let text;
  try {
    text = await readFile(filePath, 'utf8');
  } catch (error) {
    if (error?.code === 'ENOENT') return undefined;
    if (error?.code === 'EACCES' || error?.code === 'EPERM') {
      throw bgError('E_NOT_FOUND', `无权读取：${filePath}`, '请检查文件权限');
    }
    throw error;
  }
  try {
    return JSON.parse(text.replace(BOM, ''));
  } catch (error) {
    throw bgError('E_STATE_CORRUPT', `JSON 解析失败：${filePath}`, String(error?.message ?? error));
  }
}

/** Atomic JSON write: temp file in the same directory, then `rename`. */
export async function writeJsonAtomic(filePath, value) {
  await mkdir(dirname(filePath), { recursive: true });
  const tempPath = `${filePath}.tmp-${process.pid}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  await writeFile(tempPath, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
  await rename(tempPath, filePath);
}

export async function isDirectory(path) {
  try {
    return (await stat(path)).isDirectory();
  } catch {
    return false;
  }
}

/** Resolve a workspace-relative POSIX path, refusing escape (`..`, symlink-ish). */
export function safeJoin(root, ...segments) {
  const base = resolve(root);
  const target = resolve(base, ...segments);
  if (target !== base && !target.startsWith(`${base}${sep}`)) {
    throw bgError('E_SCHEMA_INVALID', `路径越界：${segments.join('/')}`, '所有路径必须位于工作区内');
  }
  return target;
}

export function toWorkspaceAbsolute(workspaceRoot, possiblyRelative) {
  if (typeof possiblyRelative !== 'string' || possiblyRelative === '') return null;
  if (isAbsolute(possiblyRelative)) return resolve(possiblyRelative);
  return safeJoin(workspaceRoot, ...possiblyRelative.split('/'));
}

/**
 * ISO 8601 with numeric timezone offset, matching every timestamp the bundled
 * scripts write (`2026-11-08T20:00:00+08:00`). The schema rejects a `Z` suffix
 * (see bg_bidder.py validate: "时间字段必须为 ISO 8601 带时区偏移").
 */
export function formatIsoWithOffset(date = new Date()) {
  const pad = (value) => String(value).padStart(2, '0');
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? '+' : '-';
  const absolute = Math.abs(offsetMinutes);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(
    date.getMinutes(),
  )}:${pad(date.getSeconds())}${sign}${pad(Math.floor(absolute / 60))}:${pad(absolute % 60)}`;
}

/* ----------------------------------------------------------------------------- */
/* Workspace resolution (PRD 2.7 GAP-14 / CONTRACT §2)                           */
/* ----------------------------------------------------------------------------- */

/**
 * Validate an explicitly passed workspace path against the durable registry.
 * The Host NEVER infers a workspace from its own cwd or environment.
 *
 * @param {object} ctx Cordis context with `get(...)`
 * @param {string|undefined} hintPath absolute path supplied by the panel/tool
 * @returns {Promise<{workspaceRoot:string, workspace:{id:string,path:string,title:string}}>}
 */
export async function resolveWorkspace(ctx, hintPath) {
  const registry = ctx.get('workspaceRegistry');
  if (registry === undefined) {
    throw bgError('E_DEP_MISSING', '工作区服务不可用', 'DSH 尚未完成启动，请稍后重试');
  }
  if (typeof hintPath !== 'string' || hintPath.trim() === '') {
    throw bgError('E_NOT_FOUND', '缺少 workspace 参数', 'GET 请求用 ?workspace=<绝对路径>，写请求放 body.workspace');
  }
  const requested = hintPath.trim();
  if (!isAbsolute(requested)) {
    throw bgError('E_SCHEMA_INVALID', `workspace 必须是绝对路径：${requested}`, '请从 /bidgenie/workspaces 中选择工作区');
  }
  let workspace;
  try {
    workspace = await registry.resolveByPath(requested);
  } catch (error) {
    throw bgError('E_NOT_FOUND', `工作区不存在或无法访问：${requested}`, String(error?.message ?? error));
  }
  if (workspace === undefined) {
    throw bgError('E_NOT_FOUND', `目录未注册为 DSH 工作区：${requested}`, '请先在 DSH 中把该目录添加为工作区，再刷新面板');
  }
  if (!(await isDirectory(workspace.path))) {
    throw bgError('E_NOT_FOUND', `工作区目录不存在：${workspace.path}`, '目录可能已被移动或删除');
  }
  return {
    workspaceRoot: workspace.path,
    workspace: { id: String(workspace.id), path: workspace.path, title: workspace.title },
  };
}

/** `GET /bidgenie/workspaces` - synchronous projection of the durable registry. */
export function listWorkspaces(ctx) {
  const registry = ctx.get('workspaceRegistry');
  if (registry === undefined) return [];
  return registry.list().map((workspace) => ({
    id: String(workspace.id),
    path: workspace.path,
    title: workspace.title,
  }));
}

/* ----------------------------------------------------------------------------- */
/* Health / paths                                                               */
/* ----------------------------------------------------------------------------- */

/**
 * `GET /bidgenie/health` - must answer immediately after a panel reload, even
 * for a workspace that has not been initialized (no JSON read, no script).
 */
export async function health(ctx, workspaceHint) {
  let workspaceRoot = null;
  let initialized = false;
  if (typeof workspaceHint === 'string' && workspaceHint.trim() !== '') {
    const resolved = await resolveWorkspace(ctx, workspaceHint);
    workspaceRoot = resolved.workspaceRoot;
    initialized = await isDirectory(safeJoin(workspaceRoot, 'dsh'));
  }
  return {
    ok: true,
    version: PLUGIN_VERSION,
    hostVersion: HOST_VERSION_LABEL,
    workspaceRoot,
    initialized,
  };
}

/**
 * `bidgenie_paths` backing function: where the plugin and workspace live.
 * `projectRoot` / `packageRoot` stay `null` in M1 (no project argument yet).
 * Return shape is frozen by PRD 6.6.1 - do not add fields here.
 */
export async function pathsInfo(ctx, workspaceHint) {
  const python = resolvePython();
  let workspaceRoot = null;
  if (typeof workspaceHint === 'string' && workspaceHint.trim() !== '') {
    workspaceRoot = (await resolveWorkspace(ctx, workspaceHint)).workspaceRoot;
  }
  return {
    ok: true,
    pluginRoot: PLUGIN_ROOT,
    workspaceRoot,
    projectRoot: null,
    packageRoot: null,
    pythonExe: python === null ? null : python.exe,
    scriptsDir: SCRIPTS_DIR,
    templatesDir: join(PLUGIN_ROOT, 'templates'),
    hostVersion: HOST_VERSION_LABEL,
  };
}

/* ----------------------------------------------------------------------------- */
/* Workspace init                                                               */
/* ----------------------------------------------------------------------------- */

function relayScriptJson(result, fallbackMessage) {
  const failure = scriptFailure(result);
  if (failure !== null) throw bgError(failure.code, failure.message ?? fallbackMessage, failure.hint);
  return result.json !== null && typeof result.json === 'object' ? result.json : {};
}

/**
 * `POST /bidgenie/workspace/init` - create the `dsh/` tree only (decision Q1:
 * no Agent turn, no session injection). Idempotent by script contract.
 */
export async function workspaceInit(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const result = await runScript('bg_workspace.py', ['init', '--workspace', workspaceRoot, '--json'], {
    env: PANEL_ACTOR_ENV,
  });
  const json = relayScriptJson(result, '创建工作区数据目录失败');
  const created = Array.isArray(json.created) ? json.created : [];
  const registryRelative = typeof json.registryPath === 'string' ? json.registryPath : 'dsh/registry.json';
  return {
    ok: true,
    created,
    registryPath: toWorkspaceAbsolute(workspaceRoot, registryRelative),
    alreadyInitialized: json.alreadyInitialized === true,
  };
}

/* ----------------------------------------------------------------------------- */
/* Bidders                                                                      */
/* ----------------------------------------------------------------------------- */

const BIDDERS_DIR = 'dsh/bidders';
const SETTINGS_RELATIVE = 'dsh/config/user-settings.json';

/** 投标人资料完整度 → 面板 warning（创建/更新/详情响应携带，事件侧另由脚本发 bidder.incomplete）。 */
function bidderCompletenessView(completeness) {
  if (completeness === null || typeof completeness !== 'object' || Array.isArray(completeness)) {
    return { completeness: null, warnings: [] };
  }
  const overall = typeof completeness.overall === 'number' ? completeness.overall : null;
  const warnings = [];
  if (overall !== null && overall < 1) {
    const missing = [];
    if (typeof completeness.basic === 'number' && completeness.basic < 1) missing.push('基本信息');
    if (typeof completeness.certificates === 'number' && completeness.certificates < 1) missing.push('资质证书');
    if (typeof completeness.awards === 'number' && completeness.awards < 1) missing.push('业绩/获奖');
    warnings.push({
      code: 'bidder.incomplete',
      severity: 'warning',
      completeness: overall,
      message: `投标人资料完整度 ${Math.round(overall * 100)}%，建议补全：${missing.join('、')}（不影响创建，可稍后补全）`,
    });
  }
  return { completeness, warnings };
}

function userSettingsPath(workspaceRoot) {
  return safeJoin(workspaceRoot, ...SETTINGS_RELATIVE.split('/'));
}

async function readUserSettings(workspaceRoot) {
  return readJsonFile(userSettingsPath(workspaceRoot));
}

async function readRegistry(workspaceRoot) {
  return readJsonFile(safeJoin(workspaceRoot, 'dsh', 'registry.json'));
}

/** Mask one `sensitiveFields` value for panel display (raw value stays on disk). */
function maskValue(value) {
  if (typeof value !== 'string' || value === '') return value;
  if (value.length <= 4) return '****';
  if (value.length <= 8) return `${value.slice(0, 1)}****${value.slice(-1)}`;
  return `${value.slice(0, 4)}****${value.slice(-4)}`;
}

function readPath(object, dottedPath) {
  let current = object;
  for (const segment of dottedPath.split('.')) {
    if (current === null || typeof current !== 'object') return undefined;
    current = current[segment];
  }
  return current;
}

function writePath(object, dottedPath, value) {
  const segments = dottedPath.split('.');
  let current = object;
  for (const segment of segments.slice(0, -1)) {
    if (current[segment] === null || typeof current[segment] !== 'object') return false;
    current = current[segment];
  }
  const last = segments[segments.length - 1];
  if (!(last in current)) return false;
  current[last] = value;
  return true;
}

/**
 * CONTRACT §4: sensitive values are masked at the API layer only; files keep
 * the raw values. Paths come from `bidder.sensitiveFields` (PRD 7.7.1).
 */
export function maskBidderForPanel(bidder) {
  if (bidder === null || typeof bidder !== 'object') return { bidder, maskedFields: [] };
  let copy;
  try {
    copy = structuredClone(bidder);
  } catch {
    copy = JSON.parse(JSON.stringify(bidder));
  }
  const fields = Array.isArray(bidder.sensitiveFields) ? bidder.sensitiveFields : [];
  const maskedFields = [];
  for (const field of fields) {
    if (typeof field !== 'string' || field === '') continue;
    const current = readPath(copy, field);
    if (current === undefined || current === '') continue;
    if (writePath(copy, field, maskValue(current))) maskedFields.push(field);
  }
  return { bidder: copy, maskedFields };
}

function numberOrNull(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * Panel list projection. The six CONTRACT fields are mandatory; `counts` is
 * additive (bg_bidder.py list provides it) and is normalized so the panel can
 * show the four group counters (证书/业绩/奖惩). `performances` is kept as an
 * alias because the script uses that key while the panel reads `performance`.
 */
function normalizeSummary(item, currentBidderId) {
  const bidderId = typeof item?.bidderId === 'string' ? item.bidderId : null;
  const rawCounts = item?.counts !== null && typeof item?.counts === 'object' ? item.counts : null;
  const counts =
    rawCounts === null
      ? null
      : {
          certificates: numberOrNull(rawCounts.certificates),
          performance: numberOrNull(rawCounts.performance ?? rawCounts.performances),
          performances: numberOrNull(rawCounts.performances ?? rawCounts.performance),
          awards: numberOrNull(rawCounts.awards),
          penalties: numberOrNull(rawCounts.penalties),
        };
  return {
    bidderId,
    displayName: typeof item?.displayName === 'string' ? item.displayName : '',
    bidderType: typeof item?.bidderType === 'string' ? item.bidderType : 'legal_entity',
    updatedAt: typeof item?.updatedAt === 'string' ? item.updatedAt : null,
    isCurrent: item?.isCurrent === true || (currentBidderId !== null && bidderId === currentBidderId),
    completeness:
      typeof item?.completeness === 'number'
        ? item.completeness
        : item?.completeness !== null && typeof item?.completeness === 'object'
          ? item.completeness
          : null,
    ...(counts === null ? {} : { counts }),
  };
}

/**
 * `GET /bidgenie/bidders` - list view. Delegates to `bg_bidder.py list`;
 * falls back to `dsh/registry.json` read-only when the script is absent (M1
 * development window only - M2 always ships the script).
 */
export async function bidderList(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const result = await runScript('bg_bidder.py', ['list', '--workspace', workspaceRoot, '--json']);
  if (result.error?.code === 'E_DEP_MISSING') {
    return listBiddersFromRegistry(workspaceRoot);
  }
  const json = relayScriptJson(result, '读取投标人列表失败');
  const settings = await readUserSettings(workspaceRoot);
  const currentBidderId =
    typeof settings?.currentBidderId === 'string'
      ? settings.currentBidderId
      : typeof json.currentBidderId === 'string'
        ? json.currentBidderId
        : null;
  const source = Array.isArray(json.bidders) ? json.bidders : [];
  return {
    ok: true,
    bidders: source.map((item) => normalizeSummary(item, currentBidderId)),
    currentBidderId,
  };
}

async function listBiddersFromRegistry(workspaceRoot) {
  const registry = await readRegistry(workspaceRoot);
  if (registry === undefined) {
    throw bgError('E_NOT_FOUND', '工作区尚未初始化', '请先创建数据目录');
  }
  const settings = await readUserSettings(workspaceRoot);
  const currentBidderId = typeof settings?.currentBidderId === 'string' ? settings.currentBidderId : null;
  const source = Array.isArray(registry.bidders) ? registry.bidders : [];
  const bidders = [];
  for (const item of source) {
    const summary = normalizeSummary(item, currentBidderId);
    if (summary.completeness === null && typeof item?.dirName === 'string' && item.dirName !== '') {
      try {
        const meta = await readJsonFile(
          safeJoin(workspaceRoot, ...BIDDERS_DIR.split('/'), item.dirName, 'meta.json'),
        );
        if (typeof meta?.completeness?.overall === 'number') summary.completeness = meta.completeness.overall;
      } catch {
        /* a broken bidder directory must not break the whole list */
      }
    }
    bidders.push(summary);
  }
  return { ok: true, bidders, currentBidderId };
}

/** Locate one bidder directory from `bidderId` (registry first, then directory scan). */
async function findBidderDir(workspaceRoot, bidderId) {
  if (typeof bidderId !== 'string' || bidderId.trim() === '') {
    throw bgError('E_SCHEMA_INVALID', '缺少 bidderId', '请选择投标人后重试');
  }
  const registry = await readRegistry(workspaceRoot);
  const entries = Array.isArray(registry?.bidders) ? registry.bidders : [];
  const match = entries.find((item) => item?.bidderId === bidderId && typeof item?.dirName === 'string');
  if (match !== undefined) {
    const dir = safeJoin(workspaceRoot, ...BIDDERS_DIR.split('/'), match.dirName);
    if (await isDirectory(dir)) return { dirName: match.dirName, dir };
  }
  let listed = [];
  try {
    listed = await readdir(safeJoin(workspaceRoot, ...BIDDERS_DIR.split('/')), { withFileTypes: true });
  } catch (error) {
    if (error?.code === 'ENOENT') {
      throw bgError('E_NOT_FOUND', `投标人不存在：${bidderId}`, '请刷新列表');
    }
    throw error;
  }
  for (const entry of listed) {
    if (!entry.isDirectory() || entry.name.startsWith('.')) continue;
    const dir = safeJoin(workspaceRoot, ...BIDDERS_DIR.split('/'), entry.name);
    const bidder = await readJsonFile(join(dir, 'bidder.json'));
    if (bidder?.bidderId === bidderId) return { dirName: entry.name, dir };
  }
  throw bgError('E_NOT_FOUND', `投标人不存在：${bidderId}`, '请刷新列表后重试');
}

/**
 * `GET /bidgenie/bidders?bidderId=` - detail view used by the M1 edit form.
 * Read-only direct JSON reads (files are the source of truth); sensitive fields
 * are masked (CONTRACT §4). Writes always go through `bg_bidder.py`.
 */
export async function bidderDetail(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const bidderId = payload?.bidderId;
  const { dirName, dir } = await findBidderDir(workspaceRoot, bidderId);
  const rawBidder = await readJsonFile(join(dir, 'bidder.json'));
  if (rawBidder === undefined) {
    throw bgError('E_NOT_FOUND', `投标人数据文件不存在：${bidderId}`, '该投标人目录可能已损坏');
  }
  const certificates = (await readJsonFile(join(dir, 'certificates.json'))) ?? { items: [] };
  const performance = (await readJsonFile(join(dir, 'performance.json'))) ?? { items: [] };
  const awardsPenalties = (await readJsonFile(join(dir, 'awards-penalties.json'))) ?? { awards: [], penalties: [] };
  const meta = (await readJsonFile(join(dir, 'meta.json'))) ?? null;
  // S14 (`maskSensitive`, default true) governs API-layer masking; files always
  // keep raw values (CONTRACT §4). Turning S14 off lets the panel show/edit them.
  const settings = await readUserSettings(workspaceRoot);
  const maskSensitive = settings?.maskSensitive !== false;
  const { bidder, maskedFields } = maskSensitive ? maskBidderForPanel(rawBidder) : { bidder: rawBidder, maskedFields: [] };
  const completenessView = bidderCompletenessView(meta?.completeness);
  return {
    ok: true,
    bidderId,
    dirName,
    bidder,
    certificates: { items: Array.isArray(certificates.items) ? certificates.items : [] },
    performance: { items: Array.isArray(performance.items) ? performance.items : [] },
    awardsPenalties: {
      awards: Array.isArray(awardsPenalties.awards) ? awardsPenalties.awards : [],
      penalties: Array.isArray(awardsPenalties.penalties) ? awardsPenalties.penalties : [],
    },
    meta,
    completeness: completenessView.completeness,
    warnings: completenessView.warnings,
    maskedFields,
  };
}

/** Normalize `create` payload: CONTRACT body `{bidder, certificates?, ...}`. */
function normalizeBidderWritePayload(payload) {
  const source = payload?.data !== null && typeof payload?.data === 'object' ? payload.data : payload;
  const bidder =
    source?.bidder !== null && typeof source?.bidder === 'object'
      ? source.bidder
      : payload?.bidder !== null && typeof payload?.bidder === 'object'
        ? payload.bidder
        : null;
  if (bidder === null) {
    throw bgError('E_SCHEMA_INVALID', '缺少 bidder 数据', 'create 需要 body.bidder 对象（displayName 必填）');
  }
  const out = { bidder };
  for (const key of ['certificates', 'performance', 'awardsPenalties']) {
    const value = source?.[key] ?? payload?.[key];
    if (value !== null && typeof value === 'object') out[key] = value;
  }
  return out;
}

/** `POST /bidgenie/bidders` + `POST /bidgenie/bidders/create`. */
export async function bidderCreate(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const data = normalizeBidderWritePayload(payload);
  const result = await withTempJson('bidder-create', data, (dataFile) =>
    runScript('bg_bidder.py', ['create', '--workspace', workspaceRoot, '--data', dataFile, '--json'], {
      env: PANEL_ACTOR_ENV,
    }),
  );
  const json = relayScriptJson(result, '创建投标人失败');
  const completenessView = bidderCompletenessView(json.completeness);
  return {
    ok: true,
    bidderId: typeof json.bidderId === 'string' ? json.bidderId : null,
    dirName: typeof json.dirName === 'string' ? json.dirName : null,
    alreadyExists: json.alreadyExists === true,
    completeness: completenessView.completeness,
    warnings: completenessView.warnings,
    source: 'panel',
    actor: 'user',
  };
}

/** `POST /bidgenie/bidders/update` + compat `PUT /bidgenie/bidders/<id>`. */
export async function bidderUpdate(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const bidderId = payload?.bidderId;
  if (typeof bidderId !== 'string' || bidderId.trim() === '') {
    throw bgError('E_SCHEMA_INVALID', '缺少 bidderId', 'update 需要 body.bidderId');
  }
  const source = payload?.data !== null && typeof payload?.data === 'object' ? payload.data : payload;
  const patch =
    source?.patch !== null && typeof source?.patch === 'object'
      ? source.patch
      : source?.bidder !== null && typeof source?.bidder === 'object'
        ? source.bidder
        : payload?.bidder !== null && typeof payload?.bidder === 'object'
          ? payload.bidder
          : null;
  if (patch === null || Object.keys(patch).length === 0) {
    throw bgError('E_SCHEMA_INVALID', '缺少要更新的字段', 'update 需要 body.patch（或 body.bidder）对象');
  }
  const data = { bidder: patch };
  for (const key of ['certificates', 'performance', 'awardsPenalties']) {
    const value = source?.[key] ?? payload?.[key];
    if (value !== null && typeof value === 'object') data[key] = value;
  }
  const result = await withTempJson('bidder-update', data, (dataFile) =>
    runScript('bg_bidder.py', ['update', '--workspace', workspaceRoot, '--bidder', bidderId, '--data', dataFile, '--json'], {
      env: PANEL_ACTOR_ENV,
    }),
  );
  const json = relayScriptJson(result, '更新投标人失败');
  const completenessView = bidderCompletenessView(json.completeness);
  return {
    ok: true,
    bidderId: typeof json.bidderId === 'string' ? json.bidderId : bidderId,
    updatedAt: typeof json.updatedAt === 'string' ? json.updatedAt : null,
    completeness: completenessView.completeness,
    warnings: completenessView.warnings,
    source: 'panel',
    actor: 'user',
  };
}

/** `POST /bidgenie/bidders/delete` + compat `DELETE /bidgenie/bidders/<id>`. */
export async function bidderDelete(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  if (payload?.confirm !== true) {
    throw bgError('E_USER_DECISION_REQUIRED', '删除投标人需要二次确认', '请在请求体中传 confirm: true');
  }
  const bidderId = payload?.bidderId;
  if (typeof bidderId !== 'string' || bidderId.trim() === '') {
    throw bgError('E_SCHEMA_INVALID', '缺少 bidderId', 'delete 需要 body.bidderId');
  }
  const result = await runScript('bg_bidder.py', [
    'delete',
    '--workspace',
    workspaceRoot,
    '--bidder',
    bidderId,
    '--confirm',
    '--json',
  ], { env: PANEL_ACTOR_ENV });
  const json = relayScriptJson(result, '删除投标人失败');
  return {
    ok: true,
    bidderId,
    trashedTo: typeof json.trashedTo === 'string' ? json.trashedTo : typeof json.trashDir === 'string' ? json.trashDir : null,
  };
}

/** `POST /bidgenie/bidders/set-current` + compat `POST /bidgenie/bidders/<id>/current`. */
export async function bidderSetCurrent(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const bidderId = payload?.bidderId;
  if (typeof bidderId !== 'string' || bidderId.trim() === '') {
    throw bgError('E_SCHEMA_INVALID', '缺少 bidderId', 'set-current 需要 body.bidderId');
  }
  const result = await runScript('bg_bidder.py', [
    'set-current',
    '--workspace',
    workspaceRoot,
    '--bidder',
    bidderId,
    '--json',
  ], { env: PANEL_ACTOR_ENV });
  const json = relayScriptJson(result, '设置当前投标人失败');
  const settings = await readUserSettings(workspaceRoot);
  return {
    ok: true,
    currentBidderId:
      typeof json.currentBidderId === 'string'
        ? json.currentBidderId
        : typeof settings?.currentBidderId === 'string'
          ? settings.currentBidderId
          : bidderId,
  };
}

/* ----------------------------------------------------------------------------- */
/* Export style profiles (M3.5 v2, CONTRACT §9: two isolated official sets)     */
/* ----------------------------------------------------------------------------- */

const USER_STYLES_DIR = 'dsh/styles';
const OFFICIAL_STYLES_ROOT = join(PLUGIN_ROOT, 'templates', 'styles');
const STYLE_DOC_TYPES = ['tech', 'keyInfo', 'overview'];
/** user/official sub-directory name per docType (CONTRACT §9.2/§9.3). */
const STYLE_SUBDIR = { tech: 'tech', keyInfo: 'keyinfo', overview: 'overview' };
const STYLE_PROFILES_FIELD = 'styleProfiles';
const LEGACY_STYLE_PROFILE_FIELD = 'styleProfile';
/** v1 style ids -> v2 doc-type sets (read-time migration; PUT tolerates them too). */
const LEGACY_STYLE_ALIASES = {
  tech: ['official-default'],
  keyInfo: ['official-default', 'keyinfo'],
  overview: [],
};
/**
 * Contract-shaped mock built-ins used only until the official style files are
 * packaged under `templates/styles/{tech,keyinfo,overview}/` (backend builds them
 * in parallel). Keeps the panel/settings usable instead of returning empty lists.
 */
const MOCK_BUILTIN_STYLES = {
  tech: [{ id: 'official-tech', label: '官方-技术方案导出样式' }],
  keyInfo: [{ id: 'official-keyinfo', label: '官方-关键信息导出样式' }],
  overview: [{ id: 'official-overview', label: '官方-工作概览导出样式' }],
};

function emptyStyleBuckets() {
  return { tech: [], keyInfo: [], overview: [] };
}

function styleDocTypeLabel(docType) {
  if (docType === 'tech') return '技术方案';
  if (docType === 'keyInfo') return '关键信息';
  return '工作概览';
}

function normalizeStyleDocTypeValue(raw) {
  const list = Array.isArray(raw) ? raw : typeof raw === 'string' ? [raw] : [];
  const docTypes = [];
  for (const item of list) {
    if (typeof item !== 'string') continue;
    const value = item.trim();
    if (STYLE_DOC_TYPES.includes(value) && !docTypes.includes(value)) docTypes.push(value);
  }
  return docTypes;
}

function styleWarning(file, message) {
  return { file, message };
}

/** Keep exactly one `JSON 解析失败：` prefix (readJsonFile already prefixes it). */
function describeStyleJsonError(error) {
  const detail = String(error?.message ?? error);
  return detail.startsWith('JSON 解析失败') ? detail : `JSON 解析失败：${detail}`;
}

async function listJsonFiles(directory) {
  try {
    const entries = await readdir(directory, { withFileTypes: true });
    return entries.filter((entry) => entry.isFile() && entry.name.toLowerCase().endsWith('.json')).map((entry) => entry.name);
  } catch {
    return null;
  }
}

/** Shared minimal validation for one style document (CONTRACT §9.2/§9.3). */
function readStyleDocument(data, file, warnings, expectedDocType) {
  if (data === undefined) {
    warnings.push(styleWarning(file, '文件为空或不可读'));
    return null;
  }
  if (data === null || typeof data !== 'object' || Array.isArray(data)) {
    warnings.push(styleWarning(file, '样式文件必须是 JSON 对象'));
    return null;
  }
  const id = typeof data.id === 'string' ? data.id.trim() : '';
  const name = typeof data.name === 'string' ? data.name.trim() : '';
  const schemaVersion = typeof data.schemaVersion === 'string' ? data.schemaVersion.trim() : '';
  if (id === '') {
    warnings.push(styleWarning(file, '缺少 id'));
    return null;
  }
  if (name === '') {
    warnings.push(styleWarning(file, `缺少 name（id=${id}）`));
    return null;
  }
  if (schemaVersion === '') {
    warnings.push(styleWarning(file, `缺少 schemaVersion（id=${id}）`));
    return null;
  }
  const docTypes = normalizeStyleDocTypeValue(data.docType);
  if (docTypes.length > 0 && !docTypes.includes(expectedDocType)) {
    warnings.push(
      styleWarning(file, `docType 与目录不一致（目录 ${STYLE_SUBDIR[expectedDocType]}，实际 ${docTypes.join('/')}）`),
    );
    return null;
  }
  const label =
    (typeof data.label === 'string' && data.label.trim() !== '' ? data.label.trim() : '') || name;
  return { id, label: label || name, name: name || label, docTypes };
}

/**
 * Scan packaged official styles: `templates/styles/{tech,keyinfo}/*.json`
 * (CONTRACT §9.2). Missing/empty directories fall back to the contract-shaped
 * mock (backend ships the files in parallel); malformed files become warnings.
 */
async function readOfficialStyleProfiles() {
  const builtin = emptyStyleBuckets();
  const warnings = [];
  for (const docType of STYLE_DOC_TYPES) {
    const subdir = STYLE_SUBDIR[docType];
    const directory = join(OFFICIAL_STYLES_ROOT, subdir);
    const files = (await listJsonFiles(directory)) ?? [];
    for (const fileName of files) {
      const file = `templates/styles/${subdir}/${fileName}`;
      let data;
      try {
        data = await readJsonFile(join(directory, fileName));
      } catch (error) {
        warnings.push(styleWarning(file, describeStyleJsonError(error)));
        continue;
      }
      const profile = readStyleDocument(data, file, warnings, docType);
      if (profile === null) continue;
      builtin[docType].push({ id: profile.id, label: profile.label });
    }
    builtin[docType].sort((left, right) => left.id.localeCompare(right.id));
    if (builtin[docType].length === 0) {
      builtin[docType] = MOCK_BUILTIN_STYLES[docType].map((item) => ({ id: item.id, label: item.label }));
    }
  }
  return { builtin, warnings };
}

/**
 * Scan user custom styles (CONTRACT §9.3):
 *  - `<workspace>/dsh/styles/{tech,keyinfo}/*.json` (directory decides docType);
 *  - legacy flat `<workspace>/dsh/styles/*.json` are classified by their
 *    `docType` field and reported with a migration warning.
 * Never creates the directories; malformed/unmatched files are warnings only.
 */
async function readCustomStyleProfilesV2(workspaceRoot, builtin) {
  const custom = emptyStyleBuckets();
  const warnings = [];
  const stylesRoot = safeJoin(workspaceRoot, ...USER_STYLES_DIR.split('/'));
  if (!(await isDirectory(stylesRoot))) return { custom, warnings };

  const officialIds = new Map();
  for (const docType of STYLE_DOC_TYPES) {
    officialIds.set(docType, new Set((builtin[docType] ?? []).map((item) => item.id)));
  }
  const seen = new Map(STYLE_DOC_TYPES.map((docType) => [docType, new Set()]));

  const addProfile = (docType, profile, file) => {
    if (officialIds.get(docType).has(profile.id)) {
      warnings.push(styleWarning(file, `id 与官方样式冲突，已忽略：${profile.id}`));
      return false;
    }
    if (seen.get(docType).has(profile.id)) {
      warnings.push(styleWarning(file, `id 重复，已忽略：${profile.id}`));
      return false;
    }
    seen.get(docType).add(profile.id);
    custom[docType].push({ id: profile.id, label: profile.label, path: profile.path });
    return true;
  };

  for (const docType of STYLE_DOC_TYPES) {
    const subdir = STYLE_SUBDIR[docType];
    const directory = join(stylesRoot, subdir);
    const files = (await listJsonFiles(directory)) ?? [];
    for (const fileName of files) {
      const file = `${USER_STYLES_DIR}/${subdir}/${fileName}`;
      let data;
      try {
        data = await readJsonFile(join(directory, fileName));
      } catch (error) {
        warnings.push(styleWarning(file, describeStyleJsonError(error)));
        continue;
      }
      const profile = readStyleDocument(data, file, warnings, docType);
      if (profile === null) continue;
      addProfile(docType, { ...profile, path: join(directory, fileName) }, file);
    }
  }

  // Legacy flat files (v1 layout) directly under dsh/styles/.
  let rootEntries;
  try {
    rootEntries = await readdir(stylesRoot, { withFileTypes: true });
  } catch (error) {
    warnings.push(styleWarning(USER_STYLES_DIR, `无法读取样式目录：${String(error?.message ?? error)}`));
    rootEntries = [];
  }
  for (const entry of rootEntries) {
    if (!entry.isFile() || !entry.name.toLowerCase().endsWith('.json')) continue;
    const file = `${USER_STYLES_DIR}/${entry.name}`;
    let data;
    try {
      data = await readJsonFile(join(stylesRoot, entry.name));
    } catch (error) {
      warnings.push(styleWarning(file, describeStyleJsonError(error)));
      continue;
    }
    // expectedDocType null => only check shared fields, docType drives classification.
    const docTypesInFile = data !== null && typeof data === 'object' && !Array.isArray(data) ? normalizeStyleDocTypeValue(data.docType) : [];
    if (docTypesInFile.length === 0) {
      const base = readStyleDocument(data, file, warnings, null);
      if (base !== null) warnings.push(styleWarning(file, `旧版扁平样式缺少 docType（id=${base.id}），无法归类；建议移动到 dsh/styles/tech/ 或 dsh/styles/keyinfo/`));
      continue;
    }
    const profile = readStyleDocument(data, file, warnings, docTypesInFile[0]);
    if (profile === null) continue;
    const path = join(stylesRoot, entry.name);
    let moved = false;
    for (const docType of profile.docTypes.length > 0 ? profile.docTypes : docTypesInFile) {
      if (addProfile(docType, { ...profile, path }, file)) moved = true;
    }
    if (moved) {
      warnings.push(
        styleWarning(file, `旧版扁平样式，建议移动到 dsh/styles/${(profile.docTypes.length > 0 ? profile.docTypes : docTypesInFile).map((docType) => STYLE_SUBDIR[docType]).join('/ 或 ')}/ 子目录`),
      );
    }
  }

  for (const docType of STYLE_DOC_TYPES) custom[docType].sort((left, right) => left.id.localeCompare(right.id));
  return { custom, warnings };
}

/** docType -> Map(id -> profile) for validation/defaults (official first, then custom). */
function buildStyleCatalog(builtin, custom) {
  const catalog = new Map(STYLE_DOC_TYPES.map((docType) => [docType, new Map()]));
  for (const docType of STYLE_DOC_TYPES) {
    for (const profile of builtin[docType] ?? []) {
      catalog.get(docType).set(profile.id, { ...profile, source: 'builtin' });
    }
    for (const profile of custom[docType] ?? []) {
      catalog.get(docType).set(profile.id, { ...profile, source: 'custom' });
    }
  }
  return catalog;
}

function usableStyleIds(catalog, docType) {
  return [...catalog.get(docType).keys()];
}

function defaultStyleId(catalog, docType) {
  for (const [id, profile] of catalog.get(docType)) {
    if (profile.source === 'builtin') return id;
  }
  return MOCK_BUILTIN_STYLES[docType][0].id;
}

function defaultStyleProfiles(catalog) {
  return {
    tech: defaultStyleId(catalog, 'tech'),
    keyInfo: defaultStyleId(catalog, 'keyInfo'),
    overview: defaultStyleId(catalog, 'overview'),
  };
}

/**
 * Resolve a style id for one docType: exact -> case-insensitive canonical
 * (`official-keyInfo` vs `official-keyinfo`) -> v1 alias migration
 * (`official-default`/`keyinfo`). Returns the canonical catalog id or null.
 */
function resolveStyleId(catalog, docType, rawValue, { allowLegacyAliases = false } = {}) {
  if (typeof rawValue !== 'string' || rawValue.trim() === '') return null;
  const value = rawValue.trim();
  const byId = catalog.get(docType);
  if (byId.has(value)) return value;
  const lowered = value.toLowerCase();
  for (const id of byId.keys()) {
    if (id.toLowerCase() === lowered) return id;
  }
  if (allowLegacyAliases && LEGACY_STYLE_ALIASES[docType].some((alias) => alias.toLowerCase() === lowered)) {
    return defaultStyleId(catalog, docType);
  }
  return null;
}

async function styleCatalogForWorkspace(workspaceRoot) {
  const official = await readOfficialStyleProfiles();
  const customScan = await readCustomStyleProfilesV2(workspaceRoot, official.builtin);
  return {
    builtin: official.builtin,
    custom: customScan.custom,
    warnings: [...official.warnings, ...customScan.warnings],
    catalog: buildStyleCatalog(official.builtin, customScan.custom),
  };
}

function styleNotValidError(docType, value, catalog) {
  const allowed = usableStyleIds(catalog, docType);
  return bgError(
    'E_SCHEMA_INVALID',
    `样式不可用：${String(value)}（${styleDocTypeLabel(docType)}导出样式集合，docType=${docType}）`,
    `可选值（${docType}）：${allowed.length === 0 ? '（无）' : allowed.join(', ')}`,
  );
}

/**
 * `GET /bidgenie/styles?workspace=[&kind=tech|keyInfo|overview]` - CONTRACT §9.4 v2 shape:
 * `{ok, builtin:{tech,keyInfo,overview}, custom:{...}, warnings}`. With `kind`
 * only that docType bucket is returned (others omitted). Never creates `dsh/styles/`.
 */
export async function stylesList(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const { builtin, custom, warnings } = await styleCatalogForWorkspace(workspaceRoot);
  const rawKind = typeof payload?.kind === 'string' ? payload.kind.trim() : '';
  if (rawKind === '') return { ok: true, builtin, custom, warnings };
  const kind = STYLE_DOC_TYPES.find((docType) => docType.toLowerCase() === rawKind.toLowerCase());
  if (kind === undefined) {
    throw bgError(
      'E_SCHEMA_INVALID',
      `未知样式类别：${rawKind}`,
      `kind 可选值：${STYLE_DOC_TYPES.join(' / ')}`,
    );
  }
  return { ok: true, kind, builtin: { [kind]: builtin[kind] ?? [] }, custom: { [kind]: custom[kind] ?? [] }, warnings };
}

/**
 * CONTRACT §9.4 migration: derive `styleProfiles` in memory (never writes disk).
 * v1 values are mapped: `official-default` -> each docType default; `keyinfo` ->
 * keyInfo default; legacy `styleProfile` is the fallback for the tech candidate.
 */
export function migrateStyleProfiles(settings, catalog) {
  const existingProfiles =
    settings?.[STYLE_PROFILES_FIELD] !== null &&
    typeof settings?.[STYLE_PROFILES_FIELD] === 'object' &&
    !Array.isArray(settings[STYLE_PROFILES_FIELD])
      ? settings[STYLE_PROFILES_FIELD]
      : {};
  const defaults = defaultStyleProfiles(catalog);
  const legacyValue = typeof settings?.[LEGACY_STYLE_PROFILE_FIELD] === 'string' ? settings[LEGACY_STYLE_PROFILE_FIELD].trim() : '';
  const techRaw =
    typeof existingProfiles.tech === 'string' && existingProfiles.tech.trim() !== ''
      ? existingProfiles.tech
      : legacyValue !== ''
        ? legacyValue
        : defaults.tech;
  const keyInfoRaw =
    typeof existingProfiles.keyInfo === 'string' && existingProfiles.keyInfo.trim() !== ''
      ? existingProfiles.keyInfo
      : defaults.keyInfo;
  const overviewRaw =
    typeof existingProfiles.overview === 'string' && existingProfiles.overview.trim() !== ''
      ? existingProfiles.overview
      : defaults.overview;
  return {
    ...settings,
    [STYLE_PROFILES_FIELD]: {
      tech: resolveStyleId(catalog, 'tech', techRaw, { allowLegacyAliases: true }) ?? defaults.tech,
      keyInfo: resolveStyleId(catalog, 'keyInfo', keyInfoRaw, { allowLegacyAliases: true }) ?? defaults.keyInfo,
      overview: resolveStyleId(catalog, 'overview', overviewRaw, { allowLegacyAliases: true }) ?? defaults.overview,
    },
  };
}

/** Validate an incoming `styleProfiles` object per docType set (CONTRACT §9.4). */
function validateIncomingStyleProfiles(input, catalog) {
  if (input === null || typeof input !== 'object' || Array.isArray(input)) {
    throw bgError(
      'E_SCHEMA_INVALID',
      'styleProfiles 必须是对象',
      '形如 {"tech":"official-tech","keyInfo":"official-keyinfo","overview":"official-overview"}',
    );
  }
  // Unknown keys (e.g. from newer/older clients) are ignored instead of rejected.
  const validated = {};
  for (const docType of STYLE_DOC_TYPES) {
    const rawValue = input[docType];
    if (rawValue === undefined) continue;
    if (typeof rawValue !== 'string' || rawValue.trim() === '') {
      throw bgError(
        'E_SCHEMA_INVALID',
        `styleProfiles.${docType} 必须是非空字符串`,
        `可选值（${docType}）：${usableStyleIds(catalog, docType).join(', ')}`,
      );
    }
    const resolved = resolveStyleId(catalog, docType, rawValue, { allowLegacyAliases: true });
    if (resolved === null) throw styleNotValidError(docType, rawValue, catalog);
    validated[docType] = resolved;
  }
  return validated;
}

/* ----------------------------------------------------------------------------- */
/* Settings                                                                     */
/* ----------------------------------------------------------------------------- */

/** `GET /bidgenie/settings` - S1-S15 + in-memory `styleProfiles` migration (§9). */
export async function settingsGet(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const settings = await readUserSettings(workspaceRoot);
  if (settings === undefined) {
    throw bgError('E_NOT_FOUND', '工作区尚未初始化', '请先在面板中创建数据目录');
  }
  const { catalog } = await styleCatalogForWorkspace(workspaceRoot);
  return { ok: true, settings: migrateStyleProfiles(settings, catalog) };
}

/**
 * `PUT /bidgenie/settings` - atomic merge write (S1-S15 + §9 styleProfiles).
 * Validates each value against its own docType set; legacy `styleProfile` input is
 * accepted and mirrored into `styleProfiles.tech` but is no longer persisted.
 */
export async function settingsPut(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const incoming = payload?.settings;
  if (incoming === null || typeof incoming !== 'object' || Array.isArray(incoming)) {
    throw bgError('E_SCHEMA_INVALID', 'settings 必须是对象', '请求体形状：{workspace, settings:{...}}');
  }
  const { catalog } = await styleCatalogForWorkspace(workspaceRoot);
  const styleProfilesInput = incoming[STYLE_PROFILES_FIELD];
  const validatedProfiles = styleProfilesInput === undefined ? null : validateIncomingStyleProfiles(styleProfilesInput, catalog);
  const legacyRaw =
    typeof incoming[LEGACY_STYLE_PROFILE_FIELD] === 'string' && incoming[LEGACY_STYLE_PROFILE_FIELD].trim() !== ''
      ? incoming[LEGACY_STYLE_PROFILE_FIELD].trim()
      : null;
  const legacyTechId = legacyRaw === null ? null : resolveStyleId(catalog, 'tech', legacyRaw, { allowLegacyAliases: true });

  const existing = (await readUserSettings(workspaceRoot)) ?? { schemaVersion: '1.0' };
  const updatedAt = formatIsoWithOffset();
  const next = {
    ...existing,
    ...incoming,
    schemaVersion:
      typeof incoming.schemaVersion === 'string'
        ? incoming.schemaVersion
        : typeof existing.schemaVersion === 'string'
          ? existing.schemaVersion
          : '1.0',
    updatedAt,
  };
  // `styleProfile` is legacy-read-only: normalize away from the written document.
  delete next[LEGACY_STYLE_PROFILE_FIELD];
  delete next[STYLE_PROFILES_FIELD];

  const existingProfiles = migrateStyleProfiles(existing, catalog)[STYLE_PROFILES_FIELD];
  const mergedProfiles = { ...defaultStyleProfiles(catalog), ...existingProfiles };
  if (validatedProfiles !== null) Object.assign(mergedProfiles, validatedProfiles);
  if (legacyTechId !== null && (validatedProfiles === null || validatedProfiles.tech === undefined)) {
    mergedProfiles.tech = legacyTechId;
  }
  next[STYLE_PROFILES_FIELD] = mergedProfiles;

  await writeJsonAtomic(userSettingsPath(workspaceRoot), next);
  return { ok: true, updatedAt };
}

/* ----------------------------------------------------------------------------- */
/* Projects (read-only listing, M1)                                             */
/* ----------------------------------------------------------------------------- */

function projectStage(state, project, entry) {
  return (
    (typeof state?.currentStage === 'string' && state.currentStage) ||
    (typeof project?.currentStage === 'string' && project.currentStage) ||
    (typeof entry?.currentStage === 'string' && entry.currentStage) ||
    null
  );
}

/** `GET /bidgenie/projects` - registry mirror + per-project state summary. */
export async function projectsList(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const registry = await readRegistry(workspaceRoot);
  if (registry === undefined) {
    throw bgError('E_NOT_FOUND', '工作区尚未初始化', '请先在面板中创建数据目录');
  }
  const entries = Array.isArray(registry.projects) ? registry.projects : [];
  const projects = [];
  for (const entry of entries) {
    const dirName = typeof entry?.dirName === 'string' ? entry.dirName : null;
    if (dirName === null || dirName.startsWith('..')) continue;
    let projectRoot;
    try {
      projectRoot = safeJoin(workspaceRoot, dirName);
    } catch {
      continue;
    }
    const project = await readJsonFile(join(projectRoot, 'project.json'));
    const state = await readJsonFile(join(projectRoot, 'state.json'));
    projects.push({
      projectId:
        (typeof entry?.projectId === 'string' && entry.projectId) ||
        (typeof project?.projectId === 'string' && project.projectId) ||
        dirName,
      dirName,
      projectName:
        (typeof project?.projectName === 'string' && project.projectName) ||
        (typeof entry?.projectName === 'string' && entry.projectName) ||
        dirName,
      currentStage: projectStage(state, project, entry),
      currentPackage:
        (typeof state?.currentPackage === 'string' && state.currentPackage) ||
        (typeof state?.currentPackageId === 'string' && state.currentPackageId) ||
        null,
      updatedAt:
        (typeof state?.updatedAt === 'string' && state.updatedAt) ||
        (typeof project?.updatedAt === 'string' && project.updatedAt) ||
        (typeof entry?.updatedAt === 'string' && entry.updatedAt) ||
        null,
    });
  }
  return { ok: true, projects };
}

/**
 * Locate one project by `projectId` or directory name (CONTRACT §8.3 / PRD 6.6.3
 * `resolveProject`): registry lookup first, then the directory name itself.
 */
async function findProject(workspaceRoot, projectKey) {
  const registry = await readRegistry(workspaceRoot);
  if (registry === undefined) {
    throw bgError('E_NOT_FOUND', '工作区尚未初始化', '请先在面板中创建数据目录');
  }
  const entries = Array.isArray(registry.projects) ? registry.projects : [];
  const entry = entries.find((item) => item !== null && typeof item === 'object' && (item.projectId === projectKey || item.dirName === projectKey)) ?? null;
  const dirName = typeof entry?.dirName === 'string' && entry.dirName !== '' ? entry.dirName : projectKey;
  if (
    typeof dirName !== 'string' ||
    dirName.trim() === '' ||
    dirName.includes('/') ||
    dirName.includes('\\') ||
    dirName.startsWith('.')
  ) {
    throw bgError('E_SCHEMA_INVALID', `project 参数非法：${String(projectKey)}`, '传 projectId 或项目目录名');
  }
  const projectRoot = safeJoin(workspaceRoot, dirName);
  if (!(await isDirectory(projectRoot))) {
    throw bgError('E_NOT_FOUND', `项目不存在：${projectKey}`, '请刷新项目列表后重试');
  }
  const project = (await readJsonFile(join(projectRoot, 'project.json'))) ?? {};
  const state = await readJsonFile(join(projectRoot, 'state.json'));
  if (state === undefined) {
    throw bgError('E_NOT_FOUND', `项目状态文件缺失：${dirName}/state.json`, '项目可能尚未初始化完成，请重试或从备份恢复');
  }
  return { entry, dirName, projectRoot, project, state };
}

/**
 * `GET /bidgenie/projects/state?workspace=&project=` - read-only state summary.
 * Prefers `bg_state.py status --json` (it owns `allowedNext`); before the M2
 * script lands it degrades to reading `state.json` directly (same source of truth).
 */
export async function projectState(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const projectKey = payload?.project;
  if (typeof projectKey !== 'string' || projectKey.trim() === '') {
    throw bgError('E_SCHEMA_INVALID', '缺少 project 参数', 'GET /bidgenie/projects/state?workspace=<abs>&project=<projectId|目录名>');
  }
  const found = await findProject(workspaceRoot, projectKey.trim());
  const state = found.state;

  // Canonical: `--project <dirName>` (bg_state.py resolves it against --workspace
  // once the cwd-sensitive build was fixed). Defensive retry with the absolute
  // project root keeps older deployments usable where dirName needed cwd=workspace.
  const runStatus = (projectArg) =>
    runScript('bg_state.py', ['status', '--workspace', workspaceRoot, '--project', projectArg, '--json'], {
      timeoutMs: 60_000,
    });
  const readStatusJson = (result) =>
    scriptFailure(result) === null && result.json !== null && typeof result.json === 'object' ? result.json : null;
  let statusResult = await runStatus(found.dirName);
  let statusJson = readStatusJson(statusResult);
  if (statusJson === null) {
    const absoluteResult = await runStatus(found.projectRoot);
    const absoluteJson = readStatusJson(absoluteResult);
    if (absoluteJson !== null) {
      statusResult = absoluteResult;
      statusJson = absoluteJson;
    }
  }
  const summary = statusJson ?? {};
  const source = statusJson === null ? 'state.json' : 'bg_state.py';
  const locks =
    summary.locks !== null && typeof summary.locks === 'object'
      ? summary.locks
      : state?.locks !== null && typeof state?.locks === 'object'
        ? state.locks
        : {};
  return {
    ok: true,
    projectId:
      (typeof summary.projectId === 'string' && summary.projectId) ||
      (typeof state?.projectId === 'string' && state.projectId) ||
      (typeof found.entry?.projectId === 'string' && found.entry.projectId) ||
      (typeof found.project?.projectId === 'string' && found.project.projectId) ||
      found.dirName,
    dirName: found.dirName,
    projectName:
      (typeof summary.projectName === 'string' && summary.projectName) ||
      (typeof found.project?.projectName === 'string' && found.project.projectName) ||
      (typeof found.entry?.projectName === 'string' && found.entry.projectName) ||
      found.dirName,
    projectRoot: found.projectRoot,
    bidderRef: summary.bidderRef ?? found.project?.bidderRef ?? found.entry?.bidderRef ?? null,
    currentStage: (typeof summary.currentStage === 'string' && summary.currentStage) || (typeof state?.currentStage === 'string' ? state.currentStage : null),
    currentScope: (typeof summary.currentScope === 'string' && summary.currentScope) || (typeof state?.currentScope === 'string' ? state.currentScope : null),
    currentPackage:
      (typeof summary.currentPackage === 'string' && summary.currentPackage) ||
      (typeof state?.currentPackage === 'string' ? state.currentPackage : null),
    completedStages: Array.isArray(summary.completedStages)
      ? summary.completedStages
      : Array.isArray(state?.completedStages)
        ? state.completedStages
        : [],
    allowedNext: Array.isArray(summary.allowedNext) ? summary.allowedNext : null,
    pendingInput: summary.pendingInput ?? state?.pendingInput ?? null,
    abort: summary.abort ?? state?.abort ?? null,
    reviewLoop: summary.reviewLoop ?? state?.reviewLoop ?? null,
    envCheckSummary: summary.envCheckSummary ?? state?.envCheckSummary ?? null,
    locked: locks.draftLocked === true,
    revision: Number.isFinite(summary.revision) ? summary.revision : Number.isFinite(state?.revision) ? state.revision : null,
    historyCount: Number.isFinite(summary.historyCount) ? summary.historyCount : Array.isArray(state?.history) ? state.history.length : null,
    updatedAt:
      (typeof summary.updatedAt === 'string' && summary.updatedAt) ||
      (typeof state?.updatedAt === 'string' && state.updatedAt) ||
      (typeof found.project?.updatedAt === 'string' && found.project.updatedAt) ||
      null,
    updatedBy: summary.updatedBy ?? state?.updatedBy ?? null,
    statusSource: source,
    state,
  };
}

/**
 * `POST /bidgenie/projects/init` - create project.json/state.json/registry entry
 * through `bg_workspace.py init-project` (CONTRACT §8.3; directory creation is
 * script-only, never hand-written by the Host).
 */
export async function projectsInit(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const args = ['init-project', '--workspace', workspaceRoot];
  const bidderId = typeof payload?.bidderId === 'string' && payload.bidderId.trim() !== '' ? payload.bidderId.trim() : null;
  if (bidderId !== null) args.push('--bidder', bidderId);
  const requestedName = typeof payload?.projectName === 'string' && payload.projectName.trim() !== '' ? payload.projectName.trim() : null;
  if (requestedName !== null) args.push('--project-name', requestedName);
  args.push('--json');
  const result = await runScript('bg_workspace.py', args, { timeoutMs: 60_000, env: PANEL_ACTOR_ENV });
  const failure = scriptFailure(result);
  if (failure !== null) {
    const subcommandMissing =
      failure.code === 'E_USAGE' && /invalid choice|unrecognized arguments/i.test(`${result.stdout ?? ''}\n${result.stderr ?? ''}`);
    if (failure.code === 'E_DEP_MISSING' || subcommandMissing) {
      throw bgError(
        'E_DEP_MISSING',
        '项目初始化脚本尚未就绪（bg_workspace.py init-project 不可用）',
        '等待 M2 后端脚本就绪后重试；Host 不会手工创建 project.json/state.json',
      );
    }
    throw bgError(failure.code, failure.message, failure.hint);
  }
  const json = result.json !== null && typeof result.json === 'object' ? result.json : {};
  const rawDir =
    typeof json.projectRoot === 'string' && json.projectRoot !== ''
      ? json.projectRoot
      : typeof json.dirName === 'string'
        ? json.dirName
        : typeof json.projectDir === 'string'
          ? json.projectDir
          : null;
  if (rawDir === null || rawDir.trim() === '') {
    throw bgError('E_INTERNAL', '项目初始化脚本未返回项目目录', '请检查 bg_workspace.py init-project 的输出');
  }
  const projectRoot = isAbsolute(rawDir) ? resolve(rawDir) : safeJoin(workspaceRoot, rawDir);
  const project = (await readJsonFile(join(projectRoot, 'project.json'))) ?? {};
  const state = (await readJsonFile(join(projectRoot, 'state.json'))) ?? {};
  const scriptProject = json.project !== null && typeof json.project === 'object' ? json.project : {};
  const scriptState = json.state !== null && typeof json.state === 'object' ? json.state : {};
  return {
    ok: true,
    projectId:
      (typeof json.projectId === 'string' && json.projectId) ||
      (typeof scriptState.projectId === 'string' && scriptState.projectId) ||
      (typeof state.projectId === 'string' && state.projectId) ||
      (typeof project.projectId === 'string' && project.projectId) ||
      null,
    dirName:
      typeof json.dirName === 'string' && json.dirName !== ''
        ? json.dirName
        : isAbsolute(rawDir)
          ? basename(projectRoot)
          : rawDir,
    projectRoot,
    projectName:
      (typeof json.projectName === 'string' && json.projectName) ||
      (typeof scriptProject.projectName === 'string' && scriptProject.projectName) ||
      (typeof project.projectName === 'string' && project.projectName) ||
      requestedName,
    currentStage:
      (typeof scriptState.currentStage === 'string' && scriptState.currentStage) ||
      (typeof state.currentStage === 'string' && state.currentStage) ||
      'project_init',
    alreadyExists: json.alreadyExists === true || json.reused === true,
    warnings: Array.isArray(json.warnings) ? json.warnings : [],
    source: 'panel',
    actor: 'user',
    registryPath: toWorkspaceAbsolute(
      workspaceRoot,
      typeof json.registryPath === 'string' && json.registryPath !== '' ? json.registryPath : 'dsh/registry.json',
    ),
  };
}

/* ----------------------------------------------------------------------------- */
/* Environment check (M2, CONTRACT §8.2)                                        */
/* ----------------------------------------------------------------------------- */

const CHECKLIST_FILE = join(SCRIPTS_DIR, 'checklist.json');
const ENV_CHECK_RELATIVE = 'dsh/config/env-check.json';

async function readChecklistBaseline() {
  const baseline = await readJsonFile(CHECKLIST_FILE);
  if (baseline === null || typeof baseline !== 'object' || Array.isArray(baseline)) return null;
  return {
    checklistVersion:
      typeof baseline.checklistVersion === 'string' || typeof baseline.checklistVersion === 'number'
        ? baseline.checklistVersion
        : typeof baseline.version === 'string' || typeof baseline.version === 'number'
          ? baseline.version
          : null,
    items: Array.isArray(baseline.items) ? baseline.items : [],
  };
}

function checklistRequiredTotal(baseline) {
  const items = baseline?.items;
  if (!Array.isArray(items) || items.length === 0) return REQUIRED_CHECKS;
  const required = items.filter((item) => item !== null && typeof item === 'object' && item.required === true).length;
  return required > 0 ? required : REQUIRED_CHECKS;
}

/** CONTRACT §8.2 item shape: `{id,label,required,status,evidence,remediation,degraded}`. */
function normalizeEnvItem(baseItem, record) {
  const base = baseItem !== null && typeof baseItem === 'object' ? baseItem : {};
  const source = record !== null && typeof record === 'object' ? record : base;
  const id = typeof base.id === 'string' && base.id !== '' ? base.id : typeof source.id === 'string' ? source.id : '';
  const rawStatus = typeof source.status === 'string' && source.status !== '' ? source.status : 'not-run';
  return {
    id,
    label: typeof source.label === 'string' && source.label !== '' ? source.label : typeof base.label === 'string' ? base.label : id,
    required: typeof source.required === 'boolean' ? source.required : base.required === true,
    status: rawStatus,
    evidence: source.evidence ?? null,
    remediation:
      typeof source.remediation === 'string' && source.remediation !== ''
        ? source.remediation
        : typeof base.remediation === 'string'
          ? base.remediation
          : null,
    degraded: source.degraded === true || rawStatus === 'degraded',
  };
}

/**
 * Normalize both script report shapes into one route payload:
 *  - `{items:[{id,label,required,status,...}]}` (script-native report), or
 *  - `{items:{<id>:{status,evidence,...}}}` (`env-check.json` written shape),
 * merged with the packaged `libexec/checklist.json` baseline for labels/remediation.
 *
 * `deferred` / `requiredDeferred` / `obsoleteItems` are passed through (CONTRACT
 * §8.2 + M2 integration): deferred ids keep their item `status` (`skipped`) while
 * still counting as required; `requiredDeferred` is computed from the checklist
 * when the script does not report it directly.
 */
function normalizeEnvReport(source, baseline, extras = {}) {
  const json = source !== null && typeof source === 'object' && !Array.isArray(source) ? source : {};
  const report = json.report !== null && typeof json.report === 'object' ? json.report : json;
  const baselineItems = Array.isArray(baseline?.items) ? baseline.items : [];
  const baselineById = new Map();
  for (const item of baselineItems) {
    if (item !== null && typeof item === 'object' && typeof item.id === 'string') baselineById.set(item.id, item);
  }

  const items = [];
  const rawItems = report.items;
  if (Array.isArray(rawItems)) {
    for (const raw of rawItems) {
      if (raw === null || typeof raw !== 'object') continue;
      const base = typeof raw.id === 'string' ? baselineById.get(raw.id) : undefined;
      const item = normalizeEnvItem(base ?? raw, raw);
      if (item.id !== '') items.push(item);
    }
  } else if (rawItems !== null && typeof rawItems === 'object') {
    const knownOrder = Array.isArray(baselineItems) ? baselineItems.map((item) => item?.id).filter((id) => typeof id === 'string') : [];
    const ids = [...knownOrder, ...Object.keys(rawItems).filter((id) => !knownOrder.includes(id))];
    for (const id of ids) {
      const record = rawItems[id];
      const base = baselineById.get(id);
      if (record === null || typeof record !== 'object') continue;
      const item = normalizeEnvItem(base ?? { id }, record);
      if (item.id !== '') items.push(item);
    }
  } else {
    for (const base of baselineItems) {
      if (base !== null && typeof base === 'object' && typeof base.id === 'string') {
        items.push(normalizeEnvItem(base, null));
      }
    }
  }

  // deferred: script top-level array wins; fall back to per-item `deferred:true`.
  const deferred = Array.isArray(report.deferred)
    ? report.deferred.filter((id) => typeof id === 'string' && id !== '')
    : Array.isArray(report.deferredItems)
      ? report.deferredItems.filter((id) => typeof id === 'string' && id !== '')
      : Array.isArray(rawItems)
        ? rawItems.filter((item) => item !== null && typeof item === 'object' && item.deferred === true && typeof item.id === 'string').map((item) => item.id)
        : [];
  const requiredById = new Map(items.map((item) => [item.id, item.required === true]));
  const requiredDeferred = Number.isFinite(report.requiredDeferred)
    ? report.requiredDeferred
    : deferred.filter((id) => requiredById.get(id) === true).length;
  const obsoleteItems = Array.isArray(report.obsoleteItems)
    ? report.obsoleteItems
    : Array.isArray(extras.obsoleteItems)
      ? extras.obsoleteItems
      : [];

  const requiredTotal = Number.isFinite(report.requiredTotal) ? report.requiredTotal : checklistRequiredTotal(baseline);
  const requiredPassed = Number.isFinite(report.requiredPassed)
    ? report.requiredPassed
    : items.filter((item) => item.required && item.status === 'passed').length;
  const hasResult = items.some((item) => item.status !== 'not-run');
  const status = typeof report.status === 'string' && report.status !== '' ? report.status : hasResult ? 'ready' : 'not-run';
  const degraded =
    report.degraded === true ||
    (Array.isArray(report.degraded) && report.degraded.length > 0) ||
    items.some((item) => item.degraded === true);
  return {
    ok: true,
    status,
    checklistVersion: report.checklistVersion ?? baseline?.checklistVersion ?? null,
    checkedAt: typeof report.checkedAt === 'string' && report.checkedAt !== '' ? report.checkedAt : null,
    items,
    requiredPassed,
    requiredTotal,
    degraded,
    deferred,
    requiredDeferred,
    obsoleteItems,
  };
}

async function readLocalEnvCheck(workspaceRoot) {
  return readJsonFile(safeJoin(workspaceRoot, ...ENV_CHECK_RELATIVE.split('/')));
}

/* ----------------------------------------------------------------------------- */
/* Active acceptance run (run-manifest start not yet finalized)                  */
/* ----------------------------------------------------------------------------- */

const RUN_MANIFESTS_RELATIVE = 'dsh/run-manifests';

function runManifestsDir(workspaceRoot) {
  return safeJoin(workspaceRoot, ...RUN_MANIFESTS_RELATIVE.split('/'));
}

/**
 * 找出当前未 finalize 的验收 run（`RUN-*.start.json` 且同 runId 的完整
 * manifest 尚不存在）。面板刷新 env-check 时据此提示/记录
 * `envRefreshedAfterStart`，避免把「run 中途刷新环境快照」伪装成零漂移。
 */
async function readActiveRunManifest(workspaceRoot) {
  const dir = runManifestsDir(workspaceRoot);
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch {
    return null;
  }
  const candidates = [];
  for (const entry of entries) {
    if (!entry.isFile() || !/^RUN-.*\.start\.json$/.test(entry.name)) continue;
    const startPath = join(dir, entry.name);
    const runId = entry.name.slice(0, -'.start.json'.length);
    const [startStat, finalStat] = await Promise.all([
      stat(startPath).catch(() => null),
      stat(join(dir, `${runId}.json`)).catch(() => null),
    ]);
    if (startStat === null || finalStat !== null) continue;
    candidates.push({ runId, startPath, mtimeMs: startStat.mtimeMs });
  }
  if (candidates.length === 0) return null;
  candidates.sort((left, right) => right.mtimeMs - left.mtimeMs);
  const active = candidates[0];
  let document = null;
  try {
    const parsed = JSON.parse(await readFile(active.startPath, 'utf8'));
    document = parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch {
    document = null;
  }
  return {
    runId: active.runId,
    startPath: active.startPath,
    startedAt: typeof document?.startedAt === 'string' ? document.startedAt : null,
    envCheckedAt: typeof document?.envCheckedAt === 'string' ? document.envCheckedAt : null,
    envRefreshedAfterStart: document?.envRefreshedAfterStart === true,
    document,
  };
}

/** 刷新 env-check 成功后，把 `envRefreshedAfterStart` 写回 active run 的 start 记录。 */
async function markEnvRefreshedAfterStart(activeRun, report) {
  const document =
    activeRun.document !== null && typeof activeRun.document === 'object' && !Array.isArray(activeRun.document)
      ? { ...activeRun.document }
      : {};
  document.envRefreshedAfterStart = true;
  document.envRefreshedAt = formatIsoWithOffset();
  document.envRefreshedCheckedAt =
    typeof report?.checkedAt === 'string' && report.checkedAt !== '' ? report.checkedAt : null;
  await writeFile(activeRun.startPath, `${JSON.stringify(document, null, 2)}\n`, 'utf8');
  return {
    runId: activeRun.runId,
    startPath: activeRun.startPath,
    envRefreshedAt: document.envRefreshedAt,
    envRefreshedCheckedAt: document.envRefreshedCheckedAt,
  };
}

function activeRunView(activeRun, extra = {}) {
  if (activeRun === null) return null;
  return {
    runId: activeRun.runId,
    startedAt: activeRun.startedAt,
    envCheckedAt: activeRun.envCheckedAt,
    envRefreshedAfterStart: activeRun.envRefreshedAfterStart === true,
    ...extra,
  };
}

/**
 * `GET /bidgenie/env-check` - CONTRACT §8.2.
 * Prefers `bg_env_check.py --report`; when the script/report is unavailable it
 * degrades to the file-backed view (or `status:'not-run'`), never fails the panel.
 */
export async function envCheckGet(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const baseline = await readChecklistBaseline();
  const result = await runScript('bg_env_check.py', ['--report', '--workspace', workspaceRoot, '--json'], {
    timeoutMs: 60_000,
    env: { BIDGENIE_HOST_VERSION: HOST_VERSION },
  });
  const failure = scriptFailure(result);
  if (failure === null) {
    const local = await readLocalEnvCheck(workspaceRoot);
    const report = normalizeEnvReport(result.json, baseline, {
      obsoleteItems: local?.obsoleteItems,
    });
    const activeRun = await readActiveRunManifest(workspaceRoot);
    return {
      ...report,
      activeRun: activeRunView(activeRun),
      envRefreshedAfterStart: activeRun?.envRefreshedAfterStart === true,
      warnings:
        activeRun === null
          ? []
          : [
              `当前存在未 finalize 的验收 run（${activeRun.runId}）：刷新 env-check 会记录 envRefreshedAfterStart，并可能在 --finalize 时出现 envHash 漂移，请勿视为零漂移。`,
            ],
    };
  }
  // E_DEP_MISSING (script not shipped yet) / E_NOT_FOUND (no env-check.json)
  // are expected before the first run; other failures keep the panel usable too.
  const local = await readLocalEnvCheck(workspaceRoot);
  const report = normalizeEnvReport(local ?? {}, baseline);
  const activeRun = await readActiveRunManifest(workspaceRoot);
  return {
    ...report,
    activeRun: activeRunView(activeRun),
    envRefreshedAfterStart: activeRun?.envRefreshedAfterStart === true,
    warnings:
      activeRun === null
        ? []
        : [
            `当前存在未 finalize 的验收 run（${activeRun.runId}）：刷新 env-check 会记录 envRefreshedAfterStart，请勿视为零漂移。`,
          ],
  };
}

/**
 * `POST /bidgenie/env-check/run` - CONTRACT §8.2.
 * Runs `bg_env_check.py --apply` with the 10-minute ceiling; never installs
 * dependencies, returns the same report shape plus `checkedAt`.
 */
export async function envCheckRun(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const activeRunBefore = await readActiveRunManifest(workspaceRoot);
  const itemIds = Array.isArray(payload?.itemIds)
    ? payload.itemIds.filter((id) => typeof id === 'string' && id.trim() !== '').map((id) => id.trim())
    : [];
  const args = ['--apply', '--workspace', workspaceRoot];
  for (const itemId of itemIds) args.push('--item', itemId);
  args.push('--json');
  const result = await runScript('bg_env_check.py', args, {
    timeoutMs: ENV_CHECK_TIMEOUT_MS,
    env: { BIDGENIE_HOST_VERSION: HOST_VERSION },
  });
  const failure = scriptFailure(result);
  if (failure !== null) {
    if (failure.code === 'E_DEP_MISSING') {
      throw bgError(
        'E_DEP_MISSING',
        '环境检查脚本尚未就绪（bg_env_check.py 不可用）',
        '等待 M2 后端脚本就绪后重试；Host 不会自动安装依赖或伪造检查结果',
      );
    }
    throw bgError(failure.code, failure.message, failure.hint);
  }
  const baseline = await readChecklistBaseline();
  const local = await readLocalEnvCheck(workspaceRoot);
  const report = normalizeEnvReport(result.json, baseline, {
    obsoleteItems: local?.obsoleteItems,
  });
  const normalized = {
    ...report,
    status: report.status === 'not-run' ? 'ready' : report.status,
    checkedAt: report.checkedAt ?? formatIsoWithOffset(),
  };
  // 有未 finalize 的 run 时：刷新环境快照要如实记录，不伪装零漂移。
  if (activeRunBefore !== null) {
    const marked = await markEnvRefreshedAfterStart(activeRunBefore, normalized);
    return {
      ...normalized,
      activeRun: activeRunView(activeRunBefore, {
        envRefreshedAfterStart: true,
        envRefreshedAt: marked.envRefreshedAt,
        envRefreshedCheckedAt: marked.envRefreshedCheckedAt,
      }),
      envRefreshedAfterStart: true,
      warnings: [
        `当前存在未 finalize 的验收 run（${activeRunBefore.runId}）：环境快照已刷新，已记录 envRefreshedAfterStart；后续 --finalize 会按 envHash/环境快照时间给出漂移结论，请勿视为零漂移。`,
      ],
    };
  }
  return {
    ...normalized,
    activeRun: null,
    envRefreshedAfterStart: false,
    warnings: [],
  };
}

/* ----------------------------------------------------------------------------- */
/* Env-check repair (task-160: plan / apply / last)                              */
/* ----------------------------------------------------------------------------- */

function redactRepairValue(value, depth = 0) {
  if (depth > 8) return null;
  if (Array.isArray(value)) return value.slice(0, 200).map((item) => redactRepairValue(item, depth + 1));
  if (value !== null && typeof value === 'object') {
    const out = {};
    for (const [key, inner] of Object.entries(value)) {
      if (/token|secret|password|authorization|cookie|api[-_]?key/i.test(key)) out[key] = '***';
      else out[key] = redactRepairValue(inner, depth + 1);
    }
    return out;
  }
  if (typeof value === 'string') {
    // 去掉 URL 中可能带出的 user:pass@ 凭据
    return value.replace(/(\/\/)[^/@\s]+@/g, '$1***@');
  }
  return value;
}

function arrayOfStrings(value) {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => typeof item === 'string' && item.trim() !== '').map((item) => item.trim());
}

function uniqueStrings(values) {
  return [...new Set(values.filter((item) => typeof item === 'string' && item.trim() !== ''))];
}

function normalizeRepairAction(raw) {
  if (typeof raw === 'string' && raw.trim() !== '') return { actionId: raw.trim(), items: [], label: raw.trim() };
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const actionId = [raw.actionId, raw.id, raw.action, raw.name].find((item) => typeof item === 'string' && item.trim() !== '');
  const items = uniqueStrings([
    ...arrayOfStrings(raw.items),
    ...arrayOfStrings(raw.itemIds),
    ...arrayOfStrings(raw.targets),
  ]);
  return { ...raw, actionId: actionId === undefined ? null : actionId.trim(), items };
}

/** 把 bg_env_repair --plan 输出归一为固定 groups/autoActionIds（兼容 groups 与 actions[] 两种形状）。 */
function normalizeRepairPlan(value) {
  const raw = value !== null && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const groups = { auto: [], assisted: [], manual: [], pluginDefect: [] };
  const rawGroups = raw.groups !== null && typeof raw.groups === 'object' ? raw.groups : {};
  let usedGroups = false;
  for (const key of Object.keys(groups)) {
    const list = Array.isArray(rawGroups[key]) ? rawGroups[key] : Array.isArray(raw[key]) ? raw[key] : null;
    if (list === null) continue;
    usedGroups = true;
    groups[key] = list.map((item) => normalizeRepairAction(item)).filter((item) => item !== null);
  }
  if (!usedGroups && Array.isArray(raw.actions)) {
    for (const action of raw.actions) {
      const normalized = normalizeRepairAction(action);
      if (normalized === null) continue;
      const groupName =
        typeof action?.group === 'string' && Object.hasOwn(groups, action.group)
          ? action.group
          : action?.auto === true || action?.remediable === 'auto'
            ? 'auto'
            : action?.remediable === 'assisted'
              ? 'assisted'
              : action?.remediable === 'plugin_defect'
                ? 'pluginDefect'
                : 'manual';
      groups[groupName].push(normalized);
    }
  }
  const autoActionIds = uniqueStrings(groups.auto.map((action) => action.actionId).filter(Boolean));
  const envBefore = raw.envBefore ?? raw.before ?? raw.summary ?? null;
  const failedItems = uniqueStrings([
    ...arrayOfStrings(raw.failedItems),
    ...arrayOfStrings(raw.failed),
    ...arrayOfStrings(raw.failedItemIds),
  ]);
  const counts =
    raw.counts !== null && typeof raw.counts === 'object' && !Array.isArray(raw.counts)
      ? redactRepairValue(raw.counts)
      : { auto: groups.auto.length, assisted: groups.assisted.length, manual: groups.manual.length, pluginDefect: groups.pluginDefect.length };
  const safeGroups = {};
  for (const key of Object.keys(groups)) {
    safeGroups[key] = groups[key].map((action) => redactRepairValue(action));
  }
  return {
    groups: safeGroups,
    autoActionIds,
    failedItems,
    counts,
    status: typeof raw.status === 'string' ? raw.status : null,
    rootCause: typeof raw.rootCause === 'string' ? raw.rootCause : null,
    installHint: typeof raw.installHint === 'string' ? raw.installHint : null,
    target: typeof raw.target === 'string' ? raw.target : null,
    envBefore:
      envBefore !== null && envBefore !== undefined
        ? redactRepairValue(envBefore)
        : redactRepairValue({ failedItems, counts, status: typeof raw.status === 'string' ? raw.status : null }),
    generatedAt: typeof raw.generatedAt === 'string' ? raw.generatedAt : null,
  };
}

function envRepairArgs(mode, workspaceRoot, items = []) {
  const args = [`--${mode}`, '--workspace', workspaceRoot];
  // bg_env_repair.py 实际参数是 --item（可重复），不是 --items
  for (const item of items) args.push('--item', item);
  if (mode === 'apply') args.push('--yes');
  args.push('--json');
  return args;
}

function envRepairTimeoutFor(mode) {
  if (mode === 'apply') return ENV_REPAIR_APPLY_TIMEOUT_MS;
  if (mode === 'verify') return ENV_REPAIR_VERIFY_TIMEOUT_MS;
  return ENV_REPAIR_PLAN_TIMEOUT_MS;
}

function mapEnvRepairFailure(failure, phase) {
  if (failure === null) return;
  if (failure.code === 'E_DEP_MISSING') {
    throw bgError(
      'E_DEP_MISSING',
      '环境修复脚本尚未就绪（bg_env_repair.py 不可用）',
      '等待插件后端脚本就绪或重新安装插件；Host 不会伪造修复结果',
    );
  }
  if (failure.code === 'E_TIMEOUT') {
    throw bgError(
      'E_TIMEOUT',
      `环境修复${phase}超时`,
      phase === 'apply' ? '修复可能仍在后台执行，请稍后查看 dsh/logs/env-repair-*.jsonl' : '可稍后重试；plan/verify 超时上限 30 秒',
    );
  }
  if (failure.code === 'E_LOCK_TIMEOUT' || failure.code === 'E_BUSY') {
    throw bgError('E_BUSY', '另一个环境修复任务正在运行', '请等待当前任务结束后重试，或查看最近一次修复结果');
  }
  if (['E_REPAIR_FAILED', 'E_VALIDATE_FAILED', 'E_USER_DECISION_REQUIRED', 'E_USAGE', 'E_SCHEMA_INVALID'].includes(failure.code)) {
    throw bgError(failure.code, failure.message, failure.hint);
  }
  throw bgError('E_REPAIR_FAILED', failure.message || `环境修复${phase}失败`, failure.hint || '查看 dsh/logs/env-repair-*.jsonl 获取详情');
}

async function runEnvRepairScript(mode, workspaceRoot, items = []) {
  const result = await runScript('bg_env_repair.py', envRepairArgs(mode, workspaceRoot, items), {
    timeoutMs: envRepairTimeoutFor(mode),
    env: { BIDGENIE_HOST_VERSION: HOST_VERSION },
  });
  mapEnvRepairFailure(scriptFailure(result), mode === 'apply' ? '执行' : mode === 'verify' ? '校验' : '计划生成');
  return result;
}

function acquireEnvRepairLock(workspaceRoot) {
  if (ENV_REPAIR_LOCKS.has(workspaceRoot)) {
    throw bgError('E_BUSY', '环境修复正在执行', '请等待当前修复结束（最长 20 分钟），或查看最近一次修复结果');
  }
  ENV_REPAIR_LOCKS.set(workspaceRoot, { startedAt: formatIsoWithOffset() });
}

function releaseEnvRepairLock(workspaceRoot) {
  ENV_REPAIR_LOCKS.delete(workspaceRoot);
}

async function readLatestEnvRepairLog(workspaceRoot) {
  const directory = safeJoin(workspaceRoot, 'dsh', 'logs');
  let entries;
  try {
    entries = await readdir(directory, { withFileTypes: true });
  } catch {
    return null;
  }
  const candidates = [];
  for (const entry of entries) {
    if (!entry.isFile() || !/^env-repair-.*\.(jsonl|log)$/i.test(entry.name)) continue;
    try {
      const fileStat = await stat(join(directory, entry.name));
      candidates.push({ name: entry.name, mtimeMs: fileStat.mtimeMs });
    } catch {
      /* 忽略无法读取的日志 */
    }
  }
  if (candidates.length === 0) return null;
  candidates.sort((left, right) => right.mtimeMs - left.mtimeMs);
  const latest = candidates[0];
  let logTail = [];
  try {
    const text = await readFile(join(directory, latest.name), 'utf8');
    logTail = text
      .split(/\r?\n/)
      .filter((line) => line.trim() !== '')
      .slice(-40)
      .map((line) => redactRepairValue(line));
  } catch {
    /* 日志读取失败不影响接口 */
  }
  return {
    logPath: `dsh/logs/${latest.name}`,
    updatedAt: formatIsoWithOffset(new Date(latest.mtimeMs)),
    logTail,
  };
}

/** 根据 plan 的 auto 组做二次白名单校验；只允许自动动作，其余一律拒绝。 */
function validateRepairSelection(plan, payload) {
  const requestedActionIds = arrayOfStrings(payload?.actionIds);
  const requestedItems = arrayOfStrings(payload?.items);
  if (requestedActionIds.length === 0 && requestedItems.length === 0) {
    throw bgError('E_SCHEMA_INVALID', '未选择要修复的动作/项', '请从 plan 的 auto 组中选择 actionIds 或 items');
  }
  const autoActions = plan.groups.auto.filter((action) => action.actionId !== null);
  const autoById = new Map(autoActions.map((action) => [action.actionId, action]));
  const selected = [];
  for (const actionId of requestedActionIds) {
    const action = autoById.get(actionId);
    if (action === undefined) {
      throw bgError('E_SCHEMA_INVALID', `动作不可自动修复或不存在：${actionId}`, '服务端只允许 plan.auto 组中的动作');
    }
    if (!selected.includes(action)) selected.push(action);
  }
  for (const item of requestedItems) {
    const action = plan.groups.auto.find((candidate) => candidate.items.includes(item));
    if (action === undefined) {
      throw bgError('E_SCHEMA_INVALID', `项不可自动修复或不存在：${item}`, '服务端只允许 plan.auto 组覆盖的项');
    }
    if (!selected.includes(action)) selected.push(action);
  }
  const items = uniqueStrings(
    selected.flatMap((action) => (action.items.length > 0 ? action.items : action.actionId ? [action.actionId] : [])),
  );
  if (items.length === 0) {
    throw bgError('E_SCHEMA_INVALID', '选中的自动动作没有可执行的 item', '请检查 plan.auto[].items');
  }
  return {
    actionIds: uniqueStrings(selected.map((action) => action.actionId).filter(Boolean)),
    items,
  };
}

function activeRunRepairPayload(activeRun, extra = {}) {
  return {
    activeRun: activeRunView(activeRun, extra),
    envRefreshedAfterStart: activeRun !== null,
    warnings:
      activeRun === null
        ? []
        : [
            `当前存在未 finalize 的验收 run（${activeRun.runId}）：执行环境修复会刷新环境快照并记录 envRefreshedAfterStart，后续 run-manifest --finalize 会给出漂移结论，请勿视为零漂移。`,
          ],
  };
}

/** `POST /bidgenie/env-check/repair/plan` - 只读计划（auto/assisted/manual/pluginDefect 分组）。 */
export async function envCheckRepairPlan(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const activeRun = await readActiveRunManifest(workspaceRoot);
  const result = await runEnvRepairScript('plan', workspaceRoot);
  const plan = normalizeRepairPlan(result.json);
  const log = await readLatestEnvRepairLog(workspaceRoot);
  return {
    ok: true,
    plan,
    groups: plan.groups,
    autoActionIds: plan.autoActionIds,
    failedItems: plan.failedItems,
    counts: plan.counts,
    status: plan.status,
    rootCause: plan.rootCause,
    installHint: plan.installHint,
    target: plan.target,
    envBefore: plan.envBefore,
    generatedAt: plan.generatedAt,
    ...activeRunRepairPayload(activeRun),
    logPath: log?.logPath ?? null,
    logTail: log?.logTail ?? [],
    exitCode: typeof result.exitCode === 'number' ? result.exitCode : 0,
  };
}

/** `POST /bidgenie/env-check/repair` - 执行 auto 组修复（需 yes:true；20 分钟超时；Host 写锁）。 */
export async function envCheckRepair(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  if (payload?.yes !== true) {
    throw bgError('E_USER_DECISION_REQUIRED', '执行环境修复需要用户确认', '请在请求体传 yes: true（面板确认弹窗）后再执行');
  }
  acquireEnvRepairLock(workspaceRoot);
  try {
    const planResult = await runEnvRepairScript('plan', workspaceRoot);
    const plan = normalizeRepairPlan(planResult.json);
    const selection = validateRepairSelection(plan, payload);
    const activeRun = await readActiveRunManifest(workspaceRoot);
    const result = await runEnvRepairScript('apply', workspaceRoot, selection.items);
    const log = await readLatestEnvRepairLog(workspaceRoot);
    let marker = null;
    if (activeRun !== null) {
      marker = await markEnvRefreshedAfterStart(activeRun, {
        checkedAt: formatIsoWithOffset(),
      }).catch(() => null);
    }
    return {
      ok: true,
      actionIds: selection.actionIds,
      items: selection.items,
      report: redactRepairValue(result.json),
      logPath: (typeof result.json?.logPath === 'string' && result.json.logPath !== '' ? result.json.logPath : log?.logPath) ?? null,
      logTail: log?.logTail ?? [],
      exitCode: typeof result.exitCode === 'number' ? result.exitCode : 0,
      ...activeRunRepairPayload(
        activeRun,
        marker === null
          ? { envRefreshedAfterStart: true }
          : {
              envRefreshedAfterStart: true,
              envRefreshedAt: marker.envRefreshedAt,
              envRefreshedCheckedAt: marker.envRefreshedCheckedAt,
            },
      ),
    };
  } finally {
    releaseEnvRepairLock(workspaceRoot);
  }
}

/** `GET /bidgenie/env-check/repair/last` - 最近一次修复后的校验结果 + 最近日志尾部。 */
export async function envCheckRepairLast(ctx, payload) {
  const { workspaceRoot } = await resolveWorkspace(ctx, payload?.workspace);
  const activeRun = await readActiveRunManifest(workspaceRoot);
  const log = await readLatestEnvRepairLog(workspaceRoot);
  let result;
  try {
    result = await runEnvRepairScript('verify', workspaceRoot);
  } catch (error) {
    const noLog =
      isBgError(error) &&
      (error.code === 'E_NOT_FOUND' ||
        (error.code === 'E_REPAIR_FAILED' && /没有 env-repair 日志|先 --apply/i.test(String(error.message ?? ''))));
    if (!noLog) throw error;
    return {
      ok: true,
      verification: null,
      report: null,
      status: 'no-log',
      logPath: log?.logPath ?? null,
      logTail: log?.logTail ?? [],
      exitCode: 0,
      ...activeRunRepairPayload(activeRun),
    };
  }
  return {
    ok: true,
    verification: redactRepairValue(result.json),
    report: redactRepairValue(result.json?.report ?? result.json?.verification ?? null),
    status: typeof result.json?.status === 'string' ? result.json.status : null,
    logPath:
      (typeof result.json?.jobLog === 'string' && result.json.jobLog !== '' ? result.json.jobLog : log?.logPath) ?? null,
    logTail: log?.logTail ?? [],
    exitCode: typeof result.exitCode === 'number' ? result.exitCode : 0,
    ...activeRunRepairPayload(activeRun),
  };
}
