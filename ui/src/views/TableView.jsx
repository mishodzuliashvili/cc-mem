import { useEffect, useState } from 'react'
import { api } from '../api'

const COLUMNS = [
  { key: 'id', label: 'ID' },
  { key: 'label', label: 'Label' },
  { key: 'summary', label: 'Summary' },
  { key: 'type', label: 'Type' },
  { key: 'scope', label: 'Scope' },
  { key: 'importance', label: 'Imp' },
  { key: 'access_count', label: 'Seen' },
  { key: 'created_at', label: 'Created' },
]

const TYPES = ['fact', 'preference', 'decision', 'howto', 'gotcha', 'reference']

export default function TableView({ onOpen, tick, projects = [] }) {
  const [q, setQ] = useState('')
  const [scope, setScope] = useState('')
  const [project, setProject] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [sort, setSort] = useState('id')
  const [order, setOrder] = useState('desc')
  const [data, setData] = useState({ nodes: [], shown: 0, total: 0 })
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    const t = setTimeout(() => {
      setLoading(true)
      api.listNodes({ q, scope, sort, order, limit: 500 })
        .then(setData)
        .finally(() => setLoading(false))
    }, 180) // debounce typing
    return () => clearTimeout(t)
    // `tick` bumps when the DB changes (live) — refetch keeps current filters.
  }, [q, scope, sort, order, tick])

  const toggleSort = (key) => {
    if (sort === key) setOrder(order === 'asc' ? 'desc' : 'asc')
    else { setSort(key); setOrder('desc') }
  }

  // type + project filters are applied client-side on the fetched rows
  const rows = data.nodes.filter(
    (n) => (!typeFilter || (n.type || 'fact') === typeFilter)
      && (!project || n.project === project),
  )
  const shortProj = (p) => (p ? p.split('/').pop() : '')

  return (
    <>
      <div className="toolbar">
        <input
          className="grow"
          placeholder="Filter by label / summary / content…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select value={scope} onChange={(e) => setScope(e.target.value)}>
          <option value="">all scopes</option>
          <option value="global">global</option>
          <option value="project">project</option>
        </select>
        <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
          <option value="">all types</option>
          {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        {projects.length > 0 && (
          <select value={project} onChange={(e) => setProject(e.target.value)}>
            <option value="">all projects</option>
            {projects.map((p) => <option key={p.key} value={p.key}>{shortProj(p.key)}</option>)}
          </select>
        )}
        <span className="stats">
          {loading ? 'loading…' : `${rows.length} of ${data.total}`}
        </span>
      </div>

      {rows.length === 0 ? (
        <div className="empty">No memories match. Click “+ New memory” to add one.</div>
      ) : (
        <table>
          <thead>
            <tr>
              {COLUMNS.map((c) => (
                <th key={c.key} onClick={() => toggleSort(c.key)}>
                  {c.label}{sort === c.key ? (order === 'asc' ? ' ▲' : ' ▼') : ''}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((n) => (
              <tr key={n.id} onClick={() => onOpen(n.id)}>
                <td className="id">{n.id}</td>
                <td className="label">{n.label || <span className="empty">—</span>}</td>
                <td className="summary">{n.summary}</td>
                <td><span className="pill type">{n.type || 'fact'}</span></td>
                <td>
                  <span className={`pill ${n.scope}`}>{n.scope}</span>
                  {n.project && <span className="proj"> {shortProj(n.project)}</span>}
                </td>
                <td className="num">{n.importance}</td>
                <td className="num">{n.access_count}</td>
                <td className="num">{new Date(n.created_at * 1000).toLocaleDateString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
