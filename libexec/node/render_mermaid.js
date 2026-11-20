#!/usr/bin/env node
/**
 * BidGenie Mermaid → PNG（纯 Node，无 npm 依赖）
 *
 * 用法：
 *   node render_mermaid.js <input.mmd> <output.png> [--browser auto|chrome|edge|none] [--lib <mermaid.min.js>] [--scale 2]
 *
 * 原理：系统 Chrome/Edge headless（`--headless=new --screenshot`）+ 把内置
 * `plugin/assets/mermaid/mermaid.min.js` 内联进临时 HTML，等待 mermaid.render 完成，
 * `--virtual-time-budget` 给渲染留时间；按窗口尺寸截图。
 * 浏览器/库缺失或渲染失败时输出 JSON {ok:false,error,...} 并以非 0 退出，由 bg_render.py 降级 Pillow。
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function fail(message, code = 2, detail = {}) {
  process.stdout.write(JSON.stringify({ ok: false, error: message, detail }, null, 2) + '\n');
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
const mermaidLib = fs.readFileSync(libPath, 'utf8');
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bidgenie-mermaid-'));
const htmlPath = path.join(tmpDir, 'render.html');
const screenshotPath = path.join(tmpDir, 'render.png');
// 每次渲染用独立的 user-data-dir：默认配置目录可能正被后台 Edge/Chrome 占用，
// 此时无头进程会把命令转发给已有实例后直接退出（exit=0）但不产生截图。
const profileDir = path.join(tmpDir, 'profile');
fs.mkdirSync(profileDir, { recursive: true });
const html = `<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  html,body{margin:0;padding:0;background:#ffffff;}
  #container{display:inline-block;padding:24px;background:#ffffff;}
  #container svg{max-width:none;height:auto;}
</style></head>
<body><div id="container"></div>
<script>${mermaidLib}</script>
<script>
(async () => {
  try {
    mermaid.initialize({ startOnLoad: false, theme: 'neutral', securityLevel: 'loose', fontFamily: 'Microsoft YaHei, SimSun, Arial, sans-serif' });
    const result = await mermaid.render('bgGraph', ${JSON.stringify(code)});
    document.getElementById('container').innerHTML = result.svg;
    document.title = 'ready';
  } catch (err) {
    document.title = 'error';
    const pre = document.createElement('pre');
    pre.textContent = 'mermaid render error: ' + String(err);
    document.body.appendChild(pre);
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
  '--window-size=1600,2000',
  `--screenshot=${screenshotPath}`,
  '--virtual-time-budget=10000',
  '--disable-background-networking',
  pathToFileURL(htmlPath).href,
];
const completed = spawnSync(browserPath, args, { timeout: 90000, encoding: 'utf8' });
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
        stderrTail: stderrTail || '',
      },
      null,
      2,
    ) + '\n',
  );
  process.exit(0);
} finally {
  try {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  } catch (_) {
    /* ignore */
  }
}
