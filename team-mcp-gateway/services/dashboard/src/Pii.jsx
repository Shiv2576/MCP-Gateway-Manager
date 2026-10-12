import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'

const PLACEHOLDERS = ['REDACTED', 'PERSON', 'ADDRESS', 'FREE_TEXT', 'EMAIL', 'PHONE']
const EMPTY = { name: '', type: 'pattern', match: '' }

// The global redaction schema, the fields each tool returns, and a test box.
export default function Pii() {
  const [rules, setRules] = useState({ builtin: [], custom: [] })
  const [servers, setServers] = useState([])
  const [form, setForm] = useState(EMPTY)
  const [error, setError] = useState('')
  const [open, setOpen] = useState({}) // "server/tool" -> expanded
  const [pick, setPick] = useState({}) // "server/tool/path" -> placeholder for a new tick
  const [test, setTest] = useState({ target: '', payload: '{\n  "customer": "Alice",\n  "note": "mail a@b.co"\n}' })
  const [tested, setTested] = useState(null)

  const load = useCallback(() => {
    api('/pii-rules').then(setRules).catch((e) => setError(e.message))
    api('/pii-rules/fields').then(setServers).catch((e) => setError(e.message))
  }, [])
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

  const add = (e) => {
    e.preventDefault()
    act(async () => {
      await api('/pii-rules', { method: 'POST', body: form })
      setForm(EMPTY)
    })
  }
  const remove = (id) => act(() => api(`/pii-rules/${id}`, { method: 'DELETE' }))
  const tick = (s, t, f, on) =>
    act(() =>
      on
        ? api('/pii-rules', { method: 'POST', body: { name: pick[`${s.server_id}/${t.tool}/${f.path}`] || 'REDACTED', type: 'field', match: f.path, server_id: s.server_id, tool: t.tool } })
        : api(`/pii-rules/${f.rule.id}`, { method: 'DELETE' }),
    )

  async function runTest() {
    setError('')
    setTested(null)
    let payload
    try {
      payload = JSON.parse(test.payload)
    } catch {
      return setError('Test payload is not valid JSON')
    }
    const [server_id, tool] = test.target ? test.target.split('/') : []
    try {
      setTested(await api('/pii-rules/test', { method: 'POST', body: { payload, server_id: server_id ? Number(server_id) : null, tool: tool || null } }))
    } catch (e) {
      setError(e.message)
    }
  }

  const global = [...rules.builtin.map((r) => ({ ...r, builtin: true })), ...rules.custom.filter((r) => r.type !== 'field')]

  return (
    <>
      <div className="toolbar">
        <h2>PII Protection</h2>
        <span className="muted small">Applies to every gateway with PII protection on. Changes apply on the next call.</span>
      </div>
      {error && <p className="error" role="alert">{error}</p>}

      <h3>Global redaction schema</h3>
      <table className="card">
        <thead>
          <tr><th>Name</th><th>Type</th><th>Match</th><th>Becomes</th><th /></tr>
        </thead>
        <tbody>
          {global.map((r, i) => (
            <tr key={r.id ?? `b${i}`}>
              <td>{r.name} {r.builtin && <span className="pill">built-in</span>}</td>
              <td>{r.type}</td>
              <td className="mono small">{r.match}{r.note && <span className="muted"> ({r.note})</span>}</td>
              <td className="mono">[{r.name}]</td>
              <td>{!r.builtin && <button className="ghost danger tiny" onClick={() => remove(r.id)}>Delete</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <form className="card row" onSubmit={add}>
        <input aria-label="Rule name" placeholder="NAME, e.g. AADHAAR" required value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value.toUpperCase() })} />
        <select aria-label="Rule type" value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
          <option value="pattern">pattern (regex on every value)</option>
          <option value="key">key (JSON key, any depth)</option>
        </select>
        <input className="grow mono" aria-label="Match" required value={form.match}
          placeholder={form.type === 'pattern' ? 'regex, e.g. \\b\\d{4}\\s?\\d{4}\\s?\\d{4}\\b' : 'key, e.g. loyalty_id'}
          onChange={(e) => setForm({ ...form, match: e.target.value })} />
        <button className="primary">Add rule</button>
      </form>

      <h3>Fields by server and tool</h3>
      {servers.length === 0 && <p className="muted">No enabled servers.</p>}
      {servers.map((s) => (
        <div key={s.server_id} className="card">
          <div className="title">{s.server}</div>
          {s.tools.length === 0 && <p className="muted small">No tools yet: the server has not answered a health check.</p>}
          {s.tools.map((t) => {
            const k = `${s.server_id}/${t.tool}`
            return (
              <div key={k}>
                <button className="ghost" aria-expanded={!!open[k]} onClick={() => setOpen({ ...open, [k]: !open[k] })}>
                  {open[k] ? '▾' : '▸'} <span className="mono">{t.tool}</span>{' '}
                  <span className="muted small">{t.fields.filter((f) => f.rule || f.covered_by).length}/{t.fields.length} masked</span>
                </button>
                {open[k] && (
                  <div className="attach">
                    {!t.has_schema && (
                      <p className="muted small">This tool doesn't describe its output. Global rules still apply; add a key rule above for any other field.</p>
                    )}
                    {t.fields.map((f) => {
                      const pk = `${k}/${f.path}`
                      return (
                        <label key={f.path} className="row small">
                          <input type="checkbox" checked={!!(f.rule || f.covered_by)} disabled={!!f.covered_by}
                            onChange={(e) => tick(s, t, f, e.target.checked)} />
                          <span className="mono">{f.path}</span>
                          <span className="muted">{f.type}</span>
                          {f.covered_by && <span className="pill">covered by {f.covered_by}</span>}
                          {f.rule && <span className="pill ok">[{f.rule.name}]</span>}
                          {!f.rule && !f.covered_by && (
                            <select aria-label={`Placeholder for ${f.path}`} value={pick[pk] || 'REDACTED'}
                              onChange={(e) => setPick({ ...pick, [pk]: e.target.value })}>
                              {PLACEHOLDERS.map((p) => <option key={p}>{p}</option>)}
                            </select>
                          )}
                          {f.description && <span className="muted">{f.description}</span>}
                        </label>
                      )
                    })}
                    {t.stale.map((r) => (
                      <div key={r.id} className="row small">
                        <s className="mono">{r.path}</s> <span className="muted">no longer in schema</span>
                        <button className="ghost danger tiny" onClick={() => remove(r.id)}>Remove</button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      ))}

      <h3>Test</h3>
      <div className="card">
        <div className="row">
          <select aria-label="Apply field rules of" value={test.target} onChange={(e) => setTest({ ...test, target: e.target.value })}>
            <option value="">Global rules only</option>
            {servers.flatMap((s) => s.tools.map((t) => (
              <option key={`${s.server_id}/${t.tool}`} value={`${s.server_id}/${t.tool}`}>{s.server} → {t.tool}</option>
            )))}
          </select>
          <button className="primary" onClick={runTest}>Run</button>
        </div>
        <textarea className="mono" rows={8} aria-label="Payload JSON" value={test.payload}
          onChange={(e) => setTest({ ...test, payload: e.target.value })} style={{ width: '100%' }} />
        {tested && (
          <>
            <div className="row small">
              {Object.entries(tested.counts).map(([n, c]) => <span key={n} className="pill">{n} {c}</span>)}
              {Object.keys(tested.counts).length === 0 && <span className="muted">Nothing matched.</span>}
            </div>
            <pre className="mono small">{JSON.stringify(tested.payload, null, 2)}</pre>
          </>
        )}
      </div>
    </>
  )
}
