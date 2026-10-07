/**
 * `hermes:tv-lanes:*` reads the lamp's files and answers honestly: a lane the registry does not know is refused, a
 * missing snapshot leaves every lane unknown, the CLI's `.mcp.json` is a declaration only, a bind lands atomically.
 * Fixtures are the publisher's real shapes of 2026-10-01 (channel_status.json, channels.json).
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

const electron = vi.hoisted(() => ({
  handlers: new Map<string, (...args: unknown[]) => unknown>()
}))

vi.mock('electron', () => ({
  ipcMain: {
    handle: (channel: string, fn: (...args: unknown[]) => unknown) => {
      electron.handlers.set(channel, fn)
    }
  }
}))

import {
  applyStatus,
  declaredLane,
  holderAppOf,
  lanesFromRegistry,
  readBindings,
  registerTvLanesIpc
} from './tv-lanes-ipc'

const registry = {
  browser: { port: 9223 },
  channels: {
    atlas: { color: '#8a5cf6', label: 'ATLAS', layouts: ['LCEdRvf8'], policy: 'battlefield' },
    'hermes-atlas': { color: '#e07b39', label: 'HERMES_ATLAS', layouts: ['N06Rmf2K'], policy: 'battlefield' },
    'Bad Id!': { label: 'X' }
  }
}

const status = {
  channels: [
    {
      active: false,
      channel_id: 'hermes-atlas',
      charts: { intended: null, slugs: [], tab_present: false },
      claim: { holder: { pid: 92998 }, state: 'held' },
      health: { fresh: false, verdict: 'BINDING_TAB_CLOSED' },
      reasons: ['binding_tab_closed'],
      up: false
    },
    {
      active: true,
      channel_id: 'atlas',
      charts: { intended: 'LCEdRvf8', slugs: ['LCEdRvf8'], tab_present: true },
      claim: { holder: { pid: 11658 }, state: 'held' },
      health: { fresh: true, verdict: 'TAB_OK' },
      reasons: [],
      up: true
    },
    { channel_id: 'ghost', up: true }
  ],
  generated_at: '2026-10-01T09:53:09.732Z',
  schema: 'tv.channel_status/v1'
}

const tmpDirs: string[] = []

const makeHome = (): string => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'tv-lanes-'))
  tmpDirs.push(home)
  fs.mkdirSync(path.join(home, '.tradingview-mcp'))

  return home
}

afterEach(() => {
  for (const d of tmpDirs.splice(0)) {
    fs.rmSync(d, { force: true, recursive: true })
  }

  electron.handlers.clear()
})

describe('lanesFromRegistry + applyStatus', () => {
  it('keeps only well-formed lane ids and overlays the snapshot by channel_id', () => {
    const lanes = lanesFromRegistry(registry)
    expect(Object.keys(lanes).sort()).toEqual(['atlas', 'hermes-atlas'])
    expect(lanes.atlas.policy).toBe('battlefield')
    expect(applyStatus(lanes, status)).toBe('2026-10-01T09:53:09.732Z')
    expect(lanes['hermes-atlas']).toMatchObject({
      claimState: 'held',
      healthVerdict: 'BINDING_TAB_CLOSED',
      holderPid: 92998,
      intended: null,
      up: false
    })
    expect(lanes.atlas).toMatchObject({
      claimState: 'held',
      healthVerdict: 'TAB_OK',
      intended: 'LCEdRvf8',
      slugs: ['LCEdRvf8'],
      up: true
    })
  })

  it('negative control: no snapshot leaves every lane unknown, never up', () => {
    const lanes = lanesFromRegistry(registry)
    expect(applyStatus(lanes, null)).toBeNull()
    expect(lanes.atlas).toMatchObject({ claimState: 'unknown', holderPid: null, up: false })
  })
})

describe('holderAppOf', () => {
  it('names the app at the top of the ancestry and stops at launchd', async () => {
    const tree: Record<number, { command: string; ppid: number }> = {
      1: { command: '/sbin/launchd', ppid: 0 },
      100: { command: '/Applications/Hermes.app/Contents/MacOS/Hermes', ppid: 1 },
      200: { command: '/usr/bin/python3 hermes_cli', ppid: 100 },
      300: { command: '/opt/homebrew/bin/node src/server.js', ppid: 200 },
      400: { command: '/opt/homebrew/bin/node src/server.js', ppid: 500 },
      500: { command: '/bin/zsh', ppid: 600 },
      600: { command: '/usr/local/bin/claude', ppid: 1 },
      700: { command: '/opt/homebrew/bin/node src/server.js', ppid: 1 },
      // the background gateway: launchd -> osascript wrapper -> python -> python -> node
      800: {
        command:
          '/usr/bin/osascript -l JavaScript -e exec /Users/g/.hermes/hermes-agent/.hermes/bin/hermes --run-module hermes_cli.stderr',
        ppid: 1
      },
      810: { command: '/Users/g/.hermes/tools/python/bin/python3 -I -c import os', ppid: 800 },
      820: { command: '/Users/g/.hermes/tools/python/bin/python3 -I -c import os', ppid: 810 },
      830: { command: '/opt/homebrew/bin/node src/server.js', ppid: 820 },
      // a desktop backend whose own command names the checkout must still resolve to the app above it
      900: {
        command: '/Users/g/.hermes/tools/python/bin/python3 /Users/g/.hermes/hermes-agent/hermes_cli/main.py',
        ppid: 100
      },
      910: { command: '/opt/homebrew/bin/node src/server.js', ppid: 900 }
    }

    const read = async (pid: number) => tree[pid] ?? null
    expect(await holderAppOf(300, read)).toBe('hermes')
    expect(await holderAppOf(910, read)).toBe('hermes')
    expect(await holderAppOf(830, read)).toBe('hermes-gateway')
    expect(await holderAppOf(400, read)).toBe('claude')
    expect(await holderAppOf(700, read)).toBe('other')
    expect(await holderAppOf(999, read)).toBeNull()
  })
})

describe('declaredLane + readBindings', () => {
  it('reads the CLI declaration from .mcp.json and ignores malformed lane ids', () => {
    const home = makeHome()
    const ws = path.join(home, 'tree')
    fs.mkdirSync(ws)
    expect(declaredLane(ws)).toBeNull()
    fs.writeFileSync(
      path.join(ws, '.mcp.json'),
      JSON.stringify({ mcpServers: { tradingview: { env: { TV_CDP_CHANNEL: 'dg' } } } })
    )
    expect(declaredLane(ws)).toBe('dg')
    fs.writeFileSync(
      path.join(ws, '.mcp.json'),
      JSON.stringify({ mcpServers: { tradingview: { env: { TV_CDP_CHANNEL: '../x' } } } })
    )
    expect(declaredLane(ws)).toBeNull()
  })

  it('tolerates a missing or broken bindings file', () => {
    const home = makeHome()
    const file = path.join(home, '.tradingview-mcp', 'hermes_bindings.json')
    expect(readBindings(file)).toEqual({ sessions: {}, workspaces: {} })
    fs.writeFileSync(file, '{not json')
    expect(readBindings(file)).toEqual({ sessions: {}, workspaces: {} })
  })
})

describe('registerTvLanesIpc', () => {
  it('answers get, refuses a bind to an unknown lane, and writes a known one atomically', async () => {
    const home = makeHome()
    const dir = path.join(home, '.tradingview-mcp')
    fs.writeFileSync(path.join(dir, 'channels.json'), JSON.stringify(registry))
    fs.writeFileSync(path.join(dir, 'channel_status.json'), JSON.stringify(status))
    const ws = path.join(home, 'tree')
    fs.mkdirSync(ws)
    fs.writeFileSync(
      path.join(ws, '.mcp.json'),
      JSON.stringify({ mcpServers: { tradingview: { env: { TV_CDP_CHANNEL: 'atlas' } } } })
    )
    const now = () => Date.parse('2026-10-01T09:54:09.732Z')

    // the status fixture: `atlas` held by pid 11658 (a Claude window), `hermes-atlas` by pid 92998 (this app)
    const chain: Record<number, { command: string; ppid: number }> = {
      11658: { command: '/opt/homebrew/bin/node src/server.js', ppid: 11600 },
      11600: { command: '/usr/local/bin/claude', ppid: 1 },
      92998: { command: '/opt/homebrew/bin/node src/server.js', ppid: 92000 },
      92000: { command: '/Applications/Hermes.app/Contents/MacOS/Hermes', ppid: 1 }
    }

    registerTvLanesIpc({ homeDir: home, now, readProcess: async pid => chain[pid] ?? null })
    const get = electron.handlers.get('hermes:tv-lanes:get')!
    const bind = electron.handlers.get('hermes:tv-lanes:bind')!

    const first = (await get({}, [ws, '/etc', 42])) as Awaited<ReturnType<typeof get>> & {
      declared: Record<string, string>
      snapshotAgeS: number
    }
    expect(first).toMatchObject({ declared: { [ws]: 'atlas' }, ok: true, snapshotAgeS: 60 })

    expect(await bind({}, { key: ws, lane: 'nope', scope: 'workspace' })).toMatchObject({ ok: false })
    // ONE CHART NEVER HAS TWO DRIVERS: a lane a Claude window holds is refused, with the holder named
    const refused = (await bind({}, { key: ws, lane: 'atlas', scope: 'workspace' })) as { error: string; ok: boolean }
    expect(refused.ok).toBe(false)
    expect(refused.error).toContain('Claude window')
    expect(refused.error).toContain('11658')
    expect(await bind({}, { key: 'x', lane: 'atlas', scope: 'workspace' })).toMatchObject({ ok: false })
    expect(await bind({}, { key: ws, lane: 'hermes-atlas', scope: 'workspace' })).toEqual({ error: null, ok: true })
    const written = JSON.parse(fs.readFileSync(path.join(dir, 'hermes_bindings.json'), 'utf8'))
    expect(written.workspaces[ws]).toMatchObject({ lane: 'hermes-atlas', since: '2026-10-01T09:54:09.732Z' })
    expect(fs.readdirSync(dir).filter(n => n.endsWith('.tmp'))).toEqual([])

    const second = (await get({}, [])) as { bindings: { workspaces: Record<string, { lane: string }> } }
    expect(second.bindings.workspaces[ws].lane).toBe('hermes-atlas')
    expect(await bind({}, { key: ws, lane: null, scope: 'workspace' })).toEqual({ error: null, ok: true })
    expect((await get({}, [])) as object).toMatchObject({ bindings: { sessions: {}, workspaces: {} } })
  })

  it('negative control: an unreadable registry answers ok:false, never an empty lane list', async () => {
    const home = makeHome()
    registerTvLanesIpc({ homeDir: home, readProcess: async () => null })
    const get = electron.handlers.get('hermes:tv-lanes:get')!
    expect(await get({}, [])).toMatchObject({ lanes: {}, ok: false })
  })
})

describe('hermes:tv-lanes:run', () => {
  it('starts only allow-listed lamp scripts for a registered lane, with the injected spawner', async () => {
    const home = makeHome()
    const dir = path.join(home, '.tradingview-mcp')
    fs.writeFileSync(path.join(dir, 'channels.json'), JSON.stringify(registry))
    const lamp = fs.mkdtempSync(path.join(os.tmpdir(), 'tv-lamp-'))

    for (const f of [
      'tv-accept-chart.py',
      'tv-lane-layouts.py',
      'tv-lane-new-hermes.py',
      'tv-bind-front.sh',
      'tv-write-lease.py',
      'tv-focus-channel.sh'
    ]) {
      fs.writeFileSync(path.join(lamp, f), '#!/usr/bin/env python3\n')
    }

    process.env.HERMES_TV_LAMP_DIR = lamp
    process.env.HERMES_TV_LANE_NEW_SCRIPT = path.join(lamp, 'tv-lane-new-hermes.py')
    const started: string[][] = []

    const spawnProcess = ((cmd: string, args: string[]) => {
      started.push([cmd, ...args])

      return { unref: () => undefined }
    }) as unknown as NonNullable<Parameters<typeof registerTvLanesIpc>[0]['spawnProcess']>

    // the module read the env at import time; re-import for this test's paths
    vi.resetModules()
    const mod = await import('./tv-lanes-ipc')
    mod.registerTvLanesIpc({ homeDir: home, readProcess: async () => null, spawnProcess })
    const run = electron.handlers.get('hermes:tv-lanes:run')!
    expect(await run({}, { action: 'chart-add', lane: 'hermes-atlas' })).toEqual({ error: null, ok: true })
    expect(await run({}, { action: 'chart-remove', lane: 'hermes-atlas' })).toEqual({ error: null, ok: true })
    expect(await run({}, { action: 'chart-accept', lane: 'atlas' })).toEqual({ error: null, ok: true })
    expect(await run({}, { action: 'create' })).toEqual({ error: null, ok: true })
    expect(await run({}, { action: 'tab-assign-front', lane: 'hermes-atlas' })).toEqual({ error: null, ok: true })
    expect(await run({}, { action: 'lease-take', lane: 'hermes-atlas' })).toEqual({ error: null, ok: true })
    expect(await run({}, { action: 'lease-release', lane: 'hermes-atlas' })).toEqual({ error: null, ok: true })
    expect(await run({}, { action: 'tab-focus', lane: 'hermes-atlas' })).toEqual({ error: null, ok: true })
    expect(await run({}, { action: 'lease-release-holder', lane: 'atlas' })).toEqual({ error: null, ok: true })
    expect(started.map(a => a.map(x => path.basename(x)))).toEqual([
      ['python3', 'tv-lane-layouts.py', 'hermes-atlas', 'add'],
      ['python3', 'tv-lane-layouts.py', 'hermes-atlas', 'remove'],
      ['python3', 'tv-accept-chart.py', 'atlas'],
      ['python3', 'tv-lane-new-hermes.py'],
      ['bash', 'tv-bind-front.sh', 'hermes-atlas'],
      ['python3', 'tv-write-lease.py', 'hermes-atlas', 'take'],
      ['python3', 'tv-write-lease.py', 'hermes-atlas', 'release'],
      ['bash', 'tv-focus-channel.sh', 'hermes-atlas'],
      ['python3', 'tv-write-lease.py', 'atlas', 'release-holder']
    ])

    // negative controls: an unknown action, an unregistered lane, a lane with shell-shaped text
    for (const bad of [
      { action: 'rm-rf' },
      { action: 'chart-add', lane: 'nope' },
      { action: 'chart-add', lane: 'atlas; rm' },
      'chart-add',
      null
    ]) {
      const res = (await run({}, bad)) as { ok: boolean }
      expect(res.ok).toBe(false)
    }

    expect(started.length).toBe(9)
    delete process.env.HERMES_TV_LAMP_DIR
    delete process.env.HERMES_TV_LANE_NEW_SCRIPT
    fs.rmSync(lamp, { force: true, recursive: true })
  })
})

describe('readWriteLease — the switch as the dot reads it', () => {
  const T0 = Date.parse('2026-10-02T08:00:00.000Z')

  const write = (dir: string, lease: Record<string, unknown>, epoch = '3') => {
    fs.mkdirSync(path.join(dir, 'lease'), { recursive: true })
    fs.writeFileSync(path.join(dir, 'lease', 'revocation_epoch'), `${epoch}\n`)
    fs.writeFileSync(
      path.join(dir, 'lease', 'write_lease.json'),
      JSON.stringify({
        epoch: '3',
        expires_at: '2026-10-02T09:00:00.000Z',
        fencing_token: '11',
        holder_chart: 'N06Rmf2K',
        holder_lane: 'hermes-atlas',
        holder_pid: 4242,
        schema: 'tv.write_lease/v1',
        ...lease
      })
    )
  }

  it('held only when present, well-formed, unexpired and on the current epoch; the holder and liveness named', async () => {
    const { readWriteLease } = await import('./tv-lanes-ipc')
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tv-lease-'))
    expect(readWriteLease(dir, () => T0)).toMatchObject({ held: false, holderLane: null, why: 'no_lease' })
    write(dir, {})
    expect(
      readWriteLease(
        dir,
        () => T0,
        () => true
      )
    ).toMatchObject({
      expiresAt: '2026-10-02T09:00:00.000Z',
      held: true,
      holderAlive: true,
      holderChart: 'N06Rmf2K',
      holderLane: 'hermes-atlas',
      holderPid: 4242,
      inFlight: false,
      why: null
    })
    expect(
      readWriteLease(
        dir,
        () => T0 + 2 * 3_600_000,
        () => true
      )
    ).toMatchObject({ held: false, why: 'expired' })
    write(dir, {}, '4')
    expect(
      readWriteLease(
        dir,
        () => T0,
        () => false
      )
    ).toMatchObject({ held: false, holderAlive: false, why: 'revoked' })
    fs.writeFileSync(
      path.join(dir, 'lease', 'in_flight.hermes-atlas.json'),
      JSON.stringify({ count: 1, pid: 4242, pid_start: 's', updated_at: new Date(T0 - 1000).toISOString() })
    )
    write(dir, {})
    expect(
      readWriteLease(
        dir,
        () => T0,
        () => true
      )
    ).toMatchObject({ held: true, inFlight: true })
    expect(
      readWriteLease(
        dir,
        () => T0,
        () => false
      )
    ).toMatchObject({ held: true, holderAlive: false, inFlight: false })
    fs.writeFileSync(path.join(dir, 'lease', 'in_flight.hermes-atlas.json'), '{broken')
    expect(
      readWriteLease(
        dir,
        () => T0,
        () => true
      )
    ).toMatchObject({ held: true, inFlight: true })
    fs.rmSync(path.join(dir, 'lease', 'in_flight.hermes-atlas.json'))
    fs.writeFileSync(path.join(dir, 'lease', 'write_lease.json'), '{not json')
    expect(readWriteLease(dir, () => T0)).toMatchObject({ held: false, why: 'lease_unreadable' })
    fs.writeFileSync(path.join(dir, 'lease', 'write_lease.json'), JSON.stringify({ schema: 'something-else' }))
    expect(readWriteLease(dir, () => T0)).toMatchObject({ held: false, why: 'lease_unreadable' })
    write(dir, {})
    fs.writeFileSync(path.join(dir, 'lease', 'revocation_epoch'), '')
    expect(readWriteLease(dir, () => T0)).toMatchObject({ held: false, why: 'epoch_unavailable' })
  })
})
