/**
 * BidGenie M1 - model-facing tools.
 *
 * M0 lesson (PRD 6.6.1 / devdoc §1.3): every tool MUST declare
 * `output: { schema, render }`; omitting it fails the whole plugin activation.
 * M1 exposes the minimum set from CONTRACT §6.6.1: `bidgenie_paths` and
 * `bidgenie_bidder`. Both only call `lib/services.js` (single implementation).
 */
import {
  bidderCreate,
  bidderDelete,
  bidderDetail,
  bidderList,
  bidderSetCurrent,
  bidderUpdate,
  errorPayload,
  pathsInfo,
} from './services.js';

/** Render tool results as one JSON text block (M0-verified shape). */
function renderJson(_args, value) {
  return [{ type: 'text', text: JSON.stringify(value, null, 2) }];
}

const OUTPUT_SCHEMA = { type: 'object' };

async function guarded(operation) {
  try {
    return await operation();
  } catch (error) {
    return errorPayload(error);
  }
}

/**
 * `data` may be the CONTRACT `{bidder:{...}, certificates?, performance?, awardsPenalties?}`
 * shape or (for convenience) a bare bidder object.
 */
function normalizeToolData(data) {
  if (data === null || typeof data !== 'object' || Array.isArray(data)) return {};
  if (data.bidder !== null && typeof data.bidder === 'object') return data;
  if (typeof data.displayName === 'string' || typeof data.bidderType === 'string') return { bidder: data };
  return data;
}

function registerPathsTool(tools, ctx) {
  return tools.register({
    name: 'bidgenie_paths',
    description:
      'BidGenie: resolve plugin and workspace paths (plugin package root, bundled python executable, scripts/templates directories, workspace root). ' +
      'Pass the absolute `workspace` path supplied by the user or the session cwd; the Host never infers a workspace from its own cwd or environment. ' +
      'Returns {ok, pluginRoot, workspaceRoot, pythonExe, scriptsDir, templatesDir, hostVersion, ...} or {ok:false, error, message, hint}.',
    parameters: {
      type: 'object',
      properties: {
        workspace: {
          type: 'string',
          description: 'Absolute path of a DSH-registered workspace. Optional: omit to only resolve plugin-side paths.',
        },
      },
      required: [],
      additionalProperties: false,
    },
    output: { schema: OUTPUT_SCHEMA, render: renderJson },
    timeoutMs: 15_000,
    async execute(args) {
      const workspace = typeof args?.workspace === 'string' && args.workspace.trim() !== '' ? args.workspace.trim() : undefined;
      return guarded(() => pathsInfo(ctx, workspace));
    },
  });
}

function registerBidderTool(tools, ctx) {
  return tools.register({
    name: 'bidgenie_bidder',
    description:
      'BidGenie: manage bidders (投标人) in one DSH-registered workspace. ' +
      'action=list returns the bidder list (or one detail when bidderId is given); ' +
      'action=create needs data={bidder:{displayName,bidderType,...}, certificates?, performance?, awardsPenalties?}; ' +
      'action=update needs bidderId and data={patch:{...}} (or {bidder:{...}}); ' +
      'action=delete needs bidderId and data={confirm:true} (soft delete to .trash); ' +
      'action=set-current needs bidderId. All writes go through the bundled bg_bidder.py script. ' +
      'Returns {ok:true, ...} or {ok:false, error, message, hint}.',
    parameters: {
      type: 'object',
      properties: {
        workspace: {
          type: 'string',
          description: 'Absolute path of a DSH-registered workspace (required).',
        },
        action: {
          type: 'string',
          enum: ['list', 'create', 'update', 'delete', 'set-current'],
          description: 'Operation to perform.',
        },
        bidderId: {
          type: 'string',
          description: 'Bidder id (bdr-...). Required for update/delete/set-current and for detail reads.',
        },
        data: {
          type: 'object',
          description:
            'Payload for create/update/delete: create -> {bidder:{...}, certificates?, performance?, awardsPenalties?}; update -> {patch:{...}} or {bidder:{...}}; delete -> {confirm:true}.',
        },
      },
      required: ['workspace', 'action'],
      additionalProperties: false,
    },
    output: { schema: OUTPUT_SCHEMA, render: renderJson },
    timeoutMs: 150_000,
    async execute(args) {
      const workspace = args?.workspace;
      const action = args?.action;
      const bidderId = typeof args?.bidderId === 'string' && args.bidderId.trim() !== '' ? args.bidderId.trim() : undefined;
      const data = args?.data;
      return guarded(async () => {
        switch (action) {
          case 'list':
            if (bidderId !== undefined) return bidderDetail(ctx, { workspace, bidderId });
            return bidderList(ctx, { workspace });
          case 'create':
            return bidderCreate(ctx, { workspace, data: normalizeToolData(data) });
          case 'update':
            return bidderUpdate(ctx, { workspace, bidderId, data: data ?? {} });
          case 'delete':
            return bidderDelete(ctx, { workspace, bidderId, confirm: data?.confirm === true });
          case 'set-current':
            return bidderSetCurrent(ctx, { workspace, bidderId });
          default:
            return {
              ok: false,
              error: 'E_USAGE',
              message: `未知 action：${String(action)}`,
              hint: 'action 取值：list / create / update / delete / set-current',
            };
        }
      });
    },
  });
}

/**
 * Register BidGenie tools on the Host tool registry.
 *
 * @param {object} ctx Cordis context that declared `inject: ['tools']`.
 */
export function registerTools(ctx) {
  ctx.effect(() => {
    const tools = ctx.get('tools');
    if (tools === undefined) return () => {};
    const disposers = [registerPathsTool(tools, ctx), registerBidderTool(tools, ctx)];
    return () => {
      for (const dispose of disposers.reverse()) {
        try {
          dispose();
        } catch {
          /* registry going away */
        }
      }
    };
  }, 'bidgenie: tools');
}
