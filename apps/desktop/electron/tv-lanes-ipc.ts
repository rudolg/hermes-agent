/**
 * `hermes:tv-lanes:*` — TradingView CDP lane state for the sidebar's lane dot (owner ask, 2026-10-01). Main reads the
 * three local files the menu-bar lamp already trusts and answers one IPC; it never drives the browser:
 *   <home>/.tradingview-mcp/channels.json         the lane registry (label, color, layouts)
 *   <home>/.tradingview-mcp/channel_status.json   the ~60 s publisher snapshot (claim, health, charts)
 *   <home>/.tradingview-mcp/hermes_bindings.json  THIS app's declarations: workspace path -> lane, session id -> lane
 * plus, per workspace the renderer names, the CLI's own declaration `<workspace>/.mcp.json` env TV_CDP_CHANNEL —
 * returned as a DECLARATION only, because Hermes does not run that tree's server (the arms' ruling, 2026-10-01: never
 * present a declared lane as an operational binding).
 *
 * A bind writes the bindings file atomically (temp + rename) and refuses a lane the registry does not know; `create`
 * hands off to the lamp's own one-click "new Hermes lane" script, which asks for the project / name in its dialogs.
 */
import { execFile, execFileSync, spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

import { ipcMain } from 'electron'

import type { TvLaneBindRequest, TvLaneBindResult, TvLaneRunRequest, TvLanesSnapshot, TvLaneView, TvWriteLease } from './tv-lanes-types'

const LANE_ID_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/
const SESSION_ID_RE = /^[A-Za-z0-9_-]{6,64}$/
const SNAPSHOT_MAX_PATHS = 64
const ANCESTRY_HOPS = 8
const HOLDER_CACHE_MS = 60_000
/** The lamp's provisioner (menubar-plugins); overridable for a machine where it lives elsewhere. */
const NEW_LANE_SCRIPT = process.env.HERMES_TV_LANE_NEW_SCRIPT || '/Users/spinec/src/menubar-plugins/tv-lane-new-hermes.py'
/** The lamp's lane scripts this app may start (owner's ask 2026-10-01: allowed charts per lane from the dot menu too). */
const LAMP_DIR = process.env.HERMES_TV_LAMP_DIR || '/Users/spinec/src/menubar-plugins'

export interface TvLanesIpcDeps {
  homeDir: string
  /** Test seam: the detached process starter; defaults to child_process.spawn. */
  spawnProcess?: typeof spawn
  /** Test seam: ps ancestry lookup; defaults to /bin/ps. */
  readProcess?: (pid: number) => Promise<null | { command: string; ppid: number }>
  now?: () => number
}

export const stateDir = (homeDir: string): string => path.join(homeDir, '.tradingview-mcp')
export const bindingsPathFor = (homeDir: string): string => path.join(stateDir(homeDir), 'hermes_bindings.json')

const normalizePath = (p: string): string => p.replace(/[/\\]+$/, '')

function readJson(file: string): unknown {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8')) as unknown
  } catch {
    return null
  }
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const str = (v: unknown): null | string => (typeof v === 'string' && v.length > 0 ? v : null)

const EMPTY_LEASE: TvWriteLease = { expiresAt: null, held: false, holderAlive: null, holderChart: null, holderLane: null, holderPid: null, inFlight: false, why: 'no_lease' }

/** Is this exact process instance alive? pid liveness by signal 0, identity by `ps lstart` (the claims' own notion):
 *  a recycled pid is NOT alive; an instance whose start cannot be compared counts as alive (never reported gone). */
function instanceAlive(pid: number, pidStart: unknown): boolean | null {
  try {
    process.kill(pid, 0)
  } catch (error) {
    if ((error as { code?: string }).code !== 'EPERM') {
      return false
    }
  }

  let live = ''

  try {
    live = execFileSync('ps', ['-o', 'lstart=', '-p', String(pid)], { encoding: 'utf8', env: { ...process.env, LC_ALL: 'C', TZ: 'UTC' }, timeout: 4000 }).trim()
  } catch {
    live = ''
  }

  if (!live || typeof pidStart !== 'string' || !pidStart) {
    return true
  }

  return live === pidStart
}

/** A write is in flight when the marker exists, its count is positive and ITS process instance is alive; a marker that
 *  exists but cannot be read counts as in flight (cannot tell is never idle). Age plays no part. */
function inFlightFor(leaseDir: string, lane: string, alive: (pid: number, pidStart: unknown) => boolean | null): boolean {
  const file = path.join(leaseDir, `in_flight.${lane}.json`)

  if (!fs.existsSync(file)) {
    return false
  }

  const doc = readJson(file)

  if (!isRecord(doc) || typeof doc.pid !== 'number' || typeof doc.count !== 'number') {
    return true
  }

  return doc.count > 0 && alive(doc.pid, doc.pid_start) !== false
}

/** THE WRITE SWITCH, read at answer time from the files the servers check (cdp-channels src/core/write_lease.js):
 *  the lease is held only when it exists, is well-formed, unexpired, and carries the CURRENT revocation epoch. */
export function readWriteLease(dir: string, now: () => number = Date.now, alive: (pid: number, pidStart: unknown) => boolean | null = instanceAlive): TvWriteLease {
  const leaseDir = path.join(dir, 'lease')
  const file = path.join(leaseDir, 'write_lease.json')

  if (!fs.existsSync(file)) {
    return EMPTY_LEASE
  }

  const raw = readJson(file)

  if (!isRecord(raw) || raw.schema !== 'tv.write_lease/v1' || typeof raw.holder_lane !== 'string') {
    return { ...EMPTY_LEASE, why: 'lease_unreadable' }
  }

  const holderLane = raw.holder_lane
  const holderPid = typeof raw.holder_pid === 'number' && Number.isInteger(raw.holder_pid) && raw.holder_pid > 1 ? raw.holder_pid : null
  const holderChart = str(raw.holder_chart)
  const expiresAt = str(raw.expires_at)
  let epoch: null | string = null

  try {
    const text = fs.readFileSync(path.join(leaseDir, 'revocation_epoch'), 'utf8').trim()
    epoch = /^[0-9]+$/.test(text) ? text : null
  } catch {
    epoch = null
  }

  const base = { expiresAt, held: false, holderAlive: holderPid === null ? null : alive(holderPid, raw.holder_pid_start), holderChart, holderLane, holderPid, inFlight: inFlightFor(leaseDir, holderLane, alive) }

  if (epoch === null) {
    return { ...base, why: 'epoch_unavailable' }
  }

  if (String(raw.epoch) !== epoch) {
    return { ...base, why: 'revoked' }
  }

  const expMs = expiresAt ? Date.parse(expiresAt) : NaN

  if (!Number.isFinite(expMs) || expMs <= now()) {
    return { ...base, why: 'expired' }
  }

  return { ...base, held: true, why: null }
}

const strList = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])

