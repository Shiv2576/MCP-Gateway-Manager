import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'

const DOT = { Ready: 'ok', Failed: 'unreachable', Missing: 'unreachable', Provisioning: 'pending', Deleting: 'pending' }
const EMPTY = { slug: '', name: '', public_url: '' }

export default function Gateways({ user }) {
  const isAdmin = user.role === 'admin'
  const [gateways, setGateways] = useState([])
  const [servers, setServers] = useState([])
  const [error, setError] = useState('')
  const [form, setForm] = useState(EMPTY)
  const [editing, setEditing] = useState(null) // slug whose servers are being picked
  const [picked, setPicked] = useState([])

  const load = useCallback(() => {
    api('/gateways').then(setGateways).catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    load()
    if (isAdmin) api('/servers').then(setServers).catch(() => {})
    const t = setInterval(load, 3000) // phases move as the operator works
    return () => clearInterval(t)
  }, [load, isAdmin])

  async function act(fn) {
    setError('')
    try {
      await fn()
      load()
    } catch (e) {
      setError(e.message)
    }
  }

  const create = (e) => {
    e.preventDefault()
    act(async () => {
      await api('/gateways', { method: 'POST', body: { ...form, public_url: form.public_url || null } })
      setForm(EMPTY)
    })
  }
  const remove = (g) =>
    confirm(`Delete gateway ${g.name}? Clients using ${g.mcp_url} stop working.`) &&
    act(() => api(`/gateways/${g.slug}`, { method: 'DELETE' }))
  const edit = (g) => {
    setEditing(g.slug)
    setPicked(g.servers.map((s) => s.id))
  }
  const toggle = (id) => setPicked(picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id])
  const save = () => {
    const g = gateways.find((x) => x.slug === editing)
    const dropped = g.servers.filter((s) => !picked.includes(s.id)).map((s) => s.name)
    if (dropped.length && !confirm(`Detach ${dropped.join(', ')} from ${g.name}? Team policies and member denies for ${dropped.length > 1 ? 'them' : 'it'} on this gateway are deleted, and re-attaching starts empty.`)) return
    act(async () => {
      await api(`/gateways/${editing}/servers`, { method: 'PUT', body: { server_ids: picked } })
      setEditing(null)
    })
  }
  // Whose credential calls through this attachment carry: the server's, or each member's own.
  const setIdentity = (g, s, identity) =>
    act(() => api(`/gateways/${g.slug}/servers/${s.id}/identity`, { method: 'PUT', body: { identity } }))
  const personalHeader = (id) => servers.find((x) => x.id === id)?.personal_header
  const copy = (text) => navigator.clipboard?.writeText(text).catch(() => {})

  return (
    <>
      <div className="toolbar">
        <h2>Gateways</h2>
        <span className="muted small">Each gateway is its own deployment with its own MCP URL.</span>
      </div>
      {isAdmin && (
        <form className="card row" onSubmit={create}>
          <input aria-label="Slug" placeholder="slug, e.g. prod" required value={form.slug}
            onChange={(e) => setForm({ ...form, slug: e.target.value.toLowerCase() })} />
          <input aria-label="Name" placeholder="name, e.g. Production" required value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input className="grow" aria-label="Public URL" placeholder="public https URL (optional; turns on OAuth)"
            value={form.public_url} onChange={(e) => setForm({ ...form, public_url: e.target.value })} />
          <button className="primary">Create gateway</button>
        </form>
      )}
      {error && <p className="error">{error}</p>}
      {gateways.length === 0 && <p className="muted">No gateways yet.</p>}
      {gateways.map((g) => (
        <div key={g.slug} className="card">
          <div className="row">
            <span className={`dot ${DOT[g.phase] ?? ''}`} />
            <div className="grow">
              <div className="title">
                {g.name} <span className="pill">{g.phase}</span>
                {g.public_url && <span className="pill ok">OAuth</span>}
              </div>
              <div className="muted mono small">
                {g.mcp_url}{' '}
                <button className="ghost tiny" onClick={() => copy(g.mcp_url)}>Copy</button>
              </div>
              {g.message && <div className={g.phase === 'Failed' ? 'error small' : 'muted small'}>{g.message}</div>}
              {g.servers.length === 0 && <div className="muted small">Servers: none attached</div>}
              {g.servers.map((s) => (
                <div key={s.id} className="row small">
                  <span className="mono">{s.name}</span>
                  {isAdmin ? (
                    <select aria-label={`Identity for ${s.name}`} value={s.identity}
                      onChange={(e) => setIdentity(g, s, e.target.value)}>
                      <option value="shared">Shared credential</option>
                      <option value="personal" disabled={!personalHeader(s.id)}>
                        Personal credential{personalHeader(s.id) ? '' : ' (set a personal header on the server first)'}
                      </option>
                    </select>
                  ) : (
                    <span className="pill">{s.identity === 'personal' ? 'Personal credential' : 'Shared credential'}</span>
                  )}
                </div>
              ))}
            </div>
            {isAdmin && (
              <>
                <button className="ghost" onClick={() => edit(g)}>Servers</button>
                <button className="ghost danger" onClick={() => remove(g)}>Delete</button>
              </>
            )}
          </div>
          {editing === g.slug && (
            <div className="attach">
              {servers.map((s) => (
                <label key={s.id} className="row">
                  <input type="checkbox" checked={picked.includes(s.id)} onChange={() => toggle(s.id)} />
                  {s.name} {s.store && <span className="pill">store</span>}
                </label>
              ))}
              {servers.length === 0 && <p className="muted small">No servers yet.</p>}
              <div className="row">
                <button className="primary" onClick={save}>Save</button>
                <button className="ghost" onClick={() => setEditing(null)}>Cancel</button>
              </div>
            </div>
          )}
        </div>
      ))}
    </>
  )
}

// The gateway the Tools and Chat pages act through: its team policy applies,
// exactly as for an MCP client on it. Remembered per browser.
export function useGateway() {
  const [gateways, setGateways] = useState([])
  const [slug, setSlug] = useState(() => {
    try { return localStorage.getItem('gateway') || '' } catch { return '' }
  })
  useEffect(() => {
    api('/gateways').then(setGateways).catch(() => {})
  }, [])
  const choose = (s) => {
    try { localStorage.setItem('gateway', s) } catch {}
    setSlug(s)
  }
  return [gateways.find((g) => g.slug === slug) ?? gateways[0], gateways, choose]
}

export function GatewayPicker({ current, gateways, onChange }) {
  if (!gateways.length) return <p className="muted small">No gateways yet: an admin creates one on the Gateways page.</p>
  return (
    <label className="row small">
      Gateway
      <select value={current?.slug} onChange={(e) => onChange(e.target.value)}>
        {gateways.map((g) => (
          <option key={g.slug} value={g.slug}>{g.name} ({g.slug})</option>
        ))}
      </select>
    </label>
  )
}
