/**
 * TradingView CDP lanes as the Hermes sidebar sees them (owner ask, 2026-10-01: "a coloured dot in the sessions list,
 * blue = chat/project registered as a CDP lane, green = connected to CDP and all up and running — compulsory, at the
 * tree level too"). Shared between main (which reads the files) and the renderer (which draws the dot).
 */

/** One lane, as the registry declares it and the publisher's last snapshot saw it. */
export interface TvLaneView {
  id: string
  label: string
  color: null | string
  layouts: string[]
  /** The registry's policy for the lane (battlefield / experiment …); shown so a reader lane is never mistaken for a writer. */
  policy: null | string
  /** The publisher's own "up" (claim held by a live server AND its tab bound). */
  up: boolean
  active: boolean
  claimState: 'free' | 'held' | 'stale' | 'unknown'
  holderPid: null | number
  /** Which app the claim holder descends from, by process ancestry; null when unmeasurable. 'hermes' is THIS desktop
   *  app; 'hermes-gateway' is the background `hermes` gateway (launchd), a different Hermes process. */
  holderApp: 'claude' | 'hermes' | 'hermes-gateway' | 'other' | null
  healthVerdict: null | string
  healthFresh: boolean
  intended: null | string
  slugs: string[]
  tabPresent: boolean
  reasons: string[]
  /** The bound tab's CDP target id (the badge shows its first six characters), null when no tab is bound. */
  targetId: null | string
}

export interface TvLaneBinding {
  lane: string
  since: string
}

/** The whole answer to one `hermes:tv-lanes:get`. */
export interface TvLanesSnapshot {
  ok: boolean
  /** Why `ok` is false (registry unreadable, home missing …); null when ok. */
  error: null | string
  /** ISO time the publisher wrote channel_status.json; null when no snapshot could be read. */
  generatedAt: null | string
  /** Seconds since `generatedAt` at the time of this answer; null when unknown. */
  snapshotAgeS: null | number
  lanes: Record<string, TvLaneView>
  /** THIS app's declarations, keyed by workspace path (trailing slash stripped) and by session id. */
  bindings: { sessions: Record<string, TvLaneBinding>; workspaces: Record<string, TvLaneBinding> }
  /** The CLI's own declaration per asked workspace: `<workspace>/.mcp.json` env TV_CDP_CHANNEL. A declaration only. */
  declared: Record<string, string>
  /** Where the bindings file lives (for the tooltip / a reveal). */
  bindingsPath: string
}

export interface TvLaneBindRequest {
  scope: 'session' | 'workspace'
  /** Workspace path or session id. */
  key: string
  /** Lane id to bind, or null to unlink. */
  lane: null | string
}

export interface TvLaneBindResult {
  ok: boolean
  error: null | string
}

/** One of the lamp's own lane scripts, started detached (they show their own dialogs). The allow-list IS the contract. */
export type TvLaneRunRequest =
  | { action: 'create' }
  | { action: 'chart-accept' | 'chart-add' | 'chart-remove' | 'tab-assign-front'; lane: string }

export interface TvLanesApi {
  get: (workspacePaths: string[]) => Promise<TvLanesSnapshot>
  bind: (request: TvLaneBindRequest) => Promise<TvLaneBindResult>
  /** Start the lamp's own "new Hermes lane" flow (it asks for the project / name in its own dialogs). */
  create: () => Promise<TvLaneBindResult>
  /** Run one allow-listed lamp script: new lane, or change a lane's allowed charts (accept / add by id / remove). */
  run: (request: TvLaneRunRequest) => Promise<TvLaneBindResult>
}
