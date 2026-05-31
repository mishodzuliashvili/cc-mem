import { useEffect, useRef, useState } from 'react'
import { api } from './api'

// Polls /api/version every `intervalMs`. Returns a `tick` that increments only
// when the DB actually changed (another process committed — e.g. Claude
// inserting a memory via MCP). Views depend on `tick` to refetch without losing
// their local UI state, and without hammering the DB when nothing changed.
export function useLive(intervalMs = 2000) {
  const [tick, setTick] = useState(0)
  const last = useRef(null)
  const [online, setOnline] = useState(true)

  useEffect(() => {
    let stop = false
    const poll = async () => {
      try {
        const { version } = await api.version()
        setOnline(true)
        if (last.current === null) last.current = version
        else if (version !== last.current) {
          last.current = version
          setTick((t) => t + 1)
        }
      } catch {
        setOnline(false)
      }
    }
    poll()
    const h = setInterval(() => { if (!stop) poll() }, intervalMs)
    return () => { stop = true; clearInterval(h) }
  }, [intervalMs])

  // bump() forces a refetch after the UI's own mutations (create/edit/delete),
  // which don't move data_version on our own connection.
  const bump = () => setTick((t) => t + 1)
  return { tick, bump, online }
}
