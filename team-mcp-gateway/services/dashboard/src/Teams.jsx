import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'

const key = (serverId, tool) => `${serverId}:${tool}`
const REFUND_TOOL = 'bulk_refund_orders' // the one tool with a money budget too
const budgetText = (b) =>
  b && [b.max_items && `${b.max_items} items/h`, b.max_refund_total && `${b.max_refund_total} refunded/h`].filter(Boolean).join(', ')

// One team, read-only: members and, per gateway, the tools it allows.
function TeamView({ team }) {
  const gateways = Object.entries(team.policies)
  return (
    <>
      <div className="muted small">
        Members: {team.members.length ? team.members.map((m) => m.username).join(', ') : 'none'}
      </div>
      {gateways.length === 0 && <div className="muted small">No tools allowed on any gateway.</div>}
      {gateways.map(([slug, tools]) => {
        const budgets = Object.fromEntries((team.budgets?.[slug] || []).map((b) => [key(b.server_id, b.tool), b]))
        return (
          <div key={slug} className="small">
            <b>{slug}</b>:{' '}
            <span className="mono">
              {tools.map((t) => {
                const b = budgetText(budgets[key(t.server_id, t.tool)])
                return `${t.server}__${t.tool}${b ? ` (${b})` : ''}`
              }).join(', ')}
            </span>
          </div>
        )
      })}
    </>
  )
}

