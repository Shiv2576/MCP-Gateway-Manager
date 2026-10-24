import { useEffect, useState } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from './api.js'

const POLL_MS = 5000
const POINTS = 60 // 5 minutes at one sample per 5 s; kept only while this page is open

const time = (t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })

// One live line, in the same Recharts style as Reports.
function Graph({ title, now, data, dataKey, unit, color, max }) {
  return (
    <div>
      <div className="muted small">{title}</div>
      <div className="stat">{now ?? '—'}</div>
      <ResponsiveContainer width="100%" height={140}>
        <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--border)" />
          <XAxis dataKey="t" tickFormatter={time} minTickGap={40}
            tick={{ fill: 'var(--muted-foreground)', fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis width={44} domain={[0, max ?? 'auto']} allowDecimals={false}
            tick={{ fill: 'var(--muted-foreground)', fontSize: 11 }} tickLine={false} axisLine={false} />
          <Tooltip labelFormatter={time} formatter={(v) => [`${v}${unit}`, title]}
            contentStyle={{ background: 'var(--popover)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', fontSize: 12 }}
            labelStyle={{ color: 'var(--popover-foreground)' }} />
          <Line dataKey={dataKey} stroke={color} strokeWidth={2} dot={false} isAnimationActive={false} connectNulls={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

export default function Metrics() {
  const [history, setHistory] = useState({}) // store name -> recent samples
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    const poll = () => api('/stores/metrics').then((stores) => {
      const t = Date.now()
      setHistory((old) => Object.fromEntries(stores.map((s) => [s.name, [...(old[s.name] || []), {
        t,
        cpu: s.cpu_millicores,
        mem: s.memory_mib,
        busy: s.busy_workers,
        max: s.max_workers,
        pct: s.busy_workers != null && s.max_workers ? Math.round((100 * s.busy_workers) / s.max_workers) : null,
      }].slice(-POINTS)])))
      setError('')
      setLoaded(true)
    }).catch((e) => setError(e.message))
    poll()
    const id = setInterval(poll, POLL_MS)
    return () => clearInterval(id)
  }, [])

  const names = Object.keys(history)
  return (
    <>
      <h1>WordPress metrics</h1>
      <p className="muted small">
        Live, every {POLL_MS / 1000} s, last {(POINTS * POLL_MS) / 60000} minutes. Not stored: leaving this page clears it.
        Busy workers can hit the limit while CPU looks fine, when requests wait on the database or WooCommerce.
      </p>
      {error && <p className="error" role="alert">{error}</p>}
      {loaded && names.length === 0 && <p className="muted">No Ready stores.</p>}
      {names.map((name) => {
        const data = history[name]
        const last = data[data.length - 1]
        return (
          <div key={name} className="card">
            <h3>{name}</h3>
            <div className="metric-graphs">
              <Graph title="CPU" now={last.cpu != null ? `${last.cpu} m` : null}
                data={data} dataKey="cpu" unit=" m" color="var(--chart-2)" />
              <Graph title="Memory" now={last.mem != null ? `${last.mem} MiB` : null}
                data={data} dataKey="mem" unit=" MiB" color="var(--chart-3)" />
              <Graph title="Apache busy workers" now={last.pct != null ? `${last.busy} / ${last.max} (${last.pct}%)` : null}
                data={data} dataKey="pct" unit="%" color="var(--chart-4)" max={100} />
            </div>
          </div>
        )
      })}
    </>
  )
}
