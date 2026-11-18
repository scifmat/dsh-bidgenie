/**
 * BidGenie M1 - panel HTTP routes (`/bidgenie/*`, same-origin only).
 *
 * Frozen paths (exact, no dynamic segments - M1-00 verified):
 *   GET    /bidgenie/health
 *   GET    /bidgenie/workspaces
 *   POST   /bidgenie/workspace/init
 *   GET    /bidgenie/bidders            (list; `?bidderId=` returns one detail)
 *   POST   /bidgenie/bidders            (create; CONTRACT §3 compatibility)
 *   POST   /bidgenie/bidders/create
 *   POST   /bidgenie/bidders/update
 *   POST   /bidgenie/bidders/delete
 *   POST   /bidgenie/bidders/set-current
 *
 * Panel writes are user actions: services pass BIDGENIE_ACTOR=user /
 * BIDGENIE_SOURCE=panel to the deterministic scripts and echo
 * `source:"panel"` / `actor:"user"` in the JSON response (event attribution
 * `actor=user` is written by `_lib/logging.py`, backend P1 task-106).
 * Bidder create/update/detail responses also carry `completeness` + `warnings`
 * (bidder.incomplete) so the panel can show the completeness warning.
 *   GET    /bidgenie/settings
 *   PUT    /bidgenie/settings
 *   GET    /bidgenie/styles            (built-in + workspace custom export styles; ?kind=tech|keyInfo|overview)
 *   GET    /bidgenie/projects
 *   GET    /bidgenie/projects/state    (?workspace=&project=<projectId|目录名>)
 *   POST   /bidgenie/projects/init     ({workspace, bidderId?, projectName?})
 *   GET    /bidgenie/env-check
 *   POST   /bidgenie/env-check/run     ({workspace, itemIds?})；若存在未 finalize 的 run-manifest，
 *                                        刷新成功会把 envRefreshedAfterStart 写回 start 记录并在响应 warnings 提示
 *   POST   /bidgenie/env-check/repair/plan  ({workspace}) 只读修复计划（auto/assisted/manual/pluginDefect）
 *   POST   /bidgenie/env-check/repair       ({workspace, actionIds?, items?, yes:true}) 执行自动修复，20 分钟超时
 *   GET    /bidgenie/env-check/repair/last  (?workspace=) 最近一次修复后的校验 + 日志尾部
 *
 * One handler per path branches on the method (never two registrations for one
 * path). M1-00 additionally proved `kind:'prefix'`, so two *additive* prefix
 * routes exist: a CONTRACT §3 compatibility layer for
 * `PUT|DELETE /bidgenie/bidders/<id>` + `POST /bidgenie/bidders/<id>/current`
 * (the canonical M1 writes stay the exact paths above), and a `/bidgenie`
 * catch-all that guarantees JSON 404 for unknown sub-paths instead of the SPA
 * fallback. All responses are `{ok:true,...}` / `{ok:false,error,message,hint?}`.
 */
import {
  about,
  bidderCreate,
  bidderDelete,
  bidderDetail,
  bidderList,
  bidderSetCurrent,
  bidderUpdate,
  bgError,
  envCheckGet,
  envCheckRepair,
  envCheckRepairLast,
  envCheckRepairPlan,
  envCheckRun,
  errorPayload,
  health,
  httpStatusFor,
  listWorkspaces,
  projectsInit,
  projectsList,
  projectState,
  settingsGet,
  settingsPut,
  stylesList,
  workspaceInit,
} from './services.js';

const WRITE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
const MAX_BODY_BYTES = 2 * 1024 * 1024;

/* ----------------------------------------------------------------------------- */
/* HTTP helpers                                                                 */
/* ----------------------------------------------------------------------------- */

function sendJson(response, status, payload, extraHeaders = {}) {
  if (response.writableEnded) return;
  response.statusCode = status;
  response.setHeader('content-type', 'application/json; charset=utf-8');
  response.setHeader('cache-control', 'no-store');
  for (const [key, value] of Object.entries(extraHeaders)) response.setHeader(key, value);
  response.end(JSON.stringify(payload));
}

