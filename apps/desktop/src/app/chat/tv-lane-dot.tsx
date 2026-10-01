/**
 * The TradingView lane dot (owner ask, 2026-10-01): on a workspace (tree) header and on each session row, coloured
 * from `store/tv-lanes` — blue registered, green live, amber degraded, red conflict, hollow stale/declared. Hovering
 * says why in one sentence; clicking opens "Link or change lane": bind this tree (or only this chat) to a registered
 * Hermes lane, unlink, or start the lamp's one-click "new Hermes lane" flow. Separate from the chat-activity dot on
 * purpose: that one says what the chat is doing, this one says which chart it is on and whether the lane is alive.
 */
import { useStore } from '@nanostores/react'
import { useState } from 'react'

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import { notify, notifyError } from '@/store/notifications'
import { $tvLanes, bindTvLane, createTvLane, resolveTvLane, type TvLaneDotState } from '@/store/tv-lanes'

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
  conflict: 'CONFLICT',
  declared: 'declared by the CLI',
  degraded: 'degraded',
  linked: 'registered',
  live: 'LIVE',
  stale: 'stale',
  unlinked: 'no lane'
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
  const r = resolveTvLane(snapshot, { cwd, sessionId, workspacePath })

  if (!window.hermesDesktop?.tvLanes) {
    return null
  }

  if (r.state === 'unlinked' && hideUnlinked) {
    return null
  }

  const lanes = Object.values(snapshot?.lanes ?? {}).sort((a, b) => a.id.localeCompare(b.id))
  const hermesLanes = lanes.filter(l => l.id.startsWith('hermes'))
  const scopeKey = workspacePath ?? null
  const label = `TradingView lane: ${STATE_WORD[r.state]} — ${r.detail}`

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

  const laneItems = (scope: 'session' | 'workspace', key: null | string) =>
    hermesLanes.map(l => (
      <DropdownMenuItem key={`${scope}:${l.id}`} onSelect={() => void apply(scope, key, l.id)}>
        <span aria-hidden className={cn(DOT_BASE, 'mr-1.5')} style={{ backgroundColor: l.color ?? undefined }} />
        <span className="font-mono text-xs">{l.id}</span>
        <span className="ml-1 truncate text-(--ui-text-tertiary)">
          {l.label} · {l.layouts.join(', ') || 'no layout'}
          {r.laneId === l.id && r.source === scope ? ' · current' : ''}
        </span>
      </DropdownMenuItem>
    ))

  return (
    <DropdownMenu onOpenChange={setOpen} open={open}>
      <DropdownMenuTrigger asChild>
        <button
          aria-label={label}
          className={cn('flex size-4 shrink-0 items-center justify-center bg-transparent', className)}
          data-tv-lane-state={r.state}
          onClick={e => e.stopPropagation()}
          onPointerDown={e => e.stopPropagation()}
          title={label}
          type="button"
        >
          <span aria-hidden className={cn(DOT_BASE, DOT_CLASS[r.state])} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-64">
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
