import { Fragment, useEffect, useState } from 'react'
import { api } from './api.js'
import Servers from './Servers.jsx'
import Tools from './Tools.jsx'
import Chat from './Chat.jsx'
import Stores from './Stores.jsx'
import { Audit, Keys, Users } from './Admin.jsx'

// Lucide icon paths, 24x24 viewBox.
const ICONS = {
  servers: 'M2 4h20v6H2zM2 14h20v6H2zM6 7h.01M6 17h.01',
  stores: 'M3 9l1.5-5h15L21 9M3 9h18v11H3zM9 20v-6h6v6',
  tools: 'M14.7 6.3a4 4 0 0 0 5 5L21 13l-8 8-3-3 8-8M9.3 17.7l-5 5M3 3l7 7',
  chat: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z',
  keys: 'M15.5 7.5l3 3L22 7l-3-3M21 2l-9.6 9.6M7.5 21a5.5 5.5 0 1 1 0-11 5.5 5.5 0 0 1 0 11z',
  users: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75',
  audit: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M16 13H8M16 17H8',
  sun: 'M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4',
  moon: 'M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z',
  collapse: 'M11 17l-5-5 5-5M18 17l-5-5 5-5',
  expand: 'M13 17l5-5-5-5M6 17l5-5-5-5',
  logout: 'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9',
  trash: 'M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6',
  edit: 'M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z',
}

export function Icon({ name }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={ICONS[name]} />
    </svg>
  )
}

const PAGES = [
  { id: 'servers', label: 'Servers', el: Servers, group: 'Workspace' },
  { id: 'stores', label: 'Stores', el: Stores, admin: true, group: 'Workspace' },
  { id: 'tools', label: 'Tools', el: Tools, group: 'Workspace' },
  { id: 'chat', label: 'Chat', el: Chat, group: 'Workspace' },
  { id: 'keys', label: 'API keys', el: Keys, group: 'Settings' },
  { id: 'users', label: 'Members', el: Users, admin: true, group: 'Settings' },
  { id: 'audit', label: 'Audit log', el: Audit, admin: true, group: 'Settings' },
]

function useTheme() {
  const [dark, setDark] = useState(() => {
    try {
      const saved = localStorage.getItem('theme')
      if (saved) return saved === 'dark'
    } catch {}
    return matchMedia('(prefers-color-scheme: dark)').matches
  })
  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    try { localStorage.setItem('theme', dark ? 'dark' : 'light') } catch {}
  }, [dark])
  return [dark, () => setDark(!dark)]
}

export default function App() {
  const [user, setUser] = useState(undefined) // undefined = still checking
  const [page, setPage] = useState('servers')
  const [dark, toggleTheme] = useTheme()
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem('sidebar') === 'collapsed' } catch { return false }
  })

  function toggleSidebar() {
    try { localStorage.setItem('sidebar', collapsed ? 'open' : 'collapsed') } catch {}
    setCollapsed(!collapsed)
  }

  useEffect(() => {
    api('/me').then(setUser).catch(() => setUser(null))
  }, [])

  if (user === undefined) return null
  if (!user) return <Login onLogin={setUser} />

  const pages = PAGES.filter((p) => !p.admin || user.role === 'admin')
  const Page = pages.find((p) => p.id === page)?.el ?? Servers

  async function logout() {
    await api('/logout', { method: 'POST' }).catch(() => {})
    setUser(null)
  }

  return (
    <div className={collapsed ? 'shell collapsed' : 'shell'}>
      <aside className="sidebar">
        <div className="brand">
          <span className="logo">◆</span>
          <span className="label grow">MCP Gateway</span>
          <button className="icon-btn" onClick={toggleSidebar} aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'} title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'} aria-expanded={!collapsed}>
            <Icon name={collapsed ? 'expand' : 'collapse'} />
          </button>
        </div>
        <nav aria-label="Main">
          {pages.map((p, i) => (
            <Fragment key={p.id}>
              {p.group !== pages[i - 1]?.group && <div className="nav-group">{p.group}</div>}
              <button
                className={p.id === page ? 'nav-item active' : 'nav-item'}
                aria-current={p.id === page ? 'page' : undefined}
                onClick={() => setPage(p.id)}
                title={collapsed ? p.label : undefined}
              >
                <Icon name={p.id} /> <span className="label">{p.label}</span>
              </button>
            </Fragment>
          ))}
        </nav>
        <div className="who">
          <span className="avatar" title={`${user.username} (${user.role})`}>{user.username[0]}</span>
          <div className="label grow">
            <div>{user.username}</div>
            <div className="muted small">{user.role}</div>
          </div>
          <button className="icon-btn theme-switch" onClick={toggleTheme} aria-label={dark ? 'Light mode' : 'Dark mode'} title={dark ? 'Light mode' : 'Dark mode'}>
            <span className={dark ? 'off' : 'on'}><Icon name="sun" /></span>
            <span className={dark ? 'on' : 'off'}><Icon name="moon" /></span>
          </button>
          <button className="icon-btn" onClick={logout} aria-label="Log out" title="Log out">
            <Icon name="logout" />
          </button>
        </div>
      </aside>
      <main>
        <Page user={user} />
      </main>
    </div>
  )
}

function Login({ onLogin }) {
  const [form, setForm] = useState({ username: '', password: '' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      onLogin(await api('/login', { method: 'POST', body: form }))
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <div className="login">
      <div className="login-side">
        <div className="brand">
          <span className="logo">◆</span> MCP Gateway
        </div>
        <form className="login-form" onSubmit={submit}>
          <div className="login-head">
            <h1>Welcome back</h1>
            <p className="muted">Sign in to manage your MCP servers, tools and keys.</p>
          </div>
          <label>
            Username
            <input
              autoFocus
              required
              autoComplete="username"
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
            />
          </label>
          <label>
            Password
            <input
              type="password"
              required
              autoComplete="current-password"
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
            />
          </label>
          {error && <p className="error" role="alert">{error}</p>}
          <button className="primary" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
        </form>
        <p className="muted small">Accounts are created by an admin.</p>
      </div>
      <div className="lava" aria-hidden="true">
        {/* Goo filter: blur the blobs together, then sharpen alpha so they merge like wax. */}
        <svg width="0" height="0">
          <filter id="goo">
            <feGaussianBlur in="SourceGraphic" stdDeviation="18" />
            <feColorMatrix values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 40 -18" />
          </filter>
        </svg>
        <div className="blobs">
          {[...Array(7)].map((_, i) => <span key={i} />)}
        </div>
      </div>
    </div>
  )
}
