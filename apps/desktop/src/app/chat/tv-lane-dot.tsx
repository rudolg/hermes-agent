/**
 * The TradingView lane dot (owner ask, 2026-10-01): on a workspace (tree) header and on each session row, coloured
 * from `store/tv-lanes` — blue registered, green live, amber degraded, red conflict, hollow stale/declared. Hovering
 * says why in one sentence; clicking opens "Link or change lane": bind this tree (or only this chat) to a registered
 * Hermes lane, unlink, or start the lamp's one-click "new Hermes lane" flow. Separate from the chat-activity dot on
 * purpose: that one says what the chat is doing, this one says which chart it is on and whether the lane is alive.
 *
 * The same menu carries, for the bound lane, its ALLOWED CHARTS (accept the chart the tab shows, add one by id,
 * remove one — the lamp's own scripts, started here) and the TV MCP SERVERS this app has configured: lane, read-only
 * or write, on/off, lazy / idle recycle, whether the gateway is kept out, and which one is this tree's (owner,
 * 2026-10-01: "more option in hermes submenus for project with tvmcps allowed/loaded"). Switching a server on or off
 * writes the config through the app's own API; it takes effect at /reload-mcp, which the notice says.
 */
import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { getHermesConfigRecord, setMcpServerEnabled } from '@/hermes'
import { type TvServerRow, tvServerRows } from '@/lib/tv-server-rows'
import { cn } from '@/lib/utils'
import { notify, notifyError } from '@/store/notifications'
import { $tvLanes, $tvServerRows, bindTvLane, createTvLane, laneWritesEnabled, resolveTvLane, runTvLane, type TvLaneDotState } from '@/store/tv-lanes'

// Same size as the chat-activity dot; the faint grey hollow for "no lane" is wanted (owner, 2026-10-01: "grey is very
// faint - good"), so a tree without a lane stays quiet and a bound one stands out.
const DOT_BASE = 'inline-block size-1.5 shrink-0 rounded-full'

const DOT_CLASS: Record<TvLaneDotState, string> = {
  conflict: 'bg-red-500',
  declared: 'border border-sky-500',
  degraded: 'bg-amber-500',
  linked: 'bg-sky-500',
  live: 'bg-emerald-500',
  stale: 'border border-amber-500',
  unlinked: 'border border-(--ui-text-quaternary)'
}

const STATE_WORD: Record<TvLaneDotState, string> = {
  conflict: 'FAULT',
  declared: 'declared by the CLI',
  degraded: 'degraded',
  linked: 'registered',
  live: 'LIVE',
  stale: 'stale snapshot',
  unlinked: 'no lane (click to link)'
}

export interface TvLaneDotProps {
  className?: string
  cwd?: null | string
  sessionId?: null | string
  /** A tree header passes its path; the binding then applies to every chat in the tree. */
  workspacePath?: null | string
  /** Row mode: draw nothing when unlinked (the tree header carries the dot for linking). */
  hideUnlinked?: boolean
}

