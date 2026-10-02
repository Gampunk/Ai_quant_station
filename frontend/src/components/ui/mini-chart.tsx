import { useEffect, useRef, useState } from 'react'
import { createChart, LineSeries, CandlestickSeries, ColorType, Time } from 'lightweight-charts'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

interface MultiSeriesItem {
  label: string
  data: number[]
}

interface ChartData {
  title: string
  color?: string
  type: string
  data?: number[] | { time: number[]; value: number[] } | { time: number[]; open: number[]; high: number[]; low: number[]; close: number[] }
  multi_series?: MultiSeriesItem[]
}

export function MiniChart({ title, data, color = '#2563eb' }: { title: string; data: ChartData | number[]; color?: string }) {
  const chartContainerRef = useRef<HTMLDivElement>(null)
  const [isExpanded, setIsExpanded] = useState(false)
  const chartRef = useRef<any>(null)

  // Normalize input
  const chartData: ChartData = Array.isArray(data)
    ? { title, type: 'line', color, data }
    : { title: data.title || title, color: data.color || color, type: data.type || 'line', data: data.data, multi_series: data.multi_series }

  useEffect(() => {
    if (!chartContainerRef.current) return

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#d1d5db',
      },
      grid: {
        vertLines: { color: '#2a2a2a' },
        horzLines: { color: '#2a2a2a' },
      },
      width: chartContainerRef.current.clientWidth,
      height: isExpanded ? 400 : 150,
      handleScale: true,
      handleScroll: true,
      timeScale: { visible: true, borderVisible: false, timeVisible: true },
      rightPriceScale: { borderVisible: false },
      crosshair: {
        vertLine: { color: '#4b5563', width: 1, style: 2, labelBackgroundColor: '#1f2937' },
        horzLine: { color: '#4b5563', width: 1, style: 2, labelBackgroundColor: '#1f2937' },
      },
    })

    const colors = [chartData.color || color, '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6']

    if (chartData.type === 'candlestick' && typeof chartData.data === 'object' && !Array.isArray(chartData.data) && 'open' in chartData.data) {
      const d = chartData.data as { time: number[]; open: number[]; high: number[]; low: number[]; close: number[] }
      const series = chart.addSeries(CandlestickSeries, {
        upColor: '#22c55e', downColor: '#ef4444',
        borderUpColor: '#22c55e', borderDownColor: '#ef4444',
        wickUpColor: '#22c55e', wickDownColor: '#ef4444',
      })
      const candles = []
      for (let i = 0; i < d.time.length; i++) {
        candles.push({ time: Number(d.time[i]) as Time, open: d.open[i], high: d.high[i], low: d.low[i], close: d.close[i] })
      }
      series.setData(candles)
    } else if (chartData.type === 'multi' && chartData.multi_series) {
      chartData.multi_series.forEach((s, idx) => {
        const series = chart.addSeries(LineSeries, { color: colors[idx % colors.length], lineWidth: 2 })
        series.setData((s.data || []).map((v, i) => ({ time: i as any, value: v })))
      })
    } else {
      // Line chart — supports both array and {time:[], value:[]}
      const series = chart.addSeries(LineSeries, { color: chartData.color || color, lineWidth: 2 })
      if (typeof chartData.data === 'object' && !Array.isArray(chartData.data) && 'value' in (chartData.data || {})) {
        const d = chartData.data as { time: number[]; value: number[] }
        const points = (d.time || []).map((t, i) => ({ time: Number(t) as any, value: d.value[i] }))
        series.setData(points)
      } else {
        const values = Array.isArray(chartData.data) ? chartData.data : []
        series.setData(values.map((v, i) => ({ time: i as any, value: v })))
      }
    }

    chart.timeScale().fitContent()
    chartRef.current = chart

    const handleResize = () => {
      if (chartContainerRef.current) {
        chart.applyOptions({ width: chartContainerRef.current.clientWidth, height: chartContainerRef.current.clientHeight })
      }
    }
    window.addEventListener('resize', handleResize)

    return () => {
      window.removeEventListener('resize', handleResize)
      chart.remove()
    }
  // isExpanded only sets the starting height; the effect below resizes without rebuilding.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- see the line above
  }, [chartData.data, chartData.color, chartData.type, chartData.title, chartData.multi_series, color])

  useEffect(() => {
    if (chartRef.current) {
      chartRef.current.applyOptions({ height: isExpanded ? 400 : 150 })
    }
  }, [isExpanded])

  return (
    <Card className="mt-3 bg-background/50 border-muted">
      <CardHeader className="py-2 px-3 flex flex-row items-center justify-between">
        <CardTitle className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
          {chartData.title}
        </CardTitle>
        <div className="flex items-center gap-2">
          {chartData.type === 'candlestick' && <span className="text-[9px] text-muted-foreground">OHLC</span>}
          <button onClick={() => setIsExpanded(!isExpanded)} className="text-[10px] text-primary hover:underline">
            {isExpanded ? 'Collapse' : 'Expand'}
          </button>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        <div ref={chartContainerRef} style={{ height: isExpanded ? '400px' : '150px' }} className="w-full" />
      </CardContent>
    </Card>
  )
}
