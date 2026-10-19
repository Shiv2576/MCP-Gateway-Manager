import { useCallback, useEffect, useState } from 'react'
import { api, when } from './api.js'
import { ask } from './confirm.jsx'
import { PasswordInput } from './App.jsx'

// The member's own upstream credentials, for gateways set to personal identity.
// Values go in and never come back: the page only knows whether one is set.
export default function Credentials() {
  const [rows, setRows] = useState([])
  const [error, setError] = useState('')
  const [editing, setEditing] = useState(null) // server id
  const [value, setValue] = useState('')

  const load = useCallback(() => {
    api('/me/credentials').then(setRows).catch((e) => setError(e.message))
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

  const save = (e, r) => {
    e.preventDefault()
    act(async () => {
      await api(`/me/credentials/${r.server_id}`, { method: 'PUT', body: { value } })
      setValue('')
      setEditing(null)
    })
  }
  const remove = async (r) =>
    (await ask({ title: `Remove your credential for ${r.server}?`, description: 'Your calls to it on personal gateways stop working until you add one again.', action: 'Remove' })) &&
    act(() => api(`/me/credentials/${r.server_id}`, { method: 'DELETE' }))

  return (
    <>
      <div className="toolbar">
        <h2>My credentials</h2>
        <span className="muted small">
          On gateways set to personal credentials, your calls use your own login for the server, never a shared one.
        </span>
      </div>
      {error && <p className="error">{error}</p>}
      {rows.length === 0 && <p className="muted">No server needs your own credential.</p>}
      {rows.map((r) => (
        <div key={r.server_id} className="card">
          <div className="row">
            <span className={`dot ${r.set ? 'ok' : 'unreachable'}`} />
            <div className="grow">
              <div className="title">
                {r.server} <span className={r.set ? 'pill ok' : 'pill'}>{r.set ? 'Set' : 'Not set'}</span>
              </div>
              <div className="muted small">
                Sent as <span className="mono">{r.personal_header}</span>
                {r.gateways.length ? ` · needed on ${r.gateways.join(', ')}` : ' · no gateway needs it now'}
                {r.set && ` · updated ${when(r.updated_at)}`}
              </div>
            </div>
            <button className="ghost" onClick={() => { setEditing(editing === r.server_id ? null : r.server_id); setValue('') }}>
              {r.set ? 'Replace' : 'Add'}
            </button>
            {r.set && <button className="ghost danger" onClick={() => remove(r)}>Remove</button>}
          </div>
          {editing === r.server_id && (
            <form className="row" onSubmit={(e) => save(e, r)}>
              <PasswordInput className="grow" aria-label={`Credential for ${r.server}`} autoComplete="off" required
                placeholder={r.personal_header === 'X-Woo-Credentials' ? 'ck_…:cs_…' : 'your credential'}
                value={value} onChange={(e) => setValue(e.target.value)} />
              <button className="primary">Save</button>
            </form>
          )}
        </div>
      ))}
    </>
  )
}