export function TvLaneDot({ className, cwd, hideUnlinked = false, sessionId, workspacePath }: TvLaneDotProps) {
  const snapshot = useStore($tvLanes)
  const [open, setOpen] = useState(false)
  const [servers, setServers] = useState<null | TvServerRow[]>(null)
  const r = resolveTvLane(snapshot, { cwd, sessionId, workspacePath })
  const polledRows = useStore($tvServerRows)
  // READ vs WRITE at a glance (owner, 2026-10-02: "maybe green is read and two green is write"): a second dot when the
  // bound lane's entry has chart writes enabled. The CLASS, not a live lease: each write is still gated per chart.
  const writes = laneWritesEnabled(polledRows, r.laneId)

  // The server list is read when the menu opens (one config fetch), never on every poll.
  useEffect(() => {
    if (!open) {
      return
    }

    let live = true
    getHermesConfigRecord()
      .then(config => {
        if (live) {
          setServers(tvServerRows(config as { mcp_servers?: unknown }))
        }
      })
      .catch(() => {
        if (live) {
          setServers([])
        }
      })

    return () => {
      live = false
    }
  }, [open])

  if (!window.hermesDesktop?.tvLanes) {
    return null
  }

  if (r.state === 'unlinked' && hideUnlinked) {
    return null
  }

  const lanes = Object.values(snapshot?.lanes ?? {}).sort((a, b) => a.id.localeCompare(b.id))
  const hermesLanes = lanes.filter(l => l.id.startsWith('hermes'))
  const scopeKey = workspacePath ?? null
  const rights = r.laneId && r.state !== 'unlinked' ? (writes ? ' · WRITES ENABLED (two dots)' : ' · read-only') : ''
  const label = `TradingView lane: ${STATE_WORD[r.state]}${rights} — ${r.detail}`

  const apply = async (scope: 'session' | 'workspace', key: null | string, lane: null | string) => {
    if (!key) {
      notify({ message: 'This chat has no workspace to bind; use "only this chat".' })

      return
    }

    const error = await bindTvLane(scope, key, lane)

    if (error) {
      notifyError(new Error(error), 'Lane not changed')
    } else {
      notify({ message: lane ? `Bound to lane ${lane}` : 'Lane unlinked' })
    }
  }

  const start = async (request: Parameters<typeof runTvLane>[0], started: string) => {
    const error = await runTvLane(request)

    if (error) {
      notifyError(new Error(error), 'Not started')
    } else {
      notify({ message: started })
    }
  }

  const toggleServer = async (row: TvServerRow) => {
    try {
      const res = await setMcpServerEnabled(row.name, !row.enabled)

      if (!res.ok) {
        throw new Error('the app refused the change')
      }

      notify({ message: `${row.name} ${row.enabled ? 'OFF' : 'ON'} in the config; run /reload-mcp to apply` })
      setServers(prev => prev?.map(s => (s.name === row.name ? { ...s, enabled: !row.enabled } : s)) ?? prev)
    } catch (error) {
      notifyError(error, `${row.name} not changed`)
    }
  }

  // ONE CHART NEVER HAS TWO DRIVERS (owner, 2026-10-01): a lane whose claim another driver holds cannot be taken
  // from here — it is shown, greyed, with its holder named, and the IPC refuses it as well. Free lanes and lanes this
  // app already holds are the only choices.
  const heldElsewhere = (l: (typeof hermesLanes)[number]): null | string =>
    l.claimState !== 'held' || l.holderApp === 'hermes'
      ? null
      : l.holderApp === 'claude'
        ? `held by a Claude window (pid ${l.holderPid}): release it there first`
        : l.holderApp === 'hermes-gateway'
          ? `held by the background Hermes gateway (pid ${l.holderPid})`
          : `held by another process (pid ${l.holderPid ?? '?'})`

  const laneItems = (scope: 'session' | 'workspace', key: null | string) =>
    hermesLanes.map(l => {
      const blocked = heldElsewhere(l)

      return (
        <DropdownMenuItem disabled={blocked !== null} key={`${scope}:${l.id}`} onSelect={() => void apply(scope, key, l.id)}>
          <span aria-hidden className={cn(DOT_BASE, 'mr-1.5')} style={{ backgroundColor: l.color ?? undefined }} />
          <span className="font-mono text-xs">{l.id}</span>
          <span className="ml-1 truncate text-(--ui-text-tertiary)">
            {l.label} · {l.layouts.join(', ') || 'no layout'}
            {r.laneId === l.id && r.source === scope ? ' · current' : ''}
            {blocked ? ` · ${blocked}` : ''}
          </span>
        </DropdownMenuItem>
      )
    })

  const bound = r.laneId && r.lane && r.source !== 'declared' ? r.lane : null

  const loadedWord = (row: TvServerRow): string => {
    const lane = row.lane ? snapshot?.lanes[row.lane] : undefined

    if (!row.enabled) {
      return 'off'
    }

    if (lane?.claimState === 'held' && lane.holderApp === 'hermes') {
      return 'loaded, holding its lane'
    }

    return row.lazy ? 'idle (starts on first use)' : 'not holding'
  }

  return (
    <DropdownMenu onOpenChange={setOpen} open={open}>
      <DropdownMenuTrigger asChild>
        <button
          aria-label={label}
          className={cn('flex h-4 shrink-0 items-center justify-center bg-transparent px-0.5', className)}
          data-tv-lane-state={r.state}
          onClick={e => e.stopPropagation()}
          onPointerDown={e => e.stopPropagation()}
          title={label}
          type="button"
        >
          <span aria-hidden className={cn(DOT_BASE, DOT_CLASS[r.state])} />
          {writes && r.state !== 'unlinked' ? <span aria-hidden className={cn(DOT_BASE, 'ml-0.5', DOT_CLASS[r.state])} /> : null}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-72">
        <DropdownMenuLabel className="whitespace-normal text-xs font-normal">{label}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {scopeKey ? (
          <>
            <DropdownMenuLabel>This tree, every chat in it</DropdownMenuLabel>
            {laneItems('workspace', scopeKey)}
            {r.source === 'workspace' && (
              <DropdownMenuItem onSelect={() => void apply('workspace', scopeKey, null)}>Unlink this tree</DropdownMenuItem>
            )}
          </>
        ) : null}
        {sessionId ? (
          <>
            <DropdownMenuLabel>Only this chat</DropdownMenuLabel>
            {laneItems('session', sessionId)}
            {r.source === 'session' && (
              <DropdownMenuItem onSelect={() => void apply('session', sessionId, null)}>Unlink this chat</DropdownMenuItem>
            )}
          </>
        ) : null}
        {hermesLanes.length === 0 && (
          <DropdownMenuLabel className="font-normal text-(--ui-text-tertiary)">No Hermes lane registered yet</DropdownMenuLabel>
        )}
        {bound ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuLabel>
              Allowed charts on {bound.id}:{' '}
              <span className="font-mono font-normal">{bound.layouts.join(' · ') || '(none)'}</span>
            </DropdownMenuLabel>
            <DropdownMenuItem
              onSelect={() =>
                void start({ action: 'tab-assign-front', lane: bound.id }, `The Chrome tab in front becomes ${bound.id}'s tab (its old binding is replaced)`)
              }
            >
              Assign the Chrome tab in front to {bound.id}
            </DropdownMenuItem>
            <DropdownMenuItem
              onSelect={() =>
                void start({ action: 'chart-accept', lane: bound.id }, `Accepting the chart ${bound.id}'s tab shows; the lane restarts its controller (/reload-mcp)`)
              }
            >
              Allow the chart its tab shows now
            </DropdownMenuItem>
            <DropdownMenuItem
              onSelect={() => void start({ action: 'chart-add', lane: bound.id }, 'Answer the dialog with the chart id; the lane restarts its controller (/reload-mcp)')}
            >
              Allow a chart by id…
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={bound.layouts.length === 0}
              onSelect={() => void start({ action: 'chart-remove', lane: bound.id }, 'Pick the chart to remove; the lane restarts its controller (/reload-mcp)')}
            >
              Remove an allowed chart…
            </DropdownMenuItem>
          </>
        ) : null}
        <DropdownMenuSeparator />
        <DropdownMenuLabel>TV MCP servers in this app</DropdownMenuLabel>
        {servers === null ? (
          <DropdownMenuLabel className="font-normal text-(--ui-text-tertiary)">reading the config…</DropdownMenuLabel>
        ) : servers.length === 0 ? (
          <DropdownMenuLabel className="font-normal text-(--ui-text-tertiary)">no tradingview entry configured</DropdownMenuLabel>
        ) : (
          servers.map(row => (
            <DropdownMenuItem key={row.name} onSelect={() => void toggleServer(row)}>
              <span aria-hidden className={cn(DOT_BASE, 'mr-1.5', row.enabled ? 'bg-emerald-500' : 'border border-(--ui-text-quaternary)')} />
              <span className="font-mono text-xs">{row.name}</span>
              <span className="ml-1 truncate text-(--ui-text-tertiary)">
                {row.lane ?? 'no lane'} · {row.write ? 'WRITE' : 'read-only'} · {loadedWord(row)}
                {row.lazy ? ' · lazy' : ''}
                {row.idleS ? ` · idle ${Math.round(row.idleS / 60)} min` : ''}
                {row.gatewayOut ? '' : ' · gateway may load it'}
                {row.lane && row.lane === r.laneId ? " · this tree's" : ''}
                {` · click: turn ${row.enabled ? 'OFF' : 'ON'}`}
              </span>
            </DropdownMenuItem>
          ))
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onSelect={() =>
            void createTvLane().then(error => {
              if (error) {
                notifyError(new Error(error), 'New lane not started')
              } else {
                notify({ message: 'New Hermes lane: answer the dialogs; the lane appears here within a minute' })
              }
            })
          }
        >
          New Hermes lane… (asks which project)
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
