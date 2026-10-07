/**
 * The tradingview* entries of the app's config, as the lane menu and the lane dot read them: lane and capability from the
 * entry's env, on/off from the shared `enabled` reader, lazy / idle recycle / gateway-out from the entry.
 */
import { getServers, serverEnabled } from '@/lib/mcp-servers'

/** One configured TradingView MCP entry, as the menu shows it. */
export interface TvServerRow {
  name: string
  lane: null | string
  write: boolean
  enabled: boolean
  lazy: boolean
  idleS: null | number
  gatewayOut: boolean
}

const str = (v: unknown): null | string => (typeof v === 'string' && v ? v : null)
const bool = (v: unknown): boolean => v === true || v === 'true' || v === 1 || v === '1'

/** The tradingview* entries of the app's config, in name order. */
export function tvServerRows(config: null | { mcp_servers?: unknown }): TvServerRow[] {
  return Object.entries(getServers(config))
    .filter(([name]) => name.startsWith('tradingview'))
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, cfg]) => {
      const env = typeof cfg.env === 'object' && cfg.env !== null ? (cfg.env as Record<string, unknown>) : {}
      const caps = str(env.TV_MCP_CAPABILITIES) ?? ''
      const idle =
        typeof cfg.idle_timeout_seconds === 'number' ? cfg.idle_timeout_seconds : Number(cfg.idle_timeout_seconds)

      return {
        enabled: serverEnabled(cfg),
        gatewayOut: cfg.gateway === false || cfg.gateway === 'false',
        idleS: Number.isFinite(idle) && idle > 0 ? idle : null,
        lane: str(env.TV_CDP_CHANNEL),
        lazy: bool(cfg.lazy),
        name,
        write: /chart_write|pine_write|write/.test(caps)
      }
    })
}
