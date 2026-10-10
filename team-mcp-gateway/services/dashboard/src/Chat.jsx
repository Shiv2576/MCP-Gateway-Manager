import { useState } from 'react'
import { api } from './api.js'

// Kept in memory only: a reload starts a new chat. Tool calls run as you and are audited.
export default function Chat({ user }) {
  const [turns, setTurns] = useState([]) // { role, content, calls? }
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function send(e) {
    e.preventDefault()
    if (!text.trim() || busy) return
    const next = [...turns, { role: 'user', content: text.trim() }]
    setTurns(next)
    setText('')
    setError('')
    setBusy(true)
    try {
      const messages = next.map(({ role, content }) => ({ role, content }))
      const r = await api('/chat', { method: 'POST', body: { messages } })
      setTurns([...next, { role: 'assistant', content: r.reply, calls: r.calls }])
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="card hint">
        The agent calls gateway tools as <b>{user.username}</b>; every call is in the audit log under your name.
      </div>
      <div className="card chat">
        {turns.length === 0 && <p className="muted">Ask something, e.g. "what orders came in today?"</p>}
        {turns.map((t, i) => (
          <div key={i} className={`msg ${t.role}`}>
            {t.calls?.map((c, j) => (
              <details key={j} className={c.ok ? 'call ok' : 'call bad'}>
                <summary className="mono small">
                  {c.ok ? '✓' : '✗'} {c.name}
                </summary>
                <pre>{JSON.stringify(c.arguments, null, 2)}</pre>
                <pre>{c.result}</pre>
              </details>
            ))}
            <div className="bubble">{t.content}</div>
          </div>
        ))}
        {busy && <p className="muted small">Thinking…</p>}
        {error && <p className="error">{error}</p>}
      </div>
      <form className="row" onSubmit={send}>
        <input
          aria-label="Message"
          placeholder="Message the agent…"
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={busy}
        />
        <button className="primary" disabled={busy || !text.trim()}>
          Send
        </button>
      </form>
    </>
  )
}
