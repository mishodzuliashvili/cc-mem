import { useState } from 'react'
import { api } from '../api'

export default function SearchView({ onOpen }) {
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState('semantic')
  const [hits, setHits] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  const run = async (e) => {
    e?.preventDefault()
    if (!query.trim()) return
    setBusy(true); setErr(null)
    try {
      const r = await api.search(query, mode, 25)
      setHits(r.hits)
    } catch (e2) {
      setErr(e2.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <form className="searchbar" onSubmit={run}>
        <input
          className="grow"
          autoFocus
          placeholder={mode === 'semantic'
            ? 'Semantic search — describe what you mean…'
            : 'Text search — exact words/substrings…'}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="semantic">semantic</option>
          <option value="text">text</option>
        </select>
        <button className="btn" disabled={busy}>{busy ? '…' : 'Search'}</button>
      </form>

      {mode === 'semantic' && (
        <div className="legend">
          Semantic matches by meaning (synonyms, paraphrase). First query may take
          ~1–2s while the model warms up.
        </div>
      )}

      {err && <div className="empty">Error: {err}</div>}
      {hits && hits.length === 0 && <div className="empty">No matches.</div>}
      {hits && hits.map((h) => (
        <div key={h.id} className="hit" onClick={() => onOpen(h.id)}>
          {h.score != null && <span className="score">{h.score.toFixed(3)}</span>}
          <span className="hl">{h.label || `#${h.id}`}</span>{' '}
          <span className={`pill ${h.scope}`}>{h.scope}</span>
          <div className="meta">{h.summary}</div>
        </div>
      ))}
    </>
  )
}
