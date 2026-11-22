/**
 * BidGenie（标代精灵）DSH 插件 - Host 入口（M2）。
 *
 * 职责（CONTRACT §1/§3/§6.6/§8.1）：
 *  - 注册模型工具 `bidgenie_paths` / `bidgenie_bidder`（ctx.get('tools')）；
 *  - 注册面板同源 HTTP 路由 `/bidgenie/*`（ctx.inject(['webServer'])）；
 *  - 扫描 `skills/<name>/SKILL.md` 并在运行时注册技能（M2 只注册 3 个：
 *    `bidgenie` / `bg-bidder` / `bg-init`）；frontmatter 只解析 name/description，
 *    正文注入插件根目录的绝对路径（M0 SP-1a/1c 已证）。
 *
 * 业务逻辑不写在这里：一个操作只实现一次（plugin/lib/services.js）。
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { PLUGIN_ROOT, SCRIPTS_DIR } from './lib/python.js';
import { HOST_VERSION, PLUGIN_VERSION } from './lib/services.js';
import { registerRoutes } from './lib/routes.js';
import { registerTools } from './lib/tools.js';

export const name = 'dsh-bidgenie';

/** The tool registry is required: reading ctx.get('tools') without declaring it is rejected. */
export const inject = ['tools'];

const SKILLS_DIR = join(PLUGIN_ROOT, 'skills');
const TEMPLATES_DIR = join(PLUGIN_ROOT, 'templates');

/**
 * M2–M8 已注册技能白名单（CONTRACT §8.1 / §10.2 / M4–M8）：其余 `bg-*` 随里程碑补齐。
 * 扫描时忽略目录名不在白名单内的技能，避免半成品技能进入 catalog。
 */
export const REGISTERED_SKILL_NAMES = Object.freeze(['bidgenie', 'bg-bidder', 'bg-init', 'bg-extract', 'bg-prepare', 'bg-outline', 'bg-write', 'bg-figures', 'bg-writing-style', 'bg-review', 'bg-optimize', 'bg-export-style']);

/** Parse the tiny frontmatter subset the skill contract allows: `name` + `description`. */
function parseFrontmatter(text) {
  const normalized = text.replace(/^\uFEFF/, '').replace(/\r\n/g, '\n');
  if (!normalized.startsWith('---\n')) return { attributes: {}, body: normalized };
  const end = normalized.indexOf('\n---', 4);
  if (end < 0) return { attributes: {}, body: normalized };
  const rawFrontmatter = normalized.slice(4, end);
  const body = normalized.slice(end + 4).replace(/^\n/, '');
  const attributes = {};
  for (const line of rawFrontmatter.split('\n')) {
    const match = /^([A-Za-z0-9_-]+):\s*(.*)$/.exec(line);
    if (match === null) continue;
    let value = match[2].trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    attributes[match[1]] = value;
  }
  return { attributes, body };
}

/**
 * Inject absolute plugin-root paths into the skill body (CONTRACT §8.1):
 * token substitution + a Host-authored footer listing the real directories.
 * M0 (SP-1a/1c) proved registered content can carry absolute paths that `read`
 * resolves from inside the plugin package.
 */
function renderSkillContent(skillName, body) {
  const referencesDir = join(SKILLS_DIR, skillName, 'references');
  const footer = [
    '',
    '---',
    '',
    '## 运行时路径（由 Host 注入，绝对路径）',
    '',
    `- 插件根目录 pluginRoot：\`${PLUGIN_ROOT}\``,
    `- 本技能 references 目录：\`${referencesDir}\``,
    `- libexec 脚本目录 scriptsDir：\`${SCRIPTS_DIR}\``,
    `- 模板目录 templatesDir：\`${TEMPLATES_DIR}\``,
    '',
    '调用脚本时请使用 `bidgenie_paths` 工具返回的 `pythonExe`/`scriptsDir` 绝对路径，不要依赖 PATH 或当前目录。',
    '',
  ].join('\n');
  return body
    .replaceAll('{{PLUGIN_ROOT}}', PLUGIN_ROOT)
    .replaceAll('{{SKILL_REFS}}', referencesDir)
    .replaceAll('{{SCRIPTS}}', SCRIPTS_DIR)
    .replaceAll('{{TEMPLATES}}', TEMPLATES_DIR)
    .concat(footer);
}

