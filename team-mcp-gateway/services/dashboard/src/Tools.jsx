import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api.js'
import { GatewayPicker, useGateway } from './Gateways.jsx'

// ------------------------------------------------------------------ schema
// Tools describe their arguments as JSON Schema. These helpers turn that into
// form fields; a list of records (the bulk tools' `items`) becomes a table.

// A local "$ref": pydantic puts item models under $defs.
function resolve(p, root) {
  const ref = p?.$ref?.startsWith('#/$defs/') && root.$defs?.[p.$ref.slice(8)]
  return ref ? { ...ref, ...p, $ref: undefined } : p
}

// pydantic writes `x | None` as anyOf [x, null]: unwrap to x.
function unwrap(raw, root) {
  const real = (raw.anyOf || []).filter((v) => v.type !== 'null')
  return resolve(real.length === 1 ? { ...raw, ...real[0], anyOf: undefined } : raw, root)
}

const isRecord = (p) => p.type === 'object' && p.properties

function fields(schema) {
  const root = schema || {}
  const required = root.required || []
  return Object.entries(root.properties || {}).map(([name, raw]) => {
    const p = unwrap(raw, root)
    const type = p.enum ? 'enum' : p.type === 'array' && isRecord(unwrap(p.items || {}, root)) ? 'rows' : p.type || 'json'
    return { name, p, type, required: required.includes(name), title: raw.title || name }
  })
}

// A rows field's columns: the item's fields, with one level of nested object
// flattened to "shipping.city".
function columns(p, root) {
  const item = unwrap(p.items, root)
  const required = item.required || []
  return Object.entries(item.properties).flatMap(([name, raw]) => {
    const c = unwrap(raw, root)
    if (isRecord(c))
      return Object.entries(c.properties).map(([sub, r]) => ({ key: `${name}.${sub}`, p: unwrap(r, root), required: false }))
    return [{ key: name, p: c, required: required.includes(name) }]
  })
}

// Form state is strings (and booleans), the way inputs give them back; a rows field is a list of {column: string}.
function initial(schema) {
  const out = {}
  for (const f of fields(schema)) {
    const d = f.p.default
    out[f.name] =
      f.type === 'rows' ? [{}, {}, {}]
      : f.type === 'boolean' ? Boolean(d)
      : d === undefined || d === null ? ''
      : typeof d === 'object' ? JSON.stringify(d, null, 2)
      : String(d)
  }
  return out
}

// One typed value from a string, or an error that says where.
function convert(p, v, where) {
  if (p.type === 'integer' || p.type === 'number') {
    const n = Number(v)
    if (v.trim() === '' || !Number.isFinite(n) || (p.type === 'integer' && !Number.isInteger(n)))
      throw new Error(`${where}: must be a ${p.type === 'integer' ? 'whole number' : 'number'}`)
    return n
  }
  if (p.type === 'boolean') return v === true || v === 'true'
  if (p.pattern && !new RegExp(p.pattern).test(v)) throw new Error(`${where}: ${p.description || `must match ${p.pattern}`}`)
  return v
}

const filled = (row, cols) => cols.some((c) => (row[c.key] ?? '') !== '')

function rowsToItems(f, rows, root) {
  const cols = columns(f.p, root)
  const items = []
  rows.forEach((row, i) => {
    if (!filled(row, cols)) return // blank rows are skipped
    const item = {}
    for (const c of cols) {
      const v = row[c.key] ?? ''
      const where = `Row ${i + 1}, ${c.key}`
      if (v === '') {
        if (c.required) throw new Error(`${where}: required`)
        continue
      }
      const [a, b] = c.key.split('.')
      if (b) (item[a] ||= {})[b] = convert(c.p, v, where)
      else item[a] = convert(c.p, v, where)
    }
    items.push(item)
  })
  if (f.required && items.length === 0) throw new Error(`${f.title}: add at least one row`)
  return items
}

// The arguments to send. Throws with a readable message on bad input.
function toArgs(schema, values) {
  const out = {}
  for (const f of fields(schema)) {
    const v = values[f.name]
    if (f.type === 'rows') {
      const items = rowsToItems(f, v, schema)
      if (items.length) out[f.name] = items
    } else if (f.type === 'boolean') out[f.name] = v
    else if (v === '') continue // optional and left empty: let the tool use its default
    else if (f.type === 'json' || f.type === 'object' || f.type === 'array') {
      try {
        out[f.name] = JSON.parse(v)
      } catch {
        throw new Error(`${f.title}: not valid JSON`)
      }
    } else out[f.name] = convert(f.p, v, f.title)
  }
  return out
}

