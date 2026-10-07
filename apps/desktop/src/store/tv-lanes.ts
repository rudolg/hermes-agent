/**
 * TradingView CDP lane state for the sidebar's lane dot (owner ask, 2026-10-01: "blue = chat/project registered as a
 * CDP lane, green = connected to CDP and all up and running — compulsory, at the tree level too"). The renderer polls
 * main's `hermes:tv-lanes:get` and resolves one state per session / per workspace with the PURE function below, so
 * the semantics are testable without Electron.
 *
 * States, in precedence order (the arms' ruling folded into the owner's two colours):
 *   conflict  red     bound lane is gone from the registry, or its claim is held by a process that is not Hermes
 *   degraded  amber   (also) the claim is held by the background Hermes GATEWAY, not this desktop app's own server
 *   stale     hollow  no snapshot, or the publisher's snapshot is older than STALE_AFTER_S
 *   live      green   claim held by Hermes, publisher says up, tab health TAB_OK, intended chart present
 *   degraded  amber   claim held by Hermes but the lane is not fully up (tab closed / page unreadable / no chart)
 *   linked    blue    registered to a lane that nothing drives yet (claim free)
 *   declared  hollow  the CLI tree declares a lane in its .mcp.json; Hermes does not run that server
 *   unlinked  none    no binding at all
 */
import { atom } from 'nanostores'

import { getHermesConfigRecord } from '@/hermes'
import { type TvServerRow, tvServerRows } from '@/lib/tv-server-rows'

import type { TvLaneRunRequest, TvLanesSnapshot, TvLaneView } from '../../electron/tv-lanes-types'

import { $sessions } from './session'

export const STALE_AFTER_S = 150
export const POLL_MS = 15_000

export type TvLaneDotState = 'conflict' | 'declared' | 'degraded' | 'linked' | 'live' | 'stale' | 'unlinked'
export type TvLaneSource = 'declared' | 'session' | 'workspace' | null

export interface TvLaneResolution {
  state: TvLaneDotState
  source: TvLaneSource
  lane: null | TvLaneView
  laneId: null | string
  /** One plain sentence for the tooltip. */
  detail: string
}

export const $tvLanes = atom<null | TvLanesSnapshot>(null)
/** The app's tradingview* MCP entries, refreshed with the lane poll: which lane each drives and whether it may write. */
export const $tvServerRows = atom<null | TvServerRow[]>(null)

/** True when some enabled entry drives this lane with chart writes on (the capability class, not a live lease). */
export function laneWritesEnabled(rows: null | TvServerRow[], laneId: null | string): boolean {
  return !!laneId && !!rows?.some(r => r.enabled && r.lane === laneId && r.write)
}

/** THE WRITE SWITCH: true only when the one write lease is held, valid, by THIS lane's running server. */
export function laneHoldsWriteLease(snapshot: null | TvLanesSnapshot, laneId: null | string): boolean {
  return !!laneId && !!snapshot?.writeLease?.held && snapshot.writeLease.holderLane === laneId
}

const normalizePath = (p: null | string | undefined): string => (p ?? '').replace(/[/\\]+$/, '')

/** The longest bound workspace that is `cwd` itself or a parent of it. */
export function workspaceKeyFor(table: Record<string, unknown>, cwd: null | string | undefined): null | string {
  const c = normalizePath(cwd)

  if (!c) {
    return null
  }

  let best: null | string = null

  for (const key of Object.keys(table)) {
    if ((c === key || c.startsWith(`${key}/`)) && (best === null || key.length > best.length)) {
      best = key
    }
  }

  return best
}