/**
 * Scan `skills/<name>/SKILL.md`, keep the M2 whitelist, and return registration
 * inputs. Unreadable/invalid entries are skipped (never break plugin activation).
 */
export function loadSkillDefinitions() {
  if (!existsSync(SKILLS_DIR)) return [];
  const definitions = [];
  let entries;
  try {
    entries = readdirSync(SKILLS_DIR, { withFileTypes: true });
  } catch {
    return [];
  }
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const skillId = entry.name;
    if (!REGISTERED_SKILL_NAMES.includes(skillId)) continue;
    const skillFile = join(SKILLS_DIR, skillId, 'SKILL.md');
    if (!existsSync(skillFile)) continue;
    let text;
    try {
      text = readFileSync(skillFile, 'utf8');
    } catch {
      continue;
    }
    const { attributes, body } = parseFrontmatter(text);
    const skillName = typeof attributes.name === 'string' && attributes.name !== '' ? attributes.name : skillId;
    const description = typeof attributes.description === 'string' ? attributes.description.trim() : '';
    if (skillName !== skillId || description === '') continue;
    if (body.trim() === '') continue;
    definitions.push({
      name: skillName,
      description,
      content: renderSkillContent(skillName, body),
      skillFile,
    });
  }
  definitions.sort((left, right) => REGISTERED_SKILL_NAMES.indexOf(left.name) - REGISTERED_SKILL_NAMES.indexOf(right.name));
  return definitions;
}

/**
 * Register the packaged skills at runtime (M0-verified `ctx.get('skills').register`).
 * M2: only `bidgenie` / `bg-bidder` / `bg-init`.
 */
export function registerSkills(ctx) {
  ctx.effect(() => {
    const skills = ctx.get('skills');
    if (skills === undefined) return () => {};
    const disposers = [];
    for (const definition of loadSkillDefinitions()) {
      try {
        const dispose = skills.register({
          name: definition.name,
          description: definition.description,
          content: definition.content,
          source: 'runtime',
        });
        disposers.push(dispose);
      } catch (error) {
        // A duplicate/invalid skill must not take the whole Host plugin down.
        console.warn(`[bidgenie] skill registration skipped: ${definition.name}: ${String(error?.message ?? error)}`);
      }
    }
    return () => {
      for (const dispose of disposers.reverse()) {
        try {
          dispose();
        } catch {
          /* registry going away */
        }
      }
    };
  }, 'bidgenie: skills');
}

/** 把插件/宿主版本注入进程环境，供所有 Python 子进程继承（env-check host_version 用）。 */
export function injectVersionEnv() {
  if (typeof HOST_VERSION === 'string' && HOST_VERSION !== '') {
    process.env.BIDGENIE_HOST_VERSION = HOST_VERSION;
  }
  if (typeof PLUGIN_VERSION === 'string' && PLUGIN_VERSION !== '') {
    process.env.BIDGENIE_PLUGIN_VERSION = PLUGIN_VERSION;
  }
  return { hostVersion: process.env.BIDGENIE_HOST_VERSION ?? '', pluginVersion: process.env.BIDGENIE_PLUGIN_VERSION ?? '' };
}

/**
 * Plugin entry point.
 * @param {object} ctx Cordis context of the `dsh-bidgenie` bundle row.
 */
export function apply(ctx) {
  injectVersionEnv();
  registerTools(ctx);
  ctx.inject(['webServer'], (webCtx) => {
    registerRoutes(webCtx);
  });
  registerSkills(ctx);
}
