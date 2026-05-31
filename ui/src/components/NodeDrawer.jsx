import { useEffect, useState } from 'react'
import { marked } from 'marked'
import { api } from '../api'

const BLANK = {
  label: '', summary: '', content: '', scope: 'global', type: 'fact',
  importance: 1, confidence: 1, sources: '', project: '',
}

const TYPES = ['fact', 'preference', 'decision', 'howto', 'gotcha', 'reference']

export default function NodeDrawer({ id, onClose, onChanged, onOpenOther, flash }) {
  const isNew = id === 'new'
  const [node, setNode] = useState(null)
  const [editing, setEditing] = useState(isNew)
  const [form, setForm] = useState(BLANK)
  const [linkTarget, setLinkTarget] = useState('')

  useEffect(() => {
    if (isNew) { setNode(null); setForm(BLANK); setEditing(true); return }
    api.getNode(id).then((n) => {
      setNode(n)
      setForm({ ...BLANK, ...n })
      setEditing(false)
    }).catch((e) => flash(e.message, true))
  }, [id, isNew, flash])

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  const save = async () => {
    try {
      if (isNew) {
        if (!form.content.trim()) return flash('content is required', true)
        const r = await api.createNode({
          ...form,
          importance: Number(form.importance),
          confidence: Number(form.confidence),
        })
        flash(`created #${r.id}`)
        onChanged()
        onOpenOther(r.id)
      } else {
        await api.updateNode(id, {
          ...form,
          importance: Number(form.importance),
          confidence: Number(form.confidence),
        })
        flash('saved')
        const fresh = await api.getNode(id)
        setNode(fresh); setEditing(false); onChanged()
      }
    } catch (e) { flash(e.message, true) }
  }

  const remove = async () => {
    if (!confirm(`Delete memory #${id}? This also removes its edges.`)) return
    try {
      await api.deleteNode(id)
      flash(`deleted #${id}`)
      onChanged(); onClose()
    } catch (e) { flash(e.message, true) }
  }

  const addLink = async () => {
    const dst = linkTarget.trim()
    if (!dst) return
    try {
      await api.link(id, dst, 'related', 1)
      flash(`linked ${id} ↔ ${dst}`)
      setLinkTarget('')
      setNode(await api.getNode(id)); onChanged()
    } catch (e) { flash(e.message, true) }
  }

  const removeLink = async (dst) => {
    try {
      await api.unlink(id, dst)
      setNode(await api.getNode(id)); onChanged()
    } catch (e) { flash(e.message, true) }
  }

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer">
        <header>
          <span className="title">
            {isNew ? 'New memory' : `#${id} ${node?.label || ''}`}
          </span>
          {!isNew && !editing && (
            <button className="btn ghost" onClick={() => setEditing(true)}>Edit</button>
          )}
          <button className="close" onClick={onClose}>×</button>
        </header>

        <div className="body">
          {editing ? (
            <EditForm form={form} set={set} />
          ) : node ? (
            <ViewNode node={node} onOpenOther={onOpenOther} onRemoveLink={removeLink} />
          ) : (
            <div className="empty">loading…</div>
          )}

          {!isNew && !editing && node && (
            <div className="linkrow">
              <input
                placeholder="link to id (same tier, e.g. p:ab12)…"
                value={linkTarget}
                onChange={(e) => setLinkTarget(e.target.value)}
                style={{ width: 220 }}
              />
              <button className="btn ghost" onClick={addLink}>+ link</button>
            </div>
          )}
        </div>

        <div className="footer">
          {editing ? (
            <>
              <button className="btn" onClick={save}>{isNew ? 'Create' : 'Save'}</button>
              <button
                className="btn ghost"
                onClick={() => (isNew ? onClose() : setEditing(false))}
              >
                Cancel
              </button>
              <span className="spacer" style={{ flex: 1 }} />
              {!isNew && <button className="btn danger" onClick={remove}>Delete</button>}
            </>
          ) : (
            <button className="btn ghost" onClick={onClose}>Close</button>
          )}
        </div>
      </aside>
    </>
  )
}

function ViewNode({ node, onOpenOther, onRemoveLink }) {
  return (
    <>
      <span className={`pill ${node.scope}`}>{node.scope}</span>{' '}
      <span className="pill type">{node.type || 'fact'}</span>
      <p className="metaline">{node.summary}</p>
      <p className="metaline">
        <b>importance</b> {node.importance} · <b>seen</b> {node.access_count}× ·{' '}
        <b>confidence</b> {node.confidence}
        {node.project ? <> · <b>project</b> {node.project}</> : null}
      </p>
      {node.sources && <p className="metaline"><b>source:</b> {node.sources}</p>}

      <div className="md" dangerouslySetInnerHTML={{ __html: marked.parse(node.content || '') }} />

      {node.neighbors?.length > 0 && (
        <div className="neighbors">
          <p className="metaline"><b>Linked memories</b></p>
          {node.neighbors.map((nb) => (
            <div className="neighbor" key={`${nb.id}-${nb.kind}`}>
              <a onClick={() => onOpenOther(nb.id)}>#{nb.id} {nb.label || nb.summary}</a>
              <span className="spacer" style={{ flex: 1 }} />
              <span className="k">{nb.kind}</span>
              <span className="w">{nb.weight}</span>
              <button className="close" title="unlink" onClick={() => onRemoveLink(nb.id)}>×</button>
            </div>
          ))}
        </div>
      )}
    </>
  )
}

function EditForm({ form, set }) {
  return (
    <>
      <div className="row">
        <div className="field">
          <label>Label</label>
          <input value={form.label} onChange={set('label')} placeholder="short title" />
        </div>
        <div className="field" style={{ maxWidth: 120 }}>
          <label>Scope</label>
          <select value={form.scope} onChange={set('scope')}>
            <option value="global">global</option>
            <option value="project">project</option>
          </select>
        </div>
        <div className="field" style={{ maxWidth: 130 }}>
          <label>Type</label>
          <select value={form.type} onChange={set('type')}>
            {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
      </div>
      <div className="field">
        <label>Summary (drives recall)</label>
        <input value={form.summary} onChange={set('summary')} placeholder="one-line summary" />
      </div>
      <div className="field">
        <label>Content (markdown)</label>
        <textarea value={form.content} onChange={set('content')} placeholder="the memory…" />
      </div>
      <div className="row">
        <div className="field">
          <label>Importance</label>
          <input type="number" step="0.5" value={form.importance} onChange={set('importance')} />
        </div>
        <div className="field">
          <label>Confidence (0–1)</label>
          <input type="number" step="0.1" min="0" max="1" value={form.confidence} onChange={set('confidence')} />
        </div>
      </div>
      <div className="field">
        <label>Sources / how verified</label>
        <input value={form.sources} onChange={set('sources')} placeholder="provenance" />
      </div>
      {form.scope === 'project' && (
        <div className="field">
          <label>Project</label>
          <input value={form.project} onChange={set('project')} placeholder="/path/to/project" />
        </div>
      )}
    </>
  )
}
