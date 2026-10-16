import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { flagLabel, formatNumber, referenceText, resultValue } from '../../lib/labs.js'
import { formatDate } from '../../lib/bitacora.js'

/**
 * One analyte's values over time, in ONE unit (the page splits mixed units
 * into separate charts). Hand-rolled SVG: no chart library in the bundle.
 *
 * - Line 2px in brand-600; markers r=5 with a 2px white ring.
 * - Reference band: a light green wash per point's own range (ranges change
 *   between labs, so the band steps at the midpoint between dates). A missing
 *   bound runs the band to the plot edge; no bound at all, no band there.
 * - Out-of-range points differ by SHAPE (▲ alto, ▼ bajo, ◆ anormal) as well as
 *   color, and the tooltip and the table say it in words.
 * - `<5` / `>100` values (comparator) are drawn hollow at their bound.
 * - Values without a number are not plotted (the table shows them).
 * - Crosshair + tooltip on hover; ←/→ move between points from the keyboard.
 *
 * Palette (checked with the dataviz validator, light surface): line #0d9488,
 * alto #b91c1c, bajo #1d4ed8, anormal #b45309. The app has no dark mode.
 */

const COLORS = {
  line: '#0d9488',
  alto: '#b91c1c',
  bajo: '#1d4ed8',
  anormal: '#b45309',
  band: '#10b981',
  bandEdge: '#059669',
  grid: '#e2e8f0',
  axis: '#64748b',
  text: '#334155',
  surface: '#ffffff',
}
const R = 5
const MONTHS = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
const DAY = 86_400_000

function toTime(iso) {
  const [y, m, d] = iso.split('-').map(Number)
  return Date.UTC(y, m - 1, d)
}

function niceStep(span, count) {
  const raw = span / Math.max(1, count)
  const power = 10 ** Math.floor(Math.log10(raw))
  const unit = raw / power
  const nice = unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 2.5 ? 2.5 : unit <= 5 ? 5 : 10
  return nice * power
}

/** A clean y domain and ticks covering the values and the reference bounds. */
function yScale(values) {
  let min = Math.min(...values)
  let max = Math.max(...values)
  if (min === max) {
    const pad = Math.abs(min) * 0.2 || 1
    min -= pad
    max += pad
  }
  const pad = (max - min) * 0.08
  const allPositive = values.every((v) => v >= 0)
  min = allPositive ? Math.max(0, min - pad) : min - pad
  max += pad
  const step = niceStep(max - min, 4)
  const lo = Math.floor(min / step) * step
  const hi = Math.ceil(max / step) * step
  const ticks = []
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Number(v.toPrecision(12)))
  return { lo, hi, ticks }
}

function xTicks(t0, t1, count) {
  if (t0 === t1) return [t0]
  const n = Math.max(2, count)
  return Array.from({ length: n }, (_, i) => t0 + ((t1 - t0) * i) / (n - 1))
}

function xLabel(t, span) {
  const d = new Date(t)
  const month = MONTHS[d.getUTCMonth()]
  return span < 120 * DAY ? `${d.getUTCDate()} ${month}` : `${month} ${d.getUTCFullYear()}`
}

/** Marker for one point: shape by flag, hollow when the value is a bound. */
export function Marker({ flag, hollow, x, y, r = R, ring = true }) {
  const color = COLORS[flag] ?? COLORS.line
  const fill = hollow ? COLORS.surface : color
  const common = {
    fill,
    stroke: hollow ? color : ring ? COLORS.surface : 'none',
    strokeWidth: 2,
    paintOrder: 'stroke',
  }
  const s = r + 1.5
  if (flag === 'alto') {
    return <path d={`M${x},${y - s} L${x + s},${y + s * 0.8} L${x - s},${y + s * 0.8}Z`} {...common} />
  }
  if (flag === 'bajo') {
    return <path d={`M${x},${y + s} L${x + s},${y - s * 0.8} L${x - s},${y - s * 0.8}Z`} {...common} />
  }
  if (flag === 'anormal') {
    return <path d={`M${x},${y - s} L${x + s},${y} L${x},${y + s} L${x - s},${y}Z`} {...common} />
  }
  return <circle cx={x} cy={y} r={r} {...common} />
}

