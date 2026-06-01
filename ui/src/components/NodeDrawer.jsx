import { useEffect, useState } from 'react'
import { marked } from 'marked'
import { api } from '../api'

const BLANK = {
  label: '', summary: '', content: '', scope: 'global', type: 'fact',
  importance: 1, confidence: 1, sources: '', project: '',
}

const TYPES = ['fact', 'preference', 'decision', 'howto', 'gotcha', 'reference']

// Open a referenced file in the editor (vscode://). abspath comes from the backend.
function fileHref(r) {
  if (!r.abspath) return null
  const line = r.lines ? `:${String(r.lines).split('-')[0]}` : ''
  return `vscode://file/${r.abspath}${line}`
}

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

// Render markdown, turning [[id]] node references AND any mentioned file-ref path
// into real, clickable links (node refs open the node; file refs open the file and
// are styled by existence). Relationships still belong in edges, file deps in refs —
// this just makes prose mentions live.
function renderContent(content, refs = []) {
  let html = content.replace(
    /\[\[([\w:.\-]+)\]\]/g,
    '<a class="noderef" data-ref="$1">$1</a>',
  )
  for (const r of refs) {
    if (!r.path) continue
    const href = fileHref(r)
    const cls = `fileref ${r.exists === false ? 'missing' : ''}`
    const anchor = href
      ? `<a class="${cls}" href="${href}" title="${r.abspath || r.path}">${r.path}</a>`
      : `<span class="${cls}">${r.path}</span>`
    html = html.replace(new RegExp(escapeRe(r.path), 'g'), anchor)
  }
  return marked.parse(html)
}

export default function NodeDrawer({ id, onClose, onChanged, onOpenOther, flash, projects = [] }) {
  const isNew = id === 'new'
  const [node, setNode] = useState(null)
  const [editing, setEditing] = useState(isNew)
  const [form, setForm] = useState(BLANK)
  const [linkTarget, setLinkTarget] = useState('')
  const [suggestions, setSuggestions] = useState([])

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

  const linkTo = async (dst) => {
    if (!dst) return
    try {
      await api.link(id, dst, 'related', 1)
      flash(`linked ${id} ↔ ${dst}`)
      setLinkTarget('')
      setSuggestions((s) => s.filter((c) => c.id !== dst))
      setNode(await api.getNode(id)); onChanged()
    } catch (e) { flash(e.message, true) }
  }
  const addLink = () => linkTo(linkTarget.trim())

  const loadSuggestions = async () => {
    try {
      const r = await api.suggest(id)
      setSuggestions(r.candidates)
      if (!r.candidates.length) flash('no unlinked similar nodes found')
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
            <EditForm form={form} set={set} projects={projects} isNew={isNew} />
          ) : node ? (
            <ViewNode node={node} onOpenOther={onOpenOther} onRemoveLink={removeLink} flash={flash} />
          ) : (
            <div className="empty">loading…</div>
          )}

          {!isNew && !editing && node && (
            <>
              <div className="linkrow">
                <input
                  placeholder="link to id (same tier, e.g. p:ab12)…"
                  value={linkTarget}
                  onChange={(e) => setLinkTarget(e.target.value)}
                  style={{ width: 220 }}
                />
                <button className="btn ghost" onClick={addLink}>+ link</button>
                <button className="btn ghost" onClick={loadSuggestions}>Suggest connections</button>
              </div>
              {suggestions.length > 0 && (
                <div className="suggestions">
                  <p className="metaline"><b>Suggested connections</b> (similar, not yet linked)</p>
                  {suggestions.map((c) => (
                    <div className="neighbor" key={c.id}>
                      <a onClick={() => onOpenOther(c.id)}>{c.label || c.summary}</a>
                      <span className="spacer" style={{ flex: 1 }} />
                      <span className="w">{(c.similarity || 0).toFixed(2)}</span>
                      <button className="btn ghost" onClick={() => linkTo(c.id)}>+ link</button>
                    </div>
                  ))}
                </div>
              )}
            </>
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

function ViewNode({ node, onOpenOther, onRemoveLink, flash }) {
  const [recheck, setRecheck] = useState(null)
  const [checking, setChecking] = useState(false)
  const hasFreshness = (node.refs && node.refs.length) || node.verified_by

  const doRecheck = async () => {
    setChecking(true)
    try {
      const r = await api.recheck(node.id)
      setRecheck(r)
      flash(r.stale ? 'stale — source changed' : 'still fresh ✓', r.stale)
    } catch (e) { flash(e.message, true) } finally { setChecking(false) }
  }

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

      <div
        className="md"
        onClick={(e) => {
          const ref = e.target.getAttribute && e.target.getAttribute('data-ref')
          if (ref) onOpenOther(ref)
        }}
        dangerouslySetInnerHTML={{ __html: renderContent(node.content || '', node.refs || []) }}
      />

      {hasFreshness && (
        <div className="freshness">
          <p className="metaline">
            <b>Freshness</b>
            {node.last_verified && (
              <span className="meta"> · last verified {new Date(node.last_verified * 1000).toLocaleString()}</span>
            )}
            <button className="btn ghost" style={{ marginLeft: 8 }}
              onClick={doRecheck} disabled={checking}>{checking ? '…' : 'Re-check'}</button>
          </p>
          {node.verified_by && <div className="metaline">verify cmd: <code>{node.verified_by}</code></div>}
          {(node.refs || []).map((r, i) => {
            const live = recheck?.refs?.find((x) => x.path === r.path)
            const href = fileHref(r)
            const liveAt = live?.mtime ? new Date(live.mtime * 1000).toLocaleString() : null
            return (
              <div className="refrow" key={i}>
                <span className={`dot ${r.exists === false ? 'off' : 'on'}`}
                  title={r.exists === false ? 'file missing' : 'file exists'} />
                {href
                  ? <a className={`fileref ${r.exists === false ? 'missing' : ''}`}
                       href={href} title={`open ${r.abspath}`}>{r.path}{r.lines ? `:${r.lines}` : ''}</a>
                  : <code>{r.path}{r.lines ? `:${r.lines}` : ''}</code>}
                {live?.status && <span className={`refstatus ${live.status}`}>{live.status}</span>}
                {liveAt && <span className="meta">modified {liveAt}</span>}
              </div>
            )
          })}
          {recheck && <div className={`metaline ${recheck.stale ? 'stale' : ''}`}>
            {recheck.stale ? '⚠ stale — re-read the source and update this memory'
                           : 'still fresh ✓'}</div>}
        </div>
      )}

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

function EditForm({ form, set, projects = [], isNew }) {
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
      {form.scope === 'project' && isNew && (
        <div className="field">
          <label>Project</label>
          <select value={form.project} onChange={set('project')}>
            <option value="">— choose a project —</option>
            {projects.map((p) => (
              <option key={p.key} value={p.key}>{p.key.split('/').pop()}</option>
            ))}
          </select>
        </div>
      )}
    </>
  )
}
