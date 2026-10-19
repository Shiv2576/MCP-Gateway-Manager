import { useCallback, useEffect, useState } from 'react'
import { api, when } from './api.js'
import { ask } from './confirm.jsx'

const STATUS_PILL = { executed: 'pill ok', failed: 'pill bad', rejected: 'pill bad', expired: 'pill', running: 'pill' }

// The first 20 items of the request as a table: one column per field seen.
function Preview({ a }) {
  const cols = [...new Set(a.preview.flatMap((i) => Object.keys(i)))]
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {a.preview.map((item, n) => (
            <tr key={n}>
              {cols.map((c) => (
                <td key={c} className="mono small">{typeof item[c] === 'object' ? JSON.stringify(item[c]) : String(item[c] ?? '')}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {a.items > a.preview.length && <div className="muted small">…and {a.items - a.preview.length} more</div>}
    </div>
  )
}

function Summary({ a }) {
  return (
    <div className="muted small">
      <b>{a.username}</b>{a.teams.length > 0 && ` (${a.teams.join(', ')})`}
      {a.client && ` via ${a.client}`} on <span className="mono">{a.gateway}</span> · {when(a.requested_at)}
      <br />
      <span className="mono">{a.tool}</span> on <b>{a.items}</b> items
      {a.refund_total && <> refunding <b>{a.refund_total}</b></>} ·{' '}
      {a.budget === 'approval for every call' ? 'needs approval for every call' : `over the budget of ${a.budget}`}
    </div>
  )
}

// The requesting agent's own explanation: shown, but it is not what gets approved.
function AgentSummary({ a }) {
  if (!a.summary) return null
  return (
    <div className="small">
      <div className="muted">Written by the requester's agent, not verified:</div>
      <p style={{ whiteSpace: 'pre-wrap' }}>{a.summary}</p>
    </div>
  )
}

function PendingCard({ a, onDone }) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [done, setDone] = useState(null)

  async function decide(action) {
    if (action === 'approve' && !(await ask({ title: `Run ${a.tool} now?`, description: `It runs on ${a.items} items, as ${a.username}.`, action: 'Approve and run', destructive: false }))) return
    setError('')
    setBusy(action)
    try {
      setDone(await api(`/approvals/${a.id}/${action}`, { method: 'POST', body: { note } }))
    } catch (e) {
      setError(e.message)
    }
    setBusy('')
  }

  return (
    <div className="card">
      <div className="row">
        <div className="grow">
          <div className="title">Approval #{a.id}</div>
          <Summary a={a} />
        </div>
      </div>
      <Preview a={a} />
      <AgentSummary a={a} />
      {done ? (
        <div className="small">
          <span className={STATUS_PILL[done.status]}>{done.status}</span>{' '}
          {done.outcome && <pre className="mono small">{done.outcome}</pre>}
          <div className="row end"><button className="ghost" onClick={onDone}>Dismiss</button></div>
        </div>
      ) : (
        <>
          <input className="grow" aria-label="Note to the requester" placeholder="note (optional, sent to the agent)"
            maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} />
          {error && <p className="error">{error}</p>}
          <div className="row end">
            <button className="ghost danger" disabled={!!busy} onClick={() => decide('reject')}>
              {busy === 'reject' ? 'Rejecting…' : 'Reject'}
            </button>
            <button className="primary" disabled={!!busy} onClick={() => decide('approve')}>
              {busy === 'approve' ? 'Running…' : 'Approve and run'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}

function Approvals() {
  const [tab, setTab] = useState('pending')
  const [rows, setRows] = useState([])
  const [error, setError] = useState('')

  const load = useCallback(() => {
    api(`/approvals?status=${tab}`).then(setRows).catch((e) => setError(e.message))
  }, [tab])
  useEffect(load, [load])

  return (
    <>
      <div className="toolbar">
        <div>
          <h2>Approvals</h2>
          <div className="muted small">
            Jobs bigger than a member's hourly write budget, sent by their agent for approval. Approving runs the
            exact request as that member.
          </div>
        </div>
      </div>
      <div className="row">
        {['pending', 'history'].map((t) => (
          <button key={t} className={t === tab ? 'primary' : 'ghost'} aria-pressed={t === tab} onClick={() => setTab(t)}>
            {t === 'pending' ? 'Pending' : 'History'}
          </button>
        ))}
        <button className="ghost" onClick={load}>Refresh</button>
      </div>
      {error && <p className="error">{error}</p>}
      {rows.length === 0 && <p className="muted">{tab === 'pending' ? 'Nothing waiting for approval.' : 'No decided requests yet.'}</p>}
      {tab === 'pending' && rows.map((a) => <PendingCard key={a.id} a={a} onDone={load} />)}
      {tab === 'history' && rows.length > 0 && (
        <div className="table-wrap card flush">
          <table>
            <thead>
              <tr><th>#</th><th>Requested by</th><th>Tool</th><th>Items</th><th>Status</th><th>Decided by</th><th>Outcome</th></tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.id}>
                  <td>{a.id}</td>
                  <td>{a.username}<div className="muted small">{when(a.requested_at)}</div></td>
                  <td className="mono small">{a.tool}</td>
                  <td>{a.items}{a.refund_total && <div className="muted small">{a.refund_total}</div>}</td>
                  <td><span className={STATUS_PILL[a.status]}>{a.status}</span></td>
                  <td>{a.decided_by || '—'}<div className="muted small">{a.decided_at && when(a.decided_at)}</div>{a.note && <div className="small">“{a.note}”</div>}</td>
                  <td className="small">{a.outcome ? <span className="mono">{a.outcome.slice(0, 200)}</span> : <span className="muted">{a.audit_id ? `delivered to the agent; audit #${a.audit_id}` : '—'}</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

// Alerts for teams: what someone's tool call did that a team should act on.
function TeamAlerts() {
  const [rows, setRows] = useState([])
  const [error, setError] = useState('')

  // Every alert stays listed, done ones too; reloaded like the badge's 30 s poll.
  const load = useCallback(() => {
    api('/alerts?status=all').then(setRows).catch((e) => setError(e.message))
  }, [])
  useEffect(() => {
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [load])

  return (
    <>
      <div className="toolbar">
        <div>
          <h2>Team alerts</h2>
          <div className="muted small">
            Alerts for your teams. An alert is done when an agent calls gateway__mark_team_alert_done.
          </div>
        </div>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {rows.length === 0 && <p className="muted">No alerts yet.</p>}
      {rows.map((a) => (
        <div key={a.id} className="card">
          <div className="row">
            <b>{a.title}</b>
            <span className="pill">{a.team}</span>
            {a.done_at && <span className="pill ok">done</span>}
          </div>
          <div className="muted small">
            <span className="mono">{a.tool}</span> on <span className="mono">{a.gateway}</span> · {when(a.created_at)}
          </div>
          <ul className="small">{a.lines.map((l, i) => <li key={i} className="mono">{l}</li>)}</ul>
          <div className="muted small">
            requested by {a.requested_by || 'a removed user'}{a.approved_by && <> · approved by {a.approved_by}</>}
          </div>
          {a.done_at && <div className="small">Done by <b>{a.done_by || 'a removed user'}</b>, {when(a.done_at)}</div>}
        </div>
      ))}
    </>
  )
}

export default function Alerts({ user }) {
  return (
    <>
      {user.role === 'admin' ? <Approvals /> : <TeamAlerts />}
    </>
  )
}
