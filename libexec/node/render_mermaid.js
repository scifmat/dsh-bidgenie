#!/usr/bin/env node
/**
 * BidGenie Mermaid → PNG（纯 Node，无 npm 依赖）
 *
 * 用法：
 *   node render_mermaid.js <input.mmd> <output.png> [--browser auto|chrome|edge|none] [--lib <mermaid.min.js>] [--scale 2]
 *
 * 原理：系统 Chrome/Edge headless（`--headless=new --screenshot --dump-dom`）+
 * 把内置 `plugin/assets/mermaid/mermaid.min.js` 复制到临时目录并以外部脚本加载；
 * 等 mermaid.render 完成后按 viewBox 固有尺寸布置 SVG，截图并回传布局指标
 * （viewBox、最小字号、图类型、节点/标签数量）。
 *
 * 浏览器/库缺失或渲染失败时输出 JSON {ok:false,error,...} 并以非 0 退出，
 * 由 bg_render.py 降级 Pillow。
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** 截图窗口（CSS px）；scale 作用后实际截图会乘以该值。 */
const WINDOW_WIDTH = 2000;
const WINDOW_HEIGHT = 2400;
const PAGE_PADDING = 24;

let tempDir = null;

/** 删除本次渲染的临时目录（HTML/截图/浏览器 profile/库副本）。 */
function cleanupTempDir() {
  if (!tempDir) return;
  try {
    fs.rmSync(tempDir, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
  tempDir = null;
}

function fail(message, code = 2, detail = {}) {
  process.stdout.write(JSON.stringify({ ok: false, error: message, detail }, null, 2) + '\n');
  cleanupTempDir();
  process.exit(code);
}

const argv = process.argv.slice(2);
const positional = [];
let browserPref = process.env.BG_RENDER_BROWSER || 'auto';
let libPath = process.env.BG_MERMAID_LIB || path.resolve(__dirname, '..', '..', 'assets', 'mermaid', 'mermaid.min.js');
let scale = 2;
for (let i = 0; i < argv.length; i += 1) {
  const arg = argv[i];
  if (arg === '--browser') browserPref = String(argv[++i] || 'auto');
  else if (arg === '--lib') libPath = path.resolve(String(argv[++i] || libPath));
  else if (arg === '--scale') scale = Number(argv[++i] || 2);
  else if (arg.startsWith('--')) fail(`未知参数：${arg}`, 2);
  else positional.push(arg);
}
if (positional.length < 2) fail('用法：node render_mermaid.js <input.mmd> <output.png> [--browser auto|chrome|edge|none]', 2);
const inputPath = path.resolve(positional[0]);
const outputPath = path.resolve(positional[1]);
if (!fs.existsSync(inputPath)) fail(`缺少 mermaid 源文件：${inputPath}`, 3, { inputPath });
if (!fs.existsSync(libPath)) fail(`缺少内置 mermaid 库：${libPath}`, 3, { libPath });

const browsers = {
  chrome: [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
  ],
  edge: [
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/microsoft-edge',
  ],
};

function findBrowser() {
  if (browserPref === 'none') return null;
  if (browserPref === 'chrome' || browserPref === 'edge') {
    return (browsers[browserPref] || []).find((candidate) => fs.existsSync(candidate)) || null;
  }
  const envBrowser = process.env.BG_RENDER_BROWSER_PATH;
  if (envBrowser && fs.existsSync(envBrowser)) return envBrowser;
  for (const group of [browsers.chrome, browsers.edge]) {
    const found = group.find((candidate) => fs.existsSync(candidate));
    if (found) return found;
  }
  return null;
}

const browserPath = findBrowser();
if (!browserPath) fail('未找到可用 Chrome/Edge（或 --browser none）', 4, { browserPref, browsers });

const code = fs.readFileSync(inputPath, 'utf8');
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bidgenie-mermaid-'));
tempDir = tmpDir;
const htmlPath = path.join(tmpDir, 'render.html');
const screenshotPath = path.join(tmpDir, 'render.png');
const libCopyPath = path.join(tmpDir, 'mermaid.min.js');
fs.copyFileSync(libPath, libCopyPath);
// 每次渲染用独立的 user-data-dir：默认配置目录可能正被后台 Edge/Chrome 占用，
// 此时无头进程会把命令转发给已有实例后直接退出（exit=0）但不产生截图。
const profileDir = path.join(tmpDir, 'profile');
fs.mkdirSync(profileDir, { recursive: true });

const html = `<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  html,body{margin:0;padding:0;background:#ffffff;}
  #container{display:block;padding:${PAGE_PADDING}px;background:#ffffff;box-sizing:content-box;}
  #container svg{max-width:none;height:auto;display:block;}
  #metrics{display:none;}
</style></head>
<body><div id="container"></div><pre id="metrics"></pre>
<script src="mermaid.min.js"></script>
<script>
(async () => {
  const metricsEl = document.getElementById('metrics');
  try {
    mermaid.initialize({ startOnLoad: false, theme: 'neutral', securityLevel: 'loose', fontFamily: 'Microsoft YaHei, SimSun, Arial, sans-serif' });
    const result = await mermaid.render('bgGraph', ${JSON.stringify(code)});
    const container = document.getElementById('container');
    container.innerHTML = result.svg;
    const svg = container.querySelector('svg');
    const viewBox = svg.viewBox && svg.viewBox.baseVal ? svg.viewBox.baseVal : null;
    const intrinsicWidth = viewBox && viewBox.width ? viewBox.width : Math.max(1, svg.getBoundingClientRect().width);
    const intrinsicHeight = viewBox && viewBox.height ? viewBox.height : Math.max(1, svg.getBoundingClientRect().height);
    // 按 viewBox 固有尺寸排版；图大于截图窗口时等比缩到窗口，避免截图裁掉/压扁。
    const maxWidth = Math.max(400, window.innerWidth - ${PAGE_PADDING * 2});
    const maxHeight = Math.max(300, window.innerHeight - ${PAGE_PADDING * 2});
    const captureScale = Math.min(1, maxWidth / intrinsicWidth, maxHeight / intrinsicHeight);
    const renderWidth = Math.max(1, intrinsicWidth * captureScale);
    const renderHeight = Math.max(1, intrinsicHeight * captureScale);
    svg.setAttribute('width', renderWidth);
    svg.setAttribute('height', renderHeight);
    svg.style.width = renderWidth + 'px';
    svg.style.height = renderHeight + 'px';
    svg.style.maxWidth = 'none';
    const texts = [...svg.querySelectorAll('text, foreignObject span, foreignObject p')];
    const fontSizes = texts
      .map((node) => parseFloat(getComputedStyle(node).fontSize || '0'))
      .filter((value) => Number.isFinite(value) && value > 0);
    const labelChars = texts
      .map((node) => String(node.textContent || '').trim())
      .filter((value) => value.length > 0)
      .map((value) => value.length);
    const nodeSelectors = ['.node', '.task', '.actor', '.entityBox', '.mindmap-node', '.timeline-node', '.section', '.cluster'];
    const nodeSet = new Set();
    for (const selector of nodeSelectors) {
      for (const node of svg.querySelectorAll(selector)) nodeSet.add(node);
    }
    const metrics = {
      type: svg.getAttribute('aria-roledescription') || svg.getAttribute('class') || '',
      intrinsicWidth: Math.round(intrinsicWidth * 100) / 100,
      intrinsicHeight: Math.round(intrinsicHeight * 100) / 100,
      renderWidth: Math.round(renderWidth * 100) / 100,
      renderHeight: Math.round(renderHeight * 100) / 100,
      captureScale: Math.round(captureScale * 10000) / 10000,
      minFontPx: fontSizes.length ? Math.round(Math.min(...fontSizes) * 100) / 100 : null,
      maxFontPx: fontSizes.length ? Math.round(Math.max(...fontSizes) * 100) / 100 : null,
      labelCount: texts.length,
      nodeCount: nodeSet.size,
      maxLabelChars: labelChars.length ? Math.max(...labelChars) : 0,
    };
    metricsEl.textContent = JSON.stringify(metrics);
    document.title = 'ready';
  } catch (err) {
    metricsEl.textContent = JSON.stringify({ error: String(err) });
    document.title = 'error';
  }
})();
</script></body></html>`;
fs.writeFileSync(htmlPath, html, 'utf8');

const args = [
  '--headless=new',
  '--disable-gpu',
  '--no-sandbox',
  '--disable-dev-shm-usage',
  '--hide-scrollbars',
  `--user-data-dir=${profileDir}`,
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-extensions',
  '--disable-sync',
  `--force-device-scale-factor=${Number.isFinite(scale) && scale > 0 ? scale : 2}`,
  `--window-size=${WINDOW_WIDTH},${WINDOW_HEIGHT}`,
  `--screenshot=${screenshotPath}`,
  '--dump-dom',
  '--virtual-time-budget=12000',
  '--disable-background-networking',
  pathToFileURL(htmlPath).href,
];
const completed = spawnSync(browserPath, args, { timeout: 120000, encoding: 'utf8' });
const stderrTail = (completed.stderr || '').split('\n').slice(-6).join('\n');
try {
  if (!fs.existsSync(screenshotPath) || fs.statSync(screenshotPath).size === 0) {
    fail(`浏览器截图失败（exit=${completed.status}）`, 5, {
      browser: browserPath,
      status: completed.status,
      signal: completed.signal || '',
      error: completed.error ? String(completed.error.message || completed.error) : '',
      stderrTail,
    });
  }
  let metrics = {};
  const dom = completed.stdout || '';
  const metricsMatch = dom.match(/<pre[^>]*id="metrics"[^>]*>([\s\S]*?)<\/pre>/i);
  if (metricsMatch) {
    const raw = metricsMatch[1]
      .replaceAll('&quot;', '"')
      .replaceAll('&amp;', '&')
      .replaceAll('&lt;', '<')
      .replaceAll('&gt;', '>');
    try {
      metrics = JSON.parse(raw);
    } catch {
      metrics = {};
    }
  }
  if (metrics && metrics.error) fail(`mermaid 渲染失败：${metrics.error}`, 6, { metrics, stderrTail });
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.copyFileSync(screenshotPath, outputPath);
  const bytes = fs.statSync(outputPath).size;
  process.stdout.write(
    JSON.stringify(
      {
        ok: true,
        engine: path.basename(browserPath),
        browser: browserPath,
        input: inputPath,
        output: outputPath,
        bytes,
        scale,
        metrics,
        domBytes: dom.length,
        stderrTail: stderrTail || '',
      },
      null,
      2,
    ) + '\n',
  );
  cleanupTempDir();
  process.exit(0);
} finally {
  cleanupTempDir();
}
