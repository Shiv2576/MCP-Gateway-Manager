import { Fragment, useCallback, useEffect, useState } from 'react'
import { api, when } from './api.js'

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
    </>
  )
}

function ServerPicks({ servers, value, onChange }) {
  if (!servers.length) return <span className="muted small">No servers yet</span>
  return (
    <div className="picks">
      {servers.map((s) => (
        <label key={s.id} className="pick">
          <input
            type="checkbox"
            checked={value.includes(s.id)}
            onChange={(e) => onChange(e.target.checked ? [...value, s.id] : value.filter((id) => id !== s.id))}
          />
          <span className="mono small">{s.name}</span>
        </label>
      ))}
    </div>
  )
}

export function Users({ user }) {
  const [users, load, error, setError] = useLoad('/users')
  const [servers] = useLoad('/servers')
  const blank = { username: '', password: '', role: 'member', server_ids: [] }
  const [f, setF] = useState(blank)
  const [adding, setAdding] = useState(false)
  const [search, setSearch] = useState('')
  const [editing, setEditing] = useState(null) // { id, server_ids }

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

  const saveAccess = () =>
    act(async () => {
      await api(`/users/${editing.id}/servers`, { method: 'PUT', body: { server_ids: editing.server_ids } })
      setEditing(null)
    })

  const remove = (u) => {
    const extra = u.role === 'admin' ? ' Their stores and everything in them will be deleted too.' : ''
    if (!confirm(`Delete ${u.username}? Their API keys stop working at once.${extra}`)) return
    act(() => api(`/users/${u.id}`, { method: 'DELETE' }))
  }

  const name = (id) => servers.find((s) => s.id === id)?.name ?? `#${id}`
  const shown = users.filter((u) => u.username.toLowerCase().includes(search.toLowerCase()))

  return (
    <>
      <div className="toolbar">
        <div>
          <h2>Members</h2>
          <div className="muted small">
            {users.length} {users.length === 1 ? 'person' : 'people'} · admins see every server, members only the ones you give them
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
              Password (10+ characters)
              <input type="password" minLength={10} value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} required />
            </label>
            <label>
              Role
              <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
                <option value="member">Member: uses the servers you pick</option>
                <option value="admin">Admin: everything, incl. managing members</option>
              </select>
            </label>
          </div>
          {f.role === 'member' && (
            <div className="access-box">
              <div className="small">Servers this member can use</div>
              <ServerPicks servers={servers} value={f.server_ids} onChange={(ids) => setF({ ...f, server_ids: ids })} />
            </div>
          )}
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
              <th>Server access</th>
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
                    {u.role === 'admin' ? (
                      <span className="muted small">All servers</span>
                    ) : u.server_ids.length ? (
                      <div className="picks">
                        {u.server_ids.map((id) => (
                          <span key={id} className="pill mono">{name(id)}</span>
                        ))}
                      </div>
                    ) : (
                      <span className="muted small">No servers yet</span>
                    )}
                  </td>
                  <td>
                    <div className="actions right">
                      {u.role === 'member' && (
                        <button className="ghost" onClick={() => setEditing(editing?.id === u.id ? null : { id: u.id, server_ids: u.server_ids })}>
                          Edit access
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
                {editing?.id === u.id && (
                  <tr className="editing">
                    <td colSpan={4}>
                      <div className="small">Servers {u.username} can use</div>
                      <ServerPicks servers={servers} value={editing.server_ids} onChange={(ids) => setEditing({ ...editing, server_ids: ids })} />
                      <div className="row end">
                        <button className="ghost" onClick={() => setEditing(null)}>
                          Cancel
                        </button>
                        <button className="primary" onClick={saveAccess}>
                          Save access
                        </button>
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {shown.length === 0 && (
              <tr>
                <td colSpan={4} className="muted">
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
              <td>{r.ok ? <span className="pill ok">ok</span> : <span className="pill bad" title={r.error}>error</span>} <span className="muted small">{r.error}</span></td>
              <td>{r.duration_ms} ms</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
