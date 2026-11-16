# mermaid 渲染资源

- 文件：`mermaid.min.js`（**2,571,900 B**）
- 版本：mermaid **11.4.1**（jsDelivr CDN 构建，`mermaid@11.4.1/dist/mermaid.min.js`）
- 用途：`plugin/libexec/node/render_mermaid.js` 内联进临时 HTML，由系统 Chrome/Edge headless 截图，
  **离线**渲染 mermaid → PNG（无需 npm install / puppeteer / 在线服务）。
- 许可：MIT（mermaid 项目）。详见 https://github.com/mermaid-js/mermaid/blob/develop/LICENSE
- 更新方式：保持版本锁定；替换后跑 `bg_render.py --selftest` 与 `plugin/tests/m6/render_selftest.py`。
- 降级：浏览器不可用或库缺失时，`bg_render.py` 使用 Pillow 生成简化占位图 + warning（不中断正文），
  审查阶段可重试渲染。
