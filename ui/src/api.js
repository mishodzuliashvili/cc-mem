// Thin fetch wrapper around the cc-mem backend. In dev, Vite proxies /api to
// the Python server on :8765; in production the Python server serves this app
// and the API from the same origin — so a relative /api path works in both.

async function req(method, path, body) {
  const opts = { method, headers: {} }
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(`/api${path}`, opts)
  const text = await res.text()
  const data = text ? JSON.parse(text) : {}
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`)
  return data
}

export const api = {
  stats: () => req('GET', '/stats'),
  context: () => req('GET', '/context'),
  version: () => req('GET', '/version'),
  listNodes: (params = {}) => {
    const q = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== '' && v != null),
    ).toString()
    return req('GET', `/nodes${q ? `?${q}` : ''}`)
  },
  getNode: (id) => req('GET', `/nodes/${id}`),
  createNode: (node) => req('POST', '/nodes', node),
  updateNode: (id, fields) => req('PUT', `/nodes/${id}`, fields),
  deleteNode: (id) => req('DELETE', `/nodes/${id}`),
  graph: () => req('GET', '/graph'),
  recheck: (node_id) => req('POST', '/recheck', { node_id }),
  stale: () => req('GET', '/stale'),
  search: (query, mode = 'text', k = 20) => req('POST', '/search', { query, mode, k }),
  link: (src, dst, kind = 'related', weight = 1) =>
    req('POST', '/edges', { src, dst, kind, weight }),
  pending: () => req('GET', '/pending'),
  approve: (file, index, overrides) => req('POST', '/pending/approve', { file, index, overrides }),
  dismiss: (file, index) => req('POST', '/pending/dismiss', { file, index }),
  unlink: (a, b, kind) => {
    const q = new URLSearchParams({ a, b, ...(kind ? { kind } : {}) }).toString()
    return req('DELETE', `/edges?${q}`)
  },
}