/** Registry rows -> lane views with no snapshot data yet. */
export function lanesFromRegistry(registry: unknown): Record<string, TvLaneView> {
  const out: Record<string, TvLaneView> = {}
  const channels = isRecord(registry) && isRecord(registry.channels) ? registry.channels : {}

  for (const [id, row] of Object.entries(channels)) {
    if (!LANE_ID_RE.test(id) || !isRecord(row)) {
      continue
    }

    out[id] = {
      active: false,
      claimState: 'unknown',
      color: str(row.color),
      healthFresh: false,
      healthVerdict: null,
      holderApp: null,
      holderPid: null,
      id,
      intended: null,
      label: str(row.label) ?? id.toUpperCase(),
      layouts: strList(row.layouts),
      policy: str(row.policy),
      reasons: [],
      slugs: [],
      tabPresent: false,
      targetId: null,
      up: false
    }
  }

  return out
}

/** Overlay the publisher's snapshot onto the registry views; returns the snapshot's generated_at. */
export function applyStatus(lanes: Record<string, TvLaneView>, status: unknown): null | string {
  if (!isRecord(status) || !Array.isArray(status.channels)) {
    return null
  }

  for (const entry of status.channels) {
    if (!isRecord(entry)) {
      continue
    }

    const id = str(entry.channel_id)
    const lane = id ? lanes[id] : undefined

    if (!lane) {
      continue
    }

    const claim = isRecord(entry.claim) ? entry.claim : {}
    const holder = isRecord(claim.holder) ? claim.holder : {}
    const health = isRecord(entry.health) ? entry.health : {}
    const charts = isRecord(entry.charts) ? entry.charts : {}
    const claimState = str(claim.state)
    lane.up = entry.up === true
    lane.active = entry.active === true
    lane.claimState =
      claimState === 'held' || claimState === 'free' || claimState === 'stale' ? claimState : 'unknown'
    lane.holderPid = typeof holder.pid === 'number' && Number.isInteger(holder.pid) && holder.pid > 1 ? holder.pid : null
    lane.healthVerdict = str(health.verdict)
    lane.healthFresh = health.fresh === true
    lane.intended = str(charts.intended)
    lane.slugs = strList(charts.slugs)
    lane.tabPresent = charts.tab_present === true
    lane.reasons = strList(entry.reasons)
    const target = isRecord(entry.target) ? entry.target : {}
    const binding = isRecord(entry.binding) ? entry.binding : {}
    lane.targetId = str(target.id) ?? str(binding.target_id)
  }

  return str(status.generated_at)
}

