import { Fragment, useCallback, useEffect, useState } from 'react'
import { api, when } from './api.js'
import { PasswordInput } from './App.jsx'

function useLoad(path) {
  const [rows, setRows] = useState([])
  const [error, setError] = useState('')
  const load = useCallback(() => {
    api(path).then(setRows).catch((e) => setError(e.message))
  }, [path])
  useEffect(load, [load])
  return [rows, load, error, setError]
}

export function Keys({ user }) {
  const [keys, load, error, setError] = useLoad('/keys')
  const [apps, loadApps] = useLoad('/oauth/connections')
  const [name, setName] = useState('')
  const [fresh, setFresh] = useState(null)

  async function create(e) {
    e.preventDefault()
    try {
      setFresh(await api('/keys', { method: 'POST', body: { name } }))
      setName('')
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function revoke(id) {
    if (!confirm('Revoke this key? Clients using it stop working at once.')) return
    await api(`/keys/${id}`, { method: 'DELETE' }).catch((e) => setError(e.message))
    load()
  }

  async function disconnect(c) {
    if (!confirm(`Disconnect ${c.client_name || 'this app'}? It must sign in again to use the gateway.`)) return
    await api(`/oauth/connections/${c.user_id}/${encodeURIComponent(c.client_id)}`, { method: 'DELETE' }).catch((e) =>
      setError(e.message),
    )
    loadApps()
  }

  return (
    <>
      <h2>API keys</h2>
      <form className="card row" onSubmit={create}>
        <input className="grow" placeholder="Key name, e.g. claude-code" value={name} onChange={(e) => setName(e.target.value)} required />
        <button className="primary">Create key</button>
      </form>
      {fresh && (
        <div className="card result ok">
          <b>Copy this key now. It will not be shown again.</b>
          <div className="row">
            <code className="grow mono">{fresh.key}</code>
            <button className="ghost" onClick={() => navigator.clipboard.writeText(fresh.key)}>
              Copy
            </button>
          </div>
        </div>
      )}
      {error && <p className="error">{error}</p>}
      <table className="card">
        <thead>
          <tr>
            <th>Name</th>
            {user.role === 'admin' && <th>User</th>}
            <th>Key</th>
            <th>Created</th>
            <th>Status</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k.id}>
              <td>{k.name}</td>
              {user.role === 'admin' && <td>{k.username}</td>}
              <td className="mono">{k.prefix}…</td>
              <td>{when(k.created_at)}</td>
              <td>{k.revoked_at ? <span className="pill bad">revoked</span> : <span className="pill ok">active</span>}</td>
              <td>
                {!k.revoked_at && (
                  <button className="ghost danger" onClick={() => revoke(k.id)}>
                    Revoke
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>Connected apps</h2>
      <p className="muted small">Apps that signed in through OAuth, e.g. a Claude custom connector pointed at this gateway's /mcp.</p>
      <table className="card">
        <thead>
          <tr>
            <th>App</th>
            {user.role === 'admin' && <th>User</th>}
            <th>Last sign-in or refresh</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {apps.length === 0 && (
            <tr>
              <td colSpan={user.role === 'admin' ? 4 : 3} className="muted">
                No connected apps.
              </td>
            </tr>
          )}
          {apps.map((c) => (
            <tr key={`${c.user_id}/${c.client_id}`}>
              <td>{c.client_name || c.client_id}</td>
              {user.role === 'admin' && <td>{c.username}</td>}
              <td>{when(c.last_used)}</td>
              <td>
                <button className="ghost danger" onClick={() => disconnect(c)}>
                  Disconnect
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

export function Users({ user }) {
  const [users, load, error, setError] = useLoad('/users')
  const blank = { username: '', password: '', role: 'member' }
  const [f, setF] = useState(blank)
  const [adding, setAdding] = useState(false)
  const [search, setSearch] = useState('')
  const [denying, setDenying] = useState(null) // id of the member whose denies are open

  async function act(fn) {
    setError('')
    try {
      await fn()
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  const create = (e) => {
    e.preventDefault()
    act(async () => {
      await api('/users', { method: 'POST', body: f })
      setF(blank)
      setAdding(false)
    })
  }

  const remove = (u) => {
    const extra = u.role === 'admin' ? ' Their stores and everything in them will be deleted too.' : ''
    if (!confirm(`Delete ${u.username}? Their API keys stop working at once.${extra}`)) return
    act(() => api(`/users/${u.id}`, { method: 'DELETE' }))
  }

  const shown = users.filter((u) => u.username.toLowerCase().includes(search.toLowerCase()))

  return (
    <>
      <div className="toolbar">
        <div>
          <h2>Members</h2>
          <div className="muted small">
            {users.length} {users.length === 1 ? 'person' : 'people'} · admins use every tool, members what their teams allow (Teams page)
          </div>
        </div>
        {!adding && (
          <button className="primary" onClick={() => setAdding(true)}>
            + Add member
          </button>
        )}
      </div>
      {error && <p className="error">{error}</p>}

      {adding && (
        <form className="card form" onSubmit={create}>
          <h3>New member</h3>
          <div className="grid">
            <label>
              Username
              <input value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} required autoFocus />
            </label>
            <label>
              Password
              <PasswordInput value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} required />
            </label>
            <label>
              Role
              <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
                <option value="member">Member: uses the tools their teams allow</option>
                <option value="admin">Admin: everything, incl. managing members</option>
              </select>
            </label>
          </div>
          <div className="row end">
            <button type="button" className="ghost" onClick={() => { setAdding(false); setF(blank) }}>
              Cancel
            </button>
            <button className="primary">Add member</button>
          </div>
        </form>
      )}

      <div className="card flush">
        <div className="card-head">
          <input className="search" type="search" placeholder="Search members…" value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>
        <table>
          <thead>
            <tr>
              <th>Member</th>
              <th>Role</th>
              <th>Teams</th>
              <th title="Servers this member stored their own credential for (values are never shown)">Own credentials</th>
              <th className="right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((u) => (
              <Fragment key={u.id}>
                <tr>
                  <td>
                    <div className="row">
                      <span className="avatar">{u.username[0]}</span>
                      <b>{u.username}</b>
                      {u.id === user.id && <span className="pill">you</span>}
                    </div>
                  </td>
                  <td>
                    <span className={u.role === 'admin' ? 'pill strong' : 'pill'}>{u.role}</span>
                  </td>
                  <td>
                    {u.teams.length ? (
                      <div className="picks">
                        {u.teams.map((t) => (
                          <span key={t} className="pill">{t}</span>
                        ))}
                      </div>
                    ) : (
                      <span className="muted small">{u.role === 'admin' ? 'All tools' : 'No team: no tools'}</span>
                    )}
                  </td>
                  <td>
                    {u.credentials.length ? (
                      <div className="picks">
                        {u.credentials.map((c) => (
                          <span key={c.server} className="pill" title={`set ${when(c.updated_at)}`}>{c.server}</span>
                        ))}
                      </div>
                    ) : (
                      <span className="muted small">—</span>
                    )}
                  </td>
                  <td>
                    <div className="actions right">
                      {u.role !== 'admin' && (
                        <button className="ghost" onClick={() => setDenying(denying === u.id ? null : u.id)}>
                          Denies{Object.values(u.denies).flat().length ? ` (${Object.values(u.denies).flat().length})` : ''}
                        </button>
                      )}
                      {u.id !== user.id && (
                        <button className="ghost danger" onClick={() => remove(u)}>
                          Delete
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
                {denying === u.id && (
                  <tr>
                    <td colSpan={5}>
                      <Denies member={u} onSaved={() => { setDenying(null); load() }} onError={setError} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {shown.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  {users.length ? 'No member matches that search.' : 'No members yet.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  )
}

export function Audit() {
  const [rows, load, error] = useLoad('/audit?limit=200')
  useEffect(() => {
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [load])

  return (
    <>
      <div className="toolbar">
        <h2>Audit log</h2>
        <button className="ghost" onClick={load}>
          Refresh
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      <table className="card">
        <thead>
          <tr>
            <th>When</th>
            <th>User</th>
            <th>Tool</th>
            <th>Gateway</th>
            <th title="Whose credential the call carried upstream">Identity</th>
            <th>Result</th>
            <th>Time</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{when(r.at)}</td>
              <td>{r.username}</td>
              <td className="mono">{r.tool}</td>
              <td>{r.gateway || <span className="muted">—</span>}</td>
              <td>{r.identity || <span className="muted">—</span>}</td>
              <td>{r.ok ? <span className="pill ok">ok</span> : <span className="pill bad" title={r.error}>error</span>} <span className="muted small">{r.error}</span></td>
              <td>{r.duration_ms} ms</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}


// Tools to take away from one member on one gateway, out of what their teams allow there.
function Denies({ member, onSaved, onError }) {
  const [teams, setTeams] = useState([])
  const [slug, setSlug] = useState('')
  const [denied, setDenied] = useState([])
  useEffect(() => {
    api('/teams').then(setTeams).catch((e) => onError(e.message))
  }, [onError])

  const allowed = {} // slug -> [{server_id, server, tool}], deduplicated over the member's teams
  for (const t of teams.filter((t) => t.members.some((m) => m.id === member.id))) {
    for (const [g, rows] of Object.entries(t.policies)) {
      allowed[g] = allowed[g] || []
      for (const r of rows) {
        if (!allowed[g].some((a) => a.server_id === r.server_id && a.tool === r.tool)) allowed[g].push(r)
      }
    }
  }
  const slugs = Object.keys(allowed).sort()
  const current = slug || slugs[0] || ''
  const key = (r) => `${r.server_id}:${r.tool}`
  useEffect(() => {
    setDenied((member.denies[current] || []).map(key))
  }, [current, member])

  const toggle = (k) => setDenied(denied.includes(k) ? denied.filter((x) => x !== k) : [...denied, k])
  const save = () =>
    api(`/users/${member.id}/gateways/${current}/denies`, {
      method: 'PUT',
      body: { tools: (allowed[current] || []).filter((r) => denied.includes(key(r))).map(({ server_id, tool }) => ({ server_id, tool })) },
    }).then(onSaved, (e) => onError(e.message))

  if (!slugs.length) return <p className="muted small">{member.username}'s teams allow no tools yet, so there is nothing to deny.</p>
  return (
    <div className="attach">
      <label className="row small">
        Gateway
        <select value={current} onChange={(e) => setSlug(e.target.value)}>
          {slugs.map((g) => <option key={g} value={g}>{g}</option>)}
        </select>
      </label>
      <p className="muted small">Ticked tools are taken away from {member.username} on {current}, whatever their teams allow.</p>
      {(allowed[current] || []).map((r) => (
        <label key={key(r)} className="row">
          <input type="checkbox" checked={denied.includes(key(r))} onChange={() => toggle(key(r))} />
          <span className="mono">{r.server}__{r.tool}</span>
        </label>
      ))}
      <div className="row">
        <button className="primary" onClick={save}>Save denies</button>
      </div>
    </div>
  )
}