// ------------------------------------------------------------------ inputs

function Label({ f, htmlFor, kind }) {
  return (
    <label htmlFor={htmlFor}>
      <span className="mono">{f.name}</span>
      {f.required && <span className="req" aria-label="required">*</span>}
      <span className="pill">{kind}</span>
    </label>
  )
}

function Field({ f, value, onChange }) {
  const { p } = f
  const id = `arg-${f.name}`
  const common = { id, required: f.required, value, onChange: (e) => onChange(e.target.value) }
  let control
  if (f.type === 'boolean')
    control = <input id={id} type="checkbox" className="switch" checked={value} onChange={(e) => onChange(e.target.checked)} />
  else if (f.type === 'enum')
    control = (
      <select {...common}>
        {!f.required && <option value="">Any</option>}
        {p.enum.map((o) => <option key={o}>{o}</option>)}
      </select>
    )
  else if (f.type === 'integer' || f.type === 'number')
    control = <input {...common} type="number" min={p.minimum} max={p.maximum} step={f.type === 'integer' ? 1 : 'any'} />
  else if (f.type === 'string' && (p.maxLength || 0) <= 200)
    control = <input {...common} minLength={p.minLength} maxLength={p.maxLength} pattern={p.pattern} title={p.description} />
  else
    control = (
      <textarea {...common} rows={4} className={f.type === 'string' ? '' : 'mono'} maxLength={p.maxLength}
        placeholder={f.type === 'string' ? '' : 'JSON'} />
    )
  const range = [p.minimum !== undefined && `min ${p.minimum}`, p.maximum !== undefined && `max ${p.maximum}`,
    p.maxLength && `≤ ${p.maxLength} chars`].filter(Boolean).join(' · ')
  return (
    <div className={f.type === 'boolean' ? 'field inline' : 'field'}>
      <Label f={f} htmlFor={id} kind={f.type === 'enum' ? 'one of' : f.type} />
      {control}
      {(p.description || range) && <div className="muted small">{[p.description, range].filter(Boolean).join(' · ')}</div>}
    </div>
  )
}

function Cell({ c, value, onChange, onPaste, label }) {
  const { p } = c
  if (p.enum || p.type === 'boolean')
    return (
      <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">—</option>
        {(p.enum || ['true', 'false']).map((o) => <option key={o}>{o}</option>)}
      </select>
    )
  const numeric = p.type === 'integer' || p.type === 'number' || p.pattern?.startsWith('^\\d')
  return (
    <input aria-label={label} value={value} inputMode={numeric ? 'decimal' : undefined} title={p.description}
      placeholder={p.type === 'integer' ? 'id' : numeric ? '0.00' : ''}
      onChange={(e) => onChange(e.target.value)} onPaste={onPaste} />
  )
}