export function resolveTvLane(
  snapshot: null | TvLanesSnapshot,
  subject: { cwd?: null | string; sessionId?: null | string; workspacePath?: null | string }
): TvLaneResolution {
  const none: TvLaneResolution = {
    detail: 'not registered to a TradingView lane',
    lane: null,
    laneId: null,
    source: null,
    state: 'unlinked'
  }

  if (!snapshot) {
    return { ...none, detail: 'lane state not read yet' }
  }

  const sessionBinding = subject.sessionId ? snapshot.bindings.sessions[subject.sessionId] : undefined
  const wsKey = workspaceKeyFor(snapshot.bindings.workspaces, subject.workspacePath ?? subject.cwd)
  const wsBinding = wsKey ? snapshot.bindings.workspaces[wsKey] : undefined
  let laneId: null | string = null
  let source: TvLaneSource = null

  if (sessionBinding) {
    laneId = sessionBinding.lane
    source = 'session'
  } else if (wsBinding) {
    laneId = wsBinding.lane
    source = 'workspace'
  } else {
    const dKey = workspaceKeyFor(snapshot.declared, subject.workspacePath ?? subject.cwd)

    if (dKey) {
      laneId = snapshot.declared[dKey]
      source = 'declared'
    }
  }

  if (!laneId) {
    return none
  }

  const lane = snapshot.lanes[laneId] ?? null
  const where = source === 'session' ? 'this chat' : source === 'workspace' ? 'this tree' : 'the CLI tree'

  if (!lane) {
    return {
      detail: `${where} is bound to lane ${laneId}, which is no longer in the registry: unlink it or recreate the lane`,
      lane: null,
      laneId,
      source,
      state: 'conflict'
    }
  }

  const name = `${lane.label} (${laneId}${lane.policy ? `, ${lane.policy}` : ''})`

  if (source === 'declared') {
    return {
      detail: `${name} is declared by the CLI tree's .mcp.json; Hermes does not run that server`,
      lane,
      laneId,
      source,
      state: 'declared'
    }
  }

  if (lane.claimState === 'held' && lane.holderApp === 'hermes-gateway') {
    return {
      detail: `${name} is held by the background Hermes gateway (pid ${lane.holderPid}), not this app's own server; this app's chats cannot use it until the gateway lets go`,
      lane,
      laneId,
      source,
      state: 'degraded'
    }
  }

  if (lane.claimState === 'held' && lane.holderApp !== null && lane.holderApp !== 'hermes') {
    return {
      detail: `${name} is held by ${lane.holderApp === 'claude' ? 'a Claude window' : 'another process'} (pid ${lane.holderPid}): one chart never has two drivers, so this binding is a fault; release the lane there first`,
      lane,
      laneId,
      source,
      state: 'conflict'
    }
  }

  if (snapshot.snapshotAgeS === null || snapshot.snapshotAgeS > STALE_AFTER_S) {
    const age = snapshot.snapshotAgeS === null ? 'no lane snapshot' : `lane snapshot is ${snapshot.snapshotAgeS} s old`

    return { detail: `${name}: ${age}, state unknown`, lane, laneId, source, state: 'stale' }
  }

  if (lane.claimState !== 'held') {
    return {
      detail: `${name} is registered to ${where}; no Hermes server holds it yet`,
      lane,
      laneId,
      source,
      state: 'linked'
    }
  }

  const chartOk = lane.intended !== null && lane.slugs.includes(lane.intended)

  if (lane.up && lane.healthVerdict === 'TAB_OK' && chartOk) {
    return {
      detail: `${name} is live: Hermes holds it (pid ${lane.holderPid}), tab ${(lane.targetId ?? '').slice(0, 6).toUpperCase() || '?'} OK on chart ${lane.intended}`,
      lane,
      laneId,
      source,
      state: 'live'
    }
  }

  const why =
    lane.healthVerdict && lane.healthVerdict !== 'TAB_OK'
      ? lane.healthVerdict.toLowerCase().replace(/_/g, ' ')
      : !chartOk
        ? 'chart not on its tab'
        : 'not up'

  return {
    detail: `${name}: Hermes holds it (pid ${lane.holderPid}) but ${why}`,
    lane,
    laneId,
    source,
    state: 'degraded'
  }
}

/** The distinct workspace roots the sidebar may show, for the declaration lookup. */
export function workspacePathsOf(sessions: { cwd?: null | string; git_repo_root?: null | string }[]): string[] {
  const out = new Set<string>()

  for (const s of sessions) {
    for (const p of [s.cwd, s.git_repo_root]) {
      const n = normalizePath(p)

      if (n) {
        out.add(n)
      }
    }
  }

  return [...out].slice(0, 64)
}

let timer: null | ReturnType<typeof setInterval> = null
let inFlight = false

export async function refreshTvLanes(): Promise<void> {
  const api = window.hermesDesktop?.tvLanes

  if (!api || inFlight) {
    return
  }

  inFlight = true

  try {
    $tvLanes.set(await api.get(workspacePathsOf($sessions.get())))

    try {
      $tvServerRows.set(tvServerRows((await getHermesConfigRecord()) as { mcp_servers?: unknown }))
    } catch {
      // the config read failed: keep the last rows
    }
  } catch {
    // keep the last snapshot; the dot goes stale by age on its own
  } finally {
    inFlight = false
  }
}

export function startTvLanesPolling(): void {
  if (timer || !window.hermesDesktop?.tvLanes) {
    return
  }

  void refreshTvLanes()
  timer = setInterval(() => void refreshTvLanes(), POLL_MS)
}

export async function bindTvLane(
  scope: 'session' | 'workspace',
  key: string,
  lane: null | string
): Promise<string | null> {
  const api = window.hermesDesktop?.tvLanes

  if (!api) {
    return 'lane binding is not available in this build'
  }

  const r = await api.bind({ key, lane, scope })
  await refreshTvLanes()

  return r.ok ? null : (r.error ?? 'bind failed')
}

export async function createTvLane(): Promise<string | null> {
  const api = window.hermesDesktop?.tvLanes

  if (!api) {
    return 'lane creation is not available in this build'
  }

  const r = await api.create()

  return r.ok ? null : (r.error ?? 'could not start the new-lane flow')
}

/** Start one of the lamp's lane scripts (new lane; accept / add / remove an allowed chart). Null = started. */
export async function runTvLane(request: TvLaneRunRequest): Promise<string | null> {
  const api = window.hermesDesktop?.tvLanes

  if (!api?.run) {
    return 'lane actions are not available in this build'
  }

  const r = await api.run(request)

  return r.ok ? null : (r.error ?? 'could not start the lane action')
}
