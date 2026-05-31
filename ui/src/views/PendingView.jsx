import { useEffect, useState } from 'react'
import { api } from '../api'

// Review queue for memories the SessionEnd auto-capture hook proposed.
// Approve -> inserted into the right scope/project; Dismiss -> discarded.
export default function PendingView({ tick, onChanged, flash }) {
  const [files, setFiles] = useState([])
  const [loading, setLoading] = useState(true)

  const load = () => {
    setLoading(true)
    api.pending().then((r) => setFiles(r.files)).finally(() => setLoading(false))
  }
  useEffect(load, [tick])

  const act = async (fn, file, index, msg) => {
    try { await fn(file, index); flash(msg); load(); onChanged() }
    catch (e) { flash(e.message, true) }
  }

  if (loading) return <div className="empty">loading…</div>
  if (files.length === 0) {
    return (
      <div className="empty">
        No pending proposals. The SessionEnd capture hook drops suggested memories
        here for review (enable with <code>setup.py hooks --capture</code>).
      </div>
    )
  }

  return (
    <>
      <div className="legend">
        Captured at the end of a session — nothing is saved until you approve it.
      </div>
      {files.map((f) =>
        f.proposals.map((p, i) => (
          <div key={`${f.file}-${i}`} className="hit">
            <span className="pill type">{p.type || 'fact'}</span>{' '}
            <span className={`pill ${p.scope || 'global'}`}>{p.scope || 'global'}</span>{' '}
            <span className="hl">{p.label}</span>
            <div className="meta">{p.summary}</div>
            {p.content && <div className="md" style={{ marginTop: 6 }}>{p.content}</div>}
            <div className="linkrow">
              <button className="btn" onClick={() =>
                act(api.approve, f.file, i, `saved “${p.label}”`)}>Approve</button>
              <button className="btn ghost" onClick={() =>
                act(api.dismiss, f.file, i, 'dismissed')}>Dismiss</button>
              {f.cwd && <span className="meta" style={{ alignSelf: 'center' }}>from {f.cwd}</span>}
            </div>
          </div>
        )),
      )}
    </>
  )
}
