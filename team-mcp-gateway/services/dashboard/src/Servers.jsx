import { Fragment, useCallback, useEffect, useState } from 'react'
import { api, when } from './api.js'
import { ask } from './confirm.jsx'
import { Icon, PasswordInput } from './App.jsx'

const EMPTY = { name: '', url: '', authType: 'none', token: '', header: '', value: '', personal: '' }
const EMPTY_IMAGE = { image: '', args: '', port: '8000', path: '/mcp', env: '', tokenEnv: '', tokenHeader: 'Authorization' }
const HOSTED_DOT = { Ready: 'ok', Failed: 'unreachable', Provisioning: 'pending', Deleting: 'pending' }

// "KEY=value" per line -> {KEY: 'value'}; blank lines skipped.
function parseEnv(text) {
  const env = {}
  for (const line of text.split('\n')) {
    if (!line.trim()) continue
    const i = line.indexOf('=')
    if (i < 1) throw new Error(`env: "${line}" is not KEY=value`)
    env[line.slice(0, i).trim()] = line.slice(i + 1)
  }
  return env
}

function authBody(f) {
  if (f.authType === 'bearer') return { type: 'bearer', token: f.token }
  if (f.authType === 'header') return { type: 'header', name: f.header, value: f.value }
  return { type: 'none' }
}

// How the gateway authenticates to this upstream; the value itself never reaches the browser.
function authLabel(s) {
  if (!s.has_credential) return 'None'
  return s.auth_header === 'Authorization' ? 'Bearer token' : `Header · ${s.auth_header}`
}