function urlOf(request) {
  return new URL(request.url ?? '/', 'http://127.0.0.1');
}

/** First value of each query parameter (`?workspace=...&bidderId=...`). */
function queryObject(url) {
  const query = {};
  for (const [key, value] of url.searchParams) {
    if (!(key in query)) query[key] = value;
  }
  return query;
}

function readBody(request) {
  return new Promise((resolvePromise, rejectPromise) => {
    const chunks = [];
    let total = 0;
    request.on('data', (chunk) => {
      total += chunk.length;
      if (total > MAX_BODY_BYTES) {
        rejectPromise(bgError('E_SCHEMA_INVALID', '请求体过大', '请求体上限 2MB'));
        try {
          request.destroy();
        } catch {
          /* already closed */
        }
        return;
      }
      chunks.push(chunk);
    });
    request.on('end', () => {
      const text = Buffer.concat(chunks).toString('utf8').replace(/^\uFEFF/, '').trim();
      if (text === '') {
        resolvePromise({});
        return;
      }
      let parsed;
      try {
        parsed = JSON.parse(text);
      } catch (error) {
        rejectPromise(bgError('E_SCHEMA_INVALID', '请求体不是合法 JSON', String(error?.message ?? error)));
        return;
      }
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        rejectPromise(bgError('E_SCHEMA_INVALID', '请求体必须是 JSON 对象', '预期 {workspace: "..."} 形式'));
        return;
      }
      resolvePromise(parsed);
    });
    request.on('error', (error) => rejectPromise(error));
  });
}

/** Write requests: same-origin + `x-bidgenie-csrf: 1` (CONTRACT §3). */
function assertWriteAllowed(request) {
  if (request.headers['x-bidgenie-csrf'] !== '1') {
    throw bgError('E_USAGE', '写请求缺少 x-bidgenie-csrf: 1 请求头', '面板写操作必须携带同源 CSRF 头');
  }
  const origin = request.headers.origin;
  if (typeof origin === 'string' && origin !== '' && origin !== 'null') {
    const host = request.headers.host ?? '';
    const allowed = new Set([`http://${host}`, `https://${host}`]);
    if (!allowed.has(origin)) {
      throw bgError('E_USAGE', '跨域写请求被拒绝', `origin=${origin}`);
    }
  }
  const site = request.headers['sec-fetch-site'];
  if (typeof site === 'string' && site !== '' && site !== 'same-origin' && site !== 'none') {
    throw bgError('E_USAGE', '非同一站点写请求被拒绝', `sec-fetch-site=${site}`);
  }
}

/* ----------------------------------------------------------------------------- */
/* Exact routes (canonical M1 contract)                                         */
/* ----------------------------------------------------------------------------- */

