import { useEffect, useState } from 'react'
import { api } from './api.js'

// pydantic writes `x | None` as anyOf [x, null]: unwrap to x, and note it is nullable.
function unwrap(p) {
  const real = (p.anyOf || []).filter((v) => v.type !== 'null')
  return real.length === 1 ? { ...p, ...real[0], anyOf: undefined } : p
}

function fields(schema) {
  const required = schema?.required || []
  return Object.entries(schema?.properties || {}).map(([name, raw]) => {
    const p = unwrap(raw)
    const type = p.enum ? 'enum' : p.type || 'json'
    return { name, p, type, required: required.includes(name), title: raw.title || name }
  })
}

// Form state is strings (and booleans), the way inputs give them back.
function initial(schema) {
  const out = {}
  for (const f of fields(schema)) {
    const d = f.p.default
    out[f.name] = f.type === 'boolean' ? Boolean(d) : d === undefined || d === null ? '' : typeof d === 'object' ? JSON.stringify(d, null, 2) : String(d)
  }
  return out
}

// Throws with the field name when a JSON field does not parse.
function toArgs(schema, values) {
  const out = {}
  for (const f of fields(schema)) {
    const v = values[f.name]
    if (f.type === 'boolean') out[f.name] = v
    else if (v === '') continue // optional and left empty: let the tool use its default
    else if (f.type === 'integer' || f.type === 'number') out[f.name] = Number(v)
    else if (f.type === 'json' || f.type === 'object' || f.type === 'array') {
      try {
        out[f.name] = JSON.parse(v)
      } catch {
        throw new Error(`${f.title}: not valid JSON`)
      }
    } else out[f.name] = v
  }
  return out
}

function Field({ f, value, onChange }) {
  const { p } = f
  const id = `arg-${f.name}`
  const common = { id, required: f.required, value, onChange: (e) => onChange(e.target.value) }
  let control
  if (f.type === 'boolean')
    control = (
      <input id={id} type="checkbox" className="switch" checked={value} onChange={(e) => onChange(e.target.checked)} />
    )
  else if (f.type === 'enum')
    control = (
      <select {...common}>
        {!f.required && <option value="">Any</option>}
        {p.enum.map((o) => (
          <option key={o}>{o}</option>
        ))}
      </select>
    )
  else if (f.type === 'integer' || f.type === 'number')
    control = <input {...common} type="number" min={p.minimum} max={p.maximum} step={f.type === 'integer' ? 1 : 'any'} />
  else if (f.type === 'string' && (p.maxLength || 0) <= 200)
    control = <input {...common} minLength={p.minLength} maxLength={p.maxLength} pattern={p.pattern} title={p.description} />
  else
    control = (
      <textarea {...common} rows={4} className={f.type === 'string' ? '' : 'mono'} maxLength={p.maxLength} placeholder={f.type === 'string' ? '' : 'JSON'} />
    )
  const range = [p.minimum !== undefined && `min ${p.minimum}`, p.maximum !== undefined && `max ${p.maximum}`, p.maxLength && `≤ ${p.maxLength} chars`]
    .filter(Boolean)
    .join(' · ')
  return (
    <div className={f.type === 'boolean' ? 'field inline' : 'field'}>
      <label htmlFor={id}>
        <span className="mono">{f.name}</span>
        {f.required && <span className="req" aria-label="required">*</span>}
        <span className="pill">{f.type === 'enum' ? 'one of' : f.type}</span>
      </label>
      {control}
      {(p.description || range) && (
        <div className="muted small">
          {p.description}
          {p.description && range && ' · '}
          {range}
        </div>
      )}
    </div>
  )
}