/** The CLI's declaration for one workspace: `<workspace>/.mcp.json` -> mcpServers.tradingview.env.TV_CDP_CHANNEL. */
export function declaredLane(workspacePath: string): null | string {
  const doc = readJson(path.join(workspacePath, '.mcp.json'))

  if (!isRecord(doc) || !isRecord(doc.mcpServers)) {
    return null
  }

  const tv = doc.mcpServers.tradingview
  const env = isRecord(tv) && isRecord(tv.env) ? tv.env : null
  const lane = env ? str(env.TV_CDP_CHANNEL) : null

  return lane && LANE_ID_RE.test(lane) ? lane : null
}

export function readBindings(file: string): TvLanesSnapshot['bindings'] {
  const doc = readJson(file)
  const out: TvLanesSnapshot['bindings'] = { sessions: {}, workspaces: {} }

  if (!isRecord(doc)) {
    return out
  }

  for (const scope of ['sessions', 'workspaces'] as const) {
    const table = isRecord(doc[scope]) ? doc[scope] : {}

    for (const [key, row] of Object.entries(table)) {
      const lane = isRecord(row) ? str(row.lane) : null

      if (lane && LANE_ID_RE.test(lane)) {
        out[scope][scope === 'workspaces' ? normalizePath(key) : key] = { lane, since: (isRecord(row) && str(row.since)) || '' }
      }
    }
  }

  return out
}

function writeBindings(file: string, bindings: TvLanesSnapshot['bindings']): void {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  const tmp = `${file}.${process.pid}.tmp`
  fs.writeFileSync(tmp, `${JSON.stringify({ bindings_schema: 'tv.hermes_bindings/v1', ...bindings }, null, 2)}\n`, { mode: 0o600 })
  fs.renameSync(tmp, file)
}

const defaultReadProcess = (pid: number): Promise<null | { command: string; ppid: number }> =>
  new Promise(resolve => {
    execFile('/bin/ps', ['-o', 'ppid=,command=', '-p', String(pid)], { timeout: 3000 }, (error, stdout) => {
      if (error) {
        resolve(null)

        return
      }

      const line = String(stdout).trim()
      const m = /^(\d+)\s+(.*)$/.exec(line)
      resolve(m ? { command: m[2], ppid: Number(m[1]) } : null)
    })
  })

/** Which app a process descends from: the WHOLE ancestry is read first (bounded), then judged, so a Hermes backend
 *  whose own command names the hermes-agent checkout cannot be mistaken for the gateway before the desktop app above
 *  it is seen. Measured 2026-10-01: desktop = node -> python backend -> Hermes.app/Contents/MacOS/Hermes; gateway =
 *  node -> python -> python -> osascript wrapper exec'ing `.../hermes-agent/.hermes/bin/hermes --run-module ...`. */
export async function holderAppOf(
  pid: number,
  readProcess: NonNullable<TvLanesIpcDeps['readProcess']>
): Promise<TvLaneView['holderApp']> {
  const chain: string[] = []
  let cur = pid

  for (let hop = 0; hop < ANCESTRY_HOPS; hop += 1) {
    const info = await readProcess(cur)

    if (!info) {
      if (hop === 0) {
        return null
      }

      break
    }

    chain.push(info.command)

    if (!Number.isInteger(info.ppid) || info.ppid <= 1) {
      break
    }

    cur = info.ppid
  }

  if (chain.some(c => c.includes('Hermes.app/Contents/MacOS/Hermes'))) {
    return 'hermes'
  }

  if (chain.some(c => /\bhermes\b.*--run-module|\bhermes (gateway|serve)\b|\/hermes-agent\/.*\/bin\/hermes\b/.test(c))) {
    return 'hermes-gateway'
  }

  if (chain.some(c => /\bclaude\b/.test(c))) {
    return 'claude'
  }

  return 'other'
}