const ROUTES = [
  {
    path: '/bidgenie/health',
    methods: ['GET'],
    run: (ctx, input) => health(ctx, input.workspace),
  },
  {
    path: '/bidgenie/about',
    methods: ['GET'],
    run: () => about(),
  },
  {
    path: '/bidgenie/workspaces',
    methods: ['GET'],
    run: (ctx) => ({ ok: true, workspaces: listWorkspaces(ctx) }),
  },
  {
    path: '/bidgenie/workspace/init',
    methods: ['POST'],
    run: (ctx, input) => workspaceInit(ctx, input.body),
  },
  {
    path: '/bidgenie/bidders',
    methods: ['GET', 'POST'],
    run: (ctx, input) => {
      if (input.method === 'GET') {
        if (typeof input.query.bidderId === 'string' && input.query.bidderId !== '') {
          return bidderDetail(ctx, { workspace: input.workspace, bidderId: input.query.bidderId });
        }
        return bidderList(ctx, { workspace: input.workspace });
      }
      return bidderCreate(ctx, input.body);
    },
  },
  {
    path: '/bidgenie/bidders/create',
    methods: ['POST'],
    run: (ctx, input) => bidderCreate(ctx, input.body),
  },
  {
    path: '/bidgenie/bidders/update',
    methods: ['POST'],
    run: (ctx, input) => bidderUpdate(ctx, input.body),
  },
  {
    path: '/bidgenie/bidders/delete',
    methods: ['POST'],
    run: (ctx, input) => bidderDelete(ctx, input.body),
  },
  {
    path: '/bidgenie/bidders/set-current',
    methods: ['POST'],
    run: (ctx, input) => bidderSetCurrent(ctx, input.body),
  },
  {
    path: '/bidgenie/settings',
    methods: ['GET', 'PUT'],
    run: (ctx, input) =>
      input.method === 'GET' ? settingsGet(ctx, { workspace: input.workspace }) : settingsPut(ctx, input.body),
  },
  {
    path: '/bidgenie/styles',
    methods: ['GET'],
    run: (ctx, input) => stylesList(ctx, { workspace: input.workspace, kind: input.query.kind }),
  },
  {
    path: '/bidgenie/projects',
    methods: ['GET'],
    run: (ctx, input) => projectsList(ctx, { workspace: input.workspace }),
  },
  {
    path: '/bidgenie/projects/state',
    methods: ['GET'],
    run: (ctx, input) =>
      projectState(ctx, { workspace: input.workspace, project: input.query.project }),
  },
  {
    path: '/bidgenie/projects/init',
    methods: ['POST'],
    run: (ctx, input) => projectsInit(ctx, input.body),
  },
  {
    path: '/bidgenie/env-check',
    methods: ['GET'],
    run: (ctx, input) => envCheckGet(ctx, { workspace: input.workspace }),
  },
  {
    path: '/bidgenie/env-check/run',
    methods: ['POST'],
    run: (ctx, input) => envCheckRun(ctx, input.body),
  },
  {
    path: '/bidgenie/env-check/repair/plan',
    methods: ['POST'],
    run: (ctx, input) => envCheckRepairPlan(ctx, input.body),
  },
  {
    path: '/bidgenie/env-check/repair',
    methods: ['POST'],
    run: (ctx, input) => envCheckRepair(ctx, input.body),
  },
  {
    path: '/bidgenie/env-check/repair/last',
    methods: ['GET'],
    run: (ctx, input) => envCheckRepairLast(ctx, { workspace: input.workspace }),
  },
];

function makeRouteHandler(ctx, route) {
  return async (request, response) => {
    const method = String(request.method ?? 'GET').toUpperCase();
    if (!route.methods.includes(method)) {
      sendJson(
        response,
        405,
        {
          ok: false,
          error: 'E_USAGE',
          message: `方法不支持：${method} ${urlOf(request).pathname}`,
          hint: `允许的方法：${route.methods.join(', ')}`,
        },
        { allow: route.methods.join(', ') },
      );
      return;
    }
    try {
      if (WRITE_METHODS.has(method)) assertWriteAllowed(request);
      const url = urlOf(request);
      const query = queryObject(url);
      const body = WRITE_METHODS.has(method) ? await readBody(request) : {};
      const bodyWorkspace = typeof body.workspace === 'string' ? body.workspace : undefined;
      const queryWorkspace = typeof query.workspace === 'string' ? query.workspace : undefined;
      const input = { method, workspace: bodyWorkspace ?? queryWorkspace, query, body, request };
      const payload = await route.run(ctx, input);
      sendJson(response, 200, payload === undefined ? { ok: true } : payload);
    } catch (error) {
      sendJson(response, httpStatusFor(error), errorPayload(error));
    }
  };
}

/* ----------------------------------------------------------------------------- */
/* Additive prefix routes (M1-00 proved prefix support)                         */
/* ----------------------------------------------------------------------------- */