export default function Tools() {
  const [tools, setTools] = useState([])
  const [filter, setFilter] = useState('')
  const [selected, setSelected] = useState(null)
  const [values, setValues] = useState({})
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    api('/tools').then(setTools).catch((e) => setError(e.message))
  }, [])

  function pick(t) {
    setSelected(t)
    setValues(initial(t.inputSchema))
    setResult(null)
    setError('')
  }

  async function run(e) {
    e.preventDefault()
    setError('')
    let parsed
    try {
      parsed = toArgs(selected.inputSchema, values)
    } catch (err) {
      setError(err.message)
      return
    }
    setBusy(true)
    const started = performance.now()
    try {
      const r = await api('/tools/call', { method: 'POST', body: { name: selected.name, arguments: parsed } })
      setResult({ ...r, ms: Math.round(performance.now() - started) })
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  // Required arguments first, so what must be filled in is at the top.
  const argFields = selected ? fields(selected.inputSchema).sort((a, b) => b.required - a.required) : []
  const q = filter.toLowerCase()
  const groups = {}
  for (const t of tools.filter((t) => t.name.toLowerCase().includes(q) || (t.description || '').toLowerCase().includes(q))) {
    ;(groups[t.name.split('__')[0]] ||= []).push(t)
  }
  const server = selected?.name.split('__')[0]
  const output = result
    ? result.structuredContent
      ? JSON.stringify(result.structuredContent, null, 2)
      : (result.content || []).map((c) => c.text ?? JSON.stringify(c)).join('\n')
    : ''
  const copy = (text) => navigator.clipboard?.writeText(text).catch(() => {})

  return (
    <>
      <div className="toolbar">
        <div>
          <h2>Tools</h2>
          <div className="muted small">
            {tools.length} tools from {new Set(tools.map((t) => t.name.split('__')[0])).size} servers · pick one on the left to try it
          </div>
        </div>
      </div>

      <details className="card connect">
        <summary>
          <b>How to connect an MCP client</b> <span className="muted small">(Claude Code, MCP Inspector)</span>
        </summary>
        <ol>
          <li>
            Make a key on the <b>API keys</b> page.
          </li>
          <li>
            Point the client at{' '}
            <code>{location.origin}/mcp</code>{' '}
            <button className="ghost tiny" onClick={() => copy(`${location.origin}/mcp`)}>
              Copy
            </button>{' '}
            (or <code>http://gateway.localhost/mcp</code>).
          </li>
          <li>
            Send the header <code>Authorization: Bearer &lt;API key&gt;</code>.
          </li>
        </ol>
        <p className="muted small">Opening /mcp in a browser without a key shows "a valid API key is required". That is expected.</p>
      </details>

      <div className="split">
        <div className="card side">
          <input type="search" placeholder="Search tools…" value={filter} onChange={(e) => setFilter(e.target.value)} />
          {Object.entries(groups).map(([server, list]) => (
            <div key={server}>
              <div className="group">
                {server} <span className="count">{list.length}</span>
              </div>
              {list.map((t) => (
                <button key={t.name} className={selected?.name === t.name ? 'item active' : 'item'} onClick={() => pick(t)}>
                  <span className="mono">{t.name.slice(server.length + 2)}</span>
                  {t.description && <span className="desc">{t.description}</span>}
                </button>
              ))}
            </div>
          ))}
          {tools.length === 0 && <p className="muted">No tools. Add a server and wait for it to turn healthy.</p>}
          {tools.length > 0 && Object.keys(groups).length === 0 && <p className="muted small">No tool matches that search.</p>}
        </div>
        <div className="card grow">
          {!selected ? (
            <div className="empty">
              <h3>Try a tool</h3>
              <p className="muted">Pick a tool from the list, fill in its arguments, and press Run. Calls run as you and appear in the audit log.</p>
            </div>
          ) : (
            <>
              <span className="pill">{server}</span>
              <h3 className="mono tool-title">{selected.name.slice(server.length + 2)}</h3>
              <p className="muted">{selected.description}</p>
              <form onSubmit={run}>
                <div className="section">Arguments</div>
                {argFields.length === 0 && <p className="muted small">This tool takes no arguments.</p>}
                {argFields.map((f) => (
                  <Field key={f.name} f={f} value={values[f.name]} onChange={(v) => setValues({ ...values, [f.name]: v })} />
                ))}
                <button className="primary" disabled={busy}>
                  {busy ? 'Running…' : 'Run tool'}
                </button>
              </form>
              {error && <p className="error">{error}</p>}
              {result && (
                <>
                  <div className="section">Result</div>
                  <div className={result.isError ? 'result bad' : 'result ok'}>
                    <div className="row small">
                      <b className="grow">{result.isError ? 'Error' : 'Success'} · {result.ms} ms</b>
                      <button className="ghost tiny" onClick={() => copy(output)}>
                        Copy
                      </button>
                    </div>
                    <pre>{output}</pre>
                  </div>
                </>
              )}
              <details className="schema">
                <summary className="muted small">Raw input schema</summary>
                <pre>{JSON.stringify(selected.inputSchema, null, 2)}</pre>
              </details>
            </>
          )}
        </div>
      </div>
    </>
  )
}
