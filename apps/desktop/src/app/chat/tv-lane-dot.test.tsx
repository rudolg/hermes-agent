/**
 * The TV MCP server rows the lane menu shows are read from the app's config record: lane and capability from the
 * entry's env, on/off from `enabled` (the shared reader), lazy / idle recycle / gateway-out from the entry; only
 * tradingview* entries, in name order. A negative control: a non-tradingview entry never appears.
 */
import { describe, expect, it } from 'vitest'

import { tvServerRows } from './tv-lane-dot'

describe('tvServerRows', () => {
  it('lists the tradingview entries with lane, capability, state flags, in name order', () => {
    const rows = tvServerRows({
      mcp_servers: {
        github: { command: 'npx' },
        tradingview_hermes_atlas: {
          command: '/opt/homebrew/bin/node',
          enabled: true,
          env: { TV_CDP_CHANNEL: 'hermes-atlas', TV_MCP_CAPABILITIES: 'read_only' },
          gateway: false,
          idle_timeout_seconds: 900,
          lazy: true
        },
        tradingview_hermes: {
          command: '/opt/homebrew/bin/node',
          enabled: 'false',
          env: { TV_CDP_CHANNEL: 'hermes', TV_MCP_CAPABILITIES: 'read_only,chart_write' }
        }
      }
    })

    expect(rows.map(r => r.name)).toEqual(['tradingview_hermes', 'tradingview_hermes_atlas'])
    expect(rows[0]).toMatchObject({ enabled: false, gatewayOut: false, idleS: null, lane: 'hermes', lazy: false, write: true })
    expect(rows[1]).toMatchObject({ enabled: true, gatewayOut: true, idleS: 900, lane: 'hermes-atlas', lazy: true, write: false })
  })

  it('answers an empty list for no config, a missing block, or no tradingview entry', () => {
    expect(tvServerRows(null)).toEqual([])
    expect(tvServerRows({})).toEqual([])
    expect(tvServerRows({ mcp_servers: { github: { command: 'npx' } } })).toEqual([])
  })
})