export default function Servers({ user }) {
  const isAdmin = user.role === 'admin'
  const [servers, setServers] = useState([])
  const [hosted, setHosted] = useState([]) // container-image servers, by phase
  const [error, setError] = useState('')
  const [editing, setEditing] = useState(null) // null | 'new' | server
  const [open, setOpen] = useState({})
  const [oauth, setOauth] = useState(false)

  // OAuth sign-in exists only on gateways with a public URL; API keys work on every gateway.
  useEffect(() => {
    api('/gateways').then((gs) => setOauth(gs.some((g) => g.public_url))).catch(() => {})
  }, [])

  const load = useCallback(() => {
    api('/servers').then(setServers).catch((e) => setError(e.message))
    if (isAdmin) api('/hosted').then(setHosted).catch(() => setHosted([])) // 503 when the cluster has no operator
  }, [isAdmin])

  useEffect(() => {
    load()
    const t = setInterval(load, 3000) // status changes as the health loop runs
    return () => clearInterval(t)
  }, [load])

  async function act(fn) {
    setError('')
    try {
      await fn()
      load()
    } catch (e) {
      setError(e.message)
    }
  }

  const hostedBy = Object.fromEntries(hosted.map((h) => [h.name, h]))
  // Deploying or failed: not a registered server yet, so not in the table.
  const pending = hosted.filter((h) => !servers.some((s) => s.name === h.name))
  const removeHosted = async (name) =>
    (await ask({ title: `Remove ${name}?`, description: 'Its tools disappear for every client, then its pod and namespace are deleted.', action: 'Remove' })) &&
    act(() => api(`/hosted/${name}`, { method: 'DELETE' }))

  const up = servers.filter((s) => s.status === 'ok').length
  const tools = servers.reduce((n, s) => n + (s.enabled ? s.tools.length : 0), 0)

  return (
    <>
      <div className="stats">
        <Stat label="Servers" value={servers.length} />
        <Stat label="Healthy" value={up} tone="ok" />
        <Stat label="Unreachable" value={servers.filter((s) => s.status === 'unreachable').length} tone="bad" />
        <Stat label="Tools exposed" value={tools} />
      </div>

      <div className="toolbar">
        <h2>
          Upstream MCP servers <span className="pill ok">API key</span>
          {oauth && <> <span className="pill ok">OAuth</span></>}
        </h2>
        {isAdmin && (
          <button className="primary" onClick={() => setEditing('new')}>
            + Add server
          </button>
        )}
      </div>
      {error && <p className="error">{error}</p>}

      {editing && (
        <ServerForm
          server={editing === 'new' ? null : editing}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            load()
          }}
        />
      )}

      {pending.map((h) => (
        <div key={h.name} className="card server row">
          <span className={`dot ${HOSTED_DOT[h.phase] ?? ''}`} />
          <div className="grow">
            <div className="title">
              <LimitsHover hosted={h}>{h.name}</LimitsHover> <span className="pill">{h.phase}</span>{' '}
              <span className="pill">container</span>
            </div>
            <div className="muted mono small">{h.image}</div>
            {h.message && <div className={h.phase === 'Failed' ? 'error small' : 'muted small'}>{h.message}</div>}
          </div>
          {h.phase !== 'Deleting' && (
            <button className="ghost danger" onClick={() => removeHosted(h.name)}>
              Delete
            </button>
          )}
        </div>
      ))}

      <div className="table-wrap card flush">
        <table>
          <thead>
            <tr>
              <th>Server</th>
              <th title="How the gateway signs in to this server">Upstream auth</th>
              <th>Store</th>
              <th>Gateways</th>
              <th>Tools</th>
              <th>Created</th>
              <th>Enabled</th>
              {isAdmin && <th className="right">Actions</th>}
            </tr>
          </thead>
          <tbody>
            {servers.length === 0 && (
              <tr>
                <td colSpan={isAdmin ? 8 : 7} className="muted">
                  No servers yet.
                </td>
              </tr>
            )}
            {servers.map((s) => (
              <Fragment key={s.id}>
                <tr>
                  <td>
                    <div className="row">
                      <span className={`dot ${s.enabled ? s.status : 'disabled'}`} />
                      <div className="grow">
                        <div className="title">
                          <LimitsHover hosted={hostedBy[s.name]}>{s.name}</LimitsHover>{' '}
                          <span className="pill">{s.enabled ? s.status : 'disabled'}</span>
                          {hostedBy[s.name] && <> <span className="pill">container</span></>}
                        </div>
                        <div className="muted mono small">{hostedBy[s.name]?.image ?? s.url}</div>
                        {s.error && <div className="error small">{s.error}</div>}
                      </div>
                    </div>
                  </td>
                  <td>{authLabel(s)}</td>
                  <td>
                    {s.store ? (
                      // The dashboard is dash.<base domain>; stores sit beside it.
                      <a href={`${location.protocol}//${s.store}.${location.host.replace(/^dash\./, '')}`} target="_blank" rel="noreferrer">{s.store}</a>
                    ) : '—'}
                  </td>
                  <td>{s.gateways.length ? s.gateways.join(', ') : '—'}</td>
                  <td>
                    <button
                      className="ghost tiny"
                      aria-expanded={!!open[s.id]}
                      onClick={() => setOpen({ ...open, [s.id]: !open[s.id] })}
                    >
                      {s.tools.length} {open[s.id] ? '▴' : '▾'}
                    </button>
                  </td>
                  <td title={`checked ${when(s.checked_at)}`}>{when(s.created_at)}</td>
                  <td>
                    <button
                      className="switch"
                      role="switch"
                      aria-checked={s.enabled}
                      aria-label={`${s.enabled ? 'Disable' : 'Enable'} ${s.name}`}
                      disabled={!isAdmin}
                      onClick={() => act(() => api(`/servers/${s.id}`, { method: 'PATCH', body: { enabled: !s.enabled } }))}
                    />
                  </td>
                  {isAdmin && (
                    <td>
                      <div className="actions right">
                        <button className="icon-btn" aria-label={`Edit ${s.name}`} title="Edit" onClick={() => setEditing(s)}>
                          <Icon name="edit" />
                        </button>
                        <button
                          className="icon-btn danger"
                          aria-label={`Delete ${s.name}`}
                          title="Delete"
                          onClick={async () =>
                            hostedBy[s.name]
                              ? removeHosted(s.name)
                              : (await ask({ title: `Remove ${s.name}?`, description: 'Its tools disappear for every client.', action: 'Remove' })) &&
                                act(() => api(`/servers/${s.id}`, { method: 'DELETE' }))
                          }
                        >
                          <Icon name="trash" />
                        </button>
                      </div>
                    </td>
                  )}
                </tr>
                {open[s.id] && (
                  <tr>
                    <td colSpan={isAdmin ? 8 : 7}>
                      <ul className="tools">
                        {s.tools.map((t) => (
                          <li key={t.name}>
                            <span className="mono">
                              {s.name}__{t.name}
                            </span>
                            <span className="muted"> — {t.description}</span>
                          </li>
                        ))}
                      </ul>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

function Stat({ label, value, tone }) {
  return (
    <div className={`card stat ${tone || ''}`}>
      <div className="value">{value}</div>
      <div className="muted">{label}</div>
    </div>
  )
}

function ServerForm({ server, onCancel, onSaved }) {
  const [f, setF] = useState(server ? { ...EMPTY, name: server.name, url: server.url, authType: 'keep', personal: server.personal_header || '' } : { ...EMPTY, ...EMPTY_IMAGE })
  const [source, setSource] = useState('url') // 'url' | 'image'; only when adding
  const [error, setError] = useState('')
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })

  async function submit(e) {
    e.preventDefault()
    setError('')
    try {
      if (!server && source === 'image') {
        const body = {
          name: f.name,
          image: f.image.trim(),
          args: f.args.trim() ? f.args.trim().split(/\s+/) : [],
          port: Number(f.port),
          path: f.path.trim() || '/mcp',
          env: parseEnv(f.env),
        }
        if (f.tokenEnv.trim()) Object.assign(body, { token_env: f.tokenEnv.trim(), auth_header: f.tokenHeader.trim() })
        await api('/hosted', { method: 'POST', body })
      } else if (server) {
        const body = { url: f.url, personal_header: f.personal.trim() || null }
        if (f.authType !== 'keep') body.auth = authBody(f) // leaving auth out keeps the stored one
        await api(`/servers/${server.id}`, { method: 'PATCH', body })
      } else {
        await api('/servers', { method: 'POST', body: { name: f.name, url: f.url, auth: authBody(f), personal_header: f.personal.trim() || null } })
      }
      onSaved()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <form className="card form" onSubmit={submit}>
      <h3>{server ? `Edit ${server.name}` : 'Add an MCP server'}</h3>
      {!server && (
        <div className="templates">
          {[
            ['url', 'Remote URL', 'An MCP server already running somewhere'],
            ['image', 'Container image', 'Run an image speaking streamable HTTP in this cluster'],
          ].map(([id, title, text]) => (
            <label key={id} className={source === id ? 'template active' : 'template'}>
              <input type="radio" name="source" value={id} checked={source === id} onChange={() => setSource(id)} />
              <b>{title}</b>
              <span className="muted small">{text}</span>
            </label>
          ))}
        </div>
      )}
      {source === 'image' ? <ImageFields f={f} set={set} /> : (
      <>
      <div className="grid">
        <label>
          Name (tool prefix)
          <input value={f.name} onChange={set('name')} disabled={!!server} placeholder="woo" required />
        </label>
        <label>
          Streamable HTTP URL
          <input value={f.url} onChange={set('url')} placeholder="http://woo-mcp:8000/mcp" required />
        </label>
        <label>
          Credential
          <select value={f.authType} onChange={set('authType')}>
            {server && <option value="keep">Keep current</option>}
            <option value="none">None</option>
            <option value="bearer">Bearer token</option>
            <option value="header">Custom header</option>
          </select>
        </label>
        {f.authType === 'bearer' && (
          <label>
            Token
            <PasswordInput value={f.token} onChange={set('token')} required />
          </label>
        )}
        {f.authType === 'header' && (
          <>
            <label>
              Header name
              <input value={f.header} onChange={set('header')} placeholder="X-API-Key" required />
            </label>
            <label>
              Header value
              <PasswordInput value={f.value} onChange={set('value')} required />
            </label>
          </>
        )}
        <label>
          Personal credential header
          <input value={f.personal} onChange={set('personal')} placeholder="e.g. X-Woo-Credentials (empty: shared only)" />
        </label>
      </div>
      <p className="muted small">
        The credential is stored on the gateway and never sent back to the browser. On a gateway set to personal
        credentials, each member's own value goes in the personal header instead, and the shared credential is not sent.
      </p>
      </>
      )}
      {error && <p className="error">{error}</p>}
      <div className="row end">
        <button type="button" className="ghost" onClick={onCancel}>
          Cancel
        </button>
        <button className="primary">{server ? 'Save' : source === 'image' ? 'Deploy server' : 'Add server'}</button>
      </div>
    </form>
  )
}

function ImageFields({ f, set }) {
  return (
    <>
      <div className="grid">
        <label>
          Name (tool prefix)
          <input value={f.name} onChange={set('name')} placeholder="notes" required />
        </label>
        <label>
          Image
          <input value={f.image} onChange={set('image')} placeholder="ghcr.io/github/github-mcp-server" required />
        </label>
        <label>
          Args (optional)
          <input value={f.args} onChange={set('args')} placeholder="e.g. http" />
        </label>
        <label>
          Port
          <input type="number" min="1" max="65535" value={f.port} onChange={set('port')} required />
        </label>
        <label>
          MCP path
          <input value={f.path} onChange={set('path')} placeholder="/mcp" required />
        </label>
        <label>
          Token variable (optional)
          <input value={f.tokenEnv} onChange={set('tokenEnv')} placeholder="e.g. AUTH_TOKEN" />
        </label>
        {f.tokenEnv.trim() && (
          <label>
            Gateway sends the token in
            <input value={f.tokenHeader} onChange={set('tokenHeader')} placeholder="Authorization" required />
          </label>
        )}
      </div>
      <label className="field">
        Environment (KEY=value per line, not for secrets)
        <textarea rows={3} value={f.env} onChange={set('env')} placeholder={'DEMO_SERVER=notes'} />
      </label>
      <p className="muted small">
        Runs in its own namespace with a quota and network policies: only the gateway can reach it, and it can reach
        only the internet. With a token variable, a random token is generated, given to the container in that variable
        and sent by the gateway in the header (Authorization means Bearer). Once the pod is ready it appears below as a
        server; deleting it removes its tools first, then the pod and namespace.
      </p>
    </>
  )
}

// Hover or focus a hosted server's name: the quota and limits the operator applied.
// Fixed position, because the server table clips anything that overflows it.
function LimitsHover({ hosted, children }) {
  const [at, setAt] = useState(null)
  const lim = hosted?.limits
  if (!lim) return children
  const q = lim.quota
  const c = lim.container
  const show = (e) => {
    const r = e.currentTarget.getBoundingClientRect()
    setAt({ left: Math.min(r.left, window.innerWidth - 336), top: r.bottom + 6 })
  }
  return (
    <span
      className="limits-anchor"
      tabIndex={0}
      aria-describedby={at ? `limits-${hosted.name}` : undefined}
      onMouseEnter={show}
      onFocus={show}
      onMouseLeave={() => setAt(null)}
      onBlur={() => setAt(null)}
    >
      {children}
      {at && (
        <div id={`limits-${hosted.name}`} role="tooltip" className="limits-pop" style={at}>
          <div className="limits-head">Namespace mcp-{hosted.name}</div>
          <dl>
            <dt>Pods</dt><dd>at most {q.pods}</dd>
            <dt>CPU requested</dt><dd>up to {q['requests.cpu']}</dd>
            <dt>Memory requested</dt><dd>up to {q['requests.memory']}</dd>
            <dt>Memory limit</dt><dd>up to {q['limits.memory']}</dd>
            <dt>Disks · LB · NodePorts</dt><dd>none</dd>
          </dl>
          <div className="limits-head">Container</div>
          <dl>
            <dt>Reserved</dt><dd>{c.requests.cpu} CPU · {c.requests.memory}</dd>
            <dt>Memory cap</dt><dd>{c.limits.memory}</dd>
          </dl>
        </div>
      )}
    </span>
  )
}
