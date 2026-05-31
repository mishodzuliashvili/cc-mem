import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import { useLive } from './useLive'
import TableView from './views/TableView'
import SearchView from './views/SearchView'
import GraphView from './views/GraphView'
import PendingView from './views/PendingView'
import NodeDrawer from './components/NodeDrawer'

const TABS = [
  { id: 'table', label: 'Table' },
  { id: 'search', label: 'Search' },
  { id: 'graph', label: 'Graph' },
  { id: 'pending', label: 'Pending' },
]

export default function App() {
  const [tab, setTab] = useState('table')
  const [stats, setStats] = useState(null)
  const [openId, setOpenId] = useState(null) // node id in drawer; 'new' = create form
  const [toast, setToast] = useState(null)
  const [pending, setPending] = useState(0)
  const { tick, bump, online } = useLive(2000)

  const flash = useCallback((msg, err = false) => {
    setToast({ msg, err })
    setTimeout(() => setToast(null), 2400)
  }, [])

  // Refresh stats + pending count whenever the DB changes (live tick) or we mutate.
  useEffect(() => {
    api.stats().then(setStats).catch(() => {})
    api.pending().then((r) => setPending(r.total)).catch(() => {})
  }, [tick])

  const onChanged = useCallback(() => { bump() }, [bump])

  return (
    <div className="app">
      <div className="topbar">
        <span className="logo">cc-mem</span>
        <nav className="tabs">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`tab ${tab === t.id ? 'active' : ''}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
              {t.id === 'pending' && pending > 0 && <span className="badge">{pending}</span>}
            </button>
          ))}
        </nav>
        <span className="spacer" />
        <span className={`live ${online ? 'on' : 'off'}`} title={online ? 'auto-updating' : 'backend offline'}>
          ● {online ? 'live' : 'offline'}
        </span>
        {stats && (
          <span className="stats">
            {stats.nodes} nodes · {stats.edges} edges ·{' '}
            {Object.entries(stats.by_scope || {}).map(([k, v]) => `${k} ${v}`).join(' · ')}
          </span>
        )}
        <button className="btn" onClick={() => setOpenId('new')}>+ New memory</button>
      </div>

      <div className="main">
        {tab === 'table' && <TableView tick={tick} onOpen={setOpenId} />}
        {tab === 'search' && <SearchView onOpen={setOpenId} />}
        {tab === 'graph' && <GraphView tick={tick} onOpen={setOpenId} />}
        {tab === 'pending' && <PendingView tick={tick} onChanged={bump} flash={flash} />}
      </div>

      {openId != null && (
        <NodeDrawer
          id={openId}
          onClose={() => setOpenId(null)}
          onChanged={onChanged}
          onOpenOther={(id) => setOpenId(id)}
          flash={flash}
        />
      )}

      {toast && <div className={`toast ${toast.err ? 'err' : ''}`}>{toast.msg}</div>}
    </div>
  )
}
