/**
 * The lane dot's semantics, on the publisher's real shapes (channel_status.json of 2026-10-01): the owner's two colours
 * (blue registered, green live) plus the honest states between them. One negative control per branch so a resolver
 * that answers "live" for everything cannot pass.
 */
import { describe, expect, it } from 'vitest'

import type { TvLanesSnapshot, TvLaneView } from '../../electron/tv-lanes-types'

import { resolveTvLane, workspaceKeyFor, workspacePathsOf } from './tv-lanes'

const lane = (over: Partial<TvLaneView> = {}): TvLaneView => ({
  active: true,
  claimState: 'held',
  color: '#e07b39',
  healthFresh: true,
  healthVerdict: 'TAB_OK',
  holderApp: 'hermes',
  holderPid: 92998,
  id: 'hermes-atlas',
  intended: 'N06Rmf2K',
  label: 'HERMES_ATLAS',
  layouts: ['N06Rmf2K'],
  policy: 'battlefield',
  reasons: [],
  slugs: ['N06Rmf2K'],
  tabPresent: true,
  up: true,
  ...over
})

const snap = (over: Partial<TvLanesSnapshot> = {}, laneOver: Partial<TvLaneView> = {}): TvLanesSnapshot => ({
  bindings: { sessions: {}, workspaces: { '/Users/g/tradingview-mcp-hermes': { lane: 'hermes-atlas', since: 't' } } },
  bindingsPath: '/Users/g/.tradingview-mcp/hermes_bindings.json',
  declared: {},
  error: null,
  generatedAt: '2026-10-01T09:53:09.732Z',
  lanes: { 'hermes-atlas': lane(laneOver) },
  ok: true,
  snapshotAgeS: 12,
  ...over
})

const tree = { workspacePath: '/Users/g/tradingview-mcp-hermes' }
const chat = { cwd: '/Users/g/tradingview-mcp-hermes/sub/dir', sessionId: 'sid1' }

describe('resolveTvLane', () => {
  it('is unlinked with no binding, and says so before the first read', () => {
    expect(resolveTvLane(null, tree).state).toBe('unlinked')
    expect(resolveTvLane(snap({ bindings: { sessions: {}, workspaces: {} } }), tree).state).toBe('unlinked')
  })

  it('green only when Hermes holds the claim, the publisher says up, the tab is OK and the chart is on it', () => {
    const r = resolveTvLane(snap(), tree)
    expect(r.state).toBe('live')
    expect(r.source).toBe('workspace')
    expect(r.detail).toContain('92998')
    // a chat deeper in the tree inherits the tree's binding
    expect(resolveTvLane(snap(), chat).state).toBe('live')
  })

  it('negative controls: each missing leg drops it out of green', () => {
    expect(resolveTvLane(snap({}, { healthVerdict: 'BINDING_TAB_CLOSED', up: false }), tree).state).toBe('degraded')
    expect(resolveTvLane(snap({}, { healthVerdict: 'PAGE_UNREADABLE' }), tree).state).toBe('degraded')
    expect(resolveTvLane(snap({}, { intended: 'N06Rmf2K', slugs: [] }), tree).state).toBe('degraded')
    expect(resolveTvLane(snap({}, { claimState: 'free', holderApp: null, holderPid: null }), tree).state).toBe('linked')
  })

  it('amber, with the gateway named, when the background Hermes gateway holds the lane instead of this app', () => {
    const r = resolveTvLane(snap({}, { holderApp: 'hermes-gateway', holderPid: 92998 }), tree)
    expect(r.state).toBe('degraded')
    expect(r.detail).toContain('gateway')
    expect(r.detail).toContain('92998')
  })

  it('red when another app drives the bound lane, or the lane left the registry', () => {
    expect(resolveTvLane(snap({}, { holderApp: 'claude', holderPid: 11658 }), tree).state).toBe('conflict')
    expect(resolveTvLane(snap({ lanes: {} }), tree).state).toBe('conflict')
  })

  it('hollow when the publisher snapshot is missing or older than 150 s', () => {
    expect(resolveTvLane(snap({ snapshotAgeS: 151 }), tree).state).toBe('stale')
    expect(resolveTvLane(snap({ generatedAt: null, snapshotAgeS: null }), tree).state).toBe('stale')
    expect(resolveTvLane(snap({ snapshotAgeS: 150 }), tree).state).toBe('live')
  })

  it('a conflict outranks staleness, so an old snapshot cannot hide a foreign driver', () => {
    expect(resolveTvLane(snap({ snapshotAgeS: 999 }, { holderApp: 'claude' }), tree).state).toBe('conflict')
  })

  it('a session binding wins over the tree binding; a CLI declaration is shown as declared only', () => {
    const s = snap({
      bindings: { sessions: { sid1: { lane: 'hermes', since: 't' } }, workspaces: { '/Users/g/tradingview-mcp-hermes': { lane: 'hermes-atlas', since: 't' } } },
      declared: { '/Users/g/tradingview-mcp-dg': 'dg' },
      lanes: { dg: lane({ holderApp: 'claude', id: 'dg', label: 'DG' }), hermes: lane({ id: 'hermes', label: 'HERMES' }), 'hermes-atlas': lane() }
    })

    expect(resolveTvLane(s, chat)).toMatchObject({ laneId: 'hermes', source: 'session', state: 'live' })
    expect(resolveTvLane(s, { cwd: '/Users/g/tradingview-mcp-dg', sessionId: 'other' })).toMatchObject({ laneId: 'dg', source: 'declared', state: 'declared' })
  })
})

describe('workspaceKeyFor', () => {
  it('matches the path itself or a parent, longest first, never a sibling with a shared prefix', () => {
    const table = { '/a/b': 1, '/a/b/c': 1 }
    expect(workspaceKeyFor(table, '/a/b/c/d')).toBe('/a/b/c')
    expect(workspaceKeyFor(table, '/a/b/')).toBe('/a/b')
    expect(workspaceKeyFor(table, '/a/bb')).toBeNull()
    expect(workspaceKeyFor(table, null)).toBeNull()
  })
})

describe('workspacePathsOf', () => {
  it('collects distinct cwd and repo roots, trailing slashes stripped, capped', () => {
    const paths = workspacePathsOf([
      { cwd: '/x/y/', git_repo_root: '/x' },
      { cwd: '/x/y', git_repo_root: null },
      { cwd: null }
    ])

    expect(paths).toEqual(['/x/y', '/x'])
  })
})
