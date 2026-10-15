import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { Icon } from './App.jsx'
import { GatewayPicker, useGateway } from './Gateways.jsx'

const EXAMPLES = ['What orders came in today?', 'Which products are low on stock?', 'Sales summary for this week']

// Kept in memory only: a reload starts a new chat. Tool calls run as you and are audited.
export default function Chat({ user }) {
  const [turns, setTurns] = useState([]) // { role, content, calls?, pending? }
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [gateway, gateways, chooseGateway] = useGateway()
  const end = useRef(null)
  const box = useRef(null)

  // Braces matter: newer Chrome returns a Promise from scrollIntoView, and React would call it as the cleanup.
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [turns, busy])

  // Grows with the text up to the CSS max-height, like claude.ai's composer.
  useEffect(() => {
    const el = box.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [text])

  async function ask(next, extra) {
    setTurns(next)
    setError('')
    setBusy(true)
    try {
      const messages = next.map(({ role, content }) => ({ role, content }))
      const r = await api('/chat', { method: 'POST', body: { gateway: gateway.slug, messages, ...extra } })
      setTurns([...next, { role: 'assistant', content: r.reply, calls: r.calls, pending: r.pending }])
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
      box.current?.focus()
    }
  }

  function send(message = text) {
    if (!message.trim() || busy || !gateway) return
    setText('')
    ask([...settled(), { role: 'user', content: message.trim() }])
  }

  // Writes run only from this button: the server signed exactly this call.
  function decide(ok) {
    const { pending } = turns[turns.length - 1]
    const next = [...settled(), { role: 'user', content: ok ? 'Yes, run it.' : 'No, cancel that.' }]
    // Cancel spends the token on the server too, so it is dead at once.
    ask(next, ok ? { confirm: pending.token } : { cancel: pending.token })
  }

  // A new message drops any confirmation still waiting.
  function settled() {
    return turns.map(({ pending, ...t }) => t)
  }

  function onKey(e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className="chat-page">
      <div className="chat-scroll">
        <div className="chat-col">
          {turns.length === 0 && (
            <div className="chat-empty">
              <h2>How can I help, {user.username}?</h2>
              <p className="muted">I work with the tools your teams allow on this gateway. Every call runs as you and is audited.</p>
              <div className="chips">
                {EXAMPLES.map((q) => (
                  <button key={q} type="button" className="chip" onClick={() => send(q)} disabled={busy || !gateway}>
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}
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
              {t.content && <div className="bubble">{t.content}</div>}
              {t.pending && (
                <div className="confirm">
                  <p className="small">
                    Run <b className="mono">{t.pending.name}</b> as {user.username}?
                  </p>
                  <pre>{JSON.stringify(t.pending.arguments, null, 2)}</pre>
                  <div className="row end">
                    <button className="ghost" onClick={() => decide(false)} disabled={busy}>
                      Cancel
                    </button>
                    <button className="primary" onClick={() => decide(true)} disabled={busy}>
                      Confirm
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
          {busy && (
            <div className="msg assistant" aria-live="polite">
              <span className="typing" aria-label="Thinking"><i /><i /><i /></span>
            </div>
          )}
          {error && <p className="error">{error}</p>}
          <div ref={end} />
        </div>
      </div>
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault()
          send()
        }}
      >
        <textarea
          ref={box}
          rows={1}
          aria-label="Message"
          placeholder={gateway ? 'Message the agent…' : 'Pick a gateway first'}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          disabled={busy || !gateway}
        />
        <div className="composer-bar">
          <GatewayPicker current={gateway} gateways={gateways} onChange={chooseGateway} />
          <button className="primary send" aria-label="Send" title="Send" disabled={busy || !text.trim() || !gateway}>
            <Icon name="send" />
          </button>
        </div>
      </form>
      <p className="muted small chat-foot">Writes wait for your Confirm. Enter to send, Shift+Enter for a new line.</p>
    </div>
  )
}
