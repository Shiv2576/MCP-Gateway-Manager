import { useCallback, useEffect, useState } from 'react'
import { api, when } from './api.js'

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
      {a.refund_total && <> refunding <b>{a.refund_total}</b></>} · over the budget of {a.budget}
    </div>
  )
}

function PendingCard({ a, onDone }) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [done, setDone] = useState(null)

  async function decide(action) {
    if (action === 'approve' && !confirm(`Run ${a.tool} on ${a.items} items now, as ${a.username}?`)) return
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

export default function Alerts() {
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
          <h2>Alerts</h2>
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
