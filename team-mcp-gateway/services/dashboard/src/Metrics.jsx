import { useEffect, useState } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from './api.js'

const RANGES = ['1h', '24h', '7d', '30d']
const COLORS = ['var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)', 'var(--chart-4)', 'var(--chart-5)']

// One row per timestamp, one key per series: the shape Recharts draws several lines from.
// The server aligns every query to the same steps, so the timestamps line up.
function rows(series) {
  const byT = new Map()
  for (const s of series) {
    for (const [t, v] of s.points) {
      if (!byT.has(t)) byT.set(t, { t })
      byT.get(t)[s.name] = v
    }
  }
  return [...byT.values()].sort((a, b) => a.t - b.t)
}

function Chart({ chart, short }) {
  const tick = (t) => new Date(t * 1000).toLocaleString([], short
    ? { hour: '2-digit', minute: '2-digit' }
    : { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
  return (
    <div className="card">
      <h3>{chart.title}</h3>
      {chart.series.length === 0 ? (
        <p className="muted small">No data yet.</p>
      ) : (
        <ResponsiveContainer width="100%" height={180}>
          <LineChart data={rows(chart.series)} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke="var(--border)" />
            <XAxis dataKey="t" tickFormatter={tick} minTickGap={40}
              tick={{ fill: 'var(--muted-foreground)', fontSize: 11 }} tickLine={false} axisLine={false} />
            <YAxis width={48} domain={[0, 'auto']}
              tick={{ fill: 'var(--muted-foreground)', fontSize: 11 }} tickLine={false} axisLine={false} />
            <Tooltip labelFormatter={tick} formatter={(v, name) => [`${v}${chart.unit}`, name]}
              contentStyle={{ background: 'var(--popover)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', fontSize: 12 }}
              labelStyle={{ color: 'var(--popover-foreground)' }} />
            {chart.series.map((s, i) => (
              <Line key={s.name} dataKey={s.name} name={s.name} stroke={COLORS[i % COLORS.length]} strokeWidth={2}
                dot={false} isAnimationActive={false} connectNulls={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}

// Gateway and store metrics with history, from Prometheus (via /api/reports/metrics).
export default function Metrics() {
  const [range, setRange] = useState('24h')
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let current = true // a slow answer for the previous range must not replace this one
    setError('')
    setData(null)
    api(`/reports/metrics?range=${range}`).then((d) => current && setData(d)).catch((e) => {
      if (current) setError(e.status === 503 ? "Metrics aren't set up: Prometheus isn't configured." : e.message)
    })
    return () => { current = false }
  }, [range])

  const groups = data ? [...new Set(data.charts.map((c) => c.group))] : []
  const short = range === '1h' || range === '24h'
  return (
    <>
      <h2>Gateway and store metrics</h2>
      <div className="row">
        {RANGES.map((r) => (
          <button key={r} className={r === range ? 'primary' : 'ghost'} aria-pressed={r === range} onClick={() => setRange(r)}>
            {r}
          </button>
        ))}
        <span className="muted small">History from Prometheus.</span>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {!data && !error && <p className="muted small">Loading…</p>}
      {groups.map((g) => (
        <section key={g}>
          <h3>{g}</h3>
          <div className="charts">
            {data.charts.filter((c) => c.group === g).map((c) => <Chart key={c.id} chart={c} short={short} />)}
          </div>
        </section>
      ))}
    </>
  )
}