export function registerTvLanesIpc({ homeDir, readProcess = defaultReadProcess, now = Date.now, spawnProcess = spawn }: TvLanesIpcDeps): void {
  const dir = stateDir(homeDir)
  const bindingsFile = bindingsPathFor(homeDir)
  const holderCache = new Map<number, { at: number; app: TvLaneView['holderApp'] }>()

  const snapshot = async (workspacePaths: unknown): Promise<TvLanesSnapshot> => {
    const base: TvLanesSnapshot = {
      bindings: { sessions: {}, workspaces: {} },
      bindingsPath: bindingsFile,
      declared: {},
      error: null,
      generatedAt: null,
      lanes: {},
      ok: true,
      snapshotAgeS: null,
      writeLease: EMPTY_LEASE
    }

    const registry = readJson(path.join(dir, 'channels.json'))

    if (!registry) {
      return { ...base, error: `registry unreadable: ${path.join(dir, 'channels.json')}`, ok: false }
    }

    const lanes = lanesFromRegistry(registry)
    const generatedAt = applyStatus(lanes, readJson(path.join(dir, 'channel_status.json')))
    const ageS = generatedAt ? Math.max(0, Math.round((now() - Date.parse(generatedAt)) / 1000)) : null

    for (const lane of Object.values(lanes)) {
      if (lane.holderPid === null) {
        continue
      }

      const cached = holderCache.get(lane.holderPid)

      if (cached && now() - cached.at < HOLDER_CACHE_MS) {
        lane.holderApp = cached.app

        continue
      }

      lane.holderApp = await holderAppOf(lane.holderPid, readProcess)
      holderCache.set(lane.holderPid, { app: lane.holderApp, at: now() })
    }

    const declared: Record<string, string> = {}
    const asked = Array.isArray(workspacePaths) ? workspacePaths.filter((p): p is string => typeof p === 'string') : []

    for (const p of asked.slice(0, SNAPSHOT_MAX_PATHS)) {
      if (!path.isAbsolute(p) || !p.startsWith(homeDir)) {
        continue
      }

      const lane = declaredLane(p)

      if (lane) {
        declared[normalizePath(p)] = lane
      }
    }

    return {
      ...base,
      bindings: readBindings(bindingsFile),
      declared,
      generatedAt,
      lanes,
      snapshotAgeS: Number.isFinite(ageS) ? ageS : null,
      writeLease: readWriteLease(dir)
    }
  }

  const bind = async (request: unknown): Promise<TvLaneBindResult> => {
    if (!isRecord(request)) {
      return { error: 'bad request', ok: false }
    }

    const { key, lane, scope } = request as Partial<TvLaneBindRequest>

    if (scope !== 'session' && scope !== 'workspace') {
      return { error: 'scope must be session or workspace', ok: false }
    }

    if (typeof key !== 'string' || (scope === 'workspace' ? !path.isAbsolute(key) : !SESSION_ID_RE.test(key))) {
      return { error: 'bad key', ok: false }
    }

    if (lane !== null && (typeof lane !== 'string' || !LANE_ID_RE.test(lane))) {
      return { error: 'bad lane id', ok: false }
    }

    if (lane !== null) {
      const registry = readJson(path.join(dir, 'channels.json'))
      const lanes = lanesFromRegistry(registry)

      if (!lanes[lane]) {
        return { error: `lane ${lane} is not in the registry`, ok: false }
      }

      // ONE CHART NEVER HAS TWO DRIVERS (owner, 2026-10-01): a lane another driver holds is refused here, not merely
      // greyed in the picker, so no path can bind it. Free lanes and lanes this app holds are the only ones taken.
      applyStatus(lanes, readJson(path.join(dir, 'channel_status.json')))
      const target = lanes[lane]

      if (target.claimState === 'held') {
        const app = target.holderPid === null ? null : await holderAppOf(target.holderPid, readProcess)

        if (app !== 'hermes') {
          const who = app === 'claude' ? 'a Claude window' : app === 'hermes-gateway' ? 'the background Hermes gateway' : 'another process'

          return { error: `lane ${lane} is held by ${who} (pid ${target.holderPid ?? '?'}); release it there first — one chart never has two drivers`, ok: false }
        }
      }
    }

    const bindings = readBindings(bindingsFile)
    const table = scope === 'workspace' ? bindings.workspaces : bindings.sessions
    const k = scope === 'workspace' ? normalizePath(key) : key

    if (lane === null) {
      delete table[k]
    } else {
      table[k] = { lane, since: new Date(now()).toISOString() }
    }

    try {
      writeBindings(bindingsFile, bindings)
    } catch (error) {
      return { error: `bindings not written: ${error instanceof Error ? error.message : String(error)}`, ok: false }
    }

    return { error: null, ok: true }
  }

  /** The script and arguments for one allow-listed action; null when the request is not on the list. */
  const scriptFor = (request: unknown): null | { args: string[]; interpreter: string; script: string } => {
    if (!isRecord(request) || typeof request.action !== 'string') {
      return null
    }

    const { action } = request as Partial<TvLaneRunRequest>

    if (action === 'create') {
      return { args: [], interpreter: '/usr/bin/python3', script: NEW_LANE_SCRIPT }
    }

    const lane = (request as { lane?: unknown }).lane

    if (typeof lane !== 'string' || !LANE_ID_RE.test(lane) || !lanesFromRegistry(readJson(path.join(dir, 'channels.json')))[lane]) {
      return null
    }

    if (action === 'chart-accept') {
      return { args: [lane], interpreter: '/usr/bin/python3', script: path.join(LAMP_DIR, 'tv-accept-chart.py') }
    }

    if (action === 'chart-add' || action === 'chart-remove') {
      return { args: [lane, action === 'chart-add' ? 'add' : 'remove'], interpreter: '/usr/bin/python3', script: path.join(LAMP_DIR, 'tv-lane-layouts.py') }
    }

    // the tab in front of the automation browser becomes this lane's tab (the lamp's own one-click bind)
    if (action === 'tab-assign-front') {
      return { args: [lane], interpreter: '/bin/bash', script: path.join(LAMP_DIR, 'tv-bind-front.sh') }
    }

    // THE WRITE SWITCH (owner, 2026-10-02): take or release the one write lease for this lane through the lamp's own
    // script — it asks before a takeover and names the holder in its dialogs; the servers enforce the lease.
    if (action === 'lease-take' || action === 'lease-release' || action === 'lease-release-holder') {
      const verb = action === 'lease-take' ? 'take' : action === 'lease-release' ? 'release' : 'release-holder'

      return { args: [lane, verb], interpreter: '/usr/bin/python3', script: path.join(LAMP_DIR, 'tv-write-lease.py') }
    }

    // bring this lane's tab forward; with no open tab the script says so and ASKS before opening its most recent chart
    if (action === 'tab-focus') {
      return { args: [lane], interpreter: '/bin/bash', script: path.join(LAMP_DIR, 'tv-focus-channel.sh') }
    }

    return null
  }

  const run = (request: unknown): TvLaneBindResult => {
    const target = scriptFor(request)

    if (!target) {
      return { error: 'not an allowed lane action, or the lane is not in the registry', ok: false }
    }

    if (!fs.existsSync(target.script)) {
      return { error: `lamp script missing: ${target.script}`, ok: false }
    }

    try {
      const child = spawnProcess(target.interpreter, [target.script, ...target.args], { detached: true, stdio: 'ignore' })
      child.unref()
    } catch (error) {
      return { error: `script not started: ${error instanceof Error ? error.message : String(error)}`, ok: false }
    }

    return { error: null, ok: true }
  }

  const create = (): TvLaneBindResult => run({ action: 'create' })

  ipcMain.handle('hermes:tv-lanes:get', async (_event, workspacePaths) => snapshot(workspacePaths))
  ipcMain.handle('hermes:tv-lanes:bind', async (_event, request) => bind(request))
  ipcMain.handle('hermes:tv-lanes:create', async () => create())
  ipcMain.handle('hermes:tv-lanes:run', async (_event, request) => run(request))
}