function useWidth(ref) {
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const el = ref.current
    if (!el) return undefined
    // Fires once right after observe(), then on every resize.
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width))
    observer.observe(el)
    return () => observer.disconnect()
  }, [ref])
  return width
}

/** Band segments: one per point, from midpoint to midpoint, merged when equal. */
function bandSegments(pts, left, right) {
  const segments = []
  pts.forEach((p, i) => {
    if (p.ref_low === null && p.ref_high === null) return
    const x0 = i === 0 ? left : (pts[i - 1].x + p.x) / 2
    const x1 = i === pts.length - 1 ? right : (p.x + pts[i + 1].x) / 2
    const last = segments[segments.length - 1]
    if (last && last.x1 === x0 && last.low === p.ref_low && last.high === p.ref_high) {
      last.x1 = x1
    } else {
      segments.push({ x0, x1, low: p.ref_low, high: p.ref_high })
    }
  })
  return segments
}

export default function TrendChart({ points, unit, label, describedBy }) {
  const wrapRef = useRef(null)
  const width = useWidth(wrapRef)
  const [active, setActive] = useState(null)
  const liveId = useId()

  const numeric = useMemo(
    () =>
      points
        .filter((p) => p.value_num !== null && p.value_num !== undefined)
        .map((p) => ({ ...p, t: toTime(p.date) })),
    [points],
  )

  const height = width >= 640 ? 280 : 230
  const layout = useMemo(() => {
    if (!numeric.length || !width) return null
    const values = numeric.flatMap((p) =>
      [p.value_num, p.ref_low, p.ref_high].filter((v) => v !== null && v !== undefined),
    )
    const y = yScale(values)
    const tickText = y.ticks.map(formatNumber)
    const left = Math.max(...tickText.map((t) => t.length)) * 7 + 14
    const top = 22
    const bottom = height - 28
    const right = width - 14
    const innerPad = 14
    const t0 = numeric[0].t
    const t1 = numeric[numeric.length - 1].t
    const span = t1 - t0
    const xOf = (t) =>
      span === 0
        ? (left + right) / 2
        : left + innerPad + ((t - t0) / span) * (right - left - 2 * innerPad)
    const yOf = (v) => bottom - ((v - y.lo) / (y.hi - y.lo)) * (bottom - top)
    const pts = numeric.map((p) => ({ ...p, x: xOf(p.t), y: yOf(p.value_num) }))
    const ticksX = xTicks(t0, t1, Math.floor((right - left) / 95)).map((t) => ({
      x: xOf(t),
      label: xLabel(t, span),
    }))
    // Drop repeated labels (two ticks in the same month).
    const xLabels = ticksX.filter((tick, i) => i === 0 || tick.label !== ticksX[i - 1].label)
    const bands = bandSegments(pts, left, right).map((s) => ({
      x0: s.x0,
      x1: s.x1,
      yTop: s.high === null ? top : yOf(s.high),
      yBottom: s.low === null ? bottom : yOf(s.low),
      hasTop: s.high !== null,
      hasBottom: s.low !== null,
    }))
    return { y, tickText, left, top, bottom, right, pts, xLabels, bands, yOf }
  }, [numeric, width, height])

  const skipped = points.length - numeric.length
  const current = layout && active !== null ? layout.pts[active] : null

  function nearest(clientX) {
    const rect = wrapRef.current.getBoundingClientRect()
    const x = clientX - rect.left
    let best = 0
    layout.pts.forEach((p, i) => {
      if (Math.abs(p.x - x) < Math.abs(layout.pts[best].x - x)) best = i
    })
    return best
  }

  function onKeyDown(e) {
    if (!layout) return
    const last = layout.pts.length - 1
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault()
      const step = e.key === 'ArrowRight' ? 1 : -1
      setActive((i) => Math.min(last, Math.max(0, (i ?? (step > 0 ? -1 : last + 1)) + step)))
    } else if (e.key === 'Home' || e.key === 'End') {
      e.preventDefault()
      setActive(e.key === 'Home' ? 0 : last)
    } else if (e.key === 'Escape') {
      setActive(null)
    }
  }

  if (!numeric.length) {
    return (
      <p className="rounded-xl bg-slate-50 px-3 py-6 text-center text-sm text-slate-600">
        Ningún resultado tiene un valor numérico que se pueda graficar. Mira la tabla.
      </p>
    )
  }

  const lastPt = layout?.pts[layout.pts.length - 1]
  const tooltipLeft = current ? Math.min(Math.max(current.x, 90), width - 90) : 0
  const tooltipBelow = current ? current.y < 90 : false

  return (
    <div>
      <div ref={wrapRef} className="relative w-full select-none" style={{ height }}>
        {layout && (
          <svg
            width="100%"
            height={height}
            viewBox={`0 0 ${width} ${height}`}
            role="group"
            aria-label={`${label}. Usa las flechas para recorrer los valores.`}
            aria-describedby={describedBy}
            tabIndex={0}
            onKeyDown={onKeyDown}
            onFocus={() => setActive((i) => i ?? layout.pts.length - 1)}
            onBlur={() => setActive(null)}
            onPointerMove={(e) => setActive(nearest(e.clientX))}
            onPointerDown={(e) => setActive(nearest(e.clientX))}
            onPointerLeave={(e) => e.pointerType === 'mouse' && setActive(null)}
            className="touch-pan-y rounded-lg focus-visible:outline-2 focus-visible:outline-brand-600"
          >
            {/* Reference band */}
            {layout.bands.map((b, i) => (
              <g key={`band-${i}`}>
                <rect
                  x={b.x0}
                  y={b.yTop}
                  width={Math.max(0, b.x1 - b.x0)}
                  height={Math.max(0, b.yBottom - b.yTop)}
                  fill={COLORS.band}
                  fillOpacity={0.12}
                />
                {b.hasTop && (
                  <line
                    x1={b.x0}
                    x2={b.x1}
                    y1={b.yTop}
                    y2={b.yTop}
                    stroke={COLORS.bandEdge}
                    strokeOpacity={0.45}
                  />
                )}
                {b.hasBottom && (
                  <line
                    x1={b.x0}
                    x2={b.x1}
                    y1={b.yBottom}
                    y2={b.yBottom}
                    stroke={COLORS.bandEdge}
                    strokeOpacity={0.45}
                  />
                )}
              </g>
            ))}

            {/* Grid and y ticks */}
            {layout.y.ticks.map((v, i) => {
              const y = layout.yOf(v)
              return (
                <g key={`y-${v}`}>
                  <line x1={layout.left} x2={layout.right} y1={y} y2={y} stroke={COLORS.grid} />
                  <text
                    x={layout.left - 8}
                    y={y}
                    dy="0.32em"
                    textAnchor="end"
                    fontSize="12"
                    fill={COLORS.axis}
                    style={{ fontVariantNumeric: 'tabular-nums' }}
                  >
                    {layout.tickText[i]}
                  </text>
                </g>
              )
            })}
            {layout.xLabels.map((tick, i) => (
              <text
                key={`x-${i}`}
                x={tick.x}
                y={layout.bottom + 18}
                textAnchor={
                  layout.xLabels.length === 1
                    ? 'middle'
                    : i === 0
                      ? 'start'
                      : i === layout.xLabels.length - 1
                        ? 'end'
                        : 'middle'
                }
                fontSize="12"
                fill={COLORS.axis}
              >
                {tick.label}
              </text>
            ))}

            {/* Crosshair */}
            {current && (
              <line
                x1={current.x}
                x2={current.x}
                y1={layout.top - 6}
                y2={layout.bottom}
                stroke={COLORS.axis}
                strokeOpacity={0.6}
              />
            )}

            {/* Line */}
            {layout.pts.length > 1 && (
              <polyline
                points={layout.pts.map((p) => `${p.x},${p.y}`).join(' ')}
                fill="none"
                stroke={COLORS.line}
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
            )}

            {/* Markers */}
            {layout.pts.map((p, i) => (
              <Marker
                key={p.result_id}
                flag={p.flag}
                hollow={Boolean(p.comparator)}
                x={p.x}
                y={p.y}
                r={i === active ? R + 1.5 : R}
              />
            ))}

            {/* Direct label: the latest value only. */}
            {lastPt && active === null && (
              <text
                x={Math.min(lastPt.x, layout.right)}
                y={lastPt.y - 12}
                textAnchor={layout.pts.length > 1 ? 'end' : 'middle'}
                fontSize="12"
                fontWeight="600"
                fill={COLORS.text}
                stroke={COLORS.surface}
                strokeWidth={3}
                paintOrder="stroke"
              >
                {resultValue(lastPt)}
              </text>
            )}
          </svg>
        )}

        {current && (
          <div
            aria-hidden="true"
            className="pointer-events-none absolute z-10 w-max max-w-44 -translate-x-1/2 rounded-xl bg-white px-3 py-2 text-sm shadow-lg ring-1 ring-slate-200"
            style={{
              left: tooltipLeft,
              top: tooltipBelow ? current.y + 14 : undefined,
              bottom: tooltipBelow ? undefined : height - current.y + 14,
            }}
          >
            <p className="font-semibold text-slate-900">
              {resultValue(current)} {unit}
            </p>
            {current.flag && <p className="text-slate-700">{flagLabel(current.flag)}</p>}
            <p className="text-slate-500">{formatDate(current.date)}</p>
            {referenceText(current) && (
              <p className="text-xs text-slate-500">Ref.: {referenceText(current)}</p>
            )}
          </div>
        )}
      </div>

      {/* What the keyboard selection says, for screen readers. */}
      <p id={liveId} aria-live="polite" className="sr-only">
        {current
          ? `${formatDate(current.date)}: ${resultValue(current)} ${unit}${
              current.flag ? `, ${flagLabel(current.flag)}` : ''
            }`
          : ''}
      </p>

      {skipped > 0 && (
        <p className="mt-1 text-xs text-slate-500">
          {skipped === 1
            ? '1 resultado sin valor numérico no aparece en el gráfico; está en la tabla.'
            : `${skipped} resultados sin valor numérico no aparecen en el gráfico; están en la tabla.`}
        </p>
      )}
    </div>
  )
}