// A list of records as a small spreadsheet: one row per item.
function RowsField({ f, root, rows, onChange }) {
  const cols = useMemo(() => columns(f.p, root), [f, root])
  const max = f.p.maxItems
  const count = rows.filter((r) => filled(r, cols)).length
  const set = (i, key, v) => onChange(rows.map((r, j) => (j === i ? { ...r, [key]: v } : r)))

  // Cells copied from a spreadsheet fill the grid from this cell down and right,
  // adding rows as needed. A first line of column names maps the columns instead.
  function paste(e, i, ci) {
    const text = e.clipboardData.getData('text')
    if (!/[\t\n]/.test(text.trim())) return // one value: an ordinary paste
    e.preventDefault()
    let lines = text.replace(/\r/g, '').split('\n').filter((l) => l.trim()).map((l) => l.split('\t').map((v) => v.trim()))
    let order = cols.slice(ci).map((c) => c.key)
    if (lines[0].every((h) => cols.some((c) => c.key === h))) [order, lines] = [lines[0], lines.slice(1)]
    const next = [...rows]
    lines.forEach((cells, k) => {
      const row = { ...(next[i + k] || {}) }
      cells.forEach((v, n) => order[n] && (row[order[n]] = v))
      next[i + k] = row
    })
    onChange(next)
  }

  return (
    <div className="field">
      <Label f={f} kind="list" />
      {f.p.description && <div className="muted small">{f.p.description}</div>}
      <div className="table-wrap rows-grid">
        <table>
          <thead>
            <tr>
              <th className="num">#</th>
              {cols.map((c) => (
                <th key={c.key} title={c.p.description}>
                  <span className="mono">{c.key}</span>
                  {c.required && <span className="req" aria-label="required">*</span>}
                </th>
              ))}
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td className="num muted">{i + 1}</td>
                {cols.map((c, ci) => (
                  <td key={c.key}>
                    <Cell c={c} value={r[c.key] ?? ''} label={`Row ${i + 1}, ${c.key}`}
                      onChange={(v) => set(i, c.key, v)} onPaste={(e) => paste(e, i, ci)} />
                  </td>
                ))}
                <td>
                  <button type="button" className="ghost tiny" aria-label={`Remove row ${i + 1}`}
                    onClick={() => onChange(rows.length > 1 ? rows.filter((_, j) => j !== i) : [{}])}>✕</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row small">
        <button type="button" className="ghost tiny" disabled={max && rows.length >= max} onClick={() => onChange([...rows, {}])}>
          + Add row
        </button>
        <button type="button" className="ghost tiny" onClick={() => onChange([{}])}>Clear</button>
        <span className="muted grow">
          Paste cells from a spreadsheet into any cell. Starred columns are required; empty rows are skipped.
        </span>
        <span className={max && count > max ? 'error' : 'muted'}>
          {count} {max ? `of ${max}` : ''} item{count === 1 ? '' : 's'}
        </span>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ results

function textOf(r) {
  return (r?.content || []).map((c) => c.text ?? JSON.stringify(c)).join('\n')
}

// Bulk tools answer {ok, failed, results: [{id, ok, error}]}: show the count and the failures.
function BulkSummary({ data }) {
  const failures = data.results.filter((r) => !r.ok)
  return (
    <div className="small">
      <b>{data.ok} succeeded</b>
      {data.failed > 0 && <> · <b className="error-text">{data.failed} failed</b></>}
      {failures.length > 0 && (
        <table className="failures">
          <thead><tr><th>id</th><th>error</th></tr></thead>
          <tbody>{failures.map((r, n) => <tr key={n}><td className="mono">{r.id}</td><td>{r.error}</td></tr>)}</tbody>
        </table>
      )}
    </div>
  )
}

function Result({ result }) {
  const data = result.structuredContent
  const output = data ? JSON.stringify(data, null, 2) : textOf(result)
  const bulk = data && Array.isArray(data.results) && typeof data.ok === 'number'
  return (
    <div className={result.isError ? 'result bad' : 'result ok'}>
      <div className="row small">
        <b className="grow">{result.isError ? 'Error' : 'Success'}{result.ms !== undefined && ` · ${result.ms} ms`}</b>
        <button className="ghost tiny" onClick={() => navigator.clipboard?.writeText(output).catch(() => {})}>Copy</button>
      </div>
      {bulk && <BulkSummary data={data} />}
      {bulk ? (
        <details><summary className="muted small">Full result</summary><pre>{output}</pre></details>
      ) : (
        <pre>{output}</pre>
      )}
    </div>
  )
}

// ------------------------------------------------------------------ runner

const callTool = (gateway, name, args) => api('/tools/call', { method: 'POST', body: { gateway, name, arguments: args } })
// A budget refusal names the approval tool; see the gateway's approvals.py.
const overBudget = (r) => r?.isError && textOf(r).startsWith('Budget:') && textOf(r).includes('gateway__request_approval')

function ToolRunner({ tool, gateway, budget }) {
  const server = tool.name.split('__')[0]
  const schema = tool.inputSchema || {}
  const [values, setValues] = useState(() => initial(schema))
  const [result, setResult] = useState(null)
  const [sent, setSent] = useState(null) // the arguments of the last run, for an approval request
  const [approval, setApproval] = useState(null) // {id, state: 'waiting'|'done', text}
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const waiting = useRef(null)
  useEffect(() => () => { waiting.current = null }, []) // stop waiting when this tool is closed

  const argFields = fields(schema).sort((a, b) => b.required - a.required) // required first
  let preview
  try {
    preview = JSON.stringify(toArgs(schema, values), null, 2)
  } catch (e) {
    preview = `// ${e.message}`
  }

  async function run(e) {
    e.preventDefault()
    setError('')
    setApproval(null)
    let args
    try {
      args = toArgs(schema, values)
    } catch (err) {
      return setError(err.message)
    }
    setBusy(true)
    const started = performance.now()
    try {
      const r = await callTool(gateway, tool.name, args)
      setResult({ ...r, ms: Math.round(performance.now() - started) })
      setSent(args)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  // Each wait holds up to 25 s on the gateway; ask again until there is a decision.
  async function wait(id) {
    waiting.current = id
    setApproval({ id, state: 'waiting', text: `Approval #${id} is waiting for an admin on the Alerts page…` })
    while (waiting.current === id) {
      let r
      try {
        r = await callTool(gateway, 'gateway__wait_for_approval', { id })
      } catch (err) {
        setApproval({ id, state: 'done', text: `Stopped waiting: ${err.message}. Try again later.` })
        return
      }
      if (waiting.current !== id) return
      if (!r.isError && textOf(r).startsWith(`Approval #${id} still`)) continue
      setResult(r)
      setApproval({ id, state: 'done', text: r.isError ? `Approval #${id} did not run.` : `Approval #${id} was approved and has run.` })
      return
    }
  }

  async function requestApproval() {
    setError('')
    setBusy(true)
    try {
      const r = await callTool(gateway, 'gateway__request_approval', { tool: tool.name, arguments: sent })
      const id = Number(textOf(r).match(/Approval #(\d+)/)?.[1])
      // Requested now, or one is already open for this tool: wait on it either way.
      if (id && (!r.isError || /still open/.test(textOf(r)))) wait(id)
      else setResult(r)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <span className="pill">{server}</span>
      <h3 className="mono tool-title">{tool.name.slice(server.length + 2)}</h3>
      <p className="muted">{tool.description}</p>
      {budget && (
        <p className="hint small">
          Your write budget: <b>{budget}</b> per rolling hour. Bigger jobs need an admin's approval; you can ask for it
          here if a run is refused.
        </p>
      )}
      <form onSubmit={run}>
        <div className="section">Arguments</div>
        {argFields.length === 0 && <p className="muted small">This tool takes no arguments.</p>}
        {argFields.map((f) =>
          f.type === 'rows' ? (
            <RowsField key={f.name} f={f} root={schema} rows={values[f.name]} onChange={(v) => setValues({ ...values, [f.name]: v })} />
          ) : (
            <Field key={f.name} f={f} value={values[f.name]} onChange={(v) => setValues({ ...values, [f.name]: v })} />
          ),
        )}
        <div className="row">
          <button className="primary" disabled={busy || approval?.state === 'waiting'}>{busy ? 'Running…' : 'Run tool'}</button>
          <span className="muted small">Runs as you, through gateway <b>{gateway}</b>, and is audited.</span>
        </div>
      </form>
      {error && <p className="error">{error}</p>}

      {(result || approval) && <div className="section">Result</div>}
      {overBudget(result) && !approval && (
        <div className="hint">
          <p className="small">
            This run is over your hourly budget, so nothing was changed. Wait until budget frees up and run it again, or
            ask an admin to approve this exact job.
          </p>
          <button className="primary" disabled={busy} onClick={requestApproval}>
            {busy ? 'Requesting…' : 'Request approval'}
          </button>
        </div>
      )}
      {approval && (
        <div className="hint row small">
          <span className="grow">{approval.text}</span>
          {approval.state === 'waiting' && (
            <button className="ghost tiny" onClick={() => { waiting.current = null; setApproval({ ...approval, state: 'done', text: `Stopped waiting. Approval #${approval.id} stays open; an admin can still approve it.` }) }}>
              Stop waiting
            </button>
          )}
        </div>
      )}
      {result && <Result result={result} />}

      <details className="schema">
        <summary className="muted small">Arguments as JSON</summary>
        <pre>{preview}</pre>
      </details>
      <details className="schema">
        <summary className="muted small">Raw input schema</summary>
        <pre>{JSON.stringify(schema, null, 2)}</pre>
      </details>
    </>
  )
}

// ------------------------------------------------------------------ page

// "20 items, 1000.00 refunded" for this tool: the strictest of the member's teams' budgets.
function budgetFor(teams, slug, toolName) {
  const [server, tool] = [toolName.split('__')[0], toolName.slice(toolName.indexOf('__') + 2)]
  let items = null
  let money = null
  for (const t of teams) {
    const sid = (t.policies?.[slug] || []).find((p) => p.server === server)?.server_id
    for (const b of t.budgets?.[slug] || []) {
      if (b.server_id !== sid || b.tool !== tool) continue
      if (b.max_items && (items === null || b.max_items < items)) items = b.max_items
      if (b.max_refund_total && (money === null || Number(b.max_refund_total) < Number(money))) money = b.max_refund_total
    }
  }
  return [items && `${items} items`, money && `${money} refunded`].filter(Boolean).join(', ')
}

function ConnectHelp({ gateways }) {
  const copy = (text) => navigator.clipboard?.writeText(text).catch(() => {})
  return (
    <details className="card connect">
      <summary>
        <b>How to connect an MCP client</b> <span className="muted small">(Claude Code, MCP Inspector)</span>
      </summary>
      <ol>
        <li>Make a key on the <b>API keys</b> page.</li>
        <li>
          Point the client at a gateway's MCP URL
          {gateways.length === 0 ? ' (an admin creates gateways on the Gateways page).' : ':'}
          <ul>
            {gateways.map((g) => (
              <li key={g.slug}>
                <code>{g.mcp_url}</code>{' '}
                <button className="ghost tiny" onClick={() => copy(g.mcp_url)}>Copy</button>{' '}
                <span className="muted small">{g.name}</span>
              </li>
            ))}
          </ul>
        </li>
        <li>Send the header <code>Authorization: Bearer &lt;API key&gt;</code>.</li>
      </ol>
      <p className="muted small">Opening /mcp in a browser without a key shows "a valid API key is required". That is expected.</p>
    </details>
  )
}

function ToolList({ tools, selected, onPick }) {
  const [filter, setFilter] = useState('')
  const q = filter.toLowerCase()
  const groups = {}
  for (const t of tools.filter((t) => t.name.toLowerCase().includes(q) || (t.description || '').toLowerCase().includes(q)))
    (groups[t.name.split('__')[0]] ||= []).push(t)
  return (
    <div className="card side">
      <input type="search" placeholder="Search tools…" aria-label="Search tools" value={filter} onChange={(e) => setFilter(e.target.value)} />
      {Object.entries(groups).map(([server, list]) => (
        <div key={server}>
          <div className="group">{server} <span className="count">{list.length}</span></div>
          {list.map((t) => (
            <button key={t.name} className={selected?.name === t.name ? 'item active' : 'item'} onClick={() => onPick(t)}>
              <span className="mono">{t.name.slice(server.length + 2)}</span>
              {t.description && <span className="desc">{t.description.replace(/^\[[^\]]*\]\s*/, '')}</span>}
            </button>
          ))}
        </div>
      ))}
      {tools.length === 0 && <p className="muted">No tools. Add a server and wait for it to turn healthy.</p>}
      {tools.length > 0 && Object.keys(groups).length === 0 && <p className="muted small">No tool matches that search.</p>}
    </div>
  )
}

export default function Tools({ user }) {
  const [tools, setTools] = useState([])
  const [teams, setTeams] = useState([])
  const [selected, setSelected] = useState(null)
  const [error, setError] = useState('')
  const [gateway, gateways, chooseGateway] = useGateway()
  const slug = gateway?.slug

  useEffect(() => {
    setSelected(null)
    if (!slug) return setTools([])
    // The gateway's own approval tools are driven by the Request approval button, not listed.
    api(`/tools?gateway=${encodeURIComponent(slug)}`)
      .then((ts) => setTools(ts.filter((t) => !t.name.startsWith('gateway__'))))
      .catch((e) => setError(e.message))
  }, [slug])
  useEffect(() => {
    if (user?.role !== 'admin') api('/teams').then(setTeams).catch(() => {}) // admins have no budgets
  }, [user])

  return (
    <>
      <div className="toolbar">
        <div>
          <h2>Tools</h2>
          <div className="muted small">
            {tools.length} tools from {new Set(tools.map((t) => t.name.split('__')[0])).size} servers · what your teams allow on
            this gateway · pick one on the left to try it
          </div>
        </div>
        <GatewayPicker current={gateway} gateways={gateways} onChange={chooseGateway} />
      </div>
      <ConnectHelp gateways={gateways} />
      {error && <p className="error">{error}</p>}
      <div className="split">
        <ToolList tools={tools} selected={selected} onPick={setSelected} />
        <div className="card grow">
          {selected ? (
            // keyed: a new tool starts with a fresh form, result and approval
            <ToolRunner key={`${slug}:${selected.name}`} tool={selected} gateway={slug} budget={budgetFor(teams, slug, selected.name)} />
          ) : (
            <div className="empty">
              <h3>Try a tool</h3>
              <p className="muted">Pick a tool from the list, fill in its arguments, and press Run. Calls run as you and appear in the audit log.</p>
            </div>
          )}
        </div>
      </div>
    </>
  )
}