/** CONTRACT §3 compatibility: `PUT|DELETE /bidgenie/bidders/<id>`, `POST /bidgenie/bidders/<id>/current`. */
function makeBidderCompatHandler(ctx) {
  const prefix = '/bidgenie/bidders';
  return async (request, response) => {
    const method = String(request.method ?? 'GET').toUpperCase();
    const url = urlOf(request);
    const segments = url.pathname
      .slice(prefix.length)
      .split('/')
      .filter((segment) => segment !== '')
      .map((segment) => {
        try {
          return decodeURIComponent(segment);
        } catch {
          return segment;
        }
      });
    try {
      if (segments.length === 1 && (method === 'PUT' || method === 'PATCH')) {
        assertWriteAllowed(request);
        const body = await readBody(request);
        sendJson(response, 200, await bidderUpdate(ctx, { ...body, bidderId: segments[0] }));
        return;
      }
      if (segments.length === 1 && method === 'DELETE') {
        assertWriteAllowed(request);
        const body = await readBody(request);
        sendJson(response, 200, await bidderDelete(ctx, { ...body, bidderId: segments[0] }));
        return;
      }
      if (segments.length === 2 && segments[1] === 'current' && method === 'POST') {
        assertWriteAllowed(request);
        const body = await readBody(request);
        sendJson(response, 200, await bidderSetCurrent(ctx, { ...body, bidderId: segments[0] }));
        return;
      }
      if (segments.length === 1) {
        sendJson(
          response,
          405,
          { ok: false, error: 'E_USAGE', message: `方法不支持：${method} ${url.pathname}`, hint: '允许的方法：PUT, PATCH, DELETE' },
          { allow: 'PUT, PATCH, DELETE' },
        );
        return;
      }
      if (segments.length === 2 && segments[1] === 'current') {
        sendJson(
          response,
          405,
          { ok: false, error: 'E_USAGE', message: `方法不支持：${method} ${url.pathname}`, hint: '允许的方法：POST' },
          { allow: 'POST' },
        );
        return;
      }
      sendJson(response, 404, {
        ok: false,
        error: 'E_NOT_FOUND',
        message: `未知路径：${url.pathname}`,
        hint: '投标人相关路径支持：GET/POST /bidgenie/bidders、POST /bidgenie/bidders/{create,update,delete,set-current}',
      });
    } catch (error) {
      sendJson(response, httpStatusFor(error), errorPayload(error));
    }
  };
}

/** Unknown `/bidgenie/*` sub-path -> JSON 404 (never the SPA fallback). */
function makeNotFoundHandler() {
  return (request, response) => {
    const url = urlOf(request);
    sendJson(response, 404, {
      ok: false,
      error: 'E_NOT_FOUND',
      message: `未知的 BidGenie 接口：${url.pathname}`,
      hint: '请核对 CONTRACT §3 路由表与插件版本是否一致',
    });
  };
}

/* ----------------------------------------------------------------------------- */
/* Registration                                                                 */
/* ----------------------------------------------------------------------------- */

/**
 * Register every `/bidgenie/*` route on the Host web server.
 * Must be called inside `ctx.inject(['webServer'], ...)`.
 *
 * @param {object} webCtx Cordis context owning the `webServer` service.
 */
export function registerRoutes(webCtx) {
  webCtx.effect(() => {
    const webServer = webCtx.get('webServer');
    if (webServer === undefined) return () => {};
    const disposers = [];
    for (const route of ROUTES) {
      disposers.push(
        webServer.register({
          kind: 'exact',
          path: route.path,
          handler: makeRouteHandler(webCtx, route),
        }),
      );
    }
    disposers.push(webServer.register({ kind: 'prefix', path: '/bidgenie/bidders', handler: makeBidderCompatHandler(webCtx) }));
    disposers.push(webServer.register({ kind: 'prefix', path: '/bidgenie', handler: makeNotFoundHandler() }));
    return () => {
      for (const dispose of disposers.reverse()) {
        try {
          dispose();
        } catch {
          /* the server is going away */
        }
      }
    };
  }, 'bidgenie: routes');
}
