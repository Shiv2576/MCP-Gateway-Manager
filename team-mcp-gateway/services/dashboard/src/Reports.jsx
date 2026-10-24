import { useEffect, useState } from 'react'
import { Bar, BarChart, CartesianGrid, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from './api.js'
import Metrics from './Metrics.jsx'

const STATS = [
  ['mcps', 'MCP servers'],
  ['gateways', 'Gateways'],
  ['apps', 'Apps & agents'],
  ['connections', 'Connections'],
]

// ~7px per character of a 12px label; very long names are cut, the tooltip shows them in full.
const CHAR_PX = 7
const MAX_LABEL = 32

// shadcn's horizontal bar chart, on plain Recharts with the theme's --chart tokens.
function Chart({ title, data, unit = '' }) {
  const chars = Math.min(MAX_LABEL, Math.max(0, ...data.map((d) => d.name.length)))
  return (
    <div className="card">
      <h3>{title}</h3>
      {data.length === 0 ? (
        <p className="muted small">No calls yet.</p>
      ) : (
        <ResponsiveContainer width="100%" height={Math.max(160, data.length * 40 + 16)}>
          <BarChart data={data} layout="vertical" margin={{ top: 8, right: 72, bottom: 8, left: 8 }}>
            <CartesianGrid horizontal={false} stroke="var(--border)" />
            <XAxis type="number" hide />
            <YAxis type="category" dataKey="name" width={chars * CHAR_PX + 16} tickLine={false} axisLine={false} interval={0}
              tickFormatter={(n) => (n.length > MAX_LABEL ? n.slice(0, MAX_LABEL - 1) + '…' : n)}
              tick={{ fill: 'var(--muted-foreground)', fontSize: 12 }} />
            <Tooltip cursor={{ fill: 'var(--muted)' }} formatter={(v) => [`${v}${unit}`, title]}
              contentStyle={{ background: 'var(--popover)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', fontSize: 12 }}
              labelStyle={{ color: 'var(--popover-foreground)' }} />
            <Bar dataKey="value" fill="var(--chart-2)" radius={4} maxBarSize={28}>
              <LabelList dataKey="value" position="right" formatter={(v) => `${v}${unit}`}
                style={{ fill: 'var(--foreground)', fontSize: 12 }} />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}

export default function Reports() {
  const [r, setR] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api('/reports').then(setR).catch((e) => setError(e.message))
  }, [])

  if (error) return <p className="error" role="alert">{error}</p>
  if (!r) return null
  return (
    <>
      <h1>Reports</h1>
      <div className="stats">
        {STATS.map(([k, label]) => (
          <div key={k} className="card">
            <div className="muted small">{label}</div>
            <div className="stat">{r.counts[k]}</div>
          </div>
        ))}
      </div>
      <p className="muted small">Tool calls in the last {r.days} days, top 10.</p>
      <div className="charts">
        <Chart title="Feature popularity by call count" data={r.by_tool} />
        <Chart title="Server popularity by feature call count" data={r.by_server} />
        <Chart title="Feature calls per user" data={r.by_user} />
        <Chart title="P99 duration of feature calls" data={r.p99_by_tool} unit=" ms" />
      </div>
      <Metrics />
    </>
  )
}
