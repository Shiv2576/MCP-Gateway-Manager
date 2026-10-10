import { useCallback, useEffect, useState } from 'react'
import { api, when } from './api.js'

const EMPTY = { name: '', url: '', authType: 'none', token: '', header: '', value: '' }

function authBody(f) {
  if (f.authType === 'bearer') return { type: 'bearer', token: f.token }
  if (f.authType === 'header') return { type: 'header', name: f.header, value: f.value }
  return { type: 'none' }
}

export default function Servers({ user }) {
  const isAdmin = user.role === 'admin'
  const [servers, setServers] = useState([])
  const [error, setError] = useState('')
  const [editing, setEditing] = useState(null) // null | 'new' | server
  const [open, setOpen] = useState({})

  const load = useCallback(() => {
    api('/servers').then(setServers).catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 3000) // status changes as the health loop runs
    return () => clearInterval(t)
  }, [load])

  async function act(fn) {
    setError('')
    try {
      await fn()
      load()
    } catch (e) {
      setError(e.message)
    }
  }

  const up = servers.filter((s) => s.status === 'ok').length
  const tools = servers.reduce((n, s) => n + (s.enabled ? s.tools.length : 0), 0)

  return (
    <>
      <div className="stats">
        <Stat label="Servers" value={servers.length} />
        <Stat label="Healthy" value={up} tone="ok" />
        <Stat label="Unreachable" value={servers.filter((s) => s.status === 'unreachable').length} tone="bad" />
        <Stat label="Tools exposed" value={tools} />
      </div>

      <div className="toolbar">
        <h2>Upstream MCP servers</h2>
        {isAdmin && (
          <button className="primary" onClick={() => setEditing('new')}>
            + Add server
          </button>
        )}
      </div>
      {error && <p className="error">{error}</p>}

      {editing && (
        <ServerForm
          server={editing === 'new' ? null : editing}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            load()
          }}
        />
      )}

      <div className="list">
        {servers.length === 0 && <p className="muted">No servers yet.</p>}
        {servers.map((s) => (
          <div key={s.id} className="card server">
            <div className="row">
              <span className={`dot ${s.enabled ? s.status : 'disabled'}`} />
              <div className="grow">
                <div className="title">
                  {s.name} <span className="pill">{s.enabled ? s.status : 'disabled'}</span>
                </div>
                <div className="muted mono">{s.url}</div>
                <div className="muted small">
                  Credential: {s.has_credential ? `set (${s.auth_header})` : 'none'} · checked {when(s.checked_at)}
                </div>
                {s.error && <div className="error small">{s.error}</div>}
              </div>
              <button className="ghost" onClick={() => setOpen({ ...open, [s.id]: !open[s.id] })}>
                {s.tools.length} tools {open[s.id] ? '▴' : '▾'}
              </button>
              {isAdmin && (
                <div className="actions">
                  <button
                    className="ghost"
                    onClick={() => act(() => api(`/servers/${s.id}`, { method: 'PATCH', body: { enabled: !s.enabled } }))}
                  >
                    {s.enabled ? 'Disable' : 'Enable'}
                  </button>
                  <button className="ghost" onClick={() => setEditing(s)}>
                    Edit
                  </button>
                  <button
                    className="ghost danger"
                    onClick={() =>
                      confirm(`Remove ${s.name}? Its tools disappear for every client.`) &&
                      act(() => api(`/servers/${s.id}`, { method: 'DELETE' }))
                    }
                  >
                    Remove
                  </button>
                </div>
              )}
            </div>
            {open[s.id] && (
              <ul className="tools">
                {s.tools.map((t) => (
                  <li key={t.name}>
                    <span className="mono">
                      {s.name}__{t.name}
                    </span>
                    <span className="muted"> — {t.description}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
    </>
  )
}

function Stat({ label, value, tone }) {
  return (
    <div className={`card stat ${tone || ''}`}>
      <div className="value">{value}</div>
      <div className="muted">{label}</div>
    </div>
  )
}

function ServerForm({ server, onCancel, onSaved }) {
  const [f, setF] = useState(server ? { ...EMPTY, name: server.name, url: server.url, authType: 'keep' } : EMPTY)
  const [error, setError] = useState('')
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })

  async function submit(e) {
    e.preventDefault()
    setError('')
    try {
      if (server) {
        const body = { url: f.url }
        if (f.authType !== 'keep') body.auth = authBody(f) // leaving auth out keeps the stored one
        await api(`/servers/${server.id}`, { method: 'PATCH', body })
      } else {
        await api('/servers', { method: 'POST', body: { name: f.name, url: f.url, auth: authBody(f) } })
      }
      onSaved()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <form className="card form" onSubmit={submit}>
      <h3>{server ? `Edit ${server.name}` : 'Add an MCP server'}</h3>
      <div className="grid">
        <label>
          Name (tool prefix)
          <input value={f.name} onChange={set('name')} disabled={!!server} placeholder="woo" required />
        </label>
        <label>
          Streamable HTTP URL
          <input value={f.url} onChange={set('url')} placeholder="http://woo-mcp:8000/mcp" required />
        </label>
        <label>
          Credential
          <select value={f.authType} onChange={set('authType')}>
            {server && <option value="keep">Keep current</option>}
            <option value="none">None</option>
            <option value="bearer">Bearer token</option>
            <option value="header">Custom header</option>
          </select>
        </label>
        {f.authType === 'bearer' && (
          <label>
            Token
            <input type="password" value={f.token} onChange={set('token')} required />
          </label>
        )}
        {f.authType === 'header' && (
          <>
            <label>
              Header name
              <input value={f.header} onChange={set('header')} placeholder="X-API-Key" required />
            </label>
            <label>
              Header value
              <input type="password" value={f.value} onChange={set('value')} required />
            </label>
          </>
        )}
      </div>
      <p className="muted small">The credential is stored on the gateway and never sent back to the browser.</p>
      {error && <p className="error">{error}</p>}
      <div className="row end">
        <button type="button" className="ghost" onClick={onCancel}>
          Cancel
        </button>
        <button className="primary">{server ? 'Save' : 'Add server'}</button>
      </div>
    </form>
  )
}