/** Keys for what the chart draws (only the marks present in this series). */
export function TrendLegend({ points }) {
  const flags = new Set(points.map((p) => p.flag))
  const hasBand = points.some((p) => p.ref_low !== null || p.ref_high !== null)
  const hasBound = points.some((p) => p.comparator && p.value_num !== null)
  const items = [
    hasBand && {
      key: 'band',
      label: 'Rango de referencia',
      icon: (
        <rect
          x="1"
          y="3"
          width="16"
          height="10"
          fill={COLORS.band}
          fillOpacity={0.18}
          stroke={COLORS.bandEdge}
          strokeOpacity={0.45}
        />
      ),
    },
    (flags.has('normal') || flags.has(null) || flags.has(undefined)) && {
      key: 'normal',
      label: 'Normal o sin indicador',
      icon: <Marker x={9} y={8} r={4.5} ring={false} />,
    },
    flags.has('alto') && {
      key: 'alto',
      label: 'Alto',
      icon: <Marker flag="alto" x={9} y={8} r={4} ring={false} />,
    },
    flags.has('bajo') && {
      key: 'bajo',
      label: 'Bajo',
      icon: <Marker flag="bajo" x={9} y={8} r={4} ring={false} />,
    },
    flags.has('anormal') && {
      key: 'anormal',
      label: 'Anormal',
      icon: <Marker flag="anormal" x={9} y={8} r={4} ring={false} />,
    },
    hasBound && {
      key: 'bound',
      label: 'Informado como «<» o «>»',
      icon: <Marker hollow x={9} y={8} r={4} />,
    },
  ].filter(Boolean)

  return (
    <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600">
      {items.map((item) => (
        <li key={item.key} className="flex items-center gap-1.5">
          <svg width="18" height="16" aria-hidden="true">
            {item.icon}
          </svg>
          {item.label}
        </li>
      ))}
    </ul>
  )
}
