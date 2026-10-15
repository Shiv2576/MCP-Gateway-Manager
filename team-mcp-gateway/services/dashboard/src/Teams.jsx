import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'

const key = (serverId, tool) => `${serverId}:${tool}`
const REFUND_TOOL = 'bulk_refund_orders' // the one tool with a money budget too
const isSet = (v) => v !== '' && v != null // 0 is a budget: approval for every call
const budgetText = (b) =>
  b && [isSet(b.max_items) && (Number(b.max_items) === 0 ? 'approval for every call' : `${b.max_items} items/h`),
    b.max_refund_total && `${b.max_refund_total} refunded/h`].filter(Boolean).join(', ')

// One team, read-only: members, then one table per gateway (allowed tools, budgets, alerts).
function TeamView({ team, readOnly }) {
  const slugs = [...new Set([...Object.keys(team.policies), ...Object.keys(team.alert_rules || {})])]
  return (
    <>
      <h4>Members ({team.members.length})</h4>
      {team.members.length === 0
        ? <p className="muted small">No members yet.</p>
        : <ul className="chip-list">{team.members.map((m) => <li key={m.id} className="pill">{m.username}</li>)}</ul>}
      <h4>Tool access</h4>
      {slugs.length === 0 && <p className="muted small">No tools allowed on any gateway.</p>}
      {slugs.map((slug) => {
        const allowed = new Set((team.policies[slug] || []).map((t) => key(t.server_id, t.tool)))
        const alerted = new Set((team.alert_rules?.[slug] || []).map((t) => key(t.server_id, t.tool)))
        const budgets = Object.fromEntries((team.budgets?.[slug] || []).map((b) => [key(b.server_id, b.tool), b]))
        const rows = new Map([...(team.policies[slug] || []), ...(team.alert_rules?.[slug] || [])]
          .map((t) => [key(t.server_id, t.tool), t]))
        return (
          <div key={slug} className="table-wrap team-table">
            <table>
              <caption>Gateway <span className="mono">{slug}</span>: {allowed.size} tool{allowed.size === 1 ? '' : 's'} allowed</caption>
              <thead>
                <tr><th scope="col">Tool</th><th scope="col">Server</th><th scope="col">Allowed</th><th scope="col">Hourly budget</th><th scope="col">Alert {team.name}</th></tr>
              </thead>
              <tbody>
                {[...rows].map(([k, t]) => (
                  <tr key={k}>
                    <th scope="row" className="mono">{t.tool}</th>
                    <td className="mono">{t.server}</td>
                    <td>{allowed.has(k) ? 'Yes' : 'No'}</td>
                    <td>{!allowed.has(k) ? '—' : readOnly.has(k) ? 'Read only: per-minute limit' : budgetText(budgets[k]) || 'Gateway default'}</td>
                    <td>{alerted.has(k) ? 'Yes, on success' : 'No'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
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
  // Alert rules, per gateway: any attached tool, allowed to this team or not (the team that acts
  // is often not the one that calls).
  const savedAlerts = Object.fromEntries(
    gateways.map((g) => [g.slug, new Set((team.alert_rules?.[g.slug] || []).map((t) => key(t.server_id, t.tool)))]),
  )
  const [alertPicks, setAlertPicks] = useState(() => Object.fromEntries(gateways.map((g) => [g.slug, new Set(savedAlerts[g.slug])])))
  const toggleAlert = (slug, k) => {
    const next = new Set(alertPicks[slug])
    next.has(k) ? next.delete(k) : next.add(k)
    setAlertPicks({ ...alertPicks, [slug]: next })
  }
  // Write budgets, per gateway: tool key -> { max_items, max_refund_total } as typed. Empty = the gateway default.
  const savedBudgets = Object.fromEntries(
    gateways.map((g) => [
      g.slug,
      Object.fromEntries((team.budgets?.[g.slug] || []).map((b) => [
        key(b.server_id, b.tool),
        { max_items: isSet(b.max_items) ? String(b.max_items) : '', max_refund_total: b.max_refund_total || '' },
      ])),
    ]),
  )
  const [budgets, setBudgets] = useState(savedBudgets)
  // Read-only tools (the server says so) get only the per-minute rate limit, never a budget.
  const readOnly = new Set(servers.flatMap((s) => (s.tools || []).filter((t) => t.read_only).map((t) => key(s.id, t.name))))
  const setBudget = (slug, k, field, value) =>
    setBudgets({ ...budgets, [slug]: { ...budgets[slug], [k]: { ...budgets[slug][k], [field]: value } } })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const headingRef = useRef(null)
  useEffect(() => headingRef.current?.focus(), []) // screen readers land on the editor, not back at the top

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
          .filter(([k, b]) => picks[g.slug].has(k) && !readOnly.has(k) && (isSet(b?.max_items) || b?.max_refund_total))
          .map(([k, b]) => {
            const i = k.indexOf(':')
            return {
              server_id: Number(k.slice(0, i)),
              tool: k.slice(i + 1),
              max_items: isSet(b.max_items) ? Number(b.max_items) : null,
              max_refund_total: b.max_refund_total || null,
            }
          })
        const before = (team.budgets?.[g.slug] || []).length
        if (rows.length === 0 && before === 0) continue
        await api(`/teams/${team.id}/gateways/${g.slug}/budgets`, { method: 'PUT', body: { budgets: rows } })
      }
      for (const g of gateways) {
        if (same(alertPicks[g.slug], savedAlerts[g.slug])) continue
        const rules = [...alertPicks[g.slug]].map((k) => {
          const i = k.indexOf(':')
          return { server_id: Number(k.slice(0, i)), tool: k.slice(i + 1) }
        })
        await api(`/teams/${team.id}/gateways/${g.slug}/alert-rules`, { method: 'PUT', body: { rules } })
      }
      onSaved()
    } catch (e) {
      setError(e.message)
      setBusy(false)
    }
  }

  const helpId = `budget-help-${team.id}`
  return (
    <div className="attach team-editor">
      <h4 ref={headingRef} tabIndex={-1}>Editing {team.name}</h4>
      <fieldset>
        <legend>Members ({members.length} of {users.length} selected)</legend>
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
      </fieldset>
      <p id={helpId} className="muted small">
        Items per hour: a write budget per member per rolling hour. 0 means every call needs approval;
        empty means the gateway default (20 unless DEFAULT_ITEMS_PER_HOUR is set). Read-only tools get only the
        per-minute rate limit. Alert: notify {team.name} when anyone's call of the tool succeeds, even if {team.name} may not call it.
      </p>
      {gateways.length === 0 && <p className="muted small">No gateways yet: create one on the Gateways page.</p>}
      {gateways.map((g) => (
        <fieldset key={g.slug}>
          <legend>Tools on gateway {g.name} <span className="muted mono">({g.slug})</span>: {picks[g.slug].size} allowed</legend>
          {g.servers.length === 0 && <p className="muted small">No servers attached to this gateway.</p>}
          {g.servers.map((s) => {
            const offered = (servers.find((x) => x.id === s.id)?.tools || []).map((t) => t.name)
            // Saved picks the server no longer lists stay visible so they can be removed.
            const stale = [...picks[g.slug]]
              .filter((k) => k.startsWith(`${s.id}:`))
              .map((k) => k.slice(k.indexOf(':') + 1))
              .filter((t) => !offered.includes(t))
            if (offered.length === 0 && stale.length === 0)
              return <p key={s.id} className="muted small"><span className="mono">{s.name}</span>: no tools listed yet (server down or still being checked).</p>
            return (
              <div key={s.id} className="table-wrap team-table">
                <table>
                  <caption>Server <span className="mono">{s.name}</span></caption>
                  <thead>
                    <tr><th scope="col">Tool</th><th scope="col">Allow</th><th scope="col">Items per hour</th><th scope="col">Money per hour</th><th scope="col">Alert {team.name}</th></tr>
                  </thead>
                  <tbody>
                    {[...offered, ...stale].map((t) => {
                      const k = key(s.id, t)
                      const b = budgets[g.slug][k] || {}
                      const on = picks[g.slug].has(k)
                      const ro = readOnly.has(k)
                      return (
                        <tr key={t} className={on ? 'on' : undefined}>
                          <th scope="row" className="mono">
                            {t}{ro && <span className="pill">read</span>}
                            {stale.includes(t) && <span className="pill bad">no longer offered</span>}
                          </th>
                          <td>
                            <input type="checkbox" className="box" checked={on} onChange={() => toggle(g.slug, k)}
                              aria-label={`Allow ${t} on ${g.name}`} />
                          </td>
                          <td>
                            {on && !ro
                              ? <input className="budget" type="number" min="0" step="1" placeholder="default"
                                  aria-label={`${t}: items per hour`} aria-describedby={helpId}
                                  value={b.max_items ?? ''} onChange={(e) => setBudget(g.slug, k, 'max_items', e.target.value)} />
                              : <span className="muted small">{ro ? 'per-minute limit only' : 'not allowed'}</span>}
                          </td>
                          <td>
                            {on && !ro && t === REFUND_TOOL
                              ? <input className="budget" type="number" min="0.01" step="0.01" placeholder="no limit"
                                  aria-label={`${t}: money refunded per hour`} aria-describedby={helpId}
                                  value={b.max_refund_total || ''} onChange={(e) => setBudget(g.slug, k, 'max_refund_total', e.target.value)} />
                              : <span className="muted small" aria-label="not applicable">—</span>}
                          </td>
                          <td>
                            {ro
                              ? <span className="muted small" aria-label="not available for read-only tools">—</span>
                              : <input type="checkbox" className="box" checked={alertPicks[g.slug].has(k)} onChange={() => toggleAlert(g.slug, k)}
                                  aria-label={`Alert ${team.name} when ${t} succeeds`} aria-describedby={helpId} />}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )
          })}
        </fieldset>
      ))}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="row end">
        <button type="button" className="ghost" onClick={onCancel}>Cancel</button>
        <button type="button" className="primary" disabled={busy} onClick={save}>{busy ? 'Saving…' : `Save ${team.name}`}</button>
      </div>
      <p className="sr-only" aria-live="polite">{busy ? `Saving ${team.name}` : ''}</p>
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
  const editButtons = useRef({})
  // Closing the editor puts focus back on that team's Edit button so keyboard users keep their place.
  const close = (id) => {
    setEditing(null)
    setTimeout(() => editButtons.current[id]?.focus())
  }
  const readOnly = new Set(servers.flatMap((s) => (s.tools || []).filter((t) => t.read_only).map((t) => key(s.id, t.name))))

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
      {error && <p className="error" role="alert">{error}</p>}
      {teams.length === 0 && <p className="muted">{isAdmin ? 'No teams yet.' : 'You are in no team yet, so no tools are available to you.'}</p>}
      {teams.length > 1 && (
        <nav aria-label="Jump to team" className="team-jump">
          <span className="muted small">Jump to:</span>
          <ul className="chip-list">
            {teams.map((t) => <li key={t.id}><a href={`#team-${t.id}`}>{t.name}</a></li>)}
          </ul>
        </nav>
      )}
      {teams.map((t) => {
        const allowed = Object.values(t.policies).reduce((n, tools) => n + tools.length, 0)
        return (
          <section key={t.id} id={`team-${t.id}`} className="card team" aria-labelledby={`team-name-${t.id}`}>
            <div className="row team-head">
              <div className="grow">
                <h3 id={`team-name-${t.id}`}>{t.name}</h3>
                <div className="muted small">
                  {t.members.length} member{t.members.length === 1 ? '' : 's'} · {allowed} tool{allowed === 1 ? '' : 's'} allowed
                  across {Object.keys(t.policies).length} gateway{Object.keys(t.policies).length === 1 ? '' : 's'}
                </div>
              </div>
              {isAdmin && editing !== t.id && (
                <>
                  <button className="ghost" ref={(el) => (editButtons.current[t.id] = el)} onClick={() => setEditing(t.id)}
                    aria-label={`Edit team ${t.name}`}>Edit</button>
                  <button className="ghost danger" onClick={() => remove(t)} aria-label={`Delete team ${t.name}`}>Delete</button>
                </>
              )}
            </div>
            {editing === t.id
              ? <TeamEditor
                  team={t} users={users} gateways={gateways} servers={servers}
                  onSaved={() => { close(t.id); load() }}
                  onCancel={() => close(t.id)}
                />
              : <TeamView team={t} readOnly={readOnly} />}
          </section>
        )
      })}
    </>
  )
}