// Members and per-gateway tool picks for one team. Saves only what changed.
function TeamEditor({ team, users, gateways, servers, onSaved, onCancel }) {
  const [members, setMembers] = useState(team.members.map((m) => m.id))
  const saved = Object.fromEntries(
    gateways.map((g) => [g.slug, new Set((team.policies[g.slug] || []).map((t) => key(t.server_id, t.tool)))]),
  )
  const [picks, setPicks] = useState(() => Object.fromEntries(gateways.map((g) => [g.slug, new Set(saved[g.slug])])))
  // Write budgets, per gateway: tool key -> { max_items, max_refund_total } as typed. Empty = not budgeted.
  const savedBudgets = Object.fromEntries(
    gateways.map((g) => [
      g.slug,
      Object.fromEntries((team.budgets?.[g.slug] || []).map((b) => [
        key(b.server_id, b.tool),
        { max_items: b.max_items ? String(b.max_items) : '', max_refund_total: b.max_refund_total || '' },
      ])),
    ]),
  )
  const [budgets, setBudgets] = useState(savedBudgets)
  const setBudget = (slug, k, field, value) =>
    setBudgets({ ...budgets, [slug]: { ...budgets[slug], [k]: { ...budgets[slug][k], [field]: value } } })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const toggle = (slug, k) => {
    const next = new Set(picks[slug])
    next.has(k) ? next.delete(k) : next.add(k)
    setPicks({ ...picks, [slug]: next })
  }
  const same = (a, b) => a.size === b.size && [...a].every((x) => b.has(x))

  async function save() {
    setError('')
    setBusy(true)
    try {
      await api(`/teams/${team.id}/members`, { method: 'PUT', body: { user_ids: members } })
      for (const g of gateways) {
        if (same(picks[g.slug], saved[g.slug])) continue
        const tools = [...picks[g.slug]].map((k) => {
          const i = k.indexOf(':')
          return { server_id: Number(k.slice(0, i)), tool: k.slice(i + 1) }
        })
        await api(`/teams/${team.id}/gateways/${g.slug}/tools`, { method: 'PUT', body: { tools } })
      }
      for (const g of gateways) {
        // Only tools still allowed keep a budget.
        const rows = Object.entries(budgets[g.slug])
          .filter(([k, b]) => picks[g.slug].has(k) && (b?.max_items || b?.max_refund_total))
          .map(([k, b]) => {
            const i = k.indexOf(':')
            return {
              server_id: Number(k.slice(0, i)),
              tool: k.slice(i + 1),
              max_items: b.max_items ? Number(b.max_items) : null,
              max_refund_total: b.max_refund_total || null,
            }
          })
        const before = (team.budgets?.[g.slug] || []).length
        if (rows.length === 0 && before === 0) continue
        await api(`/teams/${team.id}/gateways/${g.slug}/budgets`, { method: 'PUT', body: { budgets: rows } })
      }
      onSaved()
    } catch (e) {
      setError(e.message)
      setBusy(false)
    }
  }

  return (
    <div className="attach">
      <div className="small">Members</div>
      <div className="picks">
        {users.map((u) => (
          <label key={u.id} className="pick">
            <input
              type="checkbox"
              checked={members.includes(u.id)}
              onChange={(e) => setMembers(e.target.checked ? [...members, u.id] : members.filter((x) => x !== u.id))}
            />
            <span className="mono small">{u.username}</span>
            {u.role === 'admin' && <span className="pill">admin</span>}
          </label>
        ))}
      </div>
      {gateways.length === 0 && <p className="muted small">No gateways yet: create one on the Gateways page.</p>}
      {gateways.map((g) => (
        <div key={g.slug} className="access-box">
          <div className="small">
            Tools on <b>{g.name}</b> <span className="muted mono">({g.slug})</span>
          </div>
          {g.servers.length === 0 && <div className="muted small">No servers attached to this gateway.</div>}
          {g.servers.map((s) => {
            const offered = (servers.find((x) => x.id === s.id)?.tools || []).map((t) => t.name)
            // Saved picks the server no longer lists stay visible so they can be removed.
            const stale = [...picks[g.slug]]
              .filter((k) => k.startsWith(`${s.id}:`))
              .map((k) => k.slice(k.indexOf(':') + 1))
              .filter((t) => !offered.includes(t))
            return (
              <div key={s.id}>
                <div className="mono small">{s.name}</div>
                <div className="picks">
                  {offered.length === 0 && stale.length === 0 && (
                    <span className="muted small">No tools listed yet (server down or still being checked).</span>
                  )}
                  {[...offered, ...stale].map((t) => {
                    const k = key(s.id, t)
                    const b = budgets[g.slug][k] || {}
                    return (
                      <span key={t} className="pick">
                        <label className="pick">
                          <input type="checkbox" checked={picks[g.slug].has(k)} onChange={() => toggle(g.slug, k)} />
                          <span className="mono small">{t}</span>
                          {stale.includes(t) && <span className="pill">no longer offered</span>}
                        </label>
                        {picks[g.slug].has(k) && (
                          <>
                            <input className="budget" type="number" min="1" step="1" placeholder="items / hour"
                              aria-label={`${t}: items per hour`} title="Write budget: items per member per rolling hour. Empty = no budget."
                              value={b.max_items || ''} onChange={(e) => setBudget(g.slug, k, 'max_items', e.target.value)} />
                            {t === REFUND_TOOL && (
                              <input className="budget" type="number" min="0.01" step="0.01" placeholder="money / hour"
                                aria-label={`${t}: money refunded per hour`} title="Refund money per member per rolling hour. Empty = no money budget."
                                value={b.max_refund_total || ''} onChange={(e) => setBudget(g.slug, k, 'max_refund_total', e.target.value)} />
                            )}
                          </>
                        )}
                      </span>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </div>
      ))}
      {error && <p className="error">{error}</p>}
      <div className="row end">
        <button className="ghost" onClick={onCancel}>Cancel</button>
        <button className="primary" disabled={busy} onClick={save}>{busy ? 'Saving…' : 'Save team'}</button>
      </div>
    </div>
  )
}

export default function Teams({ user }) {
  const isAdmin = user.role === 'admin'
  const [teams, setTeams] = useState([])
  const [users, setUsers] = useState([])
  const [gateways, setGateways] = useState([])
  const [servers, setServers] = useState([])
  const [name, setName] = useState('')
  const [editing, setEditing] = useState(null) // team id
  const [error, setError] = useState('')

  const load = useCallback(() => {
    api('/teams').then(setTeams).catch((e) => setError(e.message))
    if (isAdmin) {
      api('/users').then(setUsers).catch(() => {})
      api('/gateways').then(setGateways).catch(() => {})
      api('/servers').then(setServers).catch(() => {})
    }
  }, [isAdmin])
  useEffect(load, [load])

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
      await api('/teams', { method: 'POST', body: { name } })
      setName('')
    })
  }
  const remove = (t) =>
    confirm(`Delete team ${t.name}? Its members lose the tools it allows.`) &&
    act(() => api(`/teams/${t.id}`, { method: 'DELETE' }))

  return (
    <>
      <div className="toolbar">
        <div>
          <h2>Teams</h2>
          <div className="muted small">
            {isAdmin
              ? 'A member uses only the tools their teams allow on a gateway. Admins use every tool.'
              : 'Your teams and the tools they allow you on each gateway.'}
          </div>
        </div>
      </div>
      {isAdmin && (
        <form className="card row" onSubmit={create}>
          <input className="grow" aria-label="Team name" placeholder="team name, e.g. Support" required maxLength={40}
            value={name} onChange={(e) => setName(e.target.value)} />
          <button className="primary">Create team</button>
        </form>
      )}
      {error && <p className="error">{error}</p>}
      {teams.length === 0 && <p className="muted">{isAdmin ? 'No teams yet.' : 'You are in no team yet, so no tools are available to you.'}</p>}
      {teams.map((t) => (
        <div key={t.id} className="card">
          <div className="row">
            <div className="grow">
              <div className="title">{t.name}</div>
              {editing !== t.id && <TeamView team={t} />}
            </div>
            {isAdmin && editing !== t.id && (
              <>
                <button className="ghost" onClick={() => setEditing(t.id)}>Edit</button>
                <button className="ghost danger" onClick={() => remove(t)}>Delete</button>
              </>
            )}
          </div>
          {editing === t.id && (
            <TeamEditor
              team={t} users={users} gateways={gateways} servers={servers}
              onSaved={() => { setEditing(null); load() }}
              onCancel={() => setEditing(null)}
            />
          )}
        </div>
      ))}
    </>
  )
}
