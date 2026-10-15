import { useCallback, useEffect, useState } from 'react'
import { api, when } from './api.js'

const TEMPLATES = [
  { id: 'catalog-a', title: 'Catalog A', text: '40 everyday products with stock and 90 days of sales: restock planning demo' },
  { id: 'catalog-b', title: 'Catalog B', text: 'Cart recovery demo: 40 products with costs, 300 shoppers with abandoned carts' },
  { id: 'blank', title: 'Blank', text: '3 basic products' },
]
const MAX = 3
const DOT = { Ready: 'ok', Failed: 'unreachable', Provisioning: 'pending', Deleting: 'pending' }

export default function Stores({ user }) {
  const [stores, setStores] = useState([])
  const [error, setError] = useState('')
  const [form, setForm] = useState({ name: '', template: 'catalog-a' })

  const load = useCallback(() => {
    api('/stores').then(setStores).catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 3000) // phases move as the operator works
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

  const mine = stores.filter((s) => s.owner_id === user.id).length
  const create = (e) => {
    e.preventDefault()
    act(async () => {
      await api('/stores', { method: 'POST', body: form })
      setForm({ ...form, name: '' })
    })
  }
  const remove = (s) => {
    if (!confirm(`Delete store ${s.name}? Its data, volumes and MCP tools are removed.`)) return
    act(() => api(`/stores/${s.name}`, { method: 'DELETE' }))
  }

  return (
    <>
      <div className="toolbar">
        <h2>Stores</h2>
        <span className="muted small">
          {mine}/{MAX} of your stores used
        </span>
      </div>
      <form className="card" onSubmit={create}>
        <div className="templates">
          {TEMPLATES.map((t) => (
            <label key={t.id} className={form.template === t.id ? 'template active' : 'template'}>
              <input
                type="radio"
                name="template"
                value={t.id}
                checked={form.template === t.id}
                onChange={() => setForm({ ...form, template: t.id })}
              />
              <b>{t.title}</b>
              <span className="muted small">{t.text}</span>
            </label>
          ))}
        </div>
        <div className="row">
          <input
            className="grow"
            aria-label="Store name"
            placeholder="store name, e.g. shop1 (tools become shop1__list_orders)"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value.toLowerCase() })}
            required
          />
          <button className="primary" disabled={mine >= MAX}>
            Create store
          </button>
        </div>
      </form>
      {error && <p className="error">{error}</p>}
      {stores.length === 0 && <p className="muted">No stores yet.</p>}
      {stores.map((s) => (
        <div key={s.name} className="card server row">
          <span className={`dot ${DOT[s.phase] ?? ''}`} />
          <div className="grow">
            <div className="title">
              {s.name} <span className="pill">{s.phase}</span> <span className="pill">{s.template}</span>
            </div>
            <div className="muted small">
              {s.owner} · created {when(s.created)}
            </div>
            {s.phase === 'Ready' && (
            <div className="small">
              Public URL:{' '}
              <a href={s.url} target="_blank" rel="noreferrer">
                {s.url}
              </a>
              {' · '}Admin URL:{' '}
              <a href={s.admin_url} target="_blank" rel="noreferrer">
                {s.admin_url}
              </a>
            </div>
            )}
            {s.phase === 'Ready' && s.admin_password && (
              <div className="small">
                User: <code>{s.admin_user}</code> · Password: <code>{s.admin_password}</code>
              </div>
            )}
            {s.message && <div className={s.phase === 'Failed' ? 'error small' : 'muted small'}>{s.message}</div>}
          </div>
          {s.owner_id === user.id && s.phase !== 'Deleting' && (
            <button className="ghost danger" onClick={() => remove(s)}>
              Delete
            </button>
          )}
        </div>
      ))}
    </>
  )
}
