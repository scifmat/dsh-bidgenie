/**
 * dsh-bidgenie —— 标代精灵 · 左栏面板（Client half，M1）
 *
 * 纯动态模块（无构建）：由 DSH 的 Client ModuleLoader 加载。
 * 注册物：
 *   - sidebar.panellist : 左栏图标（id=bidgenie, order=30, label=标代精灵）
 *   - main              : 面板页面（key=bidgenie）
 *
 * 数据通道：同源 fetch -> Host 路由 /bidgenie/*（见 plugin/CONTRACT.md §3）
 *   - GET 带 ?workspace=<绝对路径>
 *   - 写请求 body 带 workspace，并带 x-bidgenie-csrf: 1
 *
 * 硬约束（PRD 决策 9.19）：
 *   - 无文件上传、无「开始/继续撰写」按钮、不向会话注入消息
 *   - 只提供只读进度、只读辅助（复制启动指令 / 复制项目位置）与资料管理
 *   - 颜色只用 DSH 主题 token（--dsw-*），不写死色值
 *   - 普通界面不出现技术词（JSON / schema / 路由 / 接口 等）
 */
window.__ModuleLoader__.load({
  id: 'dsh-bidgenie',
  factory(require) {
    'use strict';

    const React = require('react');
    const h = React.createElement;
    const useState = React.useState;
    const useEffect = React.useEffect;
    const useMemo = React.useMemo;
    const useCallback = React.useCallback;
    const useRef = React.useRef;

    // ────────────────────────────────────────────────────────────
    // 常量
    // ────────────────────────────────────────────────────────────

    const API_ROOT = '/bidgenie';
    const CSRF_HEADERS = { 'x-bidgenie-csrf': '1' };
    const REQUEST_TIMEOUT_MS = 10000;
    // 环境检查整体 10 分钟（CONTRACT 8.2；单项 120s 由脚本侧执行），客户端多留 15s 余量
    const ENV_RUN_TIMEOUT_MS = 10 * 60 * 1000 + 15000;
    // GET 会触发 Host 侧 --report（脚本超时 60s），客户端留 75s
    const ENV_GET_TIMEOUT_MS = 75000;
    // 环境修复：plan/verify 30s（客户端留 45s）；apply Host 20 分钟（客户端留 21 分钟）
    const ENV_REPAIR_PLAN_TIMEOUT_MS = 45000;
    const ENV_REPAIR_LAST_TIMEOUT_MS = 45000;
    const ENV_REPAIR_TIMEOUT_MS = 21 * 60 * 1000;
    const WS_STORAGE_KEY = 'dsh-bidgenie.workspace';
    const POLL_INTERVAL_MS = 5000;

    const STATUS = { IDLE: 'idle', LOADING: 'loading', READY: 'ready', ERROR: 'error', EMPTY: 'empty' };

    const BIDDER_TYPES = [
      { value: 'legal_entity', label: '法人' },
      { value: 'natural_person', label: '自然人' },
    ];
    const BIDDER_TYPE_LABEL = { legal_entity: '法人', natural_person: '自然人' };

    const CERT_CATEGORIES = [
      { value: 'qualification', label: '资质证书' },
      { value: 'license', label: '许可证' },
      { value: 'management', label: '体系认证' },
      { value: 'personnel', label: '人员证书' },
      { value: 'other', label: '其他' },
    ];
    const CERT_STATUS = {
      valid: { label: '有效', tone: 'ok' },
      expiring: { label: '即将到期', tone: 'warn' },
      expired: { label: '已过期', tone: 'error' },
      unknown: { label: '未知', tone: 'neutral' },
    };
    const PERFORMANCE_ROLES = [
      { value: 'main', label: '主承接' },
      { value: 'sub', label: '分包或联合体' },
      { value: 'similar', label: '类似业绩' },
    ];
    const AWARD_LEVELS = [
      { value: 'national', label: '国家级' },
      { value: 'provincial', label: '省级' },
      { value: 'municipal', label: '市级' },
      { value: 'industry', label: '行业' },
      { value: 'other', label: '其他' },
    ];
    const PENALTY_TYPES = [
      { value: 'administrative_penalty', label: '行政处罚' },
      { value: 'litigation', label: '诉讼' },
      { value: 'dishonesty', label: '失信' },
      { value: 'other', label: '其他' },
    ];
    const PENALTY_STATUS = [
      { value: 'settled', label: '已结案' },
      { value: 'pending', label: '进行中' },
      { value: 'unknown', label: '未知' },
    ];
    const SENSITIVE_KEYS = ['unifiedSocialCreditCode', 'idNumber', 'legalRepresentative'];

    // 7 步可视化；内部阶段 -> 用户可见阶段 / 下一步 / 复制指令
    const STAGE_TABLE = [
      { key: 'project_init', label: '初始化', step: 1, next: '在对话框说「开始初始化」', command: (c) => '[BidGenie] 继续《' + c.name + '》的初始化' },
      { key: 'info_extract', label: '提取招标文件', step: 2, next: '在对话框说「继续提取关键信息」', command: (c) => '[BidGenie] 继续提取《' + c.name + '》的关键信息' },
      { key: 'package_prepare', label: '撰写准备', step: 3, next: '对话里可能正在等你回答，先看对话框', command: (c) => '[BidGenie] 继续《' + c.name + '》的撰写前准备' },
      { key: 'outline', label: '大纲', step: 4, next: '在对话框说「继续生成大纲」', command: (c) => '[BidGenie] 继续生成《' + c.name + '》第 ' + c.packageNo + ' 包大纲' },
      { key: 'draft', label: '正文撰写', step: 5, next: '在对话框说「继续写正文」', command: (c) => '[BidGenie] 继续撰写《' + c.name + '》第 ' + c.packageNo + ' 包正文' },
      { key: 'review', label: '审查', step: 6, next: '在对话框说「继续审查」', command: (c) => '[BidGenie] 继续审查《' + c.name + '》第 ' + c.packageNo + ' 包' },
      { key: 'optimize', label: '优化', step: 6, next: '在对话框说「继续优化」', command: (c) => '[BidGenie] 继续优化《' + c.name + '》第 ' + c.packageNo + ' 包' },
      { key: 'finalize', label: '定稿锁定', step: 7, next: '确认内容后，在对话框说「可以定稿」', command: (c) => '[BidGenie] 可以定稿《' + c.name + '》第 ' + c.packageNo + ' 包' },
      { key: 'export', label: '导出', step: 7, next: '在对话框说「导出 Word」', command: (c) => '[BidGenie] 导出《' + c.name + '》第 ' + c.packageNo + ' 包的 Word' },
      { key: 'completed', label: '已完成', step: 7, done: true, next: '如需换样式重新导出，在对话框说「重新导出」', command: (c) => '[BidGenie] 重新导出《' + c.name + '》第 ' + c.packageNo + ' 包' },
      { key: 'aborted', label: '已中止', step: 0, aborted: true, next: '在对话框说「恢复这个项目」', command: (c) => '[BidGenie] 恢复《' + c.name + '》' },
    ];
    const STAGE_MAP = STAGE_TABLE.reduce((acc, item) => { acc[item.key] = item; return acc; }, {});

    const SETTINGS_DEFAULTS = {
      schemaVersion: '1.0',
      currentBidderId: '',
      defaultWordCountTier: 50000,
      defaultFigureQuota: { table: 5, mermaid: 5, terminal: 5 },
      styleProfiles: { tech: 'official-tech', keyInfo: 'official-keyInfo', overview: 'official-overview' },
      interactionMode: 'guided',
      maxReviewRounds: 5,
      maxParallelWriters: 3,
      allowAutoDefaults: true,
      maskSensitive: true,
      logLevel: 'info',
    };
    // CONTRACT §9 v2：两套官方样式隔离（tech / keyInfo 互不共用）
    const STYLE_BUILTIN_FALLBACK = {
      tech: [{ id: 'official-tech', label: '官方-技术方案导出样式' }],
      keyInfo: [{ id: 'official-keyInfo', label: '官方-关键信息导出样式' }],
      overview: [{ id: 'official-overview', label: '官方-工作概览导出样式' }],
    };
    const STYLE_DOC_TYPES = [
      {
        key: 'tech',
        label: '技术方案文档样式',
        hint: '新项目默认：正文导出的字体、页边距、页码样式',
        defaultText: '官方-技术方案导出样式',
        defaultId: 'official-tech',
        dirName: 'tech',
      },
      {
        key: 'keyInfo',
        label: '关键信息文档样式',
        hint: '新项目默认：招标信息汇总导出的版式',
        defaultText: '官方-关键信息导出样式',
        defaultId: 'official-keyInfo',
        dirName: 'keyinfo',
      },
      {
        key: 'overview',
        label: '工作概览导出样式',
        hint: '新项目默认：工作概览的版式',
        defaultText: '官方-工作概览导出样式',
        defaultId: 'official-overview',
        dirName: 'overview',
      },
    ];
    const STYLE_EXAMPLE_JSON = [
      '{',
      '  "schemaVersion": "1.0",',
      '  "id": "my-company-tech",',
      '  "name": "公司技术方案样式",',
      '  "docType": "tech",',
      '  "description": "公司内部统一版式",',
      '  "basedOn": null,',
      '  "params": {',
      '    "fontFamily": "宋体",',
      '    "fontSizePt": 12,',
      '    "lineSpacing": 1.5,',
      '    "pageMarginMm": [20, 20, 19, 19],',
      '    "headingNumbering": true,',
      '    "captionPrefix": "▼表/▲图"',
      '  }',
      '}',
    ].join('\n');

    // 旧官方 id 迁移（CONTRACT §9.1）：official-default / keyinfo → 两套新官方样式
    function migrateStyleId(docType, value) {
      const id = trimText(value);
      const target = docType === 'tech' ? 'official-tech' : docType === 'overview' ? 'official-overview' : 'official-keyInfo';
      if (!id) return target;
      if (docType === 'tech' && (id === 'official-default' || id === 'keyinfo' || id === 'official-keyInfo')) return 'official-tech';
      if (docType === 'keyInfo' && (id === 'official-default' || id === 'keyinfo' || id === 'official-tech')) return 'official-keyInfo';
      return id;
    }

    // 兼容旧字段 styleProfile（只读迁移），始终产出 styleProfiles 三个键
    function normalizeSettings(settings) {
      const base = Object.assign({}, SETTINGS_DEFAULTS, settings || {});
      const profiles = isPlainObject(base.styleProfiles) ? base.styleProfiles : {};
      const legacy = trimText(base.styleProfile);
      base.styleProfiles = {
        tech: migrateStyleId('tech', profiles.tech || legacy),
        keyInfo: migrateStyleId('keyInfo', profiles.keyInfo),
        overview: migrateStyleId('overview', profiles.overview),
      };
      delete base.styleProfile;
      // P1：网络配图已移除；旧 allowNetworkImages 直接忽略，配额只保留三格
      delete base.allowNetworkImages;
      const quota = isPlainObject(base.defaultFigureQuota) ? base.defaultFigureQuota : {};
      const quotaNumber = (value) => (typeof value === 'number' && isFinite(value) ? value : 5);
      base.defaultFigureQuota = {
        table: quotaNumber(quota.table),
        mermaid: quotaNumber(quota.mermaid),
        terminal: quotaNumber(quota.terminal),
      };
      return base;
    }

    function styleDocTypes(style) {
      const raw = style && (style.docTypes || style.docType);
      if (Array.isArray(raw)) return raw.map(textOf).filter(Boolean);
      if (typeof raw === 'string' && raw !== '') return [raw];
      return null;
    }

    function styleMatchesDocType(style, docType) {
      const types = styleDocTypes(style);
      return types === null || types.indexOf(docType) >= 0;
    }

    function normalizeStyleOption(style) {
      const id = textOf(style && style.id);
      if (!id) return null;
      return { id: id, label: trimText(style && (style.label || style.name)) || id };
    }

    const STYLE_KINDS = ['tech', 'keyInfo', 'overview'];

    // 契约 v2：builtin/custom 均为 {tech:[...],keyInfo:[...],overview:[...]}；
    // 兼容旧版平铺数组，以及 `GET /styles?kind=overview` 这类“已按类型过滤”的响应
    function normalizeStyleCatalog(data, kind) {
      const source = isPlainObject(data) ? data : {};
      const emptyBuckets = () => ({ tech: [], keyInfo: [], overview: [] });
      const catalog = {
        builtin: emptyBuckets(),
        custom: emptyBuckets(),
        warnings: asArray(source.warnings),
        shape: 'v2',
      };
      const builtinObject = isPlainObject(source.builtin);
      const customObject = isPlainObject(source.custom);
      if (builtinObject || customObject) {
        STYLE_KINDS.forEach((docType) => {
          catalog.builtin[docType] = asArray(builtinObject ? source.builtin[docType] : []).map(normalizeStyleOption).filter(Boolean);
          catalog.custom[docType] = asArray(customObject ? source.custom[docType] : []).map(normalizeStyleOption).filter(Boolean);
        });
        return catalog;
      }
      catalog.shape = kind ? 'kind' : 'v1';
      const kinds = kind ? [kind] : STYLE_KINDS;
      kinds.forEach((docType) => {
        const matches = kind ? () => true : (style) => styleMatchesDocType(style, docType);
        catalog.builtin[docType] = asArray(source.builtin).filter(matches).map(normalizeStyleOption).filter(Boolean);
        catalog.custom[docType] = asArray(source.custom).filter(matches).map(normalizeStyleOption).filter(Boolean);
      });
      return catalog;
    }

    // 合并两份样式目录（基础 /styles + ?kind=overview），按 id 去重
    function mergeStyleCatalogs(primary, extra) {
      const merged = {
        builtin: { tech: primary.builtin.tech.slice(), keyInfo: primary.builtin.keyInfo.slice(), overview: primary.builtin.overview.slice() },
        custom: { tech: primary.custom.tech.slice(), keyInfo: primary.custom.keyInfo.slice(), overview: primary.custom.overview.slice() },
        warnings: (primary.warnings || []).slice(),
      };
      const mergeList = (target, list) => {
        asArray(list).forEach((item) => {
          if (!target.some((existing) => existing.id === item.id)) target.push(item);
        });
      };
      STYLE_KINDS.forEach((docType) => {
        mergeList(merged.builtin[docType], extra.builtin[docType]);
        mergeList(merged.custom[docType], extra.custom[docType]);
      });
      asArray(extra.warnings).forEach((warning) => {
        const exists = merged.warnings.some(
          (item) => textOf(item && item.file) === textOf(warning && warning.file) && textOf(item && item.message) === textOf(warning && warning.message),
        );
        if (!exists) merged.warnings.push(warning);
      });
      return merged;
    }
    const WORD_TIERS = [
      { value: 50000, label: '5 万字' },
      { value: 100000, label: '10 万字' },
      { value: 150000, label: '15 万字' },
      { value: 0, label: '自定义' },
    ];

    const FRIENDLY_ERROR = {
      E_NOT_FOUND: '没有找到对应的资料，可能已被删除或还没创建。',
      E_SCHEMA_INVALID: '填写的内容不符合要求，请检查后再试。',
      E_LOCK_TIMEOUT: '另一个操作正在进行，请稍后再试。',
      E_DEP_MISSING: '运行环境缺少必要组件，请先做环境检查。',
      E_USER_DECISION_REQUIRED: '需要你做一个选择，请在对话框里继续。',
      E_TIMEOUT: '请求超时（10 秒），请检查连接后重试。',
      E_ILLEGAL_TRANSITION: '当前进度不允许这一步操作。',
      E_LOCKED: '内容已定稿锁定，先解锁才能修改。',
      E_USAGE: '请求内容不完整，请重试。',
    };

    // ────────────────────────────────────────────────────────────
    // 通用工具
    // ────────────────────────────────────────────────────────────

    function classNames() {
      let out = '';
      for (let i = 0; i < arguments.length; i += 1) {
        const value = arguments[i];
        if (value) out += (out ? ' ' : '') + value;
      }
      return out;
    }

    function isPlainObject(value) {
      return !!value && typeof value === 'object' && !Array.isArray(value);
    }

    function asArray(value) {
      return Array.isArray(value) ? value : [];
    }

    function textOf(value) {
      return typeof value === 'string' ? value : value == null ? '' : String(value);
    }

    function trimText(value) {
      return textOf(value).trim();
    }

    function versionStage(rawVersion) {
      const value = trimText(rawVersion).toLowerCase();
      if (!value) return '';
      const suffix = value.includes('-') ? value.slice(value.indexOf('-') + 1) : '';
      if (suffix.startsWith('alpha')) return 'Alpha';
      if (suffix.startsWith('beta')) return 'Beta';
      if (suffix.startsWith('rc')) return 'RC';
      return 'Stable';
    }

    function displayVersion(rawVersion) {
      const value = trimText(rawVersion);
      if (!value) return '';
      const base = value.replace(/^v/i, '').split('-')[0];
      const stage = versionStage(value);
      return 'v' + base + (stage && stage !== 'Stable' ? ' ' + stage : '');
    }

    function getPath(obj, path) {
      let cur = obj;
      for (let i = 0; i < path.length; i += 1) {
        if (cur == null) return undefined;
        cur = cur[path[i]];
      }
      return cur;
    }

    function setPath(obj, path, value) {
      const next = isPlainObject(obj) ? Object.assign({}, obj) : {};
      if (path.length === 1) {
        next[path[0]] = value;
        return next;
      }
      const head = path[0];
      next[head] = setPath(isPlainObject(next[head]) ? next[head] : {}, path.slice(1), value);
      return next;
    }

    function maskValue(value, keep) {
      const text = trimText(value);
      if (!text) return '';
      const head = keep != null ? keep : 4;
      if (text.length <= head * 2) return text.slice(0, 1) + '****';
      return text.slice(0, head) + '****' + text.slice(text.length - head);
    }

    function formatDateTime(iso) {
      if (!iso) return '';
      const date = new Date(iso);
      if (isNaN(date.getTime())) return '';
      const pad = (n) => (n < 10 ? '0' + n : String(n));
      return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate()) + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
    }

    function formatRelative(iso) {
      if (!iso) return '';
      const date = new Date(iso);
      if (isNaN(date.getTime())) return '';
      const now = Date.now();
      const diff = now - date.getTime();
      if (diff < 0) return formatDateTime(iso);
      const minute = 60000;
      const hour = 60 * minute;
      if (diff < minute) return '刚刚';
      if (diff < hour) return Math.floor(diff / minute) + ' 分钟前';
      if (diff < 24 * hour) return '今天 ' + formatDateTime(iso).slice(11);
      if (diff < 48 * hour) return '昨天 ' + formatDateTime(iso).slice(11);
      return formatDateTime(iso).slice(0, 10);
    }

    // 尚未初始化阶段的项目（registry 有记录但 state 未推进）
    const PENDING_STAGE = {
      label: '尚未开始',
      step: 1,
      next: '在对话框里说「开始这个项目」',
      command: (context) => '[BidGenie] 开始《' + context.name + '》',
    };

    function packageNoOf(packageId) {
      const match = /^package-(\d+)/.exec(textOf(packageId));
      return match ? match[1] : '';
    }

    function stageInfo(stageKey) {
      return STAGE_MAP[stageKey] || { key: stageKey || '', label: '未知进度', step: 0, next: '', command: (c) => '[BidGenie] 继续《' + c.name + '》' };
    }

    function launchCommandFor(project) {
      const name = trimText(project && project.projectName) || trimText(project && project.dirName) || '未命名项目';
      const packageNo = packageNoOf(project && project.currentPackage) || '1';
      if (!project || !project.currentStage) return PENDING_STAGE.command({ name: name });
      return stageInfo(project.currentStage).command({ name: name, packageNo: packageNo });
    }

    function baseName(pathValue) {
      const parts = textOf(pathValue).split(/[\\/]+/).filter(Boolean);
      return parts.length ? parts[parts.length - 1] : '';
    }

    function workspaceLabel(item) {
      if (!item) return '';
      return trimText(item.title) || baseName(item.path) || trimText(item.id);
    }

    function readStoredWorkspace() {
      try {
        if (typeof localStorage === 'undefined') return '';
        return localStorage.getItem(WS_STORAGE_KEY) || '';
      } catch (error) {
        return '';
      }
    }

    function storeWorkspace(pathValue) {
      try {
        if (typeof localStorage !== 'undefined' && pathValue) localStorage.setItem(WS_STORAGE_KEY, pathValue);
      } catch (error) {
        /* 本地记忆失败不影响使用 */
      }
    }

    function copyText(text) {
      const value = textOf(text);
      if (!value) return Promise.reject(new Error('没有可复制的内容'));
      if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(value);
      }
      return new Promise((resolve, reject) => {
        try {
          const area = document.createElement('textarea');
          area.value = value;
          area.setAttribute('readonly', 'readonly');
          area.style.position = 'fixed';
          area.style.left = '-9999px';
          document.body.appendChild(area);
          area.select();
          const ok = document.execCommand('copy');
          document.body.removeChild(area);
          if (ok) resolve();
          else reject(new Error('复制失败'));
        } catch (error) {
          reject(error);
        }
      });
    }

    // ────────────────────────────────────────────────────────────
    // 接口层
    // ────────────────────────────────────────────────────────────

    function ApiError(code, message, hint, status) {
      const error = new Error(message || '请求失败');
      error.name = 'ApiError';
      error.code = code || 'E_UNKNOWN';
      error.hint = hint || '';
      error.status = status || 0;
      return error;
    }

    function isNotFound(error) {
      return !!error && (error.code === 'E_NOT_FOUND' || error.status === 404);
    }

    function describeError(error) {
      if (!error) return { message: '发生未知问题，请重试。', hint: '' };
      if (error.name === 'ApiError') {
        const code = textOf(error.code);
        // 运行时/修复类错误必须把真实原因和排查提示透传出来；否则面板只剩一句
        // “运行环境缺少必要组件”，而环境检查本身也起不来，用户无从下手。
        if (code === 'E_DEP_MISSING' || code === 'E_REPAIR_FAILED') {
          const message = trimText(error.message) || FRIENDLY_ERROR[code];
          const hint = trimText(error.hint);
          return { message: hint ? message + (/[。！？]$/.test(message) ? '' : '。') + hint : message, hint: '' };
        }
        const friendly = FRIENDLY_ERROR[code] || messageOrFallback(error);
        return { message: friendly, hint: error.hint || '' };
      }
      if (error && (error.name === 'AbortError' || error.code === 'E_TIMEOUT')) {
        return { message: FRIENDLY_ERROR.E_TIMEOUT, hint: '' };
      }
      if (error && error.message && /fetch|network|Failed|NetworkError/i.test(error.message)) {
        return { message: '连接不上本地服务，请确认 DSH 正在运行后重试。', hint: '' };
      }
      return { message: '操作没有完成，请重试。', hint: '' };
    }

    function messageOrFallback(error) {
      const message = trimText(error && error.message);
      if (message && !/[A-Za-z]{4,}/.test(message)) return message;
      return '操作没有完成，请重试。';
    }

    function requestJson(path, options) {
      const opts = options || {};
      const method = opts.method || 'GET';
      const isWrite = method !== 'GET';
      const url = new URL(API_ROOT + path, window.location.origin);

      if (opts.query) {
        Object.keys(opts.query).forEach((key) => {
          const value = opts.query[key];
          if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, String(value));
        });
      }

      const headers = { accept: 'application/json' };
      let body;
      if (isWrite) {
        headers['content-type'] = 'application/json';
        headers['x-bidgenie-csrf'] = CSRF_HEADERS['x-bidgenie-csrf'];
        const payload = Object.assign({}, opts.body || {});
        if (opts.workspace && !payload.workspace) payload.workspace = opts.workspace;
        body = JSON.stringify(payload);
      } else if (opts.workspace) {
        url.searchParams.set('workspace', opts.workspace);
      }

      const timeoutMs = typeof opts.timeoutMs === 'number' && opts.timeoutMs > 0 ? opts.timeoutMs : REQUEST_TIMEOUT_MS;
      let controller = null;
      let timer = null;
      if (typeof AbortController !== 'undefined') {
        controller = new AbortController();
        timer = setTimeout(() => controller.abort(), timeoutMs);
      }

      return fetch(url.toString(), {
        method: method,
        headers: headers,
        credentials: 'same-origin',
        body: body,
        signal: controller ? controller.signal : undefined,
      })
        .then((response) => response.text().then((raw) => ({ response: response, raw: raw })))
        .then((pair) => {
          let data = null;
          if (pair.raw) {
            try {
              data = JSON.parse(pair.raw);
            } catch (error) {
              data = null;
            }
          }
          if (isPlainObject(data) && data.ok === false) {
            throw ApiError(data.error, data.message, data.hint, pair.response.status);
          }
          if (!pair.response.ok) {
            throw ApiError('E_HTTP_' + pair.response.status, '服务暂时没有响应，请稍后重试。', '', pair.response.status);
          }
          if (!isPlainObject(data)) {
            throw ApiError('E_BAD_RESPONSE', '返回内容无法识别，请重试。');
          }
          return data;
        })
        .catch((error) => {
          if (error && error.name === 'AbortError') throw ApiError('E_TIMEOUT', FRIENDLY_ERROR.E_TIMEOUT);
          throw error;
        })
        .then(
          (data) => {
            if (timer) clearTimeout(timer);
            return data;
          },
          (error) => {
            if (timer) clearTimeout(timer);
            throw error;
          },
        );
    }

    const api = {
      workspaces: function () {
        return requestJson('/workspaces');
      },
      health: function (workspace) {
        return requestJson('/health', { workspace: workspace });
      },
      about: function () {
        return requestJson('/about');
      },
      initWorkspace: function (workspace) {
        return requestJson('/workspace/init', { method: 'POST', workspace: workspace, body: { workspace: workspace } });
      },
      listBidders: function (workspace) {
        return requestJson('/bidders', { workspace: workspace });
      },
      listProjects: function (workspace) {
        return requestJson('/projects', { workspace: workspace });
      },
      detailBidder: function (workspace, bidderId) {
        return requestJson('/bidders', { workspace: workspace, query: { bidderId: bidderId } });
      },
      createBidder: function (workspace, payload) {
        return requestJson('/bidders', { method: 'POST', workspace: workspace, body: payload });
      },
      updateBidder: function (workspace, bidderId, payload) {
        const path = '/bidders/' + encodeURIComponent(bidderId);
        return requestJson(path, { method: 'PUT', workspace: workspace, body: payload });
      },
      deleteBidder: function (workspace, bidderId) {
        const path = '/bidders/' + encodeURIComponent(bidderId);
        return requestJson(path, { method: 'DELETE', workspace: workspace, body: { workspace: workspace, confirm: true } });
      },
      setCurrentBidder: function (workspace, bidderId) {
        const path = '/bidders/' + encodeURIComponent(bidderId) + '/current';
        return requestJson(path, { method: 'POST', workspace: workspace, body: { workspace: workspace } });
      },
      getSettings: function (workspace) {
        return requestJson('/settings', { workspace: workspace });
      },
      putSettings: function (workspace, settings) {
        return requestJson('/settings', { method: 'PUT', workspace: workspace, body: { workspace: workspace, settings: settings } });
      },
      getEnvCheck: function (workspace) {
        return requestJson('/env-check', { workspace: workspace, timeoutMs: ENV_GET_TIMEOUT_MS });
      },
      runEnvCheck: function (workspace) {
        return requestJson('/env-check/run', {
          method: 'POST',
          workspace: workspace,
          body: { workspace: workspace },
          timeoutMs: ENV_RUN_TIMEOUT_MS,
        });
      },
      envCheckRepairPlan: function (workspace) {
        return requestJson('/env-check/repair/plan', {
          method: 'POST',
          workspace: workspace,
          body: { workspace: workspace },
          timeoutMs: ENV_REPAIR_PLAN_TIMEOUT_MS,
        });
      },
      envCheckRepair: function (workspace, payload) {
        const body = Object.assign({ workspace: workspace }, payload || {});
        return requestJson('/env-check/repair', {
          method: 'POST',
          workspace: workspace,
          body: body,
          timeoutMs: ENV_REPAIR_TIMEOUT_MS,
        });
      },
      envCheckRepairLast: function (workspace) {
        return requestJson('/env-check/repair/last', {
          workspace: workspace,
          timeoutMs: ENV_REPAIR_LAST_TIMEOUT_MS,
        });
      },
      getStyles: function (workspace, kind) {
        return requestJson('/styles', { workspace: workspace, query: kind ? { kind: kind } : undefined });
      },
    };

    // ────────────────────────────────────────────────────────────
    // 样式（颜色一律使用 DSH 主题 token）
    // ────────────────────────────────────────────────────────────

    const STYLE_ID = 'dsh-bidgenie-panel-styles';
    const PANEL_CSS = [
      '.bgp-root{--bgp-bg:var(--dsw-alias-bg-base);--bgp-card:var(--dsw-alias-bg-layer-1);--bgp-card2:var(--dsw-alias-bg-layer-2);',
      '--bgp-line:var(--dsw-alias-border-l1);--bgp-line2:var(--dsw-alias-border-l2);--bgp-sep:var(--dsw-alias-separator-primary);',
      '--bgp-fg:var(--dsw-alias-label-primary);--bgp-fg2:var(--dsw-alias-label-secondary);--bgp-fg3:var(--dsw-alias-label-tertiary);',
      '--bgp-fg4:var(--dsw-alias-label-quaternary,var(--dsw-alias-label-tertiary));--bgp-brand:var(--dsw-alias-brand-primary);',
      '--bgp-brand-fg:var(--dsw-alias-label-primary-foreground,var(--dsw-alias-label-primary-inverted));',
      '--bgp-primary:var(--dsw-alias-button-primary-fill);--bgp-primary-hover:var(--dsw-alias-button-primary-hover);',
      '--bgp-primary-dim:var(--dsw-alias-button-primary-dimmed);--bgp-fill:var(--dsw-alias-fill-l1);--bgp-fill2:var(--dsw-alias-fill-l2);',
      '--bgp-hover:var(--dsw-alias-interactive-bg-hover);--bgp-hover-danger:var(--dsw-alias-interactive-bg-hover-danger);',
      '--bgp-ok:var(--dsw-alias-state-success-primary);--bgp-ok-bg:var(--dsw-alias-state-success-tertiary);',
      '--bgp-warn:var(--dsw-alias-state-warn-primary);--bgp-warn-bg:var(--dsw-alias-state-warn-tertiary);--bgp-warn-fg:var(--dsw-alias-state-warn-label,var(--dsw-alias-state-warn-primary));',
      '--bgp-err:var(--dsw-alias-state-error-primary);--bgp-err-bg:var(--dsw-alias-state-error-secondary);',
      '--bgp-biz:var(--dsw-alias-state-business-primary);--bgp-biz-bg:var(--dsw-alias-state-business-tertiary);',
      '--bgp-idle:var(--dsw-alias-state-idle-primary);--bgp-done:var(--dsw-alias-state-done-primary);',
      '--bgp-skeleton:var(--dsw-alias-bg-skeleton);--bgp-overlay:var(--dsw-alias-bg-overlay);',
      '--bgp-toast-bg:var(--dsw-alias-toast-bg);--bgp-toast-fg:var(--dsw-alias-toast-label);',
      '--bgp-menu:var(--dsw-alias-menu-surface-fill);--bgp-focus:var(--dsw-alias-focus-ring-color);',
      '--bgp-radius-s:var(--dsw-radius-sm,6px);--bgp-radius-m:var(--dsw-radius-md,8px);--bgp-radius-l:var(--dsw-radius-lg,12px);',
      '--bgp-shadow:var(--dsw-shadow-lv2);', '}',

      '.bgp-root{position:relative;display:flex;flex-direction:column;height:100%;max-height:100%;min-height:0;overflow:hidden;overflow-anchor:none;overscroll-behavior:none;background:var(--bgp-bg);color:var(--bgp-fg);',
      'font-family:var(--dsw-font-family,system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif);font-size:14px;line-height:1.55;box-sizing:border-box;}',
      '.bgp-root *,.bgp-root *::before,.bgp-root *::after{box-sizing:border-box;}',
      '.bgp-root ::-webkit-scrollbar{width:10px;height:10px;}',
      '.bgp-root ::-webkit-scrollbar-thumb{background:var(--dsw-alias-scrollbar-bg-l1);border-radius:8px;}',
      '.bgp-root ::-webkit-scrollbar-thumb:hover{background:var(--dsw-alias-scrollbar-hover-l1);}',

      '.bgp-header{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 16px;border-bottom:1px solid var(--bgp-line);background:var(--bgp-card);}',
      '.bgp-topbar{position:relative;z-index:30;flex:0 0 auto;min-height:0;background:var(--bgp-bg);border-bottom:1px solid var(--bgp-line);}',
      '.bgp-brand{display:flex;align-items:center;gap:8px;font-size:15px;font-weight:600;color:var(--bgp-fg);}',
      '.bgp-brand-title{display:inline-flex;align-items:flex-end;gap:6px;}',
      '.bgp-brand-version{font-size:11px;font-weight:400;color:var(--bgp-fg3);line-height:1;letter-spacing:.2px;padding-bottom:1px;}',
      '.bgp-brand-mark{display:inline-flex;align-items:center;justify-content:center;color:var(--bgp-brand);}',
      '.bgp-header-spacer{flex:1 1 auto;}',
      '.bgp-ws{display:flex;align-items:center;gap:6px;min-width:0;}',
      '.bgp-ws-label{font-size:12px;color:var(--bgp-fg3);white-space:nowrap;}',
      '.bgp-ws .bgp-select{max-width:280px;}',

      '.bgp-tabs{display:flex;align-items:stretch;gap:2px;padding:0 12px;border-bottom:1px solid var(--bgp-line);background:var(--bgp-bg);}',
      '.bgp-tab{position:relative;border:none;background:transparent;color:var(--bgp-fg2);font:inherit;font-size:14px;padding:10px 14px;cursor:pointer;border-radius:var(--bgp-radius-s) var(--bgp-radius-s) 0 0;}',
      '.bgp-tab:hover{color:var(--bgp-fg);background:var(--bgp-hover);}',
      '.bgp-tab.is-active{color:var(--bgp-fg);font-weight:600;}',
      '.bgp-tab.is-active::after{content:"";position:absolute;left:10px;right:10px;bottom:-1px;height:2px;background:var(--bgp-brand);border-radius:2px;}',

      '.bgp-content{flex:1 1 auto;min-height:0;min-width:0;overflow-y:auto;overflow-x:hidden;padding:16px;overflow-anchor:none;overscroll-behavior:contain;scrollbar-gutter:stable;position:relative;}',
      '.bgp-page{max-width:980px;min-height:0;margin:0 auto;display:flex;flex-direction:column;gap:12px;padding-bottom:24px;}',
      '.bgp-page-narrow{max-width:760px;}',

      '.bgp-card{background:var(--bgp-card);border:1px solid var(--bgp-line);border-radius:var(--bgp-radius-l);padding:14px 16px;}',
      '.bgp-card-tight{padding:10px 12px;}',
      '.bgp-card-flat{border-color:transparent;background:transparent;padding:0;}',
      '.bgp-card-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px;}',
      '.bgp-card-title{font-size:16px;font-weight:600;color:var(--bgp-fg);}',
      '.bgp-section-title{font-size:15px;font-weight:600;color:var(--bgp-fg);}',

      '.bgp-h1{font-size:20px;font-weight:600;line-height:1.35;margin:0;color:var(--bgp-fg);}',
      '.bgp-h2{font-size:16px;font-weight:600;margin:0;color:var(--bgp-fg);}',
      '.bgp-desc{font-size:14px;color:var(--bgp-fg2);margin:0;}',
      '.bgp-muted{font-size:12px;color:var(--bgp-fg3);margin:0;}',
      '.bgp-small{font-size:13px;color:var(--bgp-fg2);margin:0;}',
      '.bgp-strong{font-weight:600;}',

      '.bgp-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}',
      '.bgp-actions-end{display:flex;align-items:center;gap:8px;margin-left:auto;flex-wrap:wrap;}',

      '.bgp-btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;height:32px;padding:0 12px;border-radius:var(--bgp-radius-s);',
      'border:1px solid transparent;background:transparent;color:var(--bgp-fg);font:inherit;font-size:14px;cursor:pointer;white-space:nowrap;',
      'transition:background .15s ease,color .15s ease,border-color .15s ease;}',
      '.bgp-btn:focus-visible{outline:2px solid var(--bgp-focus);outline-offset:2px;}',
      '.bgp-btn[disabled]{cursor:not-allowed;opacity:.55;}',
      '.bgp-btn-primary{background:var(--bgp-primary);color:var(--bgp-brand-fg);font-weight:500;}',
      '.bgp-btn-primary:hover{background:var(--bgp-primary-hover);}',
      '.bgp-btn-primary[disabled]:hover{background:var(--bgp-primary-dim);}',
      '.bgp-btn-secondary{border-color:var(--bgp-line2);color:var(--bgp-fg);}',
      '.bgp-btn-secondary:hover{background:var(--bgp-hover);}',
      '.bgp-btn-text{color:var(--bgp-fg2);padding:0 8px;}',
      '.bgp-btn-text:hover{color:var(--bgp-fg);background:var(--bgp-hover);}',
      '.bgp-btn-danger{color:var(--bgp-err);}',
      '.bgp-btn-danger:hover{background:var(--bgp-hover-danger);}',
      '.bgp-btn-danger-solid{background:var(--bgp-err);color:var(--bgp-brand-fg);}',
      '.bgp-btn-sm{height:28px;padding:0 10px;font-size:13px;}',
      '.bgp-icon-btn{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;border:none;border-radius:var(--bgp-radius-s);',
      'background:transparent;color:var(--bgp-fg2);cursor:pointer;}',
      '.bgp-icon-btn:hover{background:var(--bgp-hover);color:var(--bgp-fg);}',
      '.bgp-icon-btn:focus-visible{outline:2px solid var(--bgp-focus);outline-offset:2px;}',

      '.bgp-field{display:flex;flex-direction:column;gap:4px;min-width:0;}',
      '.bgp-field-label{font-size:13px;color:var(--bgp-fg2);display:flex;align-items:center;gap:4px;}',
      '.bgp-required{color:var(--bgp-err);}',
      '.bgp-field-hint{font-size:12px;color:var(--bgp-fg3);}',
      '.bgp-field-error{font-size:12px;color:var(--bgp-err);}',
      '.bgp-input,.bgp-textarea,.bgp-select{width:100%;min-width:0;height:32px;padding:0 10px;border:1px solid var(--bgp-line2);border-radius:var(--bgp-radius-s);',
      'background:var(--bgp-bg);color:var(--bgp-fg);font:inherit;font-size:14px;}',
      '.bgp-textarea{height:auto;min-height:68px;padding:8px 10px;resize:vertical;line-height:1.5;}',
      '.bgp-select{appearance:none;padding-right:26px;cursor:pointer;}',
      '.bgp-input:focus-visible,.bgp-textarea:focus-visible,.bgp-select:focus-visible{outline:2px solid var(--bgp-focus);outline-offset:0;border-color:var(--bgp-brand);}',
      '.bgp-input[disabled],.bgp-textarea[disabled],.bgp-select[disabled]{opacity:.6;cursor:not-allowed;}',
      '.bgp-input.is-error,.bgp-textarea.is-error,.bgp-select.is-error{border-color:var(--bgp-err);}',
      '.bgp-form-group{margin-top:18px;}',
    '.bgp-form-group:first-child{margin-top:0;}',
    '.bgp-form-group-head{display:flex;align-items:baseline;gap:8px;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid var(--bgp-line);}',
    '.bgp-form-group-title{font-size:13px;font-weight:600;color:var(--bgp-fg);}',
    '.bgp-form-group-desc{font-size:12px;color:var(--bgp-fg3);}',
    '.bgp-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 14px;}',
      '.bgp-form-grid .bgp-span2{grid-column:1 / -1;}',
      '@media (max-width:720px){.bgp-form-grid{grid-template-columns:minmax(0,1fr);}}',

      '.bgp-switch{position:relative;display:inline-flex;align-items:center;gap:8px;cursor:pointer;user-select:none;font-size:14px;color:var(--bgp-fg);}',
      '.bgp-switch input{position:absolute;top:0;left:0;width:36px;height:20px;margin:0;opacity:0;cursor:pointer;}',
      '.bgp-switch-track{position:relative;width:36px;height:20px;box-sizing:border-box;border-radius:10px;background:var(--bgp-fill2);',
      'border:1px solid var(--bgp-line2);transition:background .15s ease,border-color .15s ease;flex:0 0 auto;}',
      '.bgp-switch:hover .bgp-switch-track{border-color:var(--bgp-fg3);}',
      '.bgp-switch-thumb{position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--bgp-fg2);transition:transform .15s ease,background .15s ease;}',
      '.bgp-switch input:checked + .bgp-switch-track{background:var(--bgp-primary);border-color:var(--bgp-primary);}',
      '.bgp-switch input:checked + .bgp-switch-track .bgp-switch-thumb{transform:translateX(16px);background:var(--bgp-brand-fg);}',
      '.bgp-switch input:focus-visible + .bgp-switch-track{outline:2px solid var(--bgp-focus);outline-offset:2px;}',

      '.bgp-chip{display:inline-flex;align-items:center;gap:4px;height:20px;padding:0 8px;border-radius:var(--dsw-radius-xs,4px);font-size:12px;white-space:nowrap;',
      'background:var(--bgp-fill);color:var(--bgp-fg2);}',
      '.bgp-chip.tone-ok{background:var(--bgp-ok-bg);color:var(--bgp-fg);border:1px solid var(--bgp-ok);}',
      '.bgp-chip.tone-warn{background:var(--bgp-warn-bg);color:var(--bgp-fg);border:1px solid var(--bgp-warn);}',
      '.bgp-chip.tone-error{background:var(--bgp-err-bg);color:var(--bgp-fg);border:1px solid var(--bgp-err);}',
      '.bgp-chip.tone-brand{background:var(--bgp-biz-bg);color:var(--bgp-fg);border:1px solid var(--bgp-biz);}',
      '.bgp-chip.tone-neutral{background:var(--bgp-fill);color:var(--bgp-fg3);}',

      '.bgp-banner{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border:1px solid var(--bgp-line);border-radius:var(--bgp-radius-m);background:var(--bgp-fill);}',
      '.bgp-banner.tone-warn{background:var(--bgp-warn-bg);border-color:var(--bgp-warn);}',
      '.bgp-banner.tone-error{background:var(--bgp-err-bg);border-color:var(--bgp-err);}',
      '.bgp-banner.tone-ok{background:var(--bgp-ok-bg);border-color:var(--bgp-ok);}',
      '.bgp-banner.tone-info{background:var(--bgp-fill);border-color:var(--bgp-line);}',
      '.bgp-banner-body{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;gap:4px;}',
      '.bgp-banner-title{font-size:14px;font-weight:600;color:var(--bgp-fg);}',
      '.bgp-banner-text{font-size:13px;color:var(--bgp-fg2);}',
      '.bgp-banner-icon{flex:0 0 auto;color:var(--bgp-fg2);margin-top:1px;}',
      '.bgp-banner.tone-warn .bgp-banner-icon{color:var(--bgp-warn-fg);}',
      '.bgp-banner.tone-error .bgp-banner-icon{color:var(--bgp-err);}',
      '.bgp-banner.tone-ok .bgp-banner-icon{color:var(--bgp-ok);}',

      '.bgp-next{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border:1px dashed var(--bgp-line2);border-radius:var(--bgp-radius-m);background:transparent;}',
      '.bgp-next-tag{flex:0 0 auto;font-size:12px;color:var(--bgp-fg3);padding-top:2px;}',
      '.bgp-next-text{flex:1 1 auto;min-width:0;font-size:13px;color:var(--bgp-fg2);}',

      '.bgp-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:10px;padding:36px 16px;background:var(--bgp-card);border:1px solid var(--bgp-line);border-radius:var(--bgp-radius-l);}',
      '.bgp-empty-mark{display:inline-flex;align-items:center;justify-content:center;width:64px;height:48px;color:var(--bgp-fg4);}',
      '.bgp-empty-title{font-size:16px;font-weight:600;color:var(--bgp-fg);}',
      '.bgp-empty-text{font-size:13px;color:var(--bgp-fg2);max-width:460px;}',

      '.bgp-list{display:flex;flex-direction:column;}',
      '.bgp-row{display:flex;align-items:center;gap:10px;padding:10px 12px;border-bottom:1px solid var(--bgp-line);cursor:pointer;}',
      '.bgp-row:hover{background:var(--bgp-hover);}',
      '.bgp-row:last-child{border-bottom:none;}',
      '.bgp-row-main{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;gap:3px;}',
      '.bgp-row-title{font-size:14px;font-weight:600;color:var(--bgp-fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
      '.bgp-row-sub{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;color:var(--bgp-fg3);}',
      '.bgp-row-side{display:flex;align-items:center;gap:6px;flex:0 0 auto;}',

      '.bgp-dots{display:flex;gap:3px;align-items:center;flex:1 1 auto;min-width:60px;}',
      '.bgp-dot{height:6px;flex:1 1 auto;border-radius:3px;background:var(--bgp-fill2);}',
      '.bgp-dot.is-done{background:var(--bgp-done);}',
      '.bgp-dot.is-current{background:var(--bgp-brand);}',
      '.bgp-dot.is-aborted{background:var(--bgp-warn);}',
      '.bgp-dots-label{flex:0 0 auto;font-size:12px;color:var(--bgp-fg3);}',

      '.bgp-kv{display:grid;grid-template-columns:112px minmax(0,1fr);gap:8px 12px;align-items:start;}',
      '.bgp-kv-key{font-size:13px;color:var(--bgp-fg3);}',
      '.bgp-kv-val{font-size:14px;color:var(--bgp-fg);word-break:break-word;}',
      '@media (max-width:520px){.bgp-kv{grid-template-columns:minmax(0,1fr);gap:2px 0;} .bgp-kv-key{margin-top:6px;}}',

      '.bgp-skeleton{background:var(--bgp-skeleton);border-radius:var(--bgp-radius-s);height:14px;animation:bgp-pulse 1.4s ease-in-out infinite;}',
      '@keyframes bgp-pulse{0%,100%{opacity:1;}50%{opacity:.55;}}',
      '.bgp-spin{animation:bgp-rotate .8s linear infinite;transform-origin:center;}',
      '@keyframes bgp-rotate{to{transform:rotate(360deg);}}',
      '@media (prefers-reduced-motion:reduce){.bgp-skeleton,.bgp-spin{animation:none;}}',

      '.bgp-menu-wrap{position:relative;display:inline-flex;}',
      '.bgp-menu{position:absolute;right:0;top:calc(100% + 4px);min-width:150px;padding:4px;border:1px solid var(--bgp-line);border-radius:var(--bgp-radius-m);',
      'background:var(--bgp-menu);box-shadow:var(--bgp-shadow);z-index:40;}',
      '.bgp-menu-item{display:block;width:100%;text-align:left;border:none;background:transparent;color:var(--bgp-fg);font:inherit;font-size:13px;padding:7px 10px;border-radius:var(--bgp-radius-s);cursor:pointer;}',
      '.bgp-menu-item:hover{background:var(--bgp-hover);}',
      '.bgp-menu-item.is-danger{color:var(--bgp-err);}',

      '.bgp-backdrop{position:fixed;inset:0;background:var(--bgp-overlay);display:flex;align-items:center;justify-content:center;padding:16px;z-index:120;}',
      '.bgp-dialog{width:min(440px,100%);background:var(--bgp-card2);border:1px solid var(--bgp-line);border-radius:var(--bgp-radius-l);box-shadow:var(--dsw-shadow-lv3);padding:18px;}',
      '.bgp-dialog-title{font-size:16px;font-weight:600;margin:0 0 8px;color:var(--bgp-fg);}',
      '.bgp-dialog-text{font-size:13px;color:var(--bgp-fg2);margin:0 0 16px;white-space:pre-line;}',
      '.bgp-dialog-actions{display:flex;justify-content:flex-end;gap:8px;}',

      '.bgp-toasts{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);display:flex;flex-direction:column;gap:8px;z-index:200;pointer-events:none;}',
      '.bgp-toast{pointer-events:auto;display:flex;align-items:center;gap:8px;background:var(--bgp-toast-bg);color:var(--bgp-toast-fg);',
      'padding:8px 14px;border-radius:var(--bgp-radius-m);box-shadow:var(--bgp-shadow);font-size:13px;}',

      '.bgp-about-hero-card{padding:14px 16px 16px;}',
      '.bgp-about-topbar{display:flex;align-items:center;gap:8px;margin-bottom:10px;}',
      '.bgp-about-hero{display:flex;align-items:center;gap:14px;min-width:0;}',
      '.bgp-about-mark{display:inline-flex;align-items:center;justify-content:center;width:56px;height:56px;flex:0 0 auto;border-radius:14px;border:1px solid var(--bgp-line);background:var(--bgp-fill);}',
      '.bgp-about-identity{display:flex;flex-direction:column;gap:6px;min-width:0;}',
      '.bgp-about-name{font-size:19px;font-weight:600;line-height:1.3;margin:0;color:var(--bgp-fg);}',
      '.bgp-about-meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap;}',
      '.bgp-about-tagline{margin:12px 0 0;font-size:13px;color:var(--bgp-fg2);line-height:1.6;}',
      '.bgp-about-section{display:flex;flex-direction:column;gap:10px;}',
      '.bgp-about-section-head{display:flex;align-items:center;gap:8px;}',
      '.bgp-about-icon{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;flex:0 0 auto;border-radius:6px;background:var(--bgp-biz-bg);color:var(--bgp-biz);}',
      '.bgp-about-icon-emoji{background:transparent;font-size:17px;line-height:1;}',
      '.bgp-about-section-title{font-size:15px;font-weight:600;margin:0;color:var(--bgp-fg);}',
      '.bgp-about-lead{margin:0;padding:8px 12px;border-left:3px solid var(--bgp-brand);border-radius:0 var(--bgp-radius-s) var(--bgp-radius-s) 0;background:var(--bgp-fill);font-size:13px;font-weight:600;color:var(--bgp-fg);}',
      '.bgp-about-p{margin:0;font-size:13px;color:var(--bgp-fg2);line-height:1.75;}',
      '.bgp-about-note{display:flex;align-items:flex-start;gap:6px;margin:0;font-size:12px;color:var(--bgp-fg3);line-height:1.6;}',
      '.bgp-about-note svg{flex:0 0 auto;margin-top:2px;}',
      '.bgp-about-chips{display:flex;flex-wrap:wrap;align-items:center;gap:6px;}',
      '.bgp-about-chip-label{font-size:12px;color:var(--bgp-fg3);}',
      '.bgp-about-list{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--bgp-fg2);line-height:1.7;}',
      '.bgp-about-list li::marker{color:var(--bgp-fg3);}',
      '.bgp-about-list strong{color:var(--bgp-fg);font-weight:600;}',
      '.bgp-about-contacts{display:flex;flex-direction:column;border:1px solid var(--bgp-line);border-radius:var(--bgp-radius-m);overflow:hidden;}',
      '.bgp-about-contact{display:flex;align-items:center;gap:10px;padding:7px 10px 7px 12px;border-bottom:1px solid var(--bgp-line);}',
      '.bgp-about-contact:last-child{border-bottom:none;}',
      '.bgp-about-contact-label{flex:0 0 auto;width:66px;font-size:12px;color:var(--bgp-fg3);}',
      '.bgp-about-contact-value{flex:1 1 auto;min-width:0;font-family:var(--dsw-font-mono,ui-monospace,monospace);font-size:13px;color:var(--bgp-fg);word-break:break-all;}',
      '.bgp-about-footer{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;padding:2px 4px 0;font-size:12px;color:var(--bgp-fg3);}',
      '@media (max-width:560px){.bgp-about-hero{gap:10px;}.bgp-about-mark{width:44px;height:44px;border-radius:12px;}.bgp-about-name{font-size:17px;}}',
      '.bgp-tag-list{display:flex;flex-wrap:wrap;gap:6px;}',
      '.bgp-divider{height:1px;background:var(--bgp-sep);margin:12px 0;border:none;}',

      '.bgp-fields-row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 10px;padding:10px 12px;border:1px solid var(--bgp-line);border-radius:var(--bgp-radius-m);background:var(--bgp-bg);}',
      '.bgp-fields-row .bgp-span2{grid-column:1 / -1;}',
      '@media (max-width:720px){.bgp-fields-row{grid-template-columns:minmax(0,1fr);}}',
      '.bgp-rowlist-head{display:flex;align-items:center;gap:8px;margin:4px 0 8px;}',
      '.bgp-rowlist-head .bgp-actions-end{margin-left:auto;}',
      '.bgp-subgroup{border-top:1px solid var(--bgp-line);margin-top:12px;padding-top:12px;}',
      '.bgp-link{border:none;background:transparent;padding:0;color:var(--dsw-alias-link);font:inherit;font-size:13px;cursor:pointer;}',
      '.bgp-link:hover{text-decoration:underline;}',
      '.bgp-sticky-bar{position:sticky;bottom:0;margin:4px -16px -16px;padding:10px 16px;display:flex;align-items:center;gap:10px;',
      'border-top:1px solid var(--bgp-line);background:var(--bgp-bg);}',
      '.bgp-sticky-info{flex:1 1 auto;min-width:0;font-size:13px;color:var(--bgp-fg2);}',
    ].join('');

    function ensureStyles() {
      try {
        const existing = document.getElementById(STYLE_ID);
        if (existing && existing.parentNode) existing.parentNode.removeChild(existing);
        const style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = PANEL_CSS;
        document.head.appendChild(style);
      } catch (error) {
        /* 样式注入失败不阻塞功能 */
      }
    }

    // ────────────────────────────────────────────────────────────
    // 图标（线性、单色、currentColor）
    // ────────────────────────────────────────────────────────────

    function Svg(props) {
      return h(
        'svg',
        {
          width: props.size || 16,
          height: props.size || 16,
          viewBox: '0 0 24 24',
          fill: 'none',
          stroke: 'currentColor',
          strokeWidth: props.weight || 1.6,
          strokeLinecap: 'round',
          strokeLinejoin: 'round',
          'aria-hidden': 'true',
          focusable: 'false',
        },
        props.children,
      );
    }

    function Icon(props) {
      const name = props.name;
      switch (name) {
        case 'refresh':
          return h(Svg, { size: props.size }, h('path', { d: 'M20 11a8 8 0 1 0-2.3 6.3' }), h('path', { d: 'M20 4v7h-7' }));
        case 'plus':
          return h(Svg, { size: props.size }, h('path', { d: 'M12 5v14M5 12h14' }));
        case 'chevron':
          return h(Svg, { size: props.size }, h('path', { d: 'M9 6l6 6-6 6' }));
        case 'more':
          return h(Svg, { size: props.size }, h('circle', { cx: 12, cy: 5, r: 1.4 }), h('circle', { cx: 12, cy: 12, r: 1.4 }), h('circle', { cx: 12, cy: 19, r: 1.4 }));
        case 'copy':
          return h(Svg, { size: props.size }, h('rect', { x: 9, y: 9, width: 11, height: 11, rx: 2 }), h('path', { d: 'M5 15V6a2 2 0 0 1 2-2h9' }));
        case 'shield':
          return h(Svg, { size: props.size }, h('path', { d: 'M12 3l7 3v5c0 4.2-2.8 7.8-7 9-4.2-1.2-7-4.8-7-9V6z' }), h('path', { d: 'M9 12l2 2 4-4' }));
        case 'info':
          return h(Svg, { size: props.size }, h('circle', { cx: 12, cy: 12, r: 8.2 }), h('path', { d: 'M12 11v5' }), h('path', { d: 'M12 8h.01' }));
        case 'back':
          return h(Svg, { size: props.size }, h('path', { d: 'M15 6l-6 6 6 6' }));
        case 'check':
          return h(Svg, { size: props.size }, h('path', { d: 'M5 12l4.5 4.5L19 7' }));
        case 'alert':
          return h(Svg, { size: props.size }, h('path', { d: 'M12 4l9 16H3z' }), h('path', { d: 'M12 10v4' }), h('path', { d: 'M12 17h.01' }));
        case 'error':
          return h(Svg, { size: props.size }, h('circle', { cx: 12, cy: 12, r: 8.2 }), h('path', { d: 'M9 9l6 6M15 9l-6 6' }));
        case 'eye':
          return h(Svg, { size: props.size }, h('path', { d: 'M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z' }), h('circle', { cx: 12, cy: 12, r: 2.6 }));
        case 'eye-off':
          return h(Svg, { size: props.size }, h('path', { d: 'M3 3l18 18' }), h('path', { d: 'M9.9 5.2A9.9 9.9 0 0 1 12 5c6 0 9.5 7 9.5 7a15.6 15.6 0 0 1-3.2 3.9' }), h('path', { d: 'M6.2 6.4C3.9 8.2 2.5 12 2.5 12s3.5 7 9.5 7c1.2 0 2.3-.2 3.3-.6' }));
        case 'folder':
          return h(Svg, { size: props.size }, h('path', { d: 'M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z' }));
        case 'users':
          return h(Svg, { size: props.size }, h('circle', { cx: 9, cy: 8, r: 3.2 }), h('path', { d: 'M3.5 19a5.5 5.5 0 0 1 11 0' }), h('path', { d: 'M15.5 5.5a3 3 0 0 1 0 5.6' }), h('path', { d: 'M17 14.2a5.5 5.5 0 0 1 3.5 4.8' }));
        case 'gear':
          return h(Svg, { size: props.size }, h('circle', { cx: 12, cy: 12, r: 3.2 }), h('path', { d: 'M19.4 13.5a7.6 7.6 0 0 0 0-3l2-1.2-2-3.4-2 .9a7.6 7.6 0 0 0-2.6-1.5L14.5 3h-4l-.3 2.3A7.6 7.6 0 0 0 7.6 6.8l-2-.9-2 3.4 2 1.2a7.6 7.6 0 0 0 0 3l-2 1.2 2 3.4 2-.9a7.6 7.6 0 0 0 2.6 1.5l.3 2.3h4l.3-2.3a7.6 7.6 0 0 0 2.6-1.5l2 .9 2-3.4z' }));
        case 'trash':
          return h(Svg, { size: props.size }, h('path', { d: 'M4 7h16' }), h('path', { d: 'M9 7V4.5h6V7' }), h('path', { d: 'M6.5 7l.8 13h9.4l.8-13' }));
        case 'edit':
          return h(Svg, { size: props.size }, h('path', { d: 'M4 20h4L20 8l-4-4L4 16z' }));
        case 'close':
          return h(Svg, { size: props.size }, h('path', { d: 'M6 6l12 12M18 6L6 18' }));
        case 'open':
          return h(Svg, { size: props.size }, h('path', { d: 'M14 4h6v6' }), h('path', { d: 'M20 4l-9 9' }), h('path', { d: 'M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5' }));
        case 'book':
          return h(Svg, { size: props.size }, h('path', { d: 'M4 5.2A2.2 2.2 0 0 1 6.2 3H19v15.4H6.2A2.2 2.2 0 0 0 4 20.6z' }), h('path', { d: 'M4 5.2v15.4' }), h('path', { d: 'M8 7.5h7' }));
        case 'doc':
          return h(Svg, { size: props.size }, h('path', { d: 'M6.5 3H14l4 4v14H6.5z' }), h('path', { d: 'M14 3v4h4' }), h('path', { d: 'M9.5 12h6' }), h('path', { d: 'M9.5 16h4' }));
        default:
          return h(Svg, { size: props.size }, h('circle', { cx: 12, cy: 12, r: 8 }));
      }
    }

    // 品牌图标：来自 favicon.svg（内联多边形，不使用外部 URL）
    const BIDGENIE_LOGO_VIEWBOX = '0 0 357.32 316.82';
    const BIDGENIE_LOGO_POLYGONS = [
      { points: '343.87 316.82 236.06 316.82 289.96 269.96 343.87 316.82', fill: '#ec661a' },
      { points: '357.32 154.52 357.32 161.15 178.24 316.82 30.16 316.82 287.1 93.48 357.32 154.52', fill: '#e6bd48' },
      { points: '191.67 0 336.82 0 4.53 288.84 4.53 162.67 191.67 0', fill: '#ec661a' },
      { points: '4.53 0 5.34 0 6.27 0 133.84 0 4.53 112.41 4.53 7.08 0 0 4.53 0', fill: '#e6bd48' },
    ];

    function BidGenieIcon(props) {
      const size = props && typeof props.size === 'number' ? props.size : 18;
      return h(
        'span',
        {
          'aria-hidden': 'true',
          style: {
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: size,
            height: size,
            lineHeight: 0,
          },
        },
        h(
          'svg',
          {
            viewBox: BIDGENIE_LOGO_VIEWBOX,
            preserveAspectRatio: 'xMidYMid meet',
            width: '100%',
            height: '100%',
            focusable: 'false',
            'aria-hidden': 'true',
            style: { display: 'block', width: '100%', height: '100%' },
          },
          BIDGENIE_LOGO_POLYGONS.map((polygon, index) =>
            h('polygon', {
              key: index,
              points: polygon.points,
              fill: polygon.fill,
              fillRule: 'evenodd',
            }),
          ),
        ),
      );
    }

    // ────────────────────────────────────────────────────────────
    // 基础组件
    // ────────────────────────────────────────────────────────────

    function Button(props) {
      const variant = props.variant || 'secondary';
      const className = classNames('bgp-btn', 'bgp-btn-' + variant, props.size === 'sm' ? 'bgp-btn-sm' : '', props.className);
      return h(
        'button',
        {
          type: 'button',
          className: className,
          onClick: props.onClick,
          disabled: !!props.disabled,
          title: props.title || '',
          'aria-label': props.ariaLabel || undefined,
        },
        props.icon ? h('span', { className: 'bgp-btn-icon' }, h(Icon, { name: props.icon, size: props.size === 'sm' ? 14 : 16 })) : null,
        props.children,
      );
    }

    function IconButton(props) {
      return h(
        'button',
        {
          type: 'button',
          className: 'bgp-icon-btn',
          onClick: props.onClick,
          disabled: !!props.disabled,
          title: props.label,
          'aria-label': props.label,
        },
        h(Icon, { name: props.name, size: props.size || 16 }),
      );
    }

    function Field(props) {
      return h(
        'div',
        { className: classNames('bgp-field', props.span ? 'bgp-span2' : '', props.className) },
        h(
          'label',
          { className: 'bgp-field-label' },
          props.label,
          props.required ? h('span', { className: 'bgp-required', 'aria-hidden': 'true' }, '*') : null,
        ),
        props.children,
        props.error ? h('span', { className: 'bgp-field-error', role: 'alert' }, props.error) : null,
        !props.error && props.hint ? h('span', { className: 'bgp-field-hint' }, props.hint) : null,
      );
    }

    function TextInput(props) {
      return h('input', {
        type: props.type || 'text',
        className: classNames('bgp-input', props.invalid ? 'is-error' : ''),
        value: props.value == null ? '' : props.value,
        placeholder: props.placeholder || '',
        disabled: !!props.disabled,
        onChange: (event) => props.onChange && props.onChange(event.target.value),
        onBlur: props.onBlur,
        'aria-label': props.ariaLabel || undefined,
        inputMode: props.inputMode || undefined,
      });
    }

    function TextArea(props) {
      return h('textarea', {
        className: classNames('bgp-textarea', props.invalid ? 'is-error' : ''),
        value: props.value == null ? '' : props.value,
        placeholder: props.placeholder || '',
        rows: props.rows || 3,
        disabled: !!props.disabled,
        onChange: (event) => props.onChange && props.onChange(event.target.value),
        'aria-label': props.ariaLabel || undefined,
      });
    }

    function Select(props) {
      return h(
        'select',
        {
          className: classNames('bgp-select', props.invalid ? 'is-error' : ''),
          value: props.value == null ? '' : String(props.value),
          disabled: !!props.disabled,
          onChange: (event) => props.onChange && props.onChange(event.target.value),
          'aria-label': props.ariaLabel || undefined,
        },
        (props.options || []).map((option) =>
          h('option', { key: String(option.value), value: String(option.value) }, option.label),
        ),
      );
    }

    function Switch(props) {
      return h(
        'label',
        { className: 'bgp-switch' },
        h('input', {
          type: 'checkbox',
          checked: !!props.checked,
          disabled: !!props.disabled,
          onChange: (event) => props.onChange && props.onChange(event.target.checked),
          'aria-label': props.ariaLabel || props.label || undefined,
        }),
        h('span', { className: 'bgp-switch-track', 'aria-hidden': 'true' }, h('span', { className: 'bgp-switch-thumb' })),
        props.label ? h('span', { className: 'bgp-switch-label' }, props.label) : null,
      );
    }

    function Chip(props) {
      return h('span', { className: classNames('bgp-chip', props.tone ? 'tone-' + props.tone : '') }, props.children);
    }

    function Banner(props) {
      const tone = props.tone || 'info';
      const iconName = tone === 'ok' ? 'check' : tone === 'warn' ? 'alert' : tone === 'error' ? 'error' : 'info';
      return h(
        'div',
        { className: 'bgp-banner tone-' + tone, role: tone === 'error' ? 'alert' : 'status' },
        h('span', { className: 'bgp-banner-icon' }, h(Icon, { name: iconName, size: 16 })),
        h(
          'div',
          { className: 'bgp-banner-body' },
          props.title ? h('div', { className: 'bgp-banner-title' }, props.title) : null,
          props.text ? h('div', { className: 'bgp-banner-text' }, props.text) : null,
          props.children,
        ),
        props.actions ? h('div', { className: 'bgp-actions' }, props.actions) : null,
      );
    }

    function EmptyState(props) {
      return h(
        'div',
        { className: 'bgp-empty' },
        h('span', { className: 'bgp-empty-mark' }, props.icon ? h(Icon, { name: props.icon, size: 40 }) : h(Icon, { name: 'folder', size: 40 })),
        h('div', { className: 'bgp-empty-title' }, props.title),
        props.text ? h('div', { className: 'bgp-empty-text' }, props.text) : null,
        props.actions ? h('div', { className: 'bgp-actions' }, props.actions) : null,
      );
    }

    function Skeleton(props) {
      const rows = props.rows || 3;
      const items = [];
      for (let i = 0; i < rows; i += 1) {
        items.push(h('div', { key: i, className: 'bgp-skeleton', style: { height: i === 0 ? 18 : 14, width: i === 0 ? '52%' : '100%', marginBottom: 8 } }));
      }
      return h('div', { className: 'bgp-card' }, items);
    }

    function Spinner(props) {
      return h(
        'span',
        { className: 'bgp-spin', style: { display: 'inline-flex' }, 'aria-hidden': 'true' },
        h(Svg, { size: props.size || 16 }, h('path', { d: 'M12 3a9 9 0 1 0 9 9' })),
      );
    }

    function ErrorState(props) {
      const info = describeError(props.error);
      return h(
        'div',
        { className: 'bgp-card' },
        h(Banner, {
          tone: 'error',
          title: props.title || '没有读取成功',
          text: info.message + (info.hint ? '（' + info.hint + '）' : ''),
        }),
        h(
          'div',
          { className: 'bgp-actions', style: { marginTop: 12 } },
          h(Button, { variant: 'secondary', onClick: props.onRetry, icon: 'refresh' }, '重试'),
          props.extraActions || null,
        ),
      );
    }

    function ToastHost(props) {
      if (!props.toasts || props.toasts.length === 0) return null;
      return h(
        'div',
        { className: 'bgp-toasts', 'aria-live': 'polite' },
        props.toasts.map((toast) =>
          h('div', { key: toast.id, className: 'bgp-toast' }, h(Icon, { name: toast.kind === 'error' ? 'error' : toast.kind === 'success' ? 'check' : 'info', size: 14 }), h('span', null, toast.text)),
        ),
      );
    }

    function Dialog(props) {
      useEffect(
        () => {
          if (!props.open) return undefined;
          const onKeyDown = (event) => {
            if (event.key === 'Escape') {
              if (event.preventDefault) event.preventDefault();
              if (props.onCancel) props.onCancel();
            }
          };
          document.addEventListener('keydown', onKeyDown);
          return () => document.removeEventListener('keydown', onKeyDown);
        },
        [props.open, props.onCancel],
      );
      if (!props.open) return null;
      return h(
        'div',
        { className: 'bgp-backdrop', role: 'dialog', 'aria-modal': 'true', 'aria-label': props.title },
        h(
          'div',
          { className: 'bgp-dialog' },
          h('h2', { className: 'bgp-dialog-title' }, props.title),
          props.text ? h('p', { className: 'bgp-dialog-text' }, props.text) : null,
          h(
            'div',
            { className: 'bgp-dialog-actions' },
            h(Button, { variant: 'text', onClick: props.onCancel }, props.cancelLabel || '取消'),
            h(Button, { variant: props.danger ? 'danger-solid' : 'primary', onClick: props.onConfirm }, props.confirmLabel || '确认'),
          ),
        ),
      );
    }

    function PageHeader(props) {
      return h(
        'div',
        { className: 'bgp-card' },
        h(
          'div',
          { className: 'bgp-card-head', style: { marginBottom: 6 } },
          h('h1', { className: 'bgp-h1' }, props.title),
          props.actions ? h('div', { className: 'bgp-actions-end' }, props.actions) : null,
        ),
        props.description ? h('p', { className: 'bgp-desc' }, props.description) : null,
        props.next ? h('div', { style: { marginTop: 10 } }, h(NextStep, { text: props.next, action: props.nextAction })) : null,
      );
    }

    function NextStep(props) {
      return h(
        'div',
        { className: 'bgp-next' },
        h('span', { className: 'bgp-next-tag' }, '下一步'),
        h('div', { className: 'bgp-next-text' }, props.text),
        props.action ? h('div', { className: 'bgp-actions' }, props.action) : null,
      );
    }

    function KeyValue(props) {
      return h(
        'div',
        { className: 'bgp-kv' },
        h('div', { className: 'bgp-kv-key' }, props.label),
        h('div', { className: 'bgp-kv-val' }, props.children),
      );
    }

    function ProgressDots(props) {
      const step = props.step || 0;
      const aborted = !!props.aborted;
      const done = !!props.done;
      const dots = [];
      for (let i = 1; i <= 7; i += 1) {
        let state = '';
        if (aborted) state = 'is-aborted';
        else if (done || i < step) state = 'is-done';
        else if (i === step) state = 'is-current';
        dots.push(h('span', { key: i, className: 'bgp-dot ' + state }));
      }
      return h(
        'div',
        { className: 'bgp-dots', role: 'img', 'aria-label': '项目进度 ' + Math.min(Math.max(step, 0), 7) + '/7' },
        dots,
        h('span', { className: 'bgp-dots-label' }, aborted ? '已中止' : done ? '已完成' : '第 ' + Math.max(step, 1) + ' 步 / 共 7 步'),
      );
    }

    function MenuButton(props) {
      const [open, setOpen] = useState(false);
      const wrapRef = useRef(null);
      useEffect(() => {
        if (!open) return undefined;
        const onDocClick = (event) => {
          if (wrapRef.current && !wrapRef.current.contains(event.target)) setOpen(false);
        };
        const onKey = (event) => {
          if (event.key === 'Escape') setOpen(false);
        };
        document.addEventListener('mousedown', onDocClick);
        document.addEventListener('keydown', onKey);
        return () => {
          document.removeEventListener('mousedown', onDocClick);
          document.removeEventListener('keydown', onKey);
        };
      }, [open]);
      return h(
        'span',
        { className: 'bgp-menu-wrap', ref: wrapRef },
        h(IconButton, { name: 'more', label: props.label || '更多操作', onClick: (event) => { event.stopPropagation(); setOpen(!open); } }),
        open
          ? h(
              'div',
              { className: 'bgp-menu', role: 'menu' },
              (props.items || []).map((item) =>
                h(
                  'button',
                  {
                    type: 'button',
                    key: item.key,
                    role: 'menuitem',
                    className: classNames('bgp-menu-item', item.danger ? 'is-danger' : ''),
                    onClick: (event) => {
                      event.stopPropagation();
                      setOpen(false);
                      if (item.onClick) item.onClick();
                    },
                  },
                  item.label,
                ),
              ),
            )
          : null,
      );
    }

    // ────────────────────────────────────────────────────────────
    // 附加样式（设置行、分区 tab、明细行）
    // ────────────────────────────────────────────────────────────

    const EXTRA_STYLE_ID = 'dsh-bidgenie-panel-styles-extra';
    const EXTRA_CSS = [
      '.bgp-sec-tabs{display:flex;gap:6px;flex-wrap:wrap;}',
      '.bgp-sec-tab{border:1px solid var(--bgp-line2);background:transparent;color:var(--bgp-fg2);border-radius:999px;padding:4px 12px;font:inherit;font-size:13px;cursor:pointer;}',
      '.bgp-sec-tab:hover{background:var(--bgp-hover);color:var(--bgp-fg);}',
      '.bgp-sec-tab.is-active{background:var(--bgp-fill);color:var(--bgp-fg);border-color:var(--bgp-brand);}',
      '.bgp-setting-row{display:flex;gap:16px;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--bgp-line);}',
      '.bgp-setting-row:last-child{border-bottom:none;}',
      '.bgp-setting-main{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;gap:2px;}',
      '.bgp-setting-label{font-size:14px;color:var(--bgp-fg);}',
      '.bgp-setting-hint{font-size:12px;color:var(--bgp-fg3);}',
      '.bgp-setting-default{font-size:12px;color:var(--bgp-fg4);}',
      '.bgp-setting-control{flex:0 0 auto;display:flex;flex-direction:column;align-items:flex-end;gap:4px;min-width:150px;}',
      '.bgp-setting-status{font-size:12px;color:var(--bgp-fg3);}',
      '.bgp-setting-status.is-error{color:var(--bgp-err);}',
      '.bgp-setting-status.is-ok{color:var(--bgp-ok);}',
      '.bgp-group-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 4px;}',
      '.bgp-group-head .bgp-card-title{flex:1 1 auto;}',
      '.bgp-inline-input{width:104px;}',
      '.bgp-inline-input-wide{width:132px;}',
      '.bgp-quota-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 10px;min-width:260px;}',
      '.bgp-loader{display:flex;justify-content:center;align-items:center;gap:8px;padding:26px;color:var(--bgp-fg3);font-size:13px;}',
      '.bgp-hint-list{margin:0;padding-left:18px;font-size:13px;color:var(--bgp-fg2);display:flex;flex-direction:column;gap:2px;}',
      '.bgp-detail-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}',
      '.bgp-row.is-static{cursor:default;}',
      '.bgp-row.is-static:hover{background:transparent;}',
      '.bgp-badge-current{display:inline-flex;align-items:center;gap:4px;}',
      '.bgp-counts{display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--bgp-fg3);}',
      '.bgp-mono{font-family:var(--dsw-font-mono,ui-monospace,monospace);font-size:12px;}',
      '.bgp-env-group-title{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--bgp-fg2);padding:2px 0 8px;}',
      '.bgp-env-item{display:flex;flex-direction:column;border-bottom:1px solid var(--bgp-line);}',
      '.bgp-env-item:last-child{border-bottom:none;}',
      '.bgp-env-row{border-bottom:none;padding:8px 2px;}',
      '.bgp-env-help{margin:0 2px 10px;padding:8px 10px;border-radius:var(--bgp-radius-s);background:var(--bgp-fill);display:flex;flex-direction:column;gap:4px;align-items:flex-start;}',
      '.bgp-env-help-label{font-size:12px;font-weight:600;color:var(--bgp-fg2);}',
      '.bgp-env-help-text{font-size:13px;color:var(--bgp-fg);word-break:break-word;line-height:1.5;}',
      '.bgp-env-help-note{font-size:12px;color:var(--bgp-fg3);}',
      '.bgp-env-progress{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--bgp-fg2);flex-wrap:wrap;}',
      '.bgp-style-warnings{display:flex;flex-direction:column;gap:4px;margin-top:4px;}',
      '.bgp-style-warning{display:flex;gap:8px;flex-wrap:wrap;font-size:12px;}',
      '.bgp-style-warning-file{font-family:var(--dsw-font-mono,ui-monospace,monospace);color:var(--bgp-fg);word-break:break-all;}',
      '.bgp-style-warning-msg{color:var(--bgp-fg2);}',
      '.bgp-style-example{margin:8px 0 0;padding:8px 10px;max-height:240px;overflow:auto;background:var(--bgp-fill);border:1px solid var(--bgp-line);',
      'border-radius:var(--bgp-radius-s);font-family:var(--dsw-font-mono,ui-monospace,monospace);font-size:12px;line-height:1.5;color:var(--bgp-fg);white-space:pre;}',
      '@media (max-width:720px){.bgp-setting-row{flex-direction:column;gap:6px;} .bgp-setting-control{align-items:flex-start;min-width:0;} .bgp-quota-grid{grid-template-columns:repeat(2,minmax(0,1fr));min-width:0;width:100%;}}',
    ].join('');

    function ensureExtraStyles() {
      try {
        const existing = document.getElementById(EXTRA_STYLE_ID);
        if (existing && existing.parentNode) existing.parentNode.removeChild(existing);
        const style = document.createElement('style');
        style.id = EXTRA_STYLE_ID;
        style.textContent = EXTRA_CSS;
        document.head.appendChild(style);
      } catch (error) {
        /* 样式注入失败不阻塞功能 */
      }
    }

    // ────────────────────────────────────────────────────────────
    // 数据模型：工作区 / 项目 / 投标人 / 明细
    // ────────────────────────────────────────────────────────────

    function normalizeWorkspaces(data) {
      return asArray(data && data.workspaces)
        .map((item) => ({
          id: textOf(item && (item.id || item.path)),
          path: trimText(item && (item.path || item.root || item.cwd)),
          title: trimText(item && item.title),
        }))
        .filter((item) => item.path);
    }

    function normalizeProject(item) {
      const source = item || {};
      return {
        projectId: textOf(source.projectId),
        dirName: textOf(source.dirName),
        projectName: trimText(source.projectName) || trimText(source.dirName) || '未命名项目',
        projectNo: trimText(source.projectNo),
        currentStage: textOf(source.currentStage),
        currentPackage: textOf(source.currentPackage),
        isSubcontracted: typeof source.isSubcontracted === 'boolean' ? source.isSubcontracted : null,
        packageCount: typeof source.packageCount === 'number' ? source.packageCount : null,
        packagesToWrite: asArray(source.packagesToWrite).map(textOf).filter(Boolean),
        updatedAt: textOf(source.updatedAt || source.createdAt),
      };
    }

    function normalizeProjects(data) {
      return asArray(data && data.projects).map(normalizeProject);
    }

    function normalizeBidderItem(item) {
      const source = item || {};
      const completeness = source.completeness;
      let ratio = null;
      if (typeof completeness === 'number') ratio = completeness;
      else if (isPlainObject(completeness) && typeof completeness.overall === 'number') ratio = completeness.overall;
      const counts = isPlainObject(source.counts) ? source.counts : null;
      return {
        bidderId: textOf(source.bidderId),
        displayName: trimText(source.displayName) || '未命名资料',
        bidderType: textOf(source.bidderType),
        updatedAt: textOf(source.updatedAt),
        isCurrent: !!source.isCurrent,
        completeness: ratio,
        counts: counts,
        raw: source,
      };
    }

    function normalizeBidderList(data) {
      return asArray(data && data.bidders).map(normalizeBidderItem).filter((item) => item.bidderId);
    }

    // 详情读取：Host 形态 {ok,bidder,certificates,performance,awardsPenalties}
    function pickBidderDetail(data) {
      if (!isPlainObject(data)) return null;
      const bidder = isPlainObject(data.bidder)
        ? data.bidder
        : isPlainObject(data.item) && isPlainObject(data.item.bidder)
          ? data.item.bidder
          : Array.isArray(data.bidders) && data.bidders.length === 1 && isPlainObject(data.bidders[0].bidder)
            ? data.bidders[0].bidder
            : null;
      if (!bidder) return null;
      return {
        bidder: bidder,
        certificates: isPlainObject(data.certificates) ? data.certificates : isPlainObject(bidder.certificates) ? bidder.certificates : null,
        performance: isPlainObject(data.performance) ? data.performance : isPlainObject(bidder.performance) ? bidder.performance : null,
        awardsPenalties: isPlainObject(data.awardsPenalties) ? data.awardsPenalties : isPlainObject(data.awards_penalties) ? data.awards_penalties : null,
        maskedFields: asArray(data.maskedFields),
      };
    }

    // ── 基本信息表单模型（扁平化，便于受控输入） ──

    const BASIC_FIELDS = [
      { key: 'displayName', path: ['displayName'], label: '名称', kind: 'text', required: true, placeholder: '公司名称或个人姓名', hint: '列表和方案里显示的名字', group: 'identity' },
      { key: 'bidderType', path: ['bidderType'], label: '类型', kind: 'select', required: true, options: BIDDER_TYPES, hint: '法人（公司）或自然人（个人）', group: 'identity' },
      { key: 'legalName', path: ['legalName'], label: '公司全称 / 真实姓名', kind: 'text', placeholder: '与营业执照或个人证件一致', group: 'identity' },
      { key: 'unifiedSocialCreditCode', path: ['unifiedSocialCreditCode'], label: '统一社会信用代码', kind: 'text', sensitive: true, placeholder: '18 位代码', group: 'identity' },
      { key: 'legalRepresentative', path: ['legalRepresentative'], label: '法定代表人', kind: 'text', sensitive: true, onlyFor: 'legal_entity', group: 'identity' },
      { key: 'idType', path: ['idType'], label: '证件类型', kind: 'text', placeholder: '如：居民身份证', onlyFor: 'natural_person', group: 'identity' },
      { key: 'idNumber', path: ['idNumber'], label: '证件号码', kind: 'text', sensitive: true, onlyFor: 'natural_person', group: 'identity' },
      { key: 'businessScope', path: ['businessScope'], label: '经营范围', kind: 'textarea', span: true, group: 'identity' },
      { key: 'contactName', path: ['contact', 'name'], label: '联系人', kind: 'text', group: 'contact' },
      { key: 'contactTitle', path: ['contact', 'title'], label: '联系人职务', kind: 'text', group: 'contact' },
      { key: 'contactPhone', path: ['contact', 'phone'], label: '联系电话', kind: 'text', sensitive: true, inputMode: 'tel', group: 'contact' },
      { key: 'contactEmail', path: ['contact', 'email'], label: '电子邮箱', kind: 'text', inputMode: 'email', group: 'contact' },
      { key: 'registeredAddress', path: ['registeredAddress'], label: '注册地址', kind: 'text', span: true, group: 'address' },
      { key: 'officeAddress', path: ['officeAddress'], label: '办公地址', kind: 'text', span: true, group: 'address' },
      { key: 'capital', path: ['capital'], label: '注册资本', kind: 'text', placeholder: '如：5000万元', group: 'finance' },
      { key: 'establishedDate', path: ['establishedDate'], label: '成立日期', kind: 'date', group: 'finance' },
      { key: 'bankAccountName', path: ['bank', 'accountName'], label: '开户名称', kind: 'text', onlyFor: 'legal_entity', group: 'finance' },
      { key: 'bankName', path: ['bank', 'bankName'], label: '开户行', kind: 'text', onlyFor: 'legal_entity', group: 'finance' },
      { key: 'bankAccountNo', path: ['bank', 'accountNo'], label: '银行账号', kind: 'text', sensitive: true, onlyFor: 'legal_entity', group: 'finance' },
      { key: 'writable', path: ['writable'], label: '允许写进方案', kind: 'switch', hint: '关闭后，方案不会引用这家公司或个人的资料', group: 'usage' },
      { key: 'tagsText', path: ['tags'], label: '标签', kind: 'tags', span: true, hint: '用逗号分隔，例如：系统集成，运维服务', group: 'usage' },
    ];

    const BASIC_GROUPS = [
      { key: 'identity', title: '主体信息', desc: '名称、类型、证照与经营范围' },
      { key: 'contact', title: '联系人', desc: '日常对接与方案沟通' },
      { key: 'address', title: '地址', desc: '注册地址与办公地址' },
      { key: 'finance', title: '资本与银行', desc: '法人主体适用；自然人可留空' },
      { key: 'usage', title: '方案引用', desc: '控制资料是否进入方案及标签' },
    ];

    function splitTags(value) {
      return textOf(value)
        .split(/[,，、;；]+/)
        .map((item) => item.trim())
        .filter(Boolean);
    }

    function emptyBidderForm() {
      return {
        displayName: '',
        bidderType: 'legal_entity',
        legalName: '',
        unifiedSocialCreditCode: '',
        legalRepresentative: '',
        idType: '',
        idNumber: '',
        contactName: '',
        contactTitle: '',
        contactPhone: '',
        contactEmail: '',
        registeredAddress: '',
        officeAddress: '',
        bankAccountName: '',
        bankName: '',
        bankAccountNo: '',
        capital: '',
        establishedDate: '',
        businessScope: '',
        writable: true,
        tagsText: '',
      };
    }

    function toDateText(value) {
      const text = trimText(value);
      if (!text) return '';
      const match = /^(\d{4}-\d{2}-\d{2})/.exec(text);
      return match ? match[1] : text.slice(0, 10);
    }

    function bidderToForm(bidder) {
      const source = bidder || {};
      const contact = isPlainObject(source.contact) ? source.contact : {};
      const bank = isPlainObject(source.bank) ? source.bank : {};
      return {
        displayName: textOf(source.displayName),
        bidderType: source.bidderType === 'natural_person' ? 'natural_person' : 'legal_entity',
        legalName: textOf(source.legalName),
        unifiedSocialCreditCode: textOf(source.unifiedSocialCreditCode),
        legalRepresentative: textOf(source.legalRepresentative),
        idType: textOf(source.idType),
        idNumber: textOf(source.idNumber),
        contactName: textOf(contact.name),
        contactTitle: textOf(contact.title),
        contactPhone: textOf(contact.phone),
        contactEmail: textOf(contact.email),
        registeredAddress: textOf(source.registeredAddress),
        officeAddress: textOf(source.officeAddress),
        bankAccountName: textOf(bank.accountName),
        bankName: textOf(bank.bankName),
        bankAccountNo: textOf(bank.accountNo),
        capital: textOf(source.capital),
        establishedDate: toDateText(source.establishedDate),
        businessScope: textOf(source.businessScope),
        writable: source.writable !== false,
        tagsText: asArray(source.tags).map(textOf).join('，'),
      };
    }

    function formToBidder(form) {
      return {
        schemaVersion: '1.0',
        bidderType: form.bidderType === 'natural_person' ? 'natural_person' : 'legal_entity',
        displayName: trimText(form.displayName),
        legalName: trimText(form.legalName),
        unifiedSocialCreditCode: trimText(form.unifiedSocialCreditCode),
        legalRepresentative: trimText(form.legalRepresentative),
        idType: trimText(form.idType),
        idNumber: trimText(form.idNumber),
        registeredAddress: trimText(form.registeredAddress),
        officeAddress: trimText(form.officeAddress),
        contact: {
          name: trimText(form.contactName),
          title: trimText(form.contactTitle),
          phone: trimText(form.contactPhone),
          email: trimText(form.contactEmail),
        },
        bank: {
          accountName: trimText(form.bankAccountName),
          bankName: trimText(form.bankName),
          accountNo: trimText(form.bankAccountNo),
        },
        capital: trimText(form.capital),
        establishedDate: trimText(form.establishedDate),
        businessScope: trimText(form.businessScope),
        writable: form.writable !== false,
        tags: splitTags(form.tagsText),
      };
    }

    // 只提交用户改过的字段（避免把脱敏值写回）
    function buildBidderPatch(initialForm, form) {
      const base = formToBidder(initialForm);
      const next = formToBidder(form);
      const patch = {};
      const touchedGroups = {};
      BASIC_FIELDS.forEach((field) => {
        const path = field.path;
        const before = getPath(base, path);
        const after = getPath(next, path);
        const changed = Array.isArray(before) || Array.isArray(after)
          ? JSON.stringify(asArray(before)) !== JSON.stringify(asArray(after))
          : before !== after;
        if (!changed) return;
        if (path.length > 1) touchedGroups[path[0]] = true;
        else patch[path[0]] = after;
      });
      Object.keys(touchedGroups).forEach((group) => {
        patch[group] = next[group];
      });
      return patch;
    }

    // ── 明细行模型 ──

    let rowSeq = 0;
    function nextRowKey(prefix) {
      rowSeq += 1;
      return (prefix || 'row') + '-' + rowSeq;
    }

    const CERT_FIELDS = [
      { key: 'name', label: '证书名称', kind: 'text', required: true },
      { key: 'category', label: '类别', kind: 'select', options: CERT_CATEGORIES },
      { key: 'level', label: '等级', kind: 'text', placeholder: '如：二级、甲级' },
      { key: 'issuer', label: '发证机构', kind: 'text' },
      { key: 'certificateNo', label: '证书编号', kind: 'text' },
      { key: 'validFrom', label: '开始日期', kind: 'date' },
      { key: 'validTo', label: '结束日期', kind: 'date' },
      { key: 'holderKind', label: '持有人', kind: 'select', options: [{ value: 'company', label: '公司' }, { value: 'person', label: '人员' }] },
      { key: 'holderName', label: '人员姓名', kind: 'text', showWhen: { key: 'holderKind', equals: 'person' } },
      { key: 'requiredForText', label: '适用项目类型', kind: 'text', placeholder: '如：工程类，服务类' },
      { key: 'notes', label: '备注', kind: 'text', span: true },
    ];

    const PERF_FIELDS = [
      { key: 'projectName', label: '项目名称', kind: 'text', required: true },
      { key: 'clientName', label: '客户名称', kind: 'text' },
      { key: 'contractAmount', label: '合同金额', kind: 'text', placeholder: '如：1280万元' },
      { key: 'role', label: '承担角色', kind: 'select', options: PERFORMANCE_ROLES },
      { key: 'periodStart', label: '开始时间', kind: 'date' },
      { key: 'periodEnd', label: '结束时间', kind: 'date' },
      { key: 'scope', label: '服务内容', kind: 'text', span: true },
      { key: 'notes', label: '备注', kind: 'text', span: true },
    ];

    const AWARD_FIELDS = [
      { key: 'name', label: '奖项名称', kind: 'text', required: true },
      { key: 'issuer', label: '发证单位', kind: 'text' },
      { key: 'awardedAt', label: '获奖时间', kind: 'date' },
      { key: 'level', label: '级别', kind: 'select', options: AWARD_LEVELS },
      { key: 'description', label: '说明', kind: 'text', span: true },
    ];

    const PENALTY_FIELDS = [
      { key: 'type', label: '类型', kind: 'select', options: PENALTY_TYPES },
      { key: 'status', label: '状态', kind: 'select', options: PENALTY_STATUS },
      { key: 'authority', label: '处罚机关', kind: 'text' },
      { key: 'occurredAt', label: '发生时间', kind: 'date' },
      { key: 'description', label: '说明', kind: 'text', span: true },
    ];

    function makeRow(fields, prefix) {
      const row = { __key: nextRowKey(prefix) };
      fields.forEach((field) => {
        if (field.kind === 'select') row[field.key] = field.options && field.options.length ? field.options[0].value : '';
        else row[field.key] = '';
      });
      return row;
    }

    function certificatesToRows(certificates) {
      return asArray(certificates && certificates.items).map((item) => {
        const source = item || {};
        const holder = textOf(source.holder);
        const isPerson = holder.indexOf('person:') === 0;
        return {
          __key: nextRowKey('cert'),
          certificateId: textOf(source.certificateId),
          name: textOf(source.name),
          category: textOf(source.category) || 'qualification',
          level: textOf(source.level),
          issuer: textOf(source.issuer),
          certificateNo: textOf(source.certificateNo),
          validFrom: toDateText(source.validFrom),
          validTo: toDateText(source.validTo),
          holderKind: isPerson ? 'person' : 'company',
          holderName: isPerson ? holder.slice('person:'.length) : '',
          requiredForText: asArray(source.requiredFor).map(textOf).join('，'),
          attachmentRef: textOf(source.attachmentRef),
          notes: textOf(source.notes),
          status: textOf(source.status),
        };
      });
    }

    function rowsToCertificates(rows) {
      return {
        schemaVersion: '1.0',
        items: rows.map((row) => {
          const item = {
            certificateId: row.certificateId || undefined,
            name: trimText(row.name),
            category: row.category || 'qualification',
            level: trimText(row.level),
            issuer: trimText(row.issuer),
            certificateNo: trimText(row.certificateNo),
            validFrom: trimText(row.validFrom),
            validTo: trimText(row.validTo),
            holder: row.holderKind === 'person' ? 'person:' + trimText(row.holderName) : 'company',
            requiredFor: splitTags(row.requiredForText),
            attachmentRef: trimText(row.attachmentRef),
            notes: trimText(row.notes),
            status: row.status || undefined,
          };
          if (!item.certificateId) delete item.certificateId;
          if (!item.status) delete item.status;
          return item;
        }),
      };
    }

    function performanceToRows(performance) {
      return asArray(performance && performance.items).map((item) => {
        const source = item || {};
        const period = isPlainObject(source.period) ? source.period : {};
        return {
          __key: nextRowKey('perf'),
          performanceId: textOf(source.performanceId),
          projectName: textOf(source.projectName),
          clientName: textOf(source.clientName),
          contractAmount: textOf(source.contractAmount),
          role: textOf(source.role) || 'main',
          periodStart: toDateText(period.start),
          periodEnd: toDateText(period.end),
          scope: textOf(source.scope),
          attachmentRef: textOf(source.attachmentRef),
          notes: textOf(source.notes),
        };
      });
    }

    function rowsToPerformance(rows) {
      return {
        schemaVersion: '1.0',
        items: rows.map((row) => {
          const item = {
            performanceId: row.performanceId || undefined,
            projectName: trimText(row.projectName),
            clientName: trimText(row.clientName),
            contractAmount: trimText(row.contractAmount),
            period: { start: trimText(row.periodStart), end: trimText(row.periodEnd) },
            role: row.role || 'main',
            scope: trimText(row.scope),
            attachmentRef: trimText(row.attachmentRef),
            notes: trimText(row.notes),
          };
          if (!item.performanceId) delete item.performanceId;
          return item;
        }),
      };
    }

    function awardsToRows(awardsPenalties) {
      return {
        awards: asArray(awardsPenalties && awardsPenalties.awards).map((item) => ({
          __key: nextRowKey('award'),
          awardId: textOf(item && item.awardId),
          name: textOf(item && item.name),
          issuer: textOf(item && item.issuer),
          awardedAt: toDateText(item && item.awardedAt),
          level: textOf(item && item.level) || 'other',
          description: textOf(item && item.description),
          attachmentRef: textOf(item && item.attachmentRef),
        })),
        penalties: asArray(awardsPenalties && awardsPenalties.penalties).map((item) => ({
          __key: nextRowKey('penalty'),
          penaltyId: textOf(item && item.penaltyId),
          type: textOf(item && item.type) || 'administrative_penalty',
          status: textOf(item && item.status) || 'unknown',
          authority: textOf(item && item.authority),
          occurredAt: toDateText(item && item.occurredAt),
          description: textOf(item && item.description),
          attachmentRef: textOf(item && item.attachmentRef),
        })),
      };
    }

    function rowsToAwardsPenalties(awards, penalties) {
      return {
        awards: awards.map((row) => {
          const item = {
            awardId: row.awardId || undefined,
            name: trimText(row.name),
            issuer: trimText(row.issuer),
            awardedAt: trimText(row.awardedAt),
            level: row.level || 'other',
            description: trimText(row.description),
            attachmentRef: trimText(row.attachmentRef),
          };
          if (!item.awardId) delete item.awardId;
          return item;
        }),
        penalties: penalties.map((row) => {
          const item = {
            penaltyId: row.penaltyId || undefined,
            type: row.type || 'other',
            description: trimText(row.description),
            authority: trimText(row.authority),
            occurredAt: trimText(row.occurredAt),
            status: row.status || 'unknown',
            attachmentRef: trimText(row.attachmentRef),
          };
          if (!item.penaltyId) delete item.penaltyId;
          return item;
        }),
      };
    }

    // ── 通用明细行列表 ──

    function fieldVisible(field, row) {
      if (!field.showWhen) return true;
      return row[field.showWhen.key] === field.showWhen.equals;
    }

    function RowCard(props) {
      const fields = (props.fields || []).filter((field) => fieldVisible(field, props.row));
      const titleField = fields.filter((field) => field.required)[0] || fields[0];
      const titleValue = titleField ? trimText(props.row[titleField.key]) : '';
      return h(
        'div',
        { className: 'bgp-fields-row', style: { marginBottom: 8 } },
        h(
          'div',
          { className: 'bgp-span2', style: { display: 'flex', alignItems: 'center', gap: 8 } },
          h('span', { className: 'bgp-small', style: { fontWeight: 600, color: 'var(--bgp-fg)' } }, '第 ' + (props.index + 1) + ' 项'),
          titleValue ? h('span', { className: 'bgp-muted' }, '· ' + titleValue) : null,
          props.statusChip ? h('span', { style: { marginLeft: 'auto' } }, props.statusChip) : h('span', { style: { marginLeft: 'auto' } }),
          h(Button, { size: 'sm', variant: 'text', onClick: props.onToggle }, props.expanded ? '收起' : '展开'),
          h(Button, { size: 'sm', variant: 'text', onClick: props.onRemove }, '删除'),
        ),
        props.expanded
          ? fields.map((field) =>
              h(
                Field,
                {
                  key: field.key,
                  label: field.label,
                  required: field.required,
                  span: field.span,
                  hint: field.hint,
                },
                field.kind === 'select'
                  ? h(Select, {
                      value: props.row[field.key],
                      options: field.options,
                      onChange: (value) => props.onChange(field.key, value),
                    })
                  : field.kind === 'date'
                    ? h(TextInput, {
                        type: 'date',
                        value: props.row[field.key],
                        onChange: (value) => props.onChange(field.key, value),
                      })
                    : field.kind === 'textarea'
                      ? h(TextArea, { value: props.row[field.key], onChange: (value) => props.onChange(field.key, value) })
                      : h(TextInput, {
                          value: props.row[field.key],
                          placeholder: field.placeholder,
                          onChange: (value) => props.onChange(field.key, value),
                        }),
              ),
            )
          : null,
      );
    }

    function RowList(props) {
      const rows = asArray(props.rows);
      const fields = props.fields;
      const [expandedKeys, setExpandedKeys] = useState([]);
      const isExpanded = (key) => expandedKeys.indexOf(key) >= 0;
      const toggleRow = (key) => {
        setExpandedKeys((prev) => (prev.indexOf(key) >= 0 ? prev.filter((item) => item !== key) : prev.concat([key])));
      };
      const addRow = () => {
        const row = makeRow(fields, props.prefix);
        if (props.createRow) props.createRow(row);
        setExpandedKeys(row.__key ? [row.__key] : []);
        props.onChange(rows.concat([row]));
      };
      return h(
        'div',
        null,
        h(
          'div',
          { className: 'bgp-rowlist-head' },
          h('span', { className: 'bgp-section-title', style: { fontSize: 14 } }, props.title),
          rows.length ? h('span', { className: 'bgp-muted' }, '共 ' + rows.length + ' 项') : null,
          h(
            'div',
            { className: 'bgp-actions-end' },
            h(Button, { size: 'sm', variant: 'secondary', icon: 'plus', onClick: addRow }, props.addLabel || '添加'),
          ),
        ),
        props.hint ? h('p', { className: 'bgp-muted', style: { marginBottom: 10 } }, props.hint) : null,
        rows.length === 0
          ? h('p', { className: 'bgp-muted', style: { padding: '10px 0' } }, props.emptyText || '还没有内容')
          : rows.map((row, index) =>
              h(RowCard, {
                key: row.__key,
                index: index,
                row: row,
                fields: fields,
                statusChip: props.renderStatus ? props.renderStatus(row) : null,
                expanded: isExpanded(row.__key),
                onToggle: () => toggleRow(row.__key),
                onChange: (fieldKey, value) => {
                  const next = rows.slice();
                  const updated = Object.assign({}, row);
                  updated[fieldKey] = value;
                  next[index] = updated;
                  props.onChange(next);
                },
                onRemove: () => {
                  setExpandedKeys((prev) => prev.filter((item) => item !== row.__key));
                  const next = rows.slice();
                  next.splice(index, 1);
                  props.onChange(next);
                },
              }),
            ),
      );
    }

    // ── 设置行 / 分区 tab / 脱敏文本 ──

    function SettingsRow(props) {
      return h(
        'div',
        { className: 'bgp-setting-row' },
        h(
          'div',
          { className: 'bgp-setting-main' },
          h('span', { className: 'bgp-setting-label' }, props.label),
          props.hint ? h('span', { className: 'bgp-setting-hint' }, props.hint) : null,
          props.defaultText ? h('span', { className: 'bgp-setting-default' }, '默认：' + props.defaultText) : null,
        ),
        h(
          'div',
          { className: 'bgp-setting-control' },
          props.children,
          props.status ? h('span', { className: classNames('bgp-setting-status', props.statusTone === 'error' ? 'is-error' : props.statusTone === 'ok' ? 'is-ok' : '') }, props.status) : null,
        ),
      );
    }

    function SectionTabs(props) {
      return h(
        'div',
        { className: 'bgp-sec-tabs', role: 'tablist' },
        (props.tabs || []).map((tab) =>
          h(
            'button',
            {
              key: tab.key,
              type: 'button',
              role: 'tab',
              'aria-selected': props.value === tab.key ? 'true' : 'false',
              className: classNames('bgp-sec-tab', props.value === tab.key ? 'is-active' : ''),
              onClick: () => props.onChange(tab.key),
            },
            tab.label + (tab.count != null ? '（' + tab.count + '）' : ''),
          ),
        ),
      );
    }

    function MaskedText(props) {
      const raw = trimText(props.value);
      const masked = !!props.mask && raw && raw.indexOf('*') === -1 ? maskValue(raw, 4) : raw;
      return h('span', { title: props.mask && raw ? '已脱敏显示' : undefined }, masked || '—');
    }

    function Loader(props) {
      return h('div', { className: 'bgp-loader' }, h(Spinner, { size: 16 }), h('span', null, props.text || '正在加载…'));
    }

    // ────────────────────────────────────────────────────────────
    // 通用 hooks / 小工具
    // ────────────────────────────────────────────────────────────

    function useMountedRef() {
      const ref = useRef(true);
      useEffect(() => {
        ref.current = true;
        return () => {
          ref.current = false;
        };
      }, []);
      return ref;
    }

    function useToasts() {
      const [toasts, setToasts] = useState([]);
      const seq = useRef(0);
      const notify = useCallback((kind, text) => {
        seq.current += 1;
        const id = seq.current;
        setToasts((prev) => prev.concat([{ id: id, kind: kind, text: text }]));
        setTimeout(() => {
          setToasts((prev) => prev.filter((item) => item.id !== id));
        }, 2800);
      }, []);
      return { toasts: toasts, notify: notify };
    }

    function useConfirmer() {
      const [state, setState] = useState(null);
      const confirm = useCallback(
        (options) =>
          new Promise((resolve) => {
            setState({ options: options, resolve: resolve });
          }),
        [],
      );
      const finish = useCallback(
        (value) => {
          setState((current) => {
            if (current && current.resolve) current.resolve(value);
            return null;
          });
        },
        [],
      );
      const dialog = state
        ? h(Dialog, {
            open: true,
            title: state.options.title,
            text: state.options.text,
            confirmLabel: state.options.confirmLabel,
            cancelLabel: state.options.cancelLabel,
            danger: state.options.danger,
            onCancel: () => finish(false),
            onConfirm: () => finish(true),
          })
        : null;
      return { confirm: confirm, dialog: dialog };
    }

    function useClipboard(notify) {
      return useCallback(
        (value, successText) => {
          copyText(value)
            .then(() => notify('success', successText || '已复制，粘贴到对话框发送'))
            .catch(() => notify('error', '复制没有成功，请手动选择文本复制'));
        },
        [notify],
      );
    }

    function todayText() {
      const now = new Date();
      const pad = (n) => (n < 10 ? '0' + n : String(n));
      return now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate());
    }

    function computeCompletenessText(value) {
      if (typeof value !== 'number' || isNaN(value)) return '';
      return Math.round(value * 100) + '%';
    }

    function toneForCompleteness(value) {
      if (typeof value !== 'number' || isNaN(value)) return 'neutral';
      if (value >= 0.8) return 'ok';
      if (value >= 0.5) return 'neutral';
      return 'warn';
    }

    // ────────────────────────────────────────────────────────────
    // 页面：环境检查
    // ────────────────────────────────────────────────────────────

    // 状态按「是否必需 + 状态」分级（M2.5）：
    //   必需失败 = 红「未通过」；可选失败 = 橙「降级」；必需延后/跳过 = 中性「延后」；可选跳过 = 灰「未运行」
    const ENV_STATUS_BASE = {
      passed: { label: '已通过', tone: 'ok', icon: 'check' },
      warning: { label: '有风险', tone: 'warn', icon: 'alert' },
      degraded: { label: '降级', tone: 'warn', icon: 'alert' },
      waived: { label: '已豁免', tone: 'neutral', icon: 'info' },
      unknown: { label: '未知', tone: 'neutral', icon: 'info' },
      'not-run': { label: '尚未检查', tone: 'neutral', icon: 'info' },
    };

    // 检查项中文名兜底（API 的 label 优先；旧版 Host 未带 label 时用这张表）
    const ENV_ITEM_LABELS = {
      python_runtime: '运行环境',
      python_packages: '必要组件',
      pdf_libraries: 'PDF 读取',
      word_com: '旧格式转换',
      scripts_integrity: '插件脚本完整性',
      templates_integrity: '模板完整性',
      fonts: '中文字体',
      external_converters: '转换工具',
      browser_renderer: '图表渲染',
      workspace_writable: '工作区可写',
      disk_space: '磁盘空间',
      path_length: '路径长度',
      skill_service: '技能服务',
      state_service: '状态服务',
          host_version: '版本兼容',
    };

    function envItemName(item) {
      return trimText(item && (item.label || item.name)) || ENV_ITEM_LABELS[item && item.id] || '检查项';
    }

    // 检查脚本把「尚未检查」写作 not_run，而报告顶层写作 not-run；统一成一种
    function envStatusKey(status) {
      const value = textOf(status);
      if (value === 'not_run' || value === 'not-run' || value === 'not run') return 'not-run';
      return value || 'unknown';
    }

    function envItemUnhealthy(item) {
      const status = envStatusKey(item && item.status);
      return status === 'failed' || status === 'timeout' || status === 'warning';
    }

    // 可选能力的降级：失败/超时/降级（不含 skipped，skipped 单独算「未运行」）
    function envItemDegraded(item) {
      const status = envStatusKey(item && item.status);
      return !!(item && item.degraded) || status === 'degraded' || status === 'failed' || status === 'timeout';
    }

    function envItemSkipped(item) {
      const status = envStatusKey(item && item.status);
      return status === 'skipped' || status === 'waived';
    }

    // 状态标签（required 决定同一状态显示的文案/颜色）
    function envStatusMeta(item) {
      const status = envStatusKey(item && item.status);
      const required = !!(item && item.required);
      if (status === 'passed') return ENV_STATUS_BASE.passed;
      if (status === 'failed') {
        return required ? { label: '未通过', tone: 'error', icon: 'error' } : { label: '降级', tone: 'warn', icon: 'alert' };
      }
      if (status === 'timeout') {
        return required ? { label: '检查超时', tone: 'error', icon: 'error' } : { label: '降级', tone: 'warn', icon: 'alert' };
      }
      if (status === 'skipped') {
        return required ? { label: '延后（M3）', tone: 'neutral', icon: 'info' } : { label: '未运行', tone: 'neutral', icon: 'info' };
      }
      if (status === 'degraded') return ENV_STATUS_BASE.degraded;
      if (status === 'waived') return ENV_STATUS_BASE.waived;
      if (status === 'not-run') return ENV_STATUS_BASE['not-run'];
      return ENV_STATUS_BASE.unknown;
    }

    function envNumber(value, fallback) {
      return typeof value === 'number' && isFinite(value) ? value : fallback;
    }

    function envEvidenceText(item) {
      const value = item && item.evidence;
      if (typeof value === 'string') return trimText(value);
      if (typeof value === 'number') return String(value);
      return '';
    }

    function envRepairItemNames(items) {
      return asArray(items).map((id) => envItemName({ id: textOf(id) })).filter(Boolean).join('、');
    }

    function envRepairActionName(action) {
      const id = textOf(action && action.actionId);
      return trimText(action && (action.label || action.name)) || ENV_ITEM_LABELS[id] || id || '修复动作';
    }

    function envRepairSize(action) {
      const mb = envNumber(action && action.downloadMB, null);
      if (mb === null) return '';
      return mb >= 1000 ? mb / 1000 + ' GB' : mb + ' MB';
    }

    function envRepairWrites(action) {
      const writes = action && action.writes;
      if (Array.isArray(writes)) return writes.map(textOf).filter(Boolean).join('；');
      return textOf(writes);
    }

    function EnvRepairDialog(props) {
      if (!props.open) return null;
      const plan = props.plan;
      const groups = (plan && (plan.groups || (plan.plan && plan.plan.groups))) || {};
      const auto = asArray(groups.auto);
      const assisted = asArray(groups.assisted);
      const manual = asArray(groups.manual);
      const pluginDefect = asArray(groups.pluginDefect);
      const autoSelected = auto.filter((action) => props.selected[textOf(action.actionId)] === true);
      const sectionTitle = (text, extra) =>
        h(
          'div',
          { style: { display: 'flex', alignItems: 'center', gap: 8, margin: '14px 0 6px' } },
          h('div', { style: { fontSize: 13, fontWeight: 600, color: 'var(--bgp-fg)' } }, text),
          extra ? h('span', { className: 'bgp-muted', style: { fontSize: 12 } }, extra) : null,
        );
      const row = (key, main, meta, side) =>
        h(
          'div',
          {
            key: key,
            style: {
              display: 'flex',
              alignItems: 'flex-start',
              gap: 8,
              padding: '8px 10px',
              border: '1px solid var(--bgp-line)',
              borderRadius: 'var(--bgp-radius-m)',
              marginBottom: 6,
              background: 'var(--bgp-card2)',
            },
          },
          main,
          side ? h('div', { style: { marginLeft: 'auto', flex: '0 0 auto', display: 'flex', gap: 6 } }, side) : null,
          meta || null,
        );
      return h(
        'div',
        { className: 'bgp-backdrop', role: 'dialog', 'aria-modal': 'true', 'aria-label': '修复环境' },
        h(
          'div',
          {
            className: 'bgp-dialog',
            style: { width: 'min(760px,100%)', maxHeight: '86vh', display: 'flex', flexDirection: 'column' },
          },
          h('h2', { className: 'bgp-dialog-title', style: { marginBottom: 2 } }, '修复环境'),
          h(
            'p',
            { className: 'bgp-dialog-text', style: { margin: '0 0 6px' } },
            plan
              ? '可自动修复 ' + auto.length + ' 项，需确认 ' + assisted.length + ' 项，只能手动 ' + manual.length + ' 项，需升级插件 ' + pluginDefect.length + ' 项。'
              : '正在生成修复计划…',
          ),
          props.error
            ? h(Banner, { tone: 'warn', title: '修复计划没有生成', text: describeError(props.error).message })
            : null,
          h(
            'div',
            { style: { overflowY: 'auto', paddingRight: 4 } },
            !plan && !props.error ? h(Skeleton, { rows: 3 }) : null,
            plan
              ? [
                  sectionTitle('将自动修复（默认勾选）', auto.length ? '装到用户数据目录，不装系统软件/不提权' : '没有可自动修复项'),
                  auto.length
                    ? h(
                        'div',
                        { style: { display: 'flex', gap: 8, marginBottom: 6 } },
                        h(
                          Button,
                          {
                            size: 'sm',
                            variant: 'text',
                            onClick: () => props.onSelectAll(auto.map((action) => textOf(action.actionId))),
                          },
                          '全选',
                        ),
                        h(
                          Button,
                          { size: 'sm', variant: 'text', onClick: () => props.onSelectAll([]) },
                          '全不选',
                        ),
                      )
                    : null,
                  auto.map((action, index) =>
                    row(
                      'auto-' + (textOf(action.actionId) || index),
                      h(
                        'label',
                        { style: { display: 'flex', alignItems: 'flex-start', gap: 8, cursor: 'pointer', flex: '1 1 auto' } },
                        h('input', {
                          type: 'checkbox',
                          checked: props.selected[textOf(action.actionId)] === true,
                          onChange: () => props.onToggle(textOf(action.actionId)),
                          style: { marginTop: 3, accentColor: 'var(--bgp-brand)' },
                        }),
                        h(
                          'span',
                          null,
                          h('span', { style: { fontWeight: 500 } }, envRepairActionName(action)),
                          envRepairItemNames(action && action.items) ? h('span', { className: 'bgp-muted' }, ' · ' + envRepairItemNames(action.items)) : null,
                          h(
                            'span',
                            { className: 'bgp-row-sub', style: { display: 'block' } },
                            [
                              trimText(action && action.why),
                              envRepairSize(action) ? '预计 ' + envRepairSize(action) : '',
                              envRepairWrites(action) ? '写入 ' + envRepairWrites(action) : '',
                            ]
                              .filter(Boolean)
                              .join(' · '),
                          ),
                        ),
                      ),
                      null,
                      action.actionId
                        ? h(
                            Button,
                            {
                              size: 'sm',
                              variant: 'text',
                              onClick: () => props.onRepairOne(textOf(action.actionId)),
                            },
                            '只修这项',
                          )
                        : null,
                    ),
                  ),
                  assisted.length ? sectionTitle('需确认（复制命令后自行执行）') : null,
                  assisted.map((entry, index) =>
                    row(
                      'assisted-' + index,
                      h(
                        'div',
                        null,
                        h('div', { style: { fontWeight: 500 } }, envRepairActionName(entry)),
                        h(
                          'div',
                          { className: 'bgp-row-sub' },
                          [trimText(entry && (entry.why || entry.causeDetail || entry.cause))].filter(Boolean).join(' · '),
                        ),
                        entry && entry.command
                          ? h('pre', { style: { margin: '6px 0 0', whiteSpace: 'pre-wrap', wordBreak: 'break-all', fontSize: 12, background: 'var(--bgp-fill)', padding: 6, borderRadius: 6 } }, textOf(entry.command))
                          : null,
                      ),
                      null,
                      entry && entry.command
                        ? h(Button, { size: 'sm', variant: 'text', icon: 'copy', onClick: () => props.onCopy(textOf(entry.command)) }, '复制命令')
                        : null,
                    ),
                  ),
                  manual.length ? sectionTitle('只能手动处理') : null,
                  manual.map((entry, index) =>
                    row(
                      'manual-' + index,
                      h(
                        'div',
                        null,
                        h('div', { style: { fontWeight: 500 } }, envRepairActionName(entry)),
                        h(
                          'div',
                          { className: 'bgp-row-sub' },
                          [trimText(entry && (entry.causeDetail || entry.why || entry.cause))].filter(Boolean).join(' · '),
                        ),
                      ),
                    ),
                  ),
                  pluginDefect.length ? sectionTitle('需升级插件（不是本机环境问题）') : null,
                  pluginDefect.map((entry, index) =>
                    row(
                      'plugin-' + index,
                      h(
                        'div',
                        null,
                        h('div', { style: { fontWeight: 500 } }, envRepairActionName(entry)),
                        h(
                          'div',
                          { className: 'bgp-row-sub' },
                          '这类问题来自插件版本/脚本损坏，更新或重新安装插件后再检查；本机修复不会改动插件代码。',
                        ),
                      ),
                    ),
                  ),
                ]
              : null,
          ),
          h(
            'div',
            {
              style: {
                borderTop: '1px solid var(--bgp-line)',
                marginTop: 10,
                paddingTop: 10,
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                flexWrap: 'wrap',
              },
            },
            h(
              'span',
              { className: 'bgp-muted', style: { flex: '1 1 240px', fontSize: 12, lineHeight: 1.5 } },
              '自动修复只装依赖到用户数据目录，不装系统软件/不提权。可自动修复的项默认勾选；需确认/手动项不会自动执行。',
            ),
            h(
              Button,
              {
                variant: 'text',
                onClick: () =>
                  props.onCopy(
                    '[BidGenie] action=repair-env\n' +
                      '请先运行 bg_env_repair.py --plan 查看环境问题，按 auto/assisted/manual/pluginDefect 分组说明；' +
                      '只自动修复我确认的 auto 项，修复后用 bg_env_check --verify 复检，并汇报修复前后结果、剩余项和日志路径。',
                  ),
              },
              '复制修复提示词',
            ),
            h(Button, { variant: 'text', onClick: props.onCancel }, '取消'),
            props.error
              ? h(Button, { variant: 'secondary', onClick: props.onRetryPlan }, '重试生成计划')
              : null,
            h(
              Button,
              {
                variant: 'primary',
                disabled: !plan || autoSelected.length === 0,
                onClick: () => props.onConfirm(autoSelected.map((action) => textOf(action.actionId))),
              },
              autoSelected.length ? '开始修复（' + autoSelected.length + ' 项）' : '开始修复',
            ),
          ),
        ),
      );
    }

    function EnvCheckPage(props) {
      const workspace = props.workspace;
      const notify = props.notify;
      const showCopy = props.showCopy;
      const [state, setState] = useState({ status: 'loading', data: null, error: null });
      const [runState, setRunState] = useState({ status: 'idle', error: null, startedAt: 0, elapsed: 0 });
      const [repair, setRepair] = useState({
        phase: 'idle',
        plan: null,
        planError: null,
        selected: {},
        startedAt: 0,
        elapsed: 0,
        error: null,
        result: null,
        before: null,
        after: null,
        remaining: [],
        logPath: '',
        logTail: [],
        lastIds: [],
      });
      const [repairOpen, setRepairOpen] = useState(false);
      // 修复成功后默认不再把原始日志铺在页面上，需要时再展开
      const [repairLogOpen, setRepairLogOpen] = useState(false);
      const repairPlanKeyRef = useRef('');
      const mounted = useMountedRef();

      const load = useCallback(() => {
        setState((prev) => ({ status: prev.data ? 'ready' : 'loading', data: prev.data, error: null }));
        api
          .getEnvCheck(workspace)
          .then((data) => {
            if (!mounted.current) return;
            setState({ status: 'ready', data: data, error: null });
          })
          .catch((error) => {
            if (!mounted.current) return;
            setState((prev) => ({ status: 'error', data: prev.data, error: error }));
          });
      }, [workspace, mounted]);

      useEffect(() => {
        load();
      }, [load]);

      // 运行中计时（不依赖服务端事件，避免 Host 无流式进度时页面像卡住）
      useEffect(() => {
        if (runState.status !== 'running') return undefined;
        const timer = setInterval(() => {
          if (!mounted.current) return;
          setRunState((prev) =>
            prev.status === 'running'
              ? Object.assign({}, prev, { elapsed: Math.max(0, Math.floor((Date.now() - prev.startedAt) / 1000)) })
              : prev,
          );
        }, 1000);
        return () => clearInterval(timer);
      }, [runState.status, mounted]);

      // 环境修复执行中计时
      useEffect(() => {
        if (repair.phase !== 'running') return undefined;
        const timer = setInterval(() => {
          if (!mounted.current) return;
          setRepair((prev) =>
            prev.phase === 'running'
              ? Object.assign({}, prev, { elapsed: Math.max(0, Math.floor((Date.now() - prev.startedAt) / 1000)) })
              : prev,
          );
        }, 1000);
        return () => clearInterval(timer);
      }, [repair.phase, mounted]);

      const runCheck = () => {
        if (runState.status === 'running' || repair.phase === 'running') return;
        // 手动重检后旧修复计划可能过期，重新生成
        repairPlanKeyRef.current = '';
        setRepair((prev) => Object.assign({}, prev, { plan: null, planError: null, phase: 'idle' }));
        setRunState({ status: 'running', error: null, startedAt: Date.now(), elapsed: 0 });
        api
          .runEnvCheck(workspace)
          .then((data) => {
            if (!mounted.current) return;
            setRunState({ status: 'idle', error: null, startedAt: 0, elapsed: 0 });
            setState({ status: 'ready', data: data, error: null });
            const passed = envNumber(data && data.requiredPassed, 0);
            const total = envNumber(data && data.requiredTotal, 8);
            if (passed >= total) notify('success', '检查完成：必需项 ' + passed + '/' + total + ' 通过');
            else notify('error', '检查完成：还有 ' + Math.max(total - passed, 0) + ' 项必需检查没通过');
          })
          .catch((error) => {
            if (!mounted.current) return;
            setRunState({ status: 'idle', error: error, startedAt: 0, elapsed: 0 });
            if (error && error.code === 'E_TIMEOUT') {
              notify('error', '检查时间太长（超过 10 分钟），已停止等待；可以稍后再试');
            } else if (isNotFound(error)) {
              notify('error', '当前版本还不能在这里检查，请在对话框里说「帮我检查环境」');
            } else {
              notify('error', describeError(error).message);
            }
          });
      };

      const copyHelp = (text) => {
        if (!trimText(text)) return;
        if (showCopy) showCopy(text, '修复指引已复制');
        else props.onCopyInstruction(text);
      };

      const copyAgentInstruction = () => {
        const text = '[BidGenie] 帮我检查环境';
        if (showCopy) showCopy(text, '已复制，粘贴到对话框发送');
        else props.onCopyInstruction(text);
      };

      const data = state.data || {};
      const items = asArray(data.items).map((item) => Object.assign({}, item, { id: textOf(item.id || item.itemId) }));
      const hasResult =
        state.status === 'ready' && (data.status === 'ready' || items.some((item) => envStatusKey(item.status) !== 'not-run'));
      const running = runState.status === 'running';
      const readError = state.status === 'error';
      const requiredItems = items.filter((item) => item.required);
      const optionalItems = items.filter((item) => !item.required);
      const requiredTotal = envNumber(data.requiredTotal, requiredItems.length || 8);
      const requiredPassed = envNumber(data.requiredPassed, requiredItems.filter((item) => item.status === 'passed').length);
      const optionalTotal = envNumber(data.optionalTotal, optionalItems.length || 8);
      const optionalPassed = envNumber(data.optionalPassed, optionalItems.filter((item) => item.status === 'passed').length);
      const failedRequired = requiredItems.filter(envItemUnhealthy);
      const deferredRequired = requiredItems.filter((item) => envItemSkipped(item) && !envItemUnhealthy(item));
      const downgradedOptional = optionalItems.filter(envItemDegraded);
      const skippedOptional = optionalItems.filter((item) => envItemSkipped(item) && !envItemDegraded(item));
      const checkedAt = trimText(data.checkedAt);
      const version = trimText(data.checklistVersion);
      const hostVersion = trimText(data.hostVersion);
      const summaryLine =
        '必需 ' +
        requiredPassed +
        '/' +
        requiredTotal +
        ' 通过 · 可选 ' +
        optionalPassed +
        '/' +
        optionalTotal +
        ' 通过' +
        (downgradedOptional.length ? '（' + downgradedOptional.length + ' 项降级）' : '');
      const optionalTroubleCount = downgradedOptional.length + skippedOptional.length;
      const repairRunning = repair.phase === 'running';
      const repairAllGreen =
        repair.phase === 'done' &&
        !!repair.after &&
        repair.after.passed >= repair.after.total &&
        repair.remaining.length === 0;
      const repairUnhealthy = hasResult && (failedRequired.length > 0 || downgradedOptional.length > 0);
      const repairGroups = (repair.plan && (repair.plan.groups || (repair.plan.plan && repair.plan.plan.groups))) || {};
      const autoRepairActions = asArray(repairGroups.auto);
      const autoRepairCount = autoRepairActions.length;

      const repairSelectedFromPlan = (planData) => {
        const groups = (planData && (planData.groups || (planData.plan && planData.plan.groups))) || {};
        const selected = {};
        asArray(groups.auto).forEach((action) => {
          const actionId = textOf(action && action.actionId);
          if (actionId) selected[actionId] = true;
        });
        return selected;
      };

      // 环境不健康时预取修复计划，用于顶部「N 项可自动修复」计数；不自动打开弹窗。
      useEffect(() => {
        if (!repairUnhealthy || repair.phase === 'running') return undefined;
        const key = workspace + '|' + checkedAt + '|' + failedRequired.length + '|' + downgradedOptional.length;
        if (repairPlanKeyRef.current === key) return undefined;
        repairPlanKeyRef.current = key;
        let alive = true;
        api
          .envCheckRepairPlan(workspace)
          .then((data) => {
            if (!alive || !mounted.current) return;
            setRepair((prev) => Object.assign({}, prev, { plan: data, planError: null, selected: repairSelectedFromPlan(data) }));
          })
          .catch((error) => {
            if (!alive || !mounted.current) return;
            setRepair((prev) => Object.assign({}, prev, { planError: error }));
          });
        return () => {
          alive = false;
        };
      }, [repairUnhealthy, repair.phase, workspace, checkedAt, failedRequired.length, downgradedOptional.length, mounted]);

      const fetchRepairPlan = () => {
        if (repair.phase === 'planning' || repairRunning) return;
        setRepairOpen(true);
        setRepair((prev) => Object.assign({}, prev, { phase: 'planning', planError: null, error: null }));
        api
          .envCheckRepairPlan(workspace)
          .then((data) => {
            if (!mounted.current) return;
            setRepair((prev) =>
              Object.assign({}, prev, { phase: 'confirm', plan: data, selected: repairSelectedFromPlan(data), planError: null }),
            );
          })
          .catch((error) => {
            if (!mounted.current) return;
            setRepair((prev) => Object.assign({}, prev, { phase: 'idle', planError: error }));
          });
      };

      const openRepair = () => {
        if (repair.plan) {
          setRepair((prev) => Object.assign({}, prev, { selected: prev.selected, planError: null }));
          setRepairOpen(true);
          return;
        }
        fetchRepairPlan();
      };

      const toggleRepair = (actionId) => {
        setRepair((prev) => {
          const selected = Object.assign({}, prev.selected);
          selected[actionId] = selected[actionId] !== true;
          return Object.assign({}, prev, { selected: selected });
        });
      };

      const selectAllRepair = (actionIds) => {
        const selected = {};
        asArray(actionIds).forEach((actionId) => {
          const id = textOf(actionId);
          if (id) selected[id] = true;
        });
        setRepair((prev) => Object.assign({}, prev, { selected: selected }));
      };

      const finishRepair = (result, before, checked) => {
        if (!mounted.current) return;
        const afterPassed = envNumber(checked && checked.requiredPassed, 0);
        const afterTotal = envNumber(checked && checked.requiredTotal, envNumber(before && before.total, 8));
        const remaining = asArray(checked && checked.items)
          .filter((item) => envItemUnhealthy(item) || envItemDegraded(item))
          .map((item) => envItemName(item));
        const finish = (logPath, logTail) => {
          if (!mounted.current) return;
          setRepair((prev) =>
            Object.assign({}, prev, {
              phase: 'done',
              plan: null,
              planError: null,
              startedAt: 0,
              elapsed: 0,
              error: null,
              result: result,
              before: before,
              after: { passed: afterPassed, total: afterTotal },
              remaining: remaining,
              logPath: trimText(logPath) || trimText(result && result.logPath),
              logTail: asArray(logTail),
            }),
          );
          notify(
            afterPassed >= afterTotal ? 'success' : 'warn',
            '修复完成：必需项 ' + before.passed + '/' + before.total + ' → ' + afterPassed + '/' + afterTotal,
          );
        };
        const resultTail = asArray(result && result.logTail);
        if (trimText(result && result.logPath) && resultTail.length) {
          finish(result && result.logPath, resultTail);
          return;
        }
        api
          .envCheckRepairLast(workspace)
          .then((last) => finish(textOf(last && last.logPath) || textOf(result && result.logPath), asArray(last && last.logTail)))
          .catch(() => finish(textOf(result && result.logPath), resultTail));
      };

      const executeRepair = (actionIds) => {
        const ids = asArray(actionIds).map(textOf).filter(Boolean);
        if (!ids.length || repairRunning) return;
        const before = { passed: requiredPassed, total: requiredTotal };
        setRepairOpen(false);
        setRepairLogOpen(false);
        setRepair((prev) =>
          Object.assign({}, prev, {
            phase: 'running',
            startedAt: Date.now(),
            elapsed: 0,
            error: null,
            result: null,
            before: before,
            after: null,
            remaining: [],
            logPath: '',
            logTail: [],
            lastIds: ids,
          }),
        );
        api
          .envCheckRepair(workspace, { actionIds: ids, yes: true })
          .then((result) =>
            api.runEnvCheck(workspace).then((checked) => {
              // 修复后的自动重检结果要回填页面顶部统计，否则上面仍显示修复前的“可选 5/7（2 项降级）”
              if (mounted.current && checked && asArray(checked.items).length) {
                setState({ status: 'ready', data: checked, error: null });
              }
              finishRepair(result, before, checked);
            }),
          )
          .catch((error) => {
            if (!mounted.current) return;
            api
              .envCheckRepairLast(workspace)
              .then((last) => {
                if (!mounted.current) return;
                setRepair((prev) =>
                  Object.assign({}, prev, {
                    phase: 'error',
                    startedAt: 0,
                    elapsed: 0,
                    error: error,
                    logPath: textOf(last && last.logPath),
                    logTail: asArray(last && last.logTail),
                  }),
                );
              })
              .catch(() => {
                if (!mounted.current) return;
                setRepair((prev) => Object.assign({}, prev, { phase: 'error', startedAt: 0, elapsed: 0, error: error }));
              });
            notify('error', describeError(error).message || '环境修复没有完成');
          });
      };

      const retryPlan = () => {
        repairPlanKeyRef.current = '';
        fetchRepairPlan();
      };

      // 必需项全过时只用警告/信息色，不用红色报错
      const statusBanner = !hasResult
        ? null
        : failedRequired.length > 0
          ? {
              tone: 'error',
              title: summaryLine,
              text: '必需项还有 ' + failedRequired.length + ' 项没通过，暂时不能开始撰写。按每项的修复指引处理后，点「重新检查」。',
            }
          : optionalTroubleCount > 0
            ? {
                tone: 'warn',
                title: summaryLine,
                text:
                  '可以开始撰写；' +
                  (downgradedOptional.length ? downgradedOptional.length + ' 项可选能力降级' : '') +
                  (downgradedOptional.length && skippedOptional.length ? '、' : '') +
                  (skippedOptional.length ? skippedOptional.length + ' 项可选能力未运行' : '') +
                  '，仅影响对应能力。',
              }
            : deferredRequired.length > 0
              ? {
                  tone: 'info',
                  title: summaryLine,
                  text: '必需项中有 ' + deferredRequired.length + ' 项延后到后续版本，其他必需项都已通过，可以开始撰写。',
                }
              : { tone: 'ok', title: summaryLine, text: '可以开始撰写；如果之后遇到问题，回来再检查一次即可。' };

      const renderEnvItem = (item, index) => {
        const meta = envStatusMeta(item);
        const showHelp = trimText(item.remediation) && (envItemUnhealthy(item) || envItemDegraded(item));
        return h(
          'div',
          { className: 'bgp-env-item', key: item.id || index },
          h(
            'div',
            { className: 'bgp-row is-static bgp-env-row' },
            h(
              'span',
              { className: 'bgp-row-main' },
              h(
                'span',
                { className: 'bgp-row-title', style: { fontSize: 14, fontWeight: 500, display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' } },
                envItemName(item),
                item.required ? h(Chip, { tone: 'neutral' }, '必需') : null,
              ),
              envEvidenceText(item) ? h('span', { className: 'bgp-row-sub' }, envEvidenceText(item)) : null,
            ),
            h('span', { className: 'bgp-row-side' }, h(Chip, { tone: meta.tone }, h(Icon, { name: meta.icon, size: 12 }), h('span', null, meta.label))),
          ),
          showHelp
            ? h(
                'div',
                { className: 'bgp-env-help' },
                h('span', { className: 'bgp-env-help-label' }, '修复指引'),
                !item.required && envItemDegraded(item) ? h('span', { className: 'bgp-env-help-note' }, '仅影响对应能力') : null,
                h('span', { className: 'bgp-env-help-text' }, textOf(item.remediation)),
                h(Button, { size: 'sm', variant: 'text', icon: 'copy', onClick: () => copyHelp(item.remediation) }, '复制指引'),
              )
            : null,
        );
      };

      return h(
        'div',
        { className: 'bgp-page bgp-page-narrow' },
        h(
          'div',
          { className: 'bgp-card' },
          h(
            'div',
            { className: 'bgp-card-head', style: { marginBottom: 4 } },
            h(Button, { variant: 'text', icon: 'back', onClick: props.onBack }, '返回'),
            h('h1', { className: 'bgp-h1', style: { flex: '1 1 auto' } }, '环境检查'),
            h(
              Button,
              { variant: 'primary', icon: 'refresh', onClick: runCheck, disabled: running || repairRunning },
              running ? '检查中…' : hasResult ? '重新检查' : '开始检查',
            ),
            repairUnhealthy
              ? h(
                  Button,
                  {
                    variant: 'primary',
                    icon: 'shield',
                    onClick: openRepair,
                    disabled: running || repairRunning,
                  },
                  repairRunning
                    ? '修复中…'
                    : '修复环境' + (autoRepairCount ? '（' + autoRepairCount + ' 项可自动修复）' : ''),
                )
              : null,
          ),
          h('p', { className: 'bgp-desc' }, '检查这台电脑能不能顺利撰写技术方案。必需项都通过后就可以开始；没通过的项会给出处理办法。'),
          h(
            'div',
            { className: 'bgp-actions', style: { marginTop: 10 } },
            h(Chip, { tone: requiredPassed >= requiredTotal ? 'ok' : hasResult ? 'warn' : 'neutral' }, '必需项 ' + requiredPassed + '/' + requiredTotal),
            hasResult && items.length ? h(Chip, { tone: 'neutral' }, '可选项 ' + optionalPassed + '/' + optionalTotal) : null,
            hasResult && downgradedOptional.length ? h(Chip, { tone: 'warn' }, '降级 ' + downgradedOptional.length + ' 项') : null,
            version ? h(Chip, { tone: 'neutral' }, '清单 ' + version) : null,
            hostVersion ? h(Chip, { tone: 'neutral' }, '版本 ' + hostVersion) : null,
            checkedAt ? h(Chip, { tone: 'neutral' }, '上次检查 ' + formatRelative(checkedAt)) : null,
          ),
          h(
            'div',
            { style: { marginTop: 10 } },
            h(NextStep, {
              text: repairRunning
                ? '正在自动修复环境，最长 20 分钟；修复期间不要重复点击。完成后会自动重新检查。'
                : running
                  ? '正在逐项检查，单项最长 2 分钟、整体最长 10 分钟；面板不会自动安装任何软件。'
                  : hasResult
                    ? '有未通过或降级的项时，可点「修复环境」自动处理依赖类问题；其他项按「修复指引」处理后点「重新检查」。'
                    : '第一次检查会逐项核对环境；有问题的项会告诉你怎么处理。',
            }),
          ),
          h(
            'div',
            { className: 'bgp-actions', style: { marginTop: 10 } },
            h(Button, { variant: 'text', icon: 'copy', onClick: copyAgentInstruction }, '复制检查指令到对话框'),
          ),
        ),
        running
          ? h(
              'div',
              { className: 'bgp-card' },
              h(
                'div',
                { className: 'bgp-env-progress' },
                h(Spinner, { size: 16 }),
                h('span', null, '正在检查…已用 ' + runState.elapsed + ' 秒'),
                h('span', { className: 'bgp-muted', style: { marginLeft: 'auto' } }, '单项最长 2 分钟 · 整体最长 10 分钟'),
              ),
              h('p', { className: 'bgp-muted', style: { marginTop: 8 } }, '检查期间可以继续使用其他页面；完成后结果会自动显示在这里。'),
            )
          : null,
        readError && !hasResult ? h(ErrorState, { title: '读取检查结果失败', error: state.error, onRetry: load }) : null,
        readError && hasResult
          ? h(Banner, {
              tone: 'warn',
              title: '结果可能不是最新的',
              text: '这次读取没有成功，下面显示的是上一次的结果。',
              actions: h(Button, { variant: 'text', onClick: load }, '重试'),
            })
          : null,
        runState.error
          ? h(Banner, {
              tone: 'error',
              title: '检查没有完成',
              text:
                runState.error && runState.error.code === 'E_TIMEOUT'
                  ? '检查时间超过 10 分钟，已停止等待；可以稍后再试。'
                  : isNotFound(runState.error)
                    ? '当前版本还不能在这里检查，请在对话框里说「帮我检查环境」，或稍后重试。'
                    : runState.error && runState.error.code === 'E_DEP_MISSING'
                      ? '检查功能还在准备中（检查脚本未就绪），稍后重试；也可以先在对话框里说「帮我检查环境」。'
                      : describeError(runState.error).message,
              actions: h(
                'div',
                { className: 'bgp-actions' },
                h(Button, { variant: 'secondary', onClick: runCheck, disabled: running }, '重试'),
                h(Button, { variant: 'text', icon: 'copy', onClick: copyAgentInstruction }, '复制检查指令'),
              ),
            })
          : null,
        repairRunning
          ? h(
              'div',
              { className: 'bgp-card' },
              h(
                'div',
                { className: 'bgp-env-progress' },
                h(Spinner, { size: 16 }),
                h('span', null, '正在修复环境…已用 ' + repair.elapsed + ' 秒'),
                h('span', { className: 'bgp-muted', style: { marginLeft: 'auto' } }, '最长 20 分钟'),
              ),
              h(
                'p',
                { className: 'bgp-muted', style: { marginTop: 8 } },
                '修复期间「重新检查」和「修复环境」已禁用；完成后会自动重新检查并显示修复前后结果与日志。',
              ),
            )
          : null,
        state.status === 'loading' ? h(Skeleton, { rows: 3 }) : null,
        state.status === 'ready' && !hasResult
          ? h(
              'div',
              { className: 'bgp-card' },
              h(EmptyState, {
                title: '还没有检查过',
                text: '一共 15 项检查，其中 8 项是必需的。第一次检查大约需要 1–2 分钟；已经通过的项下次会跳过，更快。',
                actions: h('div', { className: 'bgp-actions' }, h(Button, { variant: 'primary', icon: 'refresh', onClick: runCheck, disabled: running || repairRunning }, '开始检查')),
              }),
            )
          : null,
        repair.phase === 'done'
          ? h(Banner, {
              tone: repair.after && repair.after.passed >= repair.after.total ? 'ok' : 'warn',
              title:
                '修复完成：必需项 ' +
                (repair.before ? repair.before.passed + '/' + repair.before.total : '—') +
                ' → ' +
                (repair.after ? repair.after.passed + '/' + repair.after.total : '—'),
              text:
                (repair.after && repair.after.passed >= repair.after.total
                  ? '所有必需项已通过。'
                  : '还剩 ' + Math.max(0, (repair.after ? repair.after.total - repair.after.passed : 0)) + ' 项必需检查未通过。') +
                (repair.remaining.length ? '剩余未通过/降级：' + repair.remaining.join('、') + '。' : '') +
                (!repairAllGreen && repair.logPath ? '日志：' + repair.logPath : ''),
              actions: h(
                'div',
                { className: 'bgp-actions' },
                h(Button, { variant: 'secondary', icon: 'refresh', onClick: runCheck, disabled: running || repairRunning }, '重新检查'),
                repairAllGreen && repair.logTail.length
                  ? h(
                      Button,
                      { size: 'sm', variant: 'text', onClick: () => setRepairLogOpen((prev) => !prev) },
                      repairLogOpen ? '收起日志' : '查看日志',
                    )
                  : null,
                repair.logPath
                  ? h(Button, { size: 'sm', variant: 'text', icon: 'copy', onClick: () => copyHelp(repair.logPath) }, '复制日志路径')
                  : null,
              ),
            })
          : null,
        repair.phase === 'error'
          ? h(Banner, {
              tone: 'error',
              title: '环境修复没有完成',
              text:
                describeError(repair.error).message +
                (describeError(repair.error).hint ? '（' + describeError(repair.error).hint + '）' : '') +
                (repair.logPath ? '日志：' + repair.logPath : ''),
              actions: h(
                'div',
                { className: 'bgp-actions' },
                h(Button, { variant: 'secondary', onClick: () => executeRepair(repair.lastIds), disabled: repairRunning }, '重试修复'),
                h(Button, { variant: 'text', onClick: openRepair, disabled: repairRunning }, '查看修复计划'),
              ),
            })
          : null,
        repair.phase === 'done' && repair.logTail.length && (!repairAllGreen || repairLogOpen)
          ? h(
              'div',
              { className: 'bgp-card' },
              h('div', { className: 'bgp-env-group-title' }, '修复日志（末尾）'),
              h(
                'pre',
                {
                  style: {
                    margin: '8px 0 0',
                    maxHeight: 220,
                    overflow: 'auto',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-all',
                    fontSize: 12,
                    background: 'var(--bgp-fill)',
                    padding: 10,
                    borderRadius: 'var(--bgp-radius-m)',
                  },
                },
                repair.logTail.join('\n'),
              ),
            )
          : null,
        repair.phase === 'error' && repair.logTail.length
          ? h(
              'div',
              { className: 'bgp-card' },
              h('div', { className: 'bgp-env-group-title' }, '最近一次修复日志（末尾）'),
              h(
                'pre',
                {
                  style: {
                    margin: '8px 0 0',
                    maxHeight: 220,
                    overflow: 'auto',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-all',
                    fontSize: 12,
                    background: 'var(--bgp-fill)',
                    padding: 10,
                    borderRadius: 'var(--bgp-radius-m)',
                  },
                },
                repair.logTail.join('\n'),
              ),
            )
          : null,
        hasResult && statusBanner ? h(Banner, { tone: statusBanner.tone, title: statusBanner.title, text: statusBanner.text }) : null,
        hasResult && requiredItems.length
          ? h(
              'div',
              { className: 'bgp-card' },
              h('div', { className: 'bgp-env-group-title' }, '必需项', h('span', { className: 'bgp-muted' }, requiredPassed + '/' + requiredTotal + ' 通过')),
              requiredItems.map(renderEnvItem),
            )
          : null,
        hasResult && optionalItems.length
          ? h(
              'div',
              { className: 'bgp-card' },
              h('div', { className: 'bgp-env-group-title' }, '可选项', h('span', { className: 'bgp-muted' }, optionalPassed + '/' + optionalTotal + ' 通过')),
              optionalItems.map(renderEnvItem),
            )
          : null,
        hasResult
          ? h(
              'p',
              { className: 'bgp-muted', style: { textAlign: 'center' } },
              '自动修复只装依赖到用户数据目录，不装系统软件/不提权。检查结果按版本记录：清单版本不变时，已通过的项会跳过，不会重复检查。',
            )
          : null,
        h(EnvRepairDialog, {
          open: repairOpen,
          plan: repair.plan,
          selected: repair.selected,
          error: repair.planError,
          onCancel: () => setRepairOpen(false),
          onSelectAll: selectAllRepair,
          onToggle: toggleRepair,
          onConfirm: executeRepair,
          onRepairOne: (actionId) => executeRepair([actionId]),
          onCopy: copyHelp,
          onRetryPlan: retryPlan,
        }),
      );
    }

    // ────────────────────────────────────────────────────────────
    // 页面：关于
    // ────────────────────────────────────────────────────────────

    const ABOUT_ICON_NAMES = ['book', 'shield', 'doc', 'users', 'info', 'folder', 'gear', 'eye', 'open'];

    function AboutSectionIcon(props) {
      const icon = trimText(props.icon);
      if (icon === '') return null;
      if (ABOUT_ICON_NAMES.indexOf(icon) >= 0) {
        return h('span', { className: 'bgp-about-icon' }, h(Icon, { name: icon, size: 15 }));
      }
      // 配置里也可以直接写 emoji，保持「改 JSON 就能换」的灵活性
      return h('span', { className: 'bgp-about-icon bgp-about-icon-emoji', 'aria-hidden': 'true' }, icon);
    }

    function AboutBlock(props) {
      const block = props.block;
      if (!block) return null;
      if (block.type === 'paragraph') {
        return h('p', { className: 'bgp-about-p' }, block.text);
      }
      if (block.type === 'note') {
        return h('p', { className: 'bgp-about-note' }, h(Icon, { name: 'info', size: 14 }), h('span', null, block.text));
      }
      if (block.type === 'chips') {
        return h(
          'div',
          { className: 'bgp-about-chips' },
          block.label ? h('span', { className: 'bgp-about-chip-label' }, block.label + '：') : null,
          h(
            'div',
            { className: 'bgp-tag-list' },
            block.items.map((item, index) => h(Chip, { key: index, tone: 'brand' }, item)),
          ),
        );
      }
      if (block.type === 'bullets') {
        return h(
          'ul',
          { className: 'bgp-about-list' },
          block.items.map((item, index) =>
            h(
              'li',
              { key: index },
              item.label ? h('strong', null, item.label) : null,
              item.label && item.text ? '：' : null,
              item.text ? h('span', null, item.text) : null,
            ),
          ),
        );
      }
      if (block.type === 'contacts') {
        const copyAllText = block.items.map((item) => item.label + '：' + item.value).join('\n');
        return h(
          'div',
          null,
          h(
            'div',
            { className: 'bgp-about-contacts' },
            block.items.map((item, index) =>
              h(
                'div',
                { className: 'bgp-about-contact', key: item.label + '-' + index },
                h('span', { className: 'bgp-about-contact-label' }, item.label),
                h('span', { className: 'bgp-about-contact-value' }, item.value),
                h(
                  Button,
                  {
                    size: 'sm',
                    variant: 'text',
                    icon: 'copy',
                    onClick: () => props.showCopy(item.value, '已复制' + item.label),
                  },
                  '复制',
                ),
              ),
            ),
          ),
          h(
            'div',
            { className: 'bgp-actions', style: { marginTop: 8 } },
            h(
              Button,
              { size: 'sm', variant: 'secondary', icon: 'copy', onClick: () => props.showCopy(copyAllText, '联系方式已复制') },
              '复制全部联系方式',
            ),
          ),
        );
      }
      return null;
    }

    function AboutPage(props) {
      const [state, setState] = useState({
        status: 'loading',
        about: null,
        version: '',
        hostVersion: '',
        warning: null,
        error: null,
      });
      const mounted = useMountedRef();
      const requestSeq = useRef(0);

      const load = useCallback(() => {
        requestSeq.current += 1;
        const seq = requestSeq.current;
        setState((prev) => Object.assign({}, prev, { status: prev.about ? 'ready' : 'loading', error: null }));
        Promise.all([
          api.about().then(
            (data) => ({ ok: true, value: data }),
            (error) => ({ ok: false, error: error }),
          ),
          api.health(props.workspace).then(
            (data) => ({ ok: true, value: data }),
            (error) => ({ ok: false, error: error }),
          ),
        ]).then((results) => {
          if (!mounted.current || seq !== requestSeq.current) return;
          const aboutResult = results[0];
          const healthResult = results[1];
          const payload = aboutResult.ok && isPlainObject(aboutResult.value) ? aboutResult.value : null;
          const aboutData = payload && isPlainObject(payload.about) ? payload.about : null;
          if (!aboutData) {
            setState({
              status: 'error',
              about: null,
              version: '',
              hostVersion: '',
              warning: null,
              error: aboutResult.error || new Error('关于页内容读取失败'),
            });
            return;
          }
          const health = healthResult.ok && isPlainObject(healthResult.value) ? healthResult.value : null;
          setState({
            status: 'ready',
            about: aboutData,
            version: trimText(payload.version) || (health ? trimText(health.version) : ''),
            hostVersion: health ? trimText(health.hostVersion) : '',
            warning: isPlainObject(payload.warning) ? payload.warning : null,
            error: null,
          });
        });
      }, [props.workspace, mounted]);

      useEffect(() => {
        load();
      }, [load]);

      const about = state.about || {};
      const sections = Array.isArray(about.sections) ? about.sections : [];
      const footer = isPlainObject(about.footer) ? about.footer : {};
      const name = trimText(about.name) || 'BidGenie';
      const nameZh = trimText(about.nameZh);
      const version = displayVersion(state.version);
      const tagline = trimText(about.tagline);

      return h(
        'div',
        { className: 'bgp-page bgp-page-narrow' },
        h(
          'div',
          { className: 'bgp-card bgp-about-hero-card' },
          h(
            'div',
            { className: 'bgp-about-topbar' },
            h(Button, { variant: 'text', icon: 'back', onClick: props.onBack }, '返回'),
            h('div', { className: 'bgp-header-spacer' }),
            h(Button, { size: 'sm', variant: 'text', icon: 'refresh', onClick: load, disabled: state.status === 'loading' }, '刷新'),
          ),
          h(
            'div',
            { className: 'bgp-about-hero' },
            h('span', { className: 'bgp-about-mark' }, h(BidGenieIcon, { size: 44 })),
            h(
              'div',
              { className: 'bgp-about-identity' },
              h('h1', { className: 'bgp-about-name' }, '关于 ' + name + (nameZh ? '（' + nameZh + '）' : '')),
              h(
                'div',
                { className: 'bgp-about-meta' },
                version ? h(Chip, { tone: 'brand' }, version) : null,
                state.hostVersion ? h(Chip, null, 'DSH ' + state.hostVersion) : null,
              ),
            ),
          ),
          tagline ? h('p', { className: 'bgp-about-tagline' }, tagline) : null,
        ),
        state.status === 'loading' ? h('div', { className: 'bgp-card' }, h(Loader, { text: '正在读取关于信息…' })) : null,
        state.status === 'error' ? h(ErrorState, { title: '关于页读取失败', error: state.error, onRetry: load }) : null,
        state.warning
          ? h(Banner, {
              tone: 'warn',
              title: '内容配置读取失败',
              text: '已回退默认内容，请检查插件目录下的 config/about.json。',
            })
          : null,
        state.status === 'ready'
          ? sections.map((section) =>
              h(
                'section',
                { className: 'bgp-card bgp-about-section', key: section.id },
                h(
                  'div',
                  { className: 'bgp-about-section-head' },
                  h(AboutSectionIcon, { icon: section.icon }),
                  h('h2', { className: 'bgp-about-section-title' }, section.title),
                ),
                trimText(section.lead) ? h('p', { className: 'bgp-about-lead' }, section.lead) : null,
                ...(Array.isArray(section.blocks) ? section.blocks : []).map((block, index) =>
                  h(AboutBlock, { key: section.id + '-' + index, block: block, showCopy: props.showCopy }),
                ),
              ),
            )
          : null,
        state.status === 'ready'
          ? h(
              'div',
              { className: 'bgp-about-footer' },
              footer.license ? h('span', null, '许可证：' + footer.license) : null,
              footer.copyright ? h('span', null, footer.copyright) : null,
              footer.note ? h('span', null, footer.note) : null,
            )
          : null,
      );
    }

    // ────────────────────────────────────────────────────────────
    // 页面：项目（只读进度 + 复制启动指令）
    // ────────────────────────────────────────────────────────────

    function ProjectCard(props) {
      const project = props.project;
      const info = project.currentStage ? stageInfo(project.currentStage) : PENDING_STAGE;
      const packageNo = packageNoOf(project.currentPackage);
      return h(
        'div',
        { className: 'bgp-card bgp-card-tight', style: { padding: 0, overflow: 'hidden' } },
        h(
          'div',
          {
            className: 'bgp-row',
            role: 'button',
            tabIndex: 0,
            'aria-expanded': props.expanded ? 'true' : 'false',
            onClick: props.onToggle,
            onKeyDown: (event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                props.onToggle();
              }
            },
          },
          h(
            'div',
            { className: 'bgp-row-main' },
            h('div', { className: 'bgp-row-title' }, project.projectName),
            h(
              'div',
              { className: 'bgp-row-sub' },
              packageNo
                ? h('span', null, '第 ' + packageNo + ' 包')
                : project.isSubcontracted === false
                  ? h('span', null, '不分包')
                  : h('span', null, '等待开始'),
              h('span', null, '· ' + info.label),
              h('span', null, '· 更新于 ' + formatRelative(project.updatedAt)),
            ),
            h(ProgressDots, { step: info.step, done: info.done, aborted: info.aborted }),
          ),
          h('span', { className: 'bgp-row-side' }, h(Icon, { name: 'chevron', size: 16 })),
        ),
        props.expanded
          ? h(
              'div',
              { style: { borderTop: '1px solid var(--bgp-line)', padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 10 } },
              project.projectNo ? h(KeyValue, { label: '项目编号' }, project.projectNo) : null,
              h(
                KeyValue,
                { label: '当前包' },
                packageNo
                  ? '第 ' + packageNo + ' 包'
                  : project.isSubcontracted === false
                    ? '不分包'
                    : '尚未开始',
              ),
              project.packageCount != null
                ? h(KeyValue, { label: '包数量' }, (project.isSubcontracted ? '分包' : '不分包') + ' · 共 ' + project.packageCount + ' 个包')
                : null,
              project.packagesToWrite.length
                ? h(
                    KeyValue,
                    { label: '计划撰写' },
                    project.packagesToWrite.map((item) => '第 ' + (packageNoOf(item) || item) + ' 包').join('、'),
                  )
                : null,
              h(KeyValue, { label: '最近更新' }, formatDateTime(project.updatedAt) || '—'),
              h(NextStep, {
                text: info.next,
                action: h(
                  'div',
                  { className: 'bgp-actions' },
                  h(Button, { size: 'sm', variant: 'secondary', icon: 'copy', onClick: props.onCopyInstruction }, '复制指令'),
                  h(Button, { size: 'sm', variant: 'text', onClick: props.onCopyLocation }, '复制项目位置'),
                ),
              }),
            )
          : null,
      );
    }

    function ProjectsPage(props) {
      const workspace = props.workspace;
      const notify = props.notify;
      const showCopy = props.showCopy;
      const [state, setState] = useState({ status: 'loading', projects: [], currentBidder: null, hasBidder: false, error: null });
      const [expandedId, setExpandedId] = useState('');
      const mounted = useMountedRef();

      const load = useCallback(
        (silent) => {
          if (!silent) {
            setState((prev) => ({ status: prev.projects.length ? 'ready' : 'loading', projects: prev.projects, currentBidder: prev.currentBidder, hasBidder: prev.hasBidder, error: null }));
          }
          const jobs = [
            api.listProjects(workspace).then(
              (data) => ({ ok: true, value: normalizeProjects(data) }),
              (error) => ({ ok: false, error: error }),
            ),
            api.listBidders(workspace).then(
              (data) => {
                const list = normalizeBidderList(data);
                const current = list.filter((item) => item.isCurrent)[0] || list[0] || null;
                return { ok: true, value: { list: list, current: current } };
              },
              (error) => ({ ok: false, error: error }),
            ),
          ];
          Promise.all(jobs).then((results) => {
            if (!mounted.current) return;
            const projectResult = results[0];
            if (!projectResult.ok) {
              setState((prev) => ({ status: 'error', projects: prev.projects, currentBidder: prev.currentBidder, hasBidder: prev.hasBidder, error: projectResult.error }));
              return;
            }
            const bidderResult = results[1];
            const bidderInfo = bidderResult.ok ? bidderResult.value : { list: [], current: null };
            setState({
              status: 'ready',
              projects: projectResult.value,
              currentBidder: bidderInfo.current,
              hasBidder: bidderInfo.list.length > 0,
              error: null,
            });
          });
        },
        [workspace, mounted],
      );

      useEffect(() => {
        load(false);
      }, [load, props.refreshKey]);

      useEffect(() => {
        const timer = setInterval(() => {
          if (typeof document !== 'undefined' && document.visibilityState === 'visible') load(true);
        }, POLL_INTERVAL_MS);
        const onVisible = () => {
          if (typeof document !== 'undefined' && document.visibilityState === 'visible') load(true);
        };
        document.addEventListener('visibilitychange', onVisible);
        return () => {
          clearInterval(timer);
          document.removeEventListener('visibilitychange', onVisible);
        };
      }, [load]);

      const copyInstruction = (project) => {
        showCopy(project ? launchCommandFor(project) : '[BidGenie] 帮我开始一个新的投标项目');
      };

      const isLoading = state.status === 'loading';
      const isError = state.status === 'error' && state.projects.length === 0;
      const isEmpty = state.status === 'ready' && state.projects.length === 0;

      return h(
        'div',
        { className: 'bgp-page' },
        h(PageHeader, {
          title: '项目',
          description: '你的投标项目都在这里，进度从文件里实时读取；撰写仍在对话框里进行。',
          actions: [
            h(Button, { key: 'go', variant: 'primary', icon: 'copy', onClick: () => copyInstruction(state.projects[0]) }, '复制启动指令'),
            state.hasBidder ? null : h(Button, { key: 'bidder', variant: 'secondary', onClick: () => props.onNavigate('bidders') }, '去建投标人'),
          ].filter(Boolean),
          next: state.projects.length
            ? '展开项目可以看下一步要说什么；要继续撰写，复制指令粘贴到对话框发送即可。'
            : '项目从对话框发起：上传招标文件后说「帮我写技术方案」。',
        }),
        state.currentBidder
          ? h(
              'div',
              { className: 'bgp-card bgp-card-tight' },
              h(
                'div',
                { className: 'bgp-row is-static', style: { padding: 0, borderBottom: 'none' } },
                h('span', { className: 'bgp-row-main' }, h('span', { className: 'bgp-row-title', style: { fontSize: 14 } }, '当前投标人'), h('span', { className: 'bgp-row-sub' }, state.currentBidder.displayName)),
                h('span', { className: 'bgp-row-side' }, h(Button, { size: 'sm', variant: 'text', onClick: () => props.onNavigate('bidders') }, '更换')),
              ),
            )
          : null,
        isLoading ? h(Skeleton, { rows: 3 }) : null,
        isError ? h(ErrorState, { title: '读取项目进度失败', error: state.error, onRetry: () => load(false) }) : null,
        state.status === 'error' && state.projects.length
          ? h(Banner, { tone: 'warn', title: '进度可能不是最新的', text: '这次刷新没有成功，下面显示的是上一次读取到的内容。' })
          : null,
        isEmpty
          ? h(
              'div',
              { className: 'bgp-card' },
              h(EmptyState, {
                title: '还没有投标项目',
                text: state.hasBidder
                  ? '项目从对话框发起：在对话框里上传招标文件，然后说「帮我写技术方案」。'
                  : '先建一份投标人资料，再在对话框里上传招标文件、说「帮我写技术方案」。',
                actions: h(
                  'div',
                  { className: 'bgp-actions' },
                  state.hasBidder
                    ? h(Button, { variant: 'primary', icon: 'copy', onClick: () => copyInstruction(null) }, '复制启动指令')
                    : h(Button, { variant: 'primary', onClick: () => props.onNavigate('bidders') }, '新建投标人'),
                  h(Button, { variant: 'secondary', onClick: () => load(false) }, '刷新'),
                ),
              }),
            )
          : null,
        state.projects.map((project) =>
          h(ProjectCard, {
            key: project.projectId || project.dirName,
            project: project,
            expanded: expandedId === (project.projectId || project.dirName),
            onToggle: () => setExpandedId(expandedId === (project.projectId || project.dirName) ? '' : project.projectId || project.dirName),
            onCopyInstruction: () => copyInstruction(project),
            onCopyLocation: () => showCopy(textOf(workspace).replace(/[\\/]+$/, '') + '\\' + project.dirName, '已复制项目位置'),
          }),
        ),
        state.projects.length
          ? h('p', { className: 'bgp-muted', style: { textAlign: 'center' } }, '进度每 5 秒自动刷新；也可以点右上角刷新按钮。')
          : null,
      );
    }

    // ────────────────────────────────────────────────────────────
    // 页面：投标人列表 / 新建 / 详情编辑
    // ────────────────────────────────────────────────────────────

    function BiddersPage(props) {
      const workspace = props.workspace;
      const notify = props.notify;
      const confirm = props.confirm;
      const maskSensitive = props.maskSensitive !== false;
      const [state, setState] = useState({ status: 'loading', bidders: [], error: null });
      const [editing, setEditing] = useState(null);
      const [busy, setBusy] = useState('');
      const mounted = useMountedRef();

      const load = useCallback(
        (silent) => {
          if (!silent) setState((prev) => ({ status: prev.bidders.length ? 'ready' : 'loading', bidders: prev.bidders, error: null }));
          api
            .listBidders(workspace)
            .then((data) => {
              if (!mounted.current) return;
              setState({ status: 'ready', bidders: normalizeBidderList(data), error: null });
            })
            .catch((error) => {
              if (!mounted.current) return;
              setState((prev) => ({ status: 'error', bidders: prev.bidders, error: error }));
            });
        },
        [workspace, mounted],
      );

      useEffect(() => {
        load(false);
      }, [load, props.refreshKey]);

      const setCurrent = (item) => {
        if (item.isCurrent || busy) return;
        setBusy(item.bidderId);
        api
          .setCurrentBidder(workspace, item.bidderId)
          .then(() => {
            if (!mounted.current) return;
            notify('success', '已设为当前投标人');
            load(true);
          })
          .catch((error) => {
            if (!mounted.current) return;
            notify('error', describeError(error).message);
          })
          .then(() => {
            if (mounted.current) setBusy('');
          });
      };

      const remove = (item) => {
        confirm({
          title: '删除「' + item.displayName + '」？',
          text: '资料会移入回收站，不会立即彻底删除。已经创建的项目仍保留创建时的资料快照，不受影响。',
          confirmLabel: '移入回收站',
          danger: true,
        }).then((ok) => {
          if (!ok) return;
          setBusy(item.bidderId);
          api
            .deleteBidder(workspace, item.bidderId)
            .then(() => {
              notify('success', '已移入回收站');
              load(true);
            })
            .catch((error) => notify('error', describeError(error).message))
            .then(() => {
              if (mounted.current) setBusy('');
            });
        });
      };

      if (editing) {
        return h(BidderEditor, {
          key: (editing.mode === 'edit' ? editing.item.bidderId : 'new') + ':' + workspace,
          workspace: workspace,
          mode: editing.mode,
          item: editing.item,
          notify: notify,
          confirm: confirm,
          maskSensitive: maskSensitive,
          onBack: () => setEditing(null),
          onSaved: () => {
            setEditing(null);
            load(false);
            props.onDataChanged && props.onDataChanged();
          },
          onDeleted: () => {
            setEditing(null);
            load(false);
            props.onDataChanged && props.onDataChanged();
          },
          onCurrentChanged: () => {
            load(true);
            props.onDataChanged && props.onDataChanged();
          },
        });
      }

      const bidders = state.bidders;
      const current = bidders.filter((item) => item.isCurrent)[0] || null;
      const others = bidders.filter((item) => !item.isCurrent);
      const isEmpty = state.status === 'ready' && bidders.length === 0;

      const renderRow = (item) =>
        h(
          'div',
          {
            className: 'bgp-row',
            key: item.bidderId,
            role: 'button',
            tabIndex: 0,
            onClick: () => setEditing({ mode: 'edit', item: item }),
            onKeyDown: (event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                setEditing({ mode: 'edit', item: item });
              }
            },
          },
          h(
            'span',
            { className: 'bgp-row-main' },
            h(
              'span',
              { style: { display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' } },
              h('span', { className: 'bgp-row-title' }, item.displayName),
              h(Chip, { tone: 'neutral' }, BIDDER_TYPE_LABEL[item.bidderType] || '未设置类型'),
              item.isCurrent ? h(Chip, { tone: 'brand' }, '当前') : null,
            ),
            h(
              'span',
              { className: 'bgp-row-sub' },
              item.completeness != null
                ? h(Chip, { tone: toneForCompleteness(item.completeness) }, '完整 ' + computeCompletenessText(item.completeness))
                : null,
              item.counts && item.counts.certificates != null ? h('span', null, '证书 ' + textOf(item.counts.certificates)) : null,
              item.counts && item.counts.performance != null ? h('span', null, '业绩 ' + textOf(item.counts.performance)) : null,
              item.updatedAt ? h('span', null, '更新于 ' + formatRelative(item.updatedAt)) : null,
            ),
          ),
          h(
            'span',
            { className: 'bgp-row-side', onClick: (event) => event.stopPropagation() },
            item.isCurrent
              ? null
              : h(Button, { size: 'sm', variant: 'text', disabled: busy === item.bidderId, onClick: () => setCurrent(item) }, '设为当前'),
            h(MenuButton, {
              label: '更多操作',
              items: [
                { key: 'edit', label: '编辑资料', onClick: () => setEditing({ mode: 'edit', item: item }) },
                { key: 'current', label: '设为当前', onClick: () => setCurrent(item) },
                { key: 'delete', label: '删除', danger: true, onClick: () => remove(item) },
              ],
            }),
          ),
        );

      return h(
        'div',
        { className: 'bgp-page' },
        h(PageHeader, {
          title: '投标人',
          description: '管理投标用的公司或个人资料。资料可以复用到多个项目，当前投标人会写进方案。',
          actions: h(Button, { variant: 'primary', icon: 'plus', onClick: () => setEditing({ mode: 'new', item: null }) }, '新建投标人'),
          next: '选一个设为当前；也可以先在对话框里让助手帮你建档。',
        }),
        state.status === 'loading' ? h(Skeleton, { rows: 2 }) : null,
        state.status === 'error' && bidders.length === 0
          ? h(ErrorState, { title: '读取投标人资料失败', error: state.error, onRetry: () => load(false) })
          : null,
        isEmpty
          ? h(
              'div',
              { className: 'bgp-card' },
              h(EmptyState, {
                title: '还没有投标人',
                text: '先建一份公司或个人资料，方案里才有可引用的证书和业绩。',
                actions: h(
                  'div',
                  { className: 'bgp-actions' },
                  h(Button, { variant: 'primary', icon: 'plus', onClick: () => setEditing({ mode: 'new', item: null }) }, '新建投标人'),
                  h(
                    Button,
                    { variant: 'secondary', icon: 'copy', onClick: () => props.showCopy('[BidGenie] 帮我新建一个投标人') },
                    '复制建档指令',
                  ),
                ),
              }),
            )
          : null,
        current
          ? h(
              'div',
              { className: 'bgp-card', style: { padding: '10px 0' } },
              h('div', { style: { padding: '0 12px 6px', fontSize: 12, color: 'var(--bgp-fg3)' } }, '当前投标人'),
              renderRow(current),
            )
          : null,
        others.length
          ? h(
              'div',
              { className: 'bgp-card', style: { padding: '10px 0' } },
              h('div', { style: { padding: '0 12px 6px', fontSize: 12, color: 'var(--bgp-fg3)' } }, '其他资料 ' + others.length),
              others.map(renderRow),
            )
          : null,
      );
    }

    function isMaskedValue(value) {
      return trimText(value).indexOf('*') >= 0;
    }

    function validateBidderForm(form) {
      const errors = {};
      if (!trimText(form.displayName)) errors.displayName = '请填写名称';
      if (!['legal_entity', 'natural_person'].includes(form.bidderType)) errors.bidderType = '请选择类型';
      if (trimText(form.contactEmail) && !isMaskedValue(form.contactEmail) && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimText(form.contactEmail))) {
        errors.contactEmail = '邮箱格式不对，例如 name@company.com';
      }
      if (trimText(form.contactPhone) && !isMaskedValue(form.contactPhone) && !/^[0-9+\-\s]{7,20}$/.test(trimText(form.contactPhone))) {
        errors.contactPhone = '电话只能包含数字、空格、+、-，请检查后重试';
      }
      if (trimText(form.establishedDate) && trimText(form.establishedDate) > todayText()) {
        errors.establishedDate = '日期不能晚于今天，请修改';
      }
      return errors;
    }

    function validateBidderSections(sections) {
      const issues = [];
      sections.certificates.forEach((row, index) => {
        if (!trimText(row.name)) issues.push({ tab: 'certificates', text: '第 ' + (index + 1) + ' 项证书还需要填写名称' });
        if (row.validFrom && row.validTo && row.validFrom > row.validTo) {
          issues.push({ tab: 'certificates', text: '第 ' + (index + 1) + ' 项证书的结束日期早于开始日期，请修改' });
        }
      });
      sections.performance.forEach((row, index) => {
        if (!trimText(row.projectName)) issues.push({ tab: 'performance', text: '第 ' + (index + 1) + ' 项业绩还需要填写项目名称' });
        if (row.periodStart && row.periodEnd && row.periodStart > row.periodEnd) {
          issues.push({ tab: 'performance', text: '第 ' + (index + 1) + ' 项业绩的结束日期早于开始时间，请修改' });
        }
      });
      sections.awards.forEach((row, index) => {
        if (!trimText(row.name)) issues.push({ tab: 'awards', text: '第 ' + (index + 1) + ' 项获奖还需要填写奖项名称' });
      });
      return issues;
    }

    function BidderEditor(props) {
      const isEdit = props.mode === 'edit';
      const item = props.item || {};
      const workspace = props.workspace;
      const notify = props.notify;
      const confirm = props.confirm;
      const maskSensitive = props.maskSensitive !== false;
      const mounted = useMountedRef();

      const [detail, setDetail] = useState({ status: isEdit ? 'loading' : 'new', error: null, maskedFields: [], partial: false });
      const [form, setForm] = useState(emptyBidderForm());
      const [initialForm, setInitialForm] = useState(null);
      const [sections, setSections] = useState({ certificates: [], performance: [], awards: [], penalties: [] });
      const [initialSections, setInitialSections] = useState(null);
      const [tab, setTab] = useState('basic');
      const [errors, setErrors] = useState({});
      const [saveState, setSaveState] = useState({ status: 'idle', error: null });
      const [reloadToken, setReloadToken] = useState(0);

      const snapshotSections = useCallback((value) => JSON.parse(JSON.stringify(value)), []);

      const loadDetail = useCallback(() => {
        if (!isEdit) return;
        setDetail({ status: 'loading', error: null, maskedFields: [], partial: false });
        api
          .detailBidder(workspace, item.bidderId)
          .then((data) => {
            if (!mounted.current) return;
            const picked = pickBidderDetail(data);
            if (!picked) {
              const summary = Object.assign({}, item.raw || {}, {
                bidderId: item.bidderId,
                displayName: item.displayName,
                bidderType: item.bidderType || 'legal_entity',
              });
              const nextForm = bidderToForm(summary);
              const emptySections = { certificates: [], performance: [], awards: [], penalties: [] };
              setForm(nextForm);
              setInitialForm(nextForm);
              setSections(emptySections);
              setInitialSections(snapshotSections(emptySections));
              setDetail({ status: 'ready', error: null, maskedFields: [], partial: true });
              return;
            }
            const nextForm = bidderToForm(picked.bidder);
            const awardRows = awardsToRows(picked.awardsPenalties);
            const nextSections = {
              certificates: certificatesToRows(picked.certificates),
              performance: performanceToRows(picked.performance),
              awards: awardRows.awards,
              penalties: awardRows.penalties,
            };
            setForm(nextForm);
            setInitialForm(nextForm);
            setSections(nextSections);
            setInitialSections(snapshotSections(nextSections));
            setDetail({ status: 'ready', error: null, maskedFields: picked.maskedFields, partial: false });
          })
          .catch((error) => {
            if (!mounted.current) return;
            setDetail({ status: 'error', error: error, maskedFields: [], partial: false });
          });
      }, [isEdit, workspace, item.bidderId, item.raw, item.displayName, item.bidderType, mounted, snapshotSections]);

      useEffect(() => {
        loadDetail();
      }, [loadDetail, reloadToken]);

      const updateForm = useCallback((key, value) => {
        setForm((prev) => {
          const next = Object.assign({}, prev);
          next[key] = value;
          return next;
        });
        setErrors((prev) => {
          if (!prev[key]) return prev;
          const next = Object.assign({}, prev);
          delete next[key];
          return next;
        });
      }, []);

      const updateSection = useCallback((name, rows) => {
        setSections((prev) => {
          const next = Object.assign({}, prev);
          next[name] = rows;
          return next;
        });
      }, []);

      const basicPatch = useMemo(() => (initialForm ? buildBidderPatch(initialForm, form) : {}), [form, initialForm]);
      const dirtyFlags = useMemo(() => {
        if (!initialSections) return { certificates: false, performance: false, awards: false, penalties: false };
        return {
          certificates: JSON.stringify(sections.certificates) !== JSON.stringify(initialSections.certificates),
          performance: JSON.stringify(sections.performance) !== JSON.stringify(initialSections.performance),
          awards: JSON.stringify(sections.awards) !== JSON.stringify(initialSections.awards),
          penalties: JSON.stringify(sections.penalties) !== JSON.stringify(initialSections.penalties),
        };
      }, [sections, initialSections]);
      const isDirty = isEdit
        ? Object.keys(basicPatch).length > 0 || dirtyFlags.certificates || dirtyFlags.performance || dirtyFlags.awards || dirtyFlags.penalties
        : trimText(form.displayName) !== '' || trimText(form.legalName) !== '' || trimText(form.contactName) !== '';

      const currentBidderType = form.bidderType;
      const visibleBasicFields = BASIC_FIELDS.filter((field) => !field.onlyFor || field.onlyFor === currentBidderType);

      const validate = () => {
        const nextErrors = validateBidderForm(form);
        const issues = validateBidderSections(sections);
        setErrors(nextErrors);
        return { fieldErrors: nextErrors, issues: issues };
      };

      const handleSave = () => {
        if (saveState.status === 'saving') return;
        const result = validate();
        const hasFieldError = Object.keys(result.fieldErrors).length > 0;
        if (hasFieldError || result.issues.length) {
          if (hasFieldError) setTab('basic');
          else setTab(result.issues[0].tab);
          notify('error', hasFieldError ? '还有内容需要修改，请看标红的项' : result.issues[0].text);
          return;
        }
        setSaveState({ status: 'saving', error: null });

        if (!isEdit) {
          const payload = { bidder: formToBidder(form) };
          api
            .createBidder(workspace, payload)
            .then((data) => {
              if (!mounted.current) return;
              setSaveState({ status: 'idle', error: null });
              notify('success', '已创建');
              props.onSaved && props.onSaved(data);
            })
            .catch((error) => {
              if (!mounted.current) return;
              setSaveState({ status: 'error', error: error });
              notify('error', describeError(error).message);
            });
          return;
        }

        const payload = { patch: basicPatch };
        if (Object.keys(basicPatch).length === 0) delete payload.patch;
        if (dirtyFlags.certificates) payload.certificates = rowsToCertificates(sections.certificates);
        if (dirtyFlags.performance) payload.performance = rowsToPerformance(sections.performance);
        if (dirtyFlags.awards || dirtyFlags.penalties) {
          payload.awardsPenalties = rowsToAwardsPenalties(sections.awards, sections.penalties);
        }
        if (!payload.patch && !payload.certificates && !payload.performance && !payload.awardsPenalties) {
          setSaveState({ status: 'idle', error: null });
          notify('info', '没有需要保存的修改');
          return;
        }
        // Host 的 update 需要一个非空的 patch（即使只改了证书/业绩/奖惩），补一个同值字段
        if (!payload.patch) payload.patch = { displayName: trimText(form.displayName) };
        api
          .updateBidder(workspace, item.bidderId, payload)
          .then(() => {
            if (!mounted.current) return;
            setSaveState({ status: 'idle', error: null });
            setInitialForm(form);
            setInitialSections(snapshotSections(sections));
            notify('success', '已保存');
          })
          .catch((error) => {
            if (!mounted.current) return;
            setSaveState({ status: 'error', error: error });
            notify('error', describeError(error).message);
          });
      };

      const handleBack = () => {
        if (!isDirty) {
          props.onBack();
          return;
        }
        confirm({
          title: '放弃这次修改？',
          text: '你填的内容还没有保存，返回后会丢失。',
          confirmLabel: '放弃修改',
          danger: true,
        }).then((ok) => {
          if (ok) props.onBack();
        });
      };

      const handleSetCurrent = () => {
        api
          .setCurrentBidder(workspace, item.bidderId)
          .then(() => {
            notify('success', '已设为当前投标人');
            props.onCurrentChanged && props.onCurrentChanged();
          })
          .catch((error) => notify('error', describeError(error).message));
      };

      const handleDelete = () => {
        confirm({
          title: '删除「' + (item.displayName || '这份资料') + '」？',
          text: '资料会移入回收站，不会立即彻底删除。已经创建的项目仍保留创建时的资料快照，不受影响。',
          confirmLabel: '移入回收站',
          danger: true,
        }).then((ok) => {
          if (!ok) return;
          api
            .deleteBidder(workspace, item.bidderId)
            .then(() => {
              notify('success', '已移入回收站');
              props.onDeleted && props.onDeleted();
            })
            .catch((error) => notify('error', describeError(error).message));
        });
      };

      const renderBasicControl = (field) => {
        const value = form[field.key];
        const isSensitiveMasked = field.sensitive && maskSensitive && textOf(value).indexOf('*') >= 0;
        const invalid = !!errors[field.key];
        if (field.kind === 'select') {
          return h(Select, { value: value, options: field.options, invalid: invalid, onChange: (next) => updateForm(field.key, next) });
        }
        if (field.kind === 'switch') {
          return h(Switch, { checked: !!value, onChange: (next) => updateForm(field.key, next) });
        }
        if (field.kind === 'textarea') {
          return h(TextArea, { value: value, invalid: invalid, onChange: (next) => updateForm(field.key, next) });
        }
        return h(TextInput, {
          type: field.kind === 'date' ? 'date' : 'text',
          value: value,
          invalid: invalid,
          placeholder: field.placeholder,
          inputMode: field.inputMode,
          onChange: (next) => updateForm(field.key, next),
          hint: isSensitiveMasked ? '已脱敏显示；修改时直接输入完整内容' : undefined,
        });
      };

      const counts = {
        certificates: sections.certificates.length,
        performance: sections.performance.length,
        awards: sections.awards.length + sections.penalties.length,
      };

      if (isEdit && detail.status === 'loading') {
        return h(
          'div',
          { className: 'bgp-page bgp-page-narrow' },
          h(
            'div',
            { className: 'bgp-card' },
            h('div', { className: 'bgp-card-head' }, h(Button, { variant: 'text', icon: 'back', onClick: handleBack }, '返回')),
            h(Loader, { text: '正在读取资料…' }),
          ),
        );
      }

      if (isEdit && detail.status === 'error') {
        return h(
          'div',
          { className: 'bgp-page bgp-page-narrow' },
          h(
            'div',
            { className: 'bgp-card' },
            h('div', { className: 'bgp-card-head' }, h(Button, { variant: 'text', icon: 'back', onClick: handleBack }, '返回')),
            h(ErrorState, { title: '资料读取失败', error: detail.error, onRetry: () => setReloadToken((value) => value + 1) }),
          ),
        );
      }

      const issueList = validateBidderSections(sections);
      const visibleIssues = issueList.filter((issue) => issue.tab === tab || (tab === 'awards' && issue.tab === 'awards'));

      return h(
        'div',
        { className: 'bgp-page' },
        h(
          'div',
          { className: 'bgp-card' },
          h(
            'div',
            { className: 'bgp-card-head', style: { marginBottom: 4 } },
            h(Button, { variant: 'text', icon: 'back', onClick: handleBack }, '返回'),
            h('h1', { className: 'bgp-h1', style: { flex: '1 1 auto' } }, isEdit ? item.displayName || '编辑资料' : '新建投标人'),
            isEdit && item.isCurrent ? h(Chip, { tone: 'brand' }, '当前投标人') : null,
          ),
          h(
            'p',
            { className: 'bgp-desc' },
            isEdit
              ? '证书、业绩、奖惩会作为方案里的依据；只有填写在这里的内容才会被写进方案。'
              : '填一份资料，之后所有项目都能复用。只有带 * 的项必填，其余可以以后再补。',
          ),
          h(
            'div',
            { className: 'bgp-actions', style: { marginTop: 10 } },
            h(Button, { variant: 'primary', onClick: handleSave, disabled: saveState.status === 'saving' }, saveState.status === 'saving' ? '保存中…' : '保存'),
            isEdit ? h(Button, { variant: 'secondary', onClick: handleSetCurrent, disabled: !!item.isCurrent }, item.isCurrent ? '已是当前' : '设为当前') : null,
            isEdit ? h(Button, { variant: 'danger', onClick: handleDelete }, '删除') : null,
          ),
          h('div', { style: { marginTop: 10 } }, h(NextStep, { text: isEdit ? '保存后可以继续补充证书、业绩和奖惩；之后到项目页复制启动指令。' : '保存后会回到列表；打开资料可以继续补充证书、业绩和奖惩，设为当前后再去项目页复制启动指令。' })),
        ),
        detail.partial
          ? h(Banner, {
              tone: 'warn',
              title: '明细暂未读取到',
              text: '当前只能修改基本资料。证书、业绩、奖惩如果填写，保存时会以你这次填写的内容为准。',
              actions: h(Button, { variant: 'text', onClick: () => setReloadToken((value) => value + 1) }, '再试一次'),
            })
          : null,
        saveState.status === 'error'
          ? h(Banner, {
              tone: 'error',
              title: '保存没有成功',
              text: describeError(saveState.error).message,
              actions: h(Button, { variant: 'text', onClick: handleSave }, '重试'),
            })
          : null,
        h(
          'div',
          { className: 'bgp-card' },
          h(SectionTabs, {
            value: tab,
            onChange: setTab,
            tabs: isEdit
              ? [
                  { key: 'basic', label: '基本信息' },
                  { key: 'certificates', label: '证书', count: counts.certificates },
                  { key: 'performance', label: '业绩', count: counts.performance },
                  { key: 'awards', label: '奖惩', count: counts.awards },
                ]
              : [{ key: 'basic', label: '基本信息' }],
          }),
          visibleIssues.length
            ? h('div', { style: { marginTop: 10 } }, h(Banner, { tone: 'error', title: '还需要修改', text: visibleIssues.map((issue) => issue.text).join('；') }))
            : null,
          tab === 'basic'
            ? h(
                'div',
                { style: { marginTop: 12 } },
                BASIC_GROUPS.map((group) => {
                  const fields = visibleBasicFields.filter((field) => (field.group || 'identity') === group.key);
                  if (!fields.length) return null;
                  return h(
                    'div',
                    { key: group.key, className: 'bgp-form-group' },
                    h(
                      'div',
                      { className: 'bgp-form-group-head' },
                      h('div', { className: 'bgp-form-group-title' }, group.title),
                      group.desc ? h('div', { className: 'bgp-form-group-desc' }, group.desc) : null,
                    ),
                    h(
                      'div',
                      { className: 'bgp-form-grid' },
                      fields.map((field) =>
                        h(
                          Field,
                          {
                            key: field.key,
                            label: field.label,
                            required: field.required,
                            span: field.span,
                            hint: field.hint,
                            error: errors[field.key],
                          },
                          renderBasicControl(field),
                        ),
                      ),
                    ),
                  );
                }),
              )
            : null,
          tab === 'certificates' && isEdit
            ? h(
                'div',
                { style: { marginTop: 12 } },
                h('p', { className: 'bgp-muted', style: { marginBottom: 8 } }, '证书有效期会决定方案里能不能引用这项资质；过期证书不会被写进正文。'),
                h(RowList, {
                  title: '证书',
                  rows: sections.certificates,
                  fields: CERT_FIELDS,
                  prefix: 'cert',
                  addLabel: '添加证书',
                  emptyText: '还没有证书。添加后，方案里才会引用这项资质。',
                  onChange: (rows) => updateSection('certificates', rows),
                  renderStatus: (row) => {
                    const meta = CERT_STATUS[row.status];
                    if (!meta) return null;
                    return h(Chip, { tone: meta.tone }, meta.label);
                  },
                }),
              )
            : null,
          tab === 'performance' && isEdit
            ? h(
                'div',
                { style: { marginTop: 12 } },
                h('p', { className: 'bgp-muted', style: { marginBottom: 8 } }, '只有登记在这里的业绩会被写进方案；建议填 1–3 个类似项目。'),
                h(RowList, {
                  title: '类似业绩',
                  rows: sections.performance,
                  fields: PERF_FIELDS,
                  prefix: 'perf',
                  addLabel: '添加业绩',
                  emptyText: '还没有业绩。添加后，方案里的「类似业绩」才有依据。',
                  onChange: (rows) => updateSection('performance', rows),
                }),
              )
            : null,
          tab === 'awards' && isEdit
            ? h(
                'div',
                { style: { marginTop: 12 } },
                h(RowList, {
                  title: '获奖',
                  rows: sections.awards,
                  fields: AWARD_FIELDS,
                  prefix: 'award',
                  addLabel: '添加获奖',
                  emptyText: '暂无获奖记录。',
                  onChange: (rows) => updateSection('awards', rows),
                }),
                h('div', { className: 'bgp-subgroup' },
                  h(RowList, {
                    title: '处罚',
                    rows: sections.penalties,
                    fields: PENALTY_FIELDS,
                    prefix: 'penalty',
                    addLabel: '添加处罚',
                    emptyText: '暂无处罚记录。',
                    onChange: (rows) => updateSection('penalties', rows),
                  }),
                ),
              )
            : null,
        ),
        h(
          'div',
          { className: 'bgp-card', style: { padding: '10px 12px' } },
          h(
            'div',
            { className: 'bgp-actions' },
            h(Button, { variant: 'primary', onClick: handleSave, disabled: saveState.status === 'saving' }, saveState.status === 'saving' ? '保存中…' : '保存'),
            h(Button, { variant: 'text', onClick: handleBack }, '返回'),
            h('span', { className: 'bgp-muted', style: { marginLeft: 'auto' } }, isDirty ? '有未保存的修改' : '改动会保存在这个工作区'),
          ),
        ),
      );
    }

    // ────────────────────────────────────────────────────────────
    // 页面：设置（S1–S15）
    // ────────────────────────────────────────────────────────────

    function SettingsPage(props) {
      const workspace = props.workspace;
      const notify = props.notify;
      const [draft, setDraft] = useState(() => normalizeSettings(props.settings));
      const [stylesState, setStylesState] = useState({ status: 'loading', data: null, error: null });
      const [saveState, setSaveState] = useState({ status: 'idle', error: null });
      const [advancedOpen, setAdvancedOpen] = useState(false);
      const [styleExampleOpen, setStyleExampleOpen] = useState(false);
      const [currentBidderName, setCurrentBidderName] = useState('');
      const timerRef = useRef(null);
      const mounted = useMountedRef();
      const saveRef = useRef(null);
      const overviewSupportedRef = useRef(false);

      useEffect(() => {
        setDraft(normalizeSettings(props.settings));
      }, [props.settings]);

      useEffect(() => {
        api
          .listBidders(workspace)
          .then((data) => {
            if (!mounted.current) return;
            const list = normalizeBidderList(data);
            const current = list.filter((item) => item.isCurrent)[0] || null;
            setCurrentBidderName(current ? current.displayName : '');
          })
          .catch(() => {
            if (mounted.current) setCurrentBidderName('');
          });
      }, [workspace, mounted, props.refreshKey]);

      const loadStyles = useCallback(() => {
        setStylesState((prev) => ({ status: prev.data ? 'ready' : 'loading', data: prev.data, error: null }));
        const baseRequest = api.getStyles(workspace).then(
          (data) => ({ ok: true, data: data }),
          (error) => ({ ok: false, error: error }),
        );
        const overviewRequest = api.getStyles(workspace, 'overview').then(
          (data) => ({ ok: true, data: data }),
          (error) => ({ ok: false, error: error }),
        );
        Promise.all([baseRequest, overviewRequest]).then((results) => {
          if (!mounted.current) return;
          const base = results[0];
          const overview = results[1];
          if (!base.ok && !overview.ok) {
            setStylesState((prev) => ({ status: 'error', data: prev.data, error: base.error || overview.error }));
            return;
          }
          setStylesState({
            status: 'ready',
            data: { base: base.ok ? base.data : null, overview: overview.ok ? overview.data : null },
            error: null,
          });
        });
      }, [workspace, mounted]);

      useEffect(() => {
        loadStyles();
      }, [loadStyles, props.refreshKey]);

      const putSettings = useCallback(
        (next) => {
          setSaveState({ status: 'saving', error: null });
          const payload = Object.assign({}, next);
          delete payload.styleProfile; // M2.5：只用 styleProfiles 键，旧字段不再提交
          delete payload.allowNetworkImages; // P1：网络配图已移除，旧字段不再提交
          const quota = isPlainObject(payload.defaultFigureQuota) ? payload.defaultFigureQuota : {};
          const quotaNumber = (value) => (typeof value === 'number' && isFinite(value) ? value : 5);
          payload.defaultFigureQuota = {
            table: quotaNumber(quota.table),
            mermaid: quotaNumber(quota.mermaid),
            terminal: quotaNumber(quota.terminal),
          };
          if (!overviewSupportedRef.current && isPlainObject(payload.styleProfiles)) {
            const profiles = Object.assign({}, payload.styleProfiles);
            delete profiles.overview;
            payload.styleProfiles = profiles;
          }
          api
            .putSettings(workspace, payload)
            .then(() => {
              if (!mounted.current) return;
              setSaveState({ status: 'saved', error: null });
              props.onSettingsChanged && props.onSettingsChanged(next);
            })
            .catch((error) => {
              if (!mounted.current) return;
              setSaveState({ status: 'error', error: error });
              notify('error', describeError(error).message);
            });
        },
        [workspace, mounted, props, notify],
      );
      saveRef.current = putSettings;

      useEffect(
        () => () => {
          if (timerRef.current) clearTimeout(timerRef.current);
        },
        [],
      );

      const update = useCallback((key, value) => {
        setDraft((prev) => {
          const next = Object.assign({}, prev);
          next[key] = value;
          if (timerRef.current) clearTimeout(timerRef.current);
          timerRef.current = setTimeout(() => {
            if (saveRef.current) saveRef.current(next);
          }, 600);
          return next;
        });
      }, []);

      const updateQuota = (key, value) => {
        const quota = Object.assign({}, draft.defaultFigureQuota || {}, {});
        quota[key] = value;
        update('defaultFigureQuota', quota);
      };

      const figureQuota = draft.defaultFigureQuota || SETTINGS_DEFAULTS.defaultFigureQuota;
      const wordTier = draft.defaultWordCountTier == null ? 50000 : draft.defaultWordCountTier;
      const isKnownTier = WORD_TIERS.some((tier) => tier.value === wordTier);

      const styleBaseData = stylesState.data && stylesState.data.base;
      const styleOverviewData = stylesState.data && stylesState.data.overview;
      const styleCatalog = mergeStyleCatalogs(normalizeStyleCatalog(styleBaseData), normalizeStyleCatalog(styleOverviewData, 'overview'));
      // 仅当服务端真的返回了 overview 样式时才算支持：
      // 旧宿主会忽略 ?kind=overview 而返回同一份两套样式响应，不能按“请求成功”判断。
      const overviewSupported = styleCatalog.builtin.overview.length > 0 || styleCatalog.custom.overview.length > 0;
      overviewSupportedRef.current = overviewSupported;
      const styleWarnings = styleCatalog.warnings;
      const styleProfiles = isPlainObject(draft.styleProfiles) ? draft.styleProfiles : normalizeSettings(draft).styleProfiles;
      const styleCustomCount = styleCatalog.custom.tech.length + styleCatalog.custom.keyInfo.length + styleCatalog.custom.overview.length;
      const styleDirHint = 'dsh/styles/tech/、keyinfo/ 或 overview/';

      const styleOptionsFor = (docType) => {
        const list = styleCatalog.builtin[docType].concat(styleCatalog.custom[docType]);
        const source = list.length ? list : asArray(STYLE_BUILTIN_FALLBACK[docType]);
        const options = source.map((style) => ({ value: style.id, label: style.label }));
        const current = textOf(styleProfiles[docType]);
        if (current && !options.some((option) => option.value === current)) {
          options.push({ value: current, label: current + '（当前不可用）' });
        }
        return options;
      };

      const updateStyleProfile = (docType, value) => {
        update('styleProfiles', Object.assign({}, styleProfiles, { [docType]: value }));
      };

      const statusText = saveState.status === 'saving' ? '保存中…' : saveState.status === 'saved' ? '已保存' : saveState.status === 'error' ? '未保存 · 重试' : '';

      const settingRow = (key, config) =>
        h(
          SettingsRow,
          {
            key: key,
            label: config.label,
            hint: config.hint,
            defaultText: config.defaultText,
          },
          config.control,
        );

      return h(
        'div',
        { className: 'bgp-page bgp-page-narrow' },
        h(PageHeader, {
          title: '设置',
          description: '常规设置立即生效。这些默认值只影响之后新建的项目包；已准备/进行中的项目以各自 package-meta 为准。每个项目还能在对话里单独调整。',
          actions: saveState.status === 'error'
            ? h(Button, { variant: 'secondary', icon: 'refresh', onClick: () => putSettings(draft) }, '重试')
            : h('span', { className: classNames('bgp-muted', saveState.status === 'saved' ? 'is-ok' : '') }, statusText || '改动会自动保存'),
          next: '改完即生效，但默认值只作用于之后新建的项目；项目级参数（字数、图表数量）仍会在对话里逐项目确认。',
        }),
        h(
          'div',
          { className: 'bgp-card' },
          h('div', { className: 'bgp-group-head' }, h('h2', { className: 'bgp-card-title' }, '常规')),
          settingRow('s1', {
            label: '当前投标人',
            hint: '新建项目时默认使用它；方案素材都来自它',
            control: h(
              'div',
              { style: { display: 'flex', alignItems: 'center', gap: 8 } },
              h('span', { className: 'bgp-small' }, currentBidderName || '未设置'),
              h(Button, { size: 'sm', variant: 'text', onClick: () => props.onNavigate('bidders') }, '去选择'),
            ),
          }),
          settingRow('s14', {
            label: '脱敏显示',
            hint: '证件号、账号、电话在界面打码显示',
            defaultText: '开',
            control: h(Switch, { checked: draft.maskSensitive !== false, onChange: (value) => update('maskSensitive', value), ariaLabel: '脱敏显示' }),
          }),
          settingRow('s11', {
            label: '工作区',
            hint: '所有资料和项目都存在这里；换工作区等于换资料库',
            control: h(
              'div',
              { style: { display: 'flex', alignItems: 'center', gap: 8, maxWidth: 360 } },
              h('span', { className: 'bgp-mono', style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, textOf(workspace)),
              h(Button, { size: 'sm', variant: 'text', onClick: () => props.showCopy(textOf(workspace), '已复制工作区位置') }, '复制'),
            ),
          }),
          settingRow('s12', {
            label: '项目列表与进度',
            hint: '查看每个项目跑到哪一步',
            control: h(Button, { size: 'sm', variant: 'text', onClick: () => props.onNavigate('projects') }, '前往'),
          }),
          settingRow('s13', {
            label: '环境检查',
            hint: '检查这台电脑能不能顺利撰写',
            control: h(Button, { size: 'sm', variant: 'text', onClick: () => props.onOpenEnv() }, '前往'),
          }),
        ),
        h(
          'div',
          { className: 'bgp-card' },
          h('div', { className: 'bgp-group-head' }, h('h2', { className: 'bgp-card-title' }, '撰写偏好（新项目默认值）')),
          settingRow('s2', {
            label: '默认字数档位',
            hint: '新项目默认：方案与预期总字数档位，对话里确认字数时，默认选这一档',
            defaultText: '5 万字',
            control: h(
              'div',
              { style: { display: 'flex', alignItems: 'center', gap: 8 } },
              h(Select, {
                value: isKnownTier ? String(wordTier) : '0',
                options: WORD_TIERS.map((tier) => ({ value: String(tier.value), label: tier.label })),
                onChange: (value) => {
                  const numeric = Number(value);
                  if (numeric === 0) {
                    if (isKnownTier) update('defaultWordCountTier', 60000);
                  } else {
                    update('defaultWordCountTier', numeric);
                  }
                },
              }),
              !isKnownTier
                ? h('input', {
                    className: 'bgp-input bgp-inline-input-wide',
                    type: 'number',
                    min: 10000,
                    step: 5000,
                    value: wordTier,
                    onChange: (event) => {
                      const numeric = Number(event.target.value);
                      if (numeric > 0) update('defaultWordCountTier', numeric);
                    },
                  })
                : null,
            ),
          }),
          settingRow('s3', {
            label: '默认图表数量',
            hint: '新项目默认：生成大纲时每类图的默认数量；可在对话里改',
            defaultText: '各 5 张',
            control: h(
              'div',
              { className: 'bgp-quota-grid' },
              ['table', 'mermaid', 'terminal'].map((key) =>
                h(
                  'label',
                  { key: key, style: { display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, color: 'var(--bgp-fg2)' } },
                  { table: '表\u3000格', mermaid: '示意图', terminal: '终端图' }[key],
                  h('input', {
                    className: 'bgp-input bgp-inline-input',
                    type: 'number',
                    min: 0,
                    max: 50,
                    value: figureQuota[key] == null ? 0 : figureQuota[key],
                    onChange: (event) => {
                      const numeric = Number(event.target.value);
                      if (Number.isFinite(numeric) && numeric >= 0) updateQuota(key, Math.min(50, Math.round(numeric)));
                    },
                  }),
                ),
              ),
            ),
          }),
          settingRow('s5', {
            label: '交互模式',
            hint: '逐步确认每次问你；自动推进只问关键项',
            defaultText: '逐步确认',
            control: h(Select, {
              value: draft.interactionMode === 'auto' ? 'auto' : 'guided',
              options: [
                { value: 'guided', label: '逐步确认' },
                { value: 'auto', label: '自动推进' },
              ],
              onChange: (value) => update('interactionMode', value),
            }),
          }),
          settingRow('s6', {
            label: '提问等待策略',
            hint: '助手会一直等你回答，不会替你猜（推荐）',
            defaultText: '一直等待',
            control: h('span', { className: 'bgp-small' }, '一直等待'),
          }),
          settingRow('s7', {
            label: '审查轮次上限',
            hint: '最多改几轮；到上限会停下来让你决定',
            defaultText: '5 轮',
            control: h('input', {
              className: 'bgp-input bgp-inline-input',
              type: 'number',
              min: 1,
              max: 10,
              value: draft.maxReviewRounds == null ? 5 : draft.maxReviewRounds,
              onChange: (event) => {
                const numeric = Number(event.target.value);
                if (Number.isFinite(numeric)) update('maxReviewRounds', Math.min(10, Math.max(1, Math.round(numeric))));
              },
            }),
          }),
          settingRow('s8', {
            label: '同时撰写章节数',
            hint: '越大越快，也越占资源',
            defaultText: '3 个',
            control: h('input', {
              className: 'bgp-input bgp-inline-input',
              type: 'number',
              min: 1,
              max: 6,
              value: draft.maxParallelWriters == null ? 3 : draft.maxParallelWriters,
              onChange: (event) => {
                const numeric = Number(event.target.value);
                if (Number.isFinite(numeric)) update('maxParallelWriters', Math.min(6, Math.max(1, Math.round(numeric))));
              },
            }),
          }),
          settingRow('s10', {
            label: '自动采用默认值',
            hint: '你不在时，非关键项自动用默认值并标注',
            defaultText: '开',
            control: h(Switch, { checked: draft.allowAutoDefaults !== false, onChange: (value) => update('allowAutoDefaults', value), ariaLabel: '自动采用默认值' }),
          }),
        ),
        h(
          'div',
          { className: 'bgp-card' },
          h('div', { className: 'bgp-group-head' }, h('h2', { className: 'bgp-card-title' }, '导出样式')),
          STYLE_DOC_TYPES.map((docType) =>
            settingRow('s4-' + docType.key, {
              label: docType.label,
              hint: docType.hint,
              defaultText: docType.defaultText,
              control: h(Select, {
                value: textOf(styleProfiles[docType.key]) || docType.defaultId,
                options: styleOptionsFor(docType.key),
                disabled: docType.key === 'overview' && !overviewSupported,
                onChange: (value) => updateStyleProfile(docType.key, value),
              }),
            }),
          ),
          h(
            'div',
            { className: 'bgp-actions', style: { marginTop: 8, flexWrap: 'wrap' } },
            stylesState.status === 'loading' ? h('span', { className: 'bgp-muted' }, '正在读取样式…') : null,
            styleCustomCount
              ? h(
                  'span',
                  { className: 'bgp-muted' },
                  '自定义样式 ' +
                    styleCustomCount +
                    ' 个（技术方案 ' +
                    styleCatalog.custom.tech.length +
                    '、关键信息 ' +
                    styleCatalog.custom.keyInfo.length +
                    '、工作概览 ' +
                    styleCatalog.custom.overview.length +
                    '）',
                )
              : h('span', { className: 'bgp-muted' }, '还没有自定义样式。把样式 JSON 放到 ' + styleDirHint + '，或让助手生成；刷新后出现。'),
            h(Button, { size: 'sm', variant: 'text', icon: 'refresh', onClick: loadStyles }, '刷新样式列表'),
            h(Button, { size: 'sm', variant: 'text', onClick: () => setStyleExampleOpen(!styleExampleOpen) }, styleExampleOpen ? '收起示例' : '看看合法样式怎么写'),
          ),
          styleExampleOpen
            ? h('pre', { className: 'bgp-style-example' }, STYLE_EXAMPLE_JSON)
            : null,
          stylesState.status === 'error'
            ? h('p', { className: 'bgp-muted', style: { marginTop: 6 } }, '样式列表暂时读取不到，先显示内置样式；点「刷新样式列表」重试。')
            : null,
          styleWarnings.length
            ? h(
                'div',
                { style: { marginTop: 8 } },
                h(
                  Banner,
                  { tone: 'warn', title: '有 ' + styleWarnings.length + ' 个样式文件没有被采用' },
                  h(
                    'div',
                    { className: 'bgp-style-warnings' },
                    styleWarnings.map((warning, index) =>
                      h(
                        'div',
                        { className: 'bgp-style-warning', key: index },
                        h('span', { className: 'bgp-style-warning-file' }, textOf(warning && warning.file) || '未知文件'),
                        h('span', { className: 'bgp-style-warning-msg' }, textOf(warning && warning.message) || '内容不完整'),
                      ),
                    ),
                  ),
                  h(
                    'p',
                    { className: 'bgp-muted', style: { marginTop: 4 } },
                    '常见原因：文件不是合法 JSON（例如复制示例时保留了「...」占位符，或逗号/引号写错），或文件没有放在对应目录。点上方「看看合法样式怎么写」可对照示例。',
                  ),
                ),
              )
            : null,
          h(
            'p',
            { className: 'bgp-muted', style: { marginTop: 6 } },
            '样式文件必须是合法 JSON，并放在对应文档的目录里（' + styleDirHint + '）；文件名随意，id/name 不能少。改好后点「刷新样式列表」。',
          ),
        ),
        h(
          'div',
          { className: 'bgp-card' },
          h(
            'div',
            { className: 'bgp-group-head' },
            h('h2', { className: 'bgp-card-title', style: { flex: '1 1 auto' } }, '高级'),
            h(Button, { size: 'sm', variant: 'text', onClick: () => setAdvancedOpen(!advancedOpen) }, advancedOpen ? '收起' : '展开'),
          ),
          advancedOpen
            ? h(
                'div',
                null,
                settingRow('s15', {
                  label: '日志级别',
                  hint: '排障用；详细会多写日志',
                  defaultText: '普通',
                  control: h(Select, {
                    value: draft.logLevel === 'debug' ? 'debug' : 'info',
                    options: [
                      { value: 'info', label: '普通' },
                      { value: 'debug', label: '详细' },
                    ],
                    onChange: (value) => update('logLevel', value),
                  }),
                }),
                h('p', { className: 'bgp-muted', style: { marginTop: 8 } }, '这一组给排障使用，平时不需要改。'),
              )
            : h('p', { className: 'bgp-muted' }, '给排障用的选项，默认收起。'),
        ),
      );
    }

    // ────────────────────────────────────────────────────────────
    // 工作区未初始化引导
    // ────────────────────────────────────────────────────────────

    function InitGuide(props) {
      const [busy, setBusy] = useState(false);
      const doInit = () => {
        if (busy) return;
        setBusy(true);
        api
          .initWorkspace(props.workspace)
          .then(() => {
            props.notify('success', '数据目录已创建');
            props.onInited && props.onInited();
          })
          .catch((error) => props.notify('error', describeError(error).message))
          .then(() => setBusy(false));
      };
      return h(
        'div',
        { className: 'bgp-page bgp-page-narrow' },
        h(
          'div',
          { className: 'bgp-card' },
          h('h1', { className: 'bgp-h1' }, '这个工作区还没有标代精灵数据'),
          h(
            'p',
            { className: 'bgp-desc', style: { marginTop: 6 } },
            '创建后就能在这里管理投标人、查看项目进度；撰写仍然在对话框里进行。创建只会新建必要的数据文件夹，不会开始撰写。',
          ),
          h(
            'div',
            { className: 'bgp-actions', style: { marginTop: 12 } },
            h(Button, { variant: 'primary', icon: 'check', onClick: doInit, disabled: busy }, busy ? '创建中…' : '创建数据目录'),
            h(Button, { variant: 'secondary', icon: 'copy', onClick: () => props.showCopy('[BidGenie] 帮我初始化工作区') }, '复制启动指令'),
            h(Button, { variant: 'text', onClick: props.onRetry }, '重新检查'),
          ),
          h('div', { style: { marginTop: 12 } }, h(NextStep, { text: '创建完成后，先建一份投标人资料，再去对话框上传招标文件。' })),
        ),
      );
    }

    // ────────────────────────────────────────────────────────────
    // 应用外壳
    // ────────────────────────────────────────────────────────────

    function Panel(props) {
      const activeWorkspace = props && typeof props.useSessions === 'function'
        ? props.useSessions((state) => {
            const byId = state && state.byId ? state.byId : {};
            const current = Object.values(byId).find((session) => ((session && session.retainedBy && session.retainedBy.mainView) || 0) > 0);
            return current && typeof current.cwd === 'string' ? current.cwd : '';
          })
        : '';
      const { toasts, notify } = useToasts();
      const { confirm, dialog } = useConfirmer();
      const copyWithToast = useClipboard(notify);

      const [wsState, setWsState] = useState({ status: 'loading', list: [], error: null });
      const [workspace, setWorkspace] = useState(() => readStoredWorkspace());
      const [ready, setReady] = useState({ status: 'idle', error: null });
      const [pluginVersion, setPluginVersion] = useState('');
      const [page, setPage] = useState('projects');
      const [returnTo, setReturnTo] = useState('projects');
      const [refreshKey, setRefreshKey] = useState(0);
      const [settingsState, setSettingsState] = useState({ status: 'idle', data: Object.assign({}, SETTINGS_DEFAULTS), error: null });
      const mounted = useMountedRef();

      useEffect(() => {
        ensureStyles();
        ensureExtraStyles();
      }, []);

      const loadWorkspaces = useCallback(() => {
        setWsState({ status: 'loading', list: [], error: null });
        api
          .workspaces()
          .then((data) => {
            if (!mounted.current) return;
            const list = normalizeWorkspaces(data);
            setWsState({ status: 'ready', list: list, error: null });
            setWorkspace((prev) => {
              if (prev && list.some((item) => item.path === prev)) return prev;
              const stored = readStoredWorkspace();
              const storedMatch = list.filter((item) => item.path === stored)[0];
              if (storedMatch) return storedMatch.path;
              return list.length ? list[0].path : '';
            });
          })
          .catch((error) => {
            if (!mounted.current) return;
            setWsState({ status: 'error', list: [], error: error });
          });
      }, [mounted]);

      useEffect(() => {
        loadWorkspaces();
      }, [loadWorkspaces]);

      useEffect(() => {
        if (workspace) storeWorkspace(workspace);
      }, [workspace]);

      useEffect(() => {
        if (!activeWorkspace) return;
        setWorkspace((prev) => (prev === activeWorkspace ? prev : activeWorkspace));
      }, [activeWorkspace, wsState.status]);

      const applyProbeResults = (results) => {
        if (!mounted.current) return;
        const failed = results.filter((item) => !item.ok);
        if (failed.length === 3 && failed.every((item) => isNotFound(item.error))) {
          setReady({ status: 'uninitialized', error: null });
          return;
        }
        if (results[1].ok) {
          const settings = isPlainObject(results[1].value && results[1].value.settings) ? results[1].value.settings : {};
          setSettingsState({ status: 'ready', data: Object.assign({}, SETTINGS_DEFAULTS, settings), error: null });
        }
        if (failed.length === 3) {
          setReady({ status: 'error', error: failed[0].error });
          return;
        }
        setReady({ status: 'ready', error: null });
      };

      const probeRoutes = () =>
        Promise.all([
          api.listBidders(workspace).then(
            (data) => ({ ok: true, value: data }),
            (error) => ({ ok: false, error: error }),
          ),
          api.getSettings(workspace).then(
            (data) => ({ ok: true, value: data }),
            (error) => ({ ok: false, error: error }),
          ),
          api.listProjects(workspace).then(
            (data) => ({ ok: true, value: data }),
            (error) => ({ ok: false, error: error }),
          ),
        ]);

      const checkReady = useCallback(() => {
        if (!workspace) {
          setReady({ status: 'idle', error: null });
          return;
        }
        setReady((prev) => ({ status: prev.status === 'ready' ? 'ready' : 'checking', error: null }));
        // Host /health 在未初始化时也返回 200；initialized=false 时直接给引导卡
        api
          .health(workspace)
          .then((data) => {
            if (!mounted.current) return;
            if (data && data.version) setPluginVersion(data.version);
            if (data && data.initialized === false) {
              setReady({ status: 'uninitialized', error: null });
              return;
            }
            probeRoutes().then(applyProbeResults);
          })
          .catch(() => {
            probeRoutes().then(applyProbeResults);
          });
      }, [workspace, mounted]);

      useEffect(() => {
        checkReady();
      }, [checkReady, refreshKey]);

      const refresh = () => {
        setRefreshKey((value) => value + 1);
      };

      const openSubpage = (name) => {
        setReturnTo(page === 'env' || page === 'about' ? returnTo : page);
        setPage(name);
      };

      const goPage = (name) => {
        setPage(name);
      };

      const onSettingsChanged = useCallback((next) => {
        setSettingsState({ status: 'ready', data: Object.assign({}, SETTINGS_DEFAULTS, next), error: null });
      }, []);

      const maskSensitive = settingsState.data.maskSensitive !== false;

      const renderTabs = () =>
        h(
          'div',
          { className: 'bgp-tabs', role: 'tablist' },
          [
            { key: 'projects', label: '项目' },
            { key: 'bidders', label: '投标人' },
            { key: 'settings', label: '设置' },
          ].map((tab) =>
            h(
              'button',
              {
                key: tab.key,
                type: 'button',
                role: 'tab',
                'aria-selected': page === tab.key ? 'true' : 'false',
                className: classNames('bgp-tab', page === tab.key ? 'is-active' : ''),
                onClick: () => goPage(tab.key),
              },
              tab.label,
            ),
          ),
        );

      let body = null;
      if (wsState.status === 'loading') {
        body = h('div', { className: 'bgp-page bgp-page-narrow' }, h(Loader, { text: '正在读取工作区…' }));
      } else if (wsState.status === 'error') {
        body = h(
          'div',
          { className: 'bgp-page bgp-page-narrow' },
          h(ErrorState, { title: '读取工作区失败', error: wsState.error, onRetry: loadWorkspaces }),
        );
      } else if (!workspace) {
        body = h(
          'div',
          { className: 'bgp-page bgp-page-narrow' },
          h(
            'div',
            { className: 'bgp-card' },
            h(EmptyState, {
              title: '还没有添加工作区',
              text: '先在 DSH 里添加一个文件夹作为工作区，标代精灵的资料会保存在它下面的数据目录里。',
              actions: h('div', { className: 'bgp-actions' }, h(Button, { variant: 'secondary', onClick: loadWorkspaces }, '刷新')),
            }),
          ),
        );
      } else if (ready.status === 'checking' || ready.status === 'idle') {
        body = h('div', { className: 'bgp-page bgp-page-narrow' }, h(Loader, { text: '正在准备…' }));
      } else if (ready.status === 'error') {
        body = h(
          'div',
          { className: 'bgp-page bgp-page-narrow' },
          h(ErrorState, { title: '这个工作区暂时读取不了', error: ready.error, onRetry: () => setRefreshKey((value) => value + 1) }),
        );
      } else if (ready.status === 'uninitialized') {
        body = h(InitGuide, {
          workspace: workspace,
          notify: notify,
          showCopy: copyWithToast,
          onRetry: () => setRefreshKey((value) => value + 1),
          onInited: () => setRefreshKey((value) => value + 1),
        });
      } else if (page === 'env') {
        body = h(EnvCheckPage, {
          workspace: workspace,
          notify: notify,
          showCopy: copyWithToast,
          onBack: () => goPage(returnTo),
          onCopyInstruction: (text) => copyWithToast(text, '已复制，粘贴到对话框发送'),
        });
      } else if (page === 'about') {
        body = h(AboutPage, {
          workspace: workspace,
          showCopy: copyWithToast,
          onBack: () => goPage(returnTo),
        });
      } else if (page === 'bidders') {
        body = h(BiddersPage, {
          key: workspace,
          workspace: workspace,
          notify: notify,
          confirm: confirm,
          maskSensitive: maskSensitive,
          refreshKey: refreshKey,
          showCopy: copyWithToast,
          onNavigate: goPage,
          onDataChanged: () => refresh(),
        });
      } else if (page === 'settings') {
        body = h(SettingsPage, {
          key: workspace,
          workspace: workspace,
          settings: settingsState.data,
          notify: notify,
          refreshKey: refreshKey,
          showCopy: copyWithToast,
          onNavigate: goPage,
          onOpenEnv: () => openSubpage('env'),
          onSettingsChanged: onSettingsChanged,
        });
      } else {
        body = h(ProjectsPage, {
          key: workspace,
          workspace: workspace,
          notify: notify,
          refreshKey: refreshKey,
          showCopy: copyWithToast,
          onNavigate: goPage,
        });
      }

      const headerEl = h(
        'div',
        { className: 'bgp-header' },
        h(
          'div',
          { className: 'bgp-brand' },
          h(BidGenieIcon, { size: 18 }),
          h(
            'span',
            { className: 'bgp-brand-title' },
            h('span', null, '标代精灵'),
            h('span', { className: 'bgp-brand-version' }, displayVersion(pluginVersion)),
          ),
        ),
        h('div', { className: 'bgp-header-spacer' }),
        h(
          'div',
          { className: 'bgp-ws' },
          h('span', { className: 'bgp-ws-label' }, '工作区'),
          h(Select, {
            value: workspace,
            disabled: wsState.status !== 'ready' || wsState.list.length === 0,
            options: wsState.list.map((item) => ({ value: item.path, label: workspaceLabel(item) })),
            onChange: (value) => setWorkspace(value),
            ariaLabel: '选择工作区',
          }),
        ),
        h(IconButton, { name: 'refresh', label: '刷新', onClick: refresh }),
        h(IconButton, { name: 'shield', label: '环境检查', onClick: () => openSubpage('env') }),
        h(IconButton, { name: 'info', label: '关于', onClick: () => openSubpage('about') }),
      );
      return h(
        'div',
        { className: 'bgp-root' },
        h(
          'div',
          { className: 'bgp-topbar' },
          headerEl,
          page === 'env' || page === 'about' ? null : renderTabs(),
        ),
        h('div', { className: 'bgp-content' }, body),
        h(ToastHost, { toasts: toasts }),
        dialog,
      );
    }

    // ────────────────────────────────────────────────────────────
    // 注册
    // ────────────────────────────────────────────────────────────

    return {
      inject: ['slots'],
      apply(ctx) {
        if (ctx && typeof ctx.effect === 'function') {
          ctx.effect(() => {
            ensureStyles();
            ensureExtraStyles();
            return () => {
              try {
                const main = document.getElementById(STYLE_ID);
                if (main && main.parentNode) main.parentNode.removeChild(main);
                const extra = document.getElementById(EXTRA_STYLE_ID);
                if (extra && extra.parentNode) extra.parentNode.removeChild(extra);
              } catch (error) {
                /* 面板退出时清理失败不阻塞 */
              }
            };
          }, 'dsh-bidgenie: styles');
        } else {
          try {
            ensureStyles();
            ensureExtraStyles();
          } catch (error) {
            /* 样式失败不影响面板注册 */
          }
        }
        ctx.slots.inject('sidebar.panellist', () =>
          ctx.slots.register(
            { name: 'sidebar.panellist', id: 'bidgenie', order: 30, label: () => '标代精灵' },
            BidGenieIcon,
          ),
        );
        ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: 'bidgenie' }, Panel));
      },
    };
  },
});
