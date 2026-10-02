import { useState, useEffect, useMemo, useCallback } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useToast } from '@/hooks/use-toast'
import { Download, TrendingUp, TrendingDown, BarChart3, Target, Award, ChevronLeft, ChevronRight, Calendar } from 'lucide-react'
import axios from 'axios'
import { cn } from '@/utils/utils'

// ── Interfaces ────────────────────────────────────────────────────────────

interface TodaySummary {
  trades: number; wins: number; losses: number; win_rate: number; pnl: number; best_prompt: string
}

interface DailySummary {
  date: string; trades: number; wins: number; losses: number; win_rate: number; pnl: number
}

interface ReportsData {
  today: TodaySummary; daily_history: DailySummary[]; prompts: PromptStats[]; trades: TradeResult[]
  // Set when the broker's trade history could not be read: the figures below are incomplete.
  mt5_error?: string | null
}

interface TradeResult {
  id: number; prompt_number: number; prompt_text: string; symbol: string; direction: string
  entry_price: number | null; stop_loss: number | null; take_profit: number | null; lot_size: number
  mt5_ticket: number | null; executed_at: string; result: string | null; profit: number | null
  closed_at: string | null; reasoning: string | null; confidence: number | null
}

interface PromptStats {
  prompt_number: number; prompt_text: string; total_trades: number; wins: number; losses: number
  win_rate: number; total_profit: number; avg_profit: number; display_name: string
}

// ── Journal interfaces ────────────────────────────────────────────────────

interface JournalTrade {
  id: number; source: 'autopilot' | 'manual' | 'mt5_connector'; symbol: string; direction: string
  entry_price: number | null; exit_price: number | null; stop_loss: number | null; take_profit: number | null
  lot_size: number; profit: number | null; result: string | null
  executed_at: string; closed_at: string | null
  prompt_number: number | null; prompt_text: string | null; confidence: number | null; reasoning: string | null
}

interface JournalSummary {
  total_trades: number; autopilot_trades: number; manual_trades: number; mt5_trades: number
  mt5_available: boolean
  wins: number; losses: number; pnl: number
  best_trade: { symbol: string; direction: string; profit: number; source: string } | null
  worst_trade: { symbol: string; direction: string; profit: number; source: string } | null
}

interface JournalData {
  from_date: string; to_date: string; summary: JournalSummary; trades: JournalTrade[]
  page: number; per_page: number; has_next: boolean; has_prev: boolean
  total_count: number
}

// ── Component ─────────────────────────────────────────────────────────────

export default function ReportsPage() {
  const { toast } = useToast()
  const [tab, setTab] = useState<'overview' | 'journal'>('overview')

  // Overview state
  const [data, setData] = useState<ReportsData | null>(null)
  const [loading, setLoading] = useState(true)
  const [symbolFilter, setSymbolFilter] = useState('all')
  const [directionFilter, setDirectionFilter] = useState('all')
  const [resultFilter, setResultFilter] = useState('all')

  // Journal state
  const todayStr = () => {
    const d = new Date()
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  }
  const sevenDaysAgo = () => {
    const d = new Date(); d.setDate(d.getDate() - 7)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  }
  const [journalFromDate, setJournalFromDate] = useState(sevenDaysAgo)
  const [journalToDate, setJournalToDate] = useState(todayStr)
  const [journalPage, setJournalPage] = useState(1)
  const [journalData, setJournalData] = useState<JournalData | null>(null)
  const [journalLoading, setJournalLoading] = useState(false)

  // eslint-disable-next-line react-hooks/exhaustive-deps -- loads once on mount
  useEffect(() => { fetchReports() }, [])
  // eslint-disable-next-line react-hooks/exhaustive-deps -- the fetch reads exactly the inputs listed; adding the function itself would refetch on every render
  useEffect(() => { fetchJournal() }, [journalFromDate, journalToDate, journalPage])

  const fetchReports = async () => {
    setLoading(true)
    try {
      const res = await axios.get('/api/analytics/reports')
      setData(res.data)
    } catch {
      toast({ title: 'Error', description: 'Failed to fetch reports', variant: 'destructive' })
    } finally {
      setLoading(false)
    }
  }

  const fetchJournal = useCallback(async () => {
    setJournalLoading(true)
    try {
      const res = await axios.get(`/api/analytics/journal?from_date=${journalFromDate}&to_date=${journalToDate}&page=${journalPage}&per_page=20`)
      setJournalData(res.data)
    } catch {
      toast({ title: 'Error', description: 'Failed to fetch journal', variant: 'destructive' })
    } finally {
      setJournalLoading(false)
    }
  }, [journalFromDate, journalToDate, journalPage, toast])

  const exportCsv = async () => {
    try {
      if (tab === 'journal') {
        toast({ title: 'Exporting...', description: 'Fetching all journal trades' })
        const res = await axios.get(`/api/analytics/journal/export?from_date=${journalFromDate}&to_date=${journalToDate}`)
        const trades = res.data.trades
        if (!trades?.length) { toast({ title: 'No data', description: 'No trades to export' }); return }
        const headers = ['Date', 'Source', 'Symbol', 'Direction', 'Entry', 'Exit', 'SL', 'TP', 'Lot', 'Result', 'P&L', 'Prompt']
        const rows = trades.map((t: any) => [
          t.executed_at?.split('T')[0] || '',
          t.source || 'autopilot',
          t.symbol,
          t.direction,
          t.entry_price ?? '',
          t.exit_price ?? '',
          t.stop_loss ?? '',
          t.take_profit ?? '',
          t.lot_size,
          t.result ?? '',
          t.profit?.toFixed(2) ?? '',
          t.prompt_number ? `#${t.prompt_number}` : t.prompt_text || '-',
        ])
        const csv = [headers.join(','), ...rows.map((r: string[]) => r.join(','))].join('\n')
        const blob = new Blob([csv], { type: 'text/csv' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a'); a.href = url; a.download = `journal_${journalFromDate}_${journalToDate}.csv`; a.click()
        URL.revokeObjectURL(url)
        toast({ title: 'Exported', description: `${trades.length} trades exported` })
      } else {
        const trades = data?.trades
        if (!trades?.length) { toast({ title: 'No data', description: 'No trades to export' }); return }
        const res = await axios.get('/api/analytics/reports/export')
        const allTrades = res.data.trades
        const headers = ['Date', 'Prompt', 'Symbol', 'Direction', 'Entry', 'Lot', 'Result', 'P&L', 'Confidence']
        const rows = allTrades.map((t: any) => [
          t.executed_at?.split('T')[0] || '',
          t.prompt_number ? `#${t.prompt_number}` : t.prompt_text || '-',
          t.symbol,
          t.direction,
          t.entry_price ?? '',
          t.lot_size,
          t.result ?? '',
          t.profit?.toFixed(2) ?? '',
          t.confidence ?? '',
        ])
        const csv = [headers.join(','), ...rows.map((r: string[]) => r.join(','))].join('\n')
        const blob = new Blob([csv], { type: 'text/csv' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a'); a.href = url; a.download = 'reports_overview_export.csv'; a.click()
        URL.revokeObjectURL(url)
        toast({ title: 'Exported', description: `${allTrades.length} trades exported` })
      }
    } catch {
      toast({ title: 'Export failed', description: 'Could not export trades', variant: 'destructive' })
    }
  }

  // ── Overview filtered trades ──

  const filteredTrades = useMemo(() => {
    if (!data?.trades) return []
    return data.trades.filter(t => {
      if (symbolFilter !== 'all' && t.symbol !== symbolFilter) return false
      if (directionFilter !== 'all' && t.direction !== directionFilter) return false
      if (resultFilter === 'win' && (t.profit ?? 0) <= 0) return false
      if (resultFilter === 'loss' && (t.profit ?? 0) > 0) return false
      if (resultFilter === 'open' && t.result !== 'pending') return false
      return true
    })
  }, [data?.trades, symbolFilter, directionFilter, resultFilter])

  const symbols = useMemo(() => {
    if (!data?.trades) return []
    return [...new Set(data.trades.map(t => t.symbol))].sort()
  }, [data?.trades])

  const tabClass = (name: string) =>
    cn(
      "px-4 py-2 rounded-lg text-sm font-medium transition-colors",
      tab === name ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground hover:bg-muted"
    )

  return (
    <div className="p-3 sm:p-6 md:p-8 space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <h1 className="font-heading text-xl sm:text-2xl md:text-3xl font-bold">Reports</h1>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={exportCsv}
            disabled={tab === 'overview' ? !data?.trades.length : !(journalData?.summary.total_trades ?? 0)}>
            <Download className="w-4 h-4 mr-2" /> Export CSV
          </Button>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-2 border-b border-border pb-2">
        <button className={tabClass('overview')} onClick={() => setTab('overview')}>
          <BarChart3 className="w-4 h-4 inline mr-1.5" />Overview
        </button>
        <button className={tabClass('journal')} onClick={() => setTab('journal')}>
          <Calendar className="w-4 h-4 inline mr-1.5" />Trade Journal
        </button>
      </div>

      {data?.mt5_error && (
        <div className="mb-4 p-3 rounded text-sm bg-red-500/10 text-red-500">
          {data.mt5_error}. The figures below leave out every trade from the broker, so they are incomplete.
        </div>
      )}

      {/* ════════════════ OVERVIEW TAB ════════════════ */}
      {tab === 'overview' && (() => {
        if (loading) return <div className="text-muted-foreground">Loading reports...</div>
        if (!data) return <div className="text-muted-foreground">No report data available.</div>
        const { today, daily_history, prompts } = data
        const maxDailyPnl = Math.max(...daily_history.map(d => Math.abs(d.pnl)), 1)
        return (
          <div className="space-y-6">
            {/* Metric cards */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 sm:gap-4">
              <Card>
                <CardHeader className="pb-1 px-3 pt-3 sm:px-4 sm:pt-4">
                  <CardTitle className="text-xs text-muted-foreground flex items-center gap-1.5">
                    <BarChart3 className="w-3.5 h-3.5" />Today's Trades
                  </CardTitle>
                </CardHeader>
                <CardContent className="px-3 pb-3 sm:px-4 sm:pb-4">
                  <div className="text-xl sm:text-2xl font-bold">{today.trades}</div>
                  <div className="text-xs text-muted-foreground mt-0.5">{today.wins} win / {today.losses} loss</div>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-1 px-3 pt-3 sm:px-4 sm:pt-4">
                  <CardTitle className="text-xs text-muted-foreground flex items-center gap-1.5">
                    <TrendingUp className="w-3.5 h-3.5" />Today's P&L
                  </CardTitle>
                </CardHeader>
                <CardContent className="px-3 pb-3 sm:px-4 sm:pb-4">
                  <div className={cn("text-xl sm:text-2xl font-bold", today.pnl >= 0 ? "text-green-500" : "text-red-500")}>
                    {today.pnl >= 0 ? '+' : ''}${today.pnl.toFixed(2)}
                  </div>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-1 px-3 pt-3 sm:px-4 sm:pt-4">
                  <CardTitle className="text-xs text-muted-foreground flex items-center gap-1.5">
                    <Target className="w-3.5 h-3.5" />Win Rate
                  </CardTitle>
                </CardHeader>
                <CardContent className="px-3 pb-3 sm:px-4 sm:pb-4">
                  <div className="text-xl sm:text-2xl font-bold">{today.win_rate}%</div>
                  <div className="text-xs text-muted-foreground mt-0.5">
                    {today.trades > 0 ? `${today.wins}/${today.trades} trades` : 'No trades'}
                  </div>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-1 px-3 pt-3 sm:px-4 sm:pt-4">
                  <CardTitle className="text-xs text-muted-foreground flex items-center gap-1.5">
                    <Award className="w-3.5 h-3.5" />Best Prompt
                  </CardTitle>
                </CardHeader>
                <CardContent className="px-3 pb-3 sm:px-4 sm:pb-4">
                  <div className="text-xl sm:text-2xl font-bold">{today.best_prompt || '-'}</div>
                  <div className="text-xs text-muted-foreground mt-0.5">Highest P&L today</div>
                </CardContent>
              </Card>
            </div>

            {/* Daily P&L Chart */}
            <Card>
              <CardHeader className="pb-2 px-3 pt-3 sm:px-4 sm:pt-4">
                <CardTitle className="text-sm font-medium">Daily P&L (Last 30 Days)</CardTitle>
              </CardHeader>
              <CardContent className="px-3 pb-3 sm:px-4 sm:pb-4">
                <div className="flex items-end gap-1 h-28 sm:h-36">
                  {daily_history.map(d => {
                    const height = Math.abs(d.pnl) / maxDailyPnl * 100
                    const isPositive = d.pnl >= 0
                    return (
                      <div key={d.date} className="flex-1 flex flex-col items-center justify-end h-full group relative">
                        <div className={cn("w-full rounded-t transition-all duration-200 hover:opacity-80",
                          isPositive ? "bg-green-500/80" : "bg-red-500/80",
                          d.trades === 0 && "bg-muted/30 h-1")}
                          style={{ height: d.trades > 0 ? `${Math.max(height, 2)}%` : '4px' }}
                          title={`${d.date}: ${d.pnl >= 0 ? '+' : ''}$${d.pnl.toFixed(2)} (${d.trades} trades)`} />
                        <div className="absolute bottom-full mb-1 hidden group-hover:block bg-popover text-popover-foreground text-[10px] px-2 py-1 rounded shadow-lg whitespace-nowrap z-10">
                          {d.date}: {isPositive ? '+' : ''}${d.pnl.toFixed(2)} / {d.trades} trades
                        </div>
                      </div>
                    )
                  })}
                </div>
                <div className="flex justify-between mt-1 text-[10px] text-muted-foreground">
                  <span>{daily_history[0]?.date?.split('-').slice(1).join('/')}</span>
                  <span>{daily_history[15]?.date?.split('-').slice(1).join('/')}</span>
                  <span>{daily_history[daily_history.length - 1]?.date?.split('-').slice(1).join('/')}</span>
                </div>
              </CardContent>
            </Card>

            {/* Strategy Scoreboard */}
            <Card>
              <CardHeader className="pb-2 px-3 pt-3 sm:px-4 sm:pt-4">
                <CardTitle className="text-sm font-medium">Strategy Scoreboard</CardTitle>
              </CardHeader>
              <CardContent className="px-0 pb-3 sm:px-0 sm:pb-4">
                {prompts.length === 0 ? (
                  <div className="text-sm text-muted-foreground px-4">No strategy data yet.</div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs sm:text-sm">
                      <thead>
                        <tr className="border-b border-border">
                          <th className="text-left px-3 sm:px-4 py-2 font-medium text-muted-foreground">#</th>
                          <th className="text-left px-3 sm:px-4 py-2 font-medium text-muted-foreground">Prompt</th>
                          <th className="text-center px-3 sm:px-4 py-2 font-medium text-muted-foreground">Trades</th>
                          <th className="text-center px-3 sm:px-4 py-2 font-medium text-muted-foreground">W</th>
                          <th className="text-center px-3 sm:px-4 py-2 font-medium text-muted-foreground">L</th>
                          <th className="text-center px-3 sm:px-4 py-2 font-medium text-muted-foreground">Win Rate</th>
                          <th className="text-right px-3 sm:px-4 py-2 font-medium text-muted-foreground">P&L</th>
                        </tr>
                      </thead>
                      <tbody>
                        {prompts.map((p, i) => (
                          <tr key={p.prompt_number} className={cn("border-b border-border/50 hover:bg-muted/50", i === 0 && "bg-primary/5")}>
                            <td className="px-3 sm:px-4 py-2">{p.display_name}</td>
                            <td className="px-3 sm:px-4 py-2 max-w-[200px] truncate" title={p.prompt_text}>{p.prompt_text}</td>
                            <td className="px-3 sm:px-4 py-2 text-center">{p.total_trades}</td>
                            <td className="px-3 sm:px-4 py-2 text-center text-green-500">{p.wins}</td>
                            <td className="px-3 sm:px-4 py-2 text-center text-red-500">{p.losses}</td>
                            <td className="px-3 sm:px-4 py-2 text-center font-medium">{p.win_rate}%</td>
                            <td className={cn("px-3 sm:px-4 py-2 text-right font-medium tabular-nums", p.total_profit >= 0 ? "text-green-500" : "text-red-500")}>
                              {p.total_profit >= 0 ? '+' : ''}${p.total_profit.toFixed(2)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </CardContent>
            </Card>

            {/* Trade History */}
            <Card>
              <CardHeader className="pb-2 px-3 pt-3 sm:px-4 sm:pt-4">
                <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                  <CardTitle className="text-sm font-medium">Trade History (Last 50)</CardTitle>
                  <div className="flex flex-wrap gap-2">
                    <Select value={symbolFilter} onValueChange={setSymbolFilter}>
                      <SelectTrigger className="w-24 h-8 text-xs"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="all">All Symbols</SelectItem>
                        {symbols.map(s => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                      </SelectContent>
                    </Select>
                    <Select value={directionFilter} onValueChange={setDirectionFilter}>
                      <SelectTrigger className="w-24 h-8 text-xs"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="all">All</SelectItem>
                        <SelectItem value="BUY">BUY</SelectItem>
                        <SelectItem value="SELL">SELL</SelectItem>
                      </SelectContent>
                    </Select>
                    <Select value={resultFilter} onValueChange={setResultFilter}>
                      <SelectTrigger className="w-24 h-8 text-xs"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="all">All Results</SelectItem>
                        <SelectItem value="win">Win</SelectItem>
                        <SelectItem value="loss">Loss</SelectItem>
                        <SelectItem value="open">Open</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="px-0 pb-3 sm:px-0 sm:pb-4">
                {filteredTrades.length === 0 ? (
                  <div className="text-sm text-muted-foreground px-4">No trades match the current filters.</div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs sm:text-sm">
                      <thead>
                        <tr className="border-b border-border">
                          <th className="text-left px-3 sm:px-4 py-2 font-medium text-muted-foreground">Date</th>
                          <th className="text-left px-3 sm:px-4 py-2 font-medium text-muted-foreground">Prompt</th>
                          <th className="text-left px-3 sm:px-4 py-2 font-medium text-muted-foreground">Symbol</th>
                          <th className="text-center px-3 sm:px-4 py-2 font-medium text-muted-foreground">Dir</th>
                          <th className="text-right px-3 sm:px-4 py-2 font-medium text-muted-foreground">Entry</th>
                          <th className="text-right px-3 sm:px-4 py-2 font-medium text-muted-foreground">Lot</th>
                          <th className="text-center px-3 sm:px-4 py-2 font-medium text-muted-foreground">Result</th>
                          <th className="text-right px-3 sm:px-4 py-2 font-medium text-muted-foreground">P&L</th>
                          <th className="text-center px-3 sm:px-4 py-2 font-medium text-muted-foreground">Conf</th>
                        </tr>
                      </thead>
                      <tbody>
                        {filteredTrades.map(t => (
                          <tr key={t.id} className="border-b border-border/50 hover:bg-muted/50">
                            <td className="px-3 sm:px-4 py-2 text-muted-foreground whitespace-nowrap">{t.executed_at?.split('T')[0]}</td>
                            <td className="px-3 sm:px-4 py-2"><span className="text-muted-foreground text-xs">#{t.prompt_number}</span></td>
                            <td className="px-3 sm:px-4 py-2 font-medium">{t.symbol}</td>
                            <td className="px-3 sm:px-4 py-2 text-center">
                              <span className={cn("inline-flex items-center gap-1 text-xs font-medium", t.direction === 'BUY' ? 'text-green-500' : 'text-red-500')}>
                                {t.direction === 'BUY' ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}{t.direction}
                              </span>
                            </td>
                            <td className="px-3 sm:px-4 py-2 text-right tabular-nums">{t.entry_price?.toFixed(2) ?? '-'}</td>
                            <td className="px-3 sm:px-4 py-2 text-right">{t.lot_size}</td>
                            <td className="px-3 sm:px-4 py-2 text-center">
                              <span className={cn("inline-block text-xs font-medium px-1.5 py-0.5 rounded",
                                t.result === 'TP_HIT' ? 'bg-green-500/10 text-green-500' :
                                t.result === 'SL_HIT' ? 'bg-red-500/10 text-red-500' :
                                t.result === 'CLOSED_MANUAL' ? 'bg-yellow-500/10 text-yellow-500' :
                                t.result === 'pending' ? 'bg-blue-500/10 text-blue-500' :
                                (t.profit ?? 0) > 0 ? 'bg-green-500/10 text-green-500' :
                                (t.profit ?? 0) < 0 ? 'bg-red-500/10 text-red-500' :
                                'bg-muted text-muted-foreground')}>
                                {t.result === 'TP_HIT' ? 'TP' : t.result === 'SL_HIT' ? 'SL' : t.result === 'CLOSED_MANUAL' ? 'Manual' : t.result === 'pending' ? 'Open' : t.result || '-'}
                              </span>
                            </td>
                            <td className={cn("px-3 sm:px-4 py-2 text-right font-medium tabular-nums", (t.profit ?? 0) >= 0 ? "text-green-500" : "text-red-500")}>
                              {(t.profit ?? 0) >= 0 ? '+' : ''}${(t.profit ?? 0).toFixed(2)}
                            </td>
                            <td className="px-3 sm:px-4 py-2 text-center text-muted-foreground text-xs">
                              {t.confidence != null ? `${Math.round(t.confidence)}%` : '-'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        )
      })()}

      {/* ════════════════ JOURNAL TAB ════════════════ */}
      {tab === 'journal' && (
        <div className="space-y-6">
          {/* Date range picker */}
          <Card>
            <CardContent className="p-3 sm:p-4">
              <div className="flex items-center justify-center gap-3 flex-wrap">
                <Calendar className="w-4 h-4 text-muted-foreground shrink-0" />
                <span className="text-xs text-muted-foreground">From:</span>
                <input
                  type="date"
                  value={journalFromDate}
                  onChange={e => { setJournalFromDate(e.target.value); setJournalPage(1) }}
                  className="bg-transparent border border-border rounded px-2 py-1 text-sm"
                />
                <span className="text-xs text-muted-foreground">To:</span>
                <input
                  type="date"
                  value={journalToDate}
                  onChange={e => { setJournalToDate(e.target.value); setJournalPage(1) }}
                  className="bg-transparent border border-border rounded px-2 py-1 text-sm"
                />
              </div>
            </CardContent>
          </Card>

          {journalLoading ? (
            <div className="text-muted-foreground">Loading journal...</div>
          ) : !journalData ? (
            <div className="text-muted-foreground">No journal data available.</div>
          ) : (
            <>
              {/* Journal Summary Cards */}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 sm:gap-4">
                <Card>
                  <CardHeader className="pb-1 px-3 pt-3 sm:px-4 sm:pt-4">
                    <CardTitle className="text-xs text-muted-foreground flex items-center gap-1.5">
                      <BarChart3 className="w-3.5 h-3.5" />Total Trades
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="px-3 pb-3 sm:px-4 sm:pb-4">
                    <div className="text-xl sm:text-2xl font-bold">{journalData.summary.total_trades}</div>
                    <div className="text-xs text-muted-foreground mt-0.5">
                      from MT5 connector{!journalData.summary.mt5_available ? ' (DB fallback)' : ''}
                    </div>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader className="pb-1 px-3 pt-3 sm:px-4 sm:pt-4">
                    <CardTitle className="text-xs text-muted-foreground flex items-center gap-1.5">
                      <TrendingUp className="w-3.5 h-3.5" />Day P&L
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="px-3 pb-3 sm:px-4 sm:pb-4">
                    <div className={cn("text-xl sm:text-2xl font-bold", journalData.summary.pnl >= 0 ? "text-green-500" : "text-red-500")}>
                      {journalData.summary.pnl >= 0 ? '+' : ''}${journalData.summary.pnl.toFixed(2)}
                    </div>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader className="pb-1 px-3 pt-3 sm:px-4 sm:pt-4">
                    <CardTitle className="text-xs text-muted-foreground flex items-center gap-1.5">
                      <Target className="w-3.5 h-3.5" />Win Rate
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="px-3 pb-3 sm:px-4 sm:pb-4">
                    <div className="text-xl sm:text-2xl font-bold">
                      {(journalData.summary.wins + journalData.summary.losses) > 0
                        ? `${((journalData.summary.wins / (journalData.summary.wins + journalData.summary.losses)) * 100).toFixed(1)}%`
                        : '-'}
                    </div>
                    <div className="text-xs text-muted-foreground mt-0.5">
                      {journalData.summary.wins} win / {journalData.summary.losses} loss
                    </div>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader className="pb-1 px-3 pt-3 sm:px-4 sm:pt-4">
                    <CardTitle className="text-xs text-muted-foreground flex items-center gap-1.5">
                      <Award className="w-3.5 h-3.5" />Best / Worst
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="px-3 pb-3 sm:px-4 sm:pb-4">
                    {journalData.summary.best_trade ? (
                      <div className="space-y-1.5">
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-[10px] text-muted-foreground">Best</span>
                          <div className="text-right">
                            <span className="text-green-500 text-sm font-bold">
                              +${journalData.summary.best_trade.profit.toFixed(2)}
                            </span>
                            <div className="text-[10px] text-muted-foreground">
                              {journalData.summary.best_trade.symbol} {journalData.summary.best_trade.direction}
                            </div>
                          </div>
                        </div>
                        {journalData.summary.worst_trade && (
                          <div className="flex items-center justify-between gap-2 pt-1 border-t border-border/50">
                            <span className="text-[10px] text-muted-foreground">Worst</span>
                            <div className="text-right">
                              <span className="text-red-500 text-sm font-bold">
                                -${Math.abs(journalData.summary.worst_trade.profit).toFixed(2)}
                              </span>
                              <div className="text-[10px] text-muted-foreground">
                                {journalData.summary.worst_trade.symbol} {journalData.summary.worst_trade.direction}
                              </div>
                            </div>
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className="text-sm text-muted-foreground">No trades</div>
                    )}
                  </CardContent>
                </Card>
              </div>

              {/* Journal Trade List */}
              <Card>
                <CardHeader className="pb-2 px-3 pt-3 sm:px-4 sm:pt-4">
                  <CardTitle className="text-sm font-medium">
                    Trades — {journalData.from_date} to {journalData.to_date}
                    <span className="text-muted-foreground font-normal ml-2">
                      (page {journalData.page} of {Math.ceil(journalData.total_count / journalData.per_page)})
                    </span>
                  </CardTitle>
                </CardHeader>
                <CardContent className="px-0 pb-3 sm:px-0 sm:pb-4">
                  {journalData.trades.length === 0 ? (
                    <div className="text-sm text-muted-foreground px-4">No trades in this date range.</div>
                  ) : (
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs sm:text-sm">
                        <thead>
                          <tr className="border-b border-border">
                            <th className="text-left px-3 sm:px-4 py-2 font-medium text-muted-foreground">Time</th>
                            <th className="text-center px-3 sm:px-4 py-2 font-medium text-muted-foreground">Src</th>
                            <th className="text-left px-3 sm:px-4 py-2 font-medium text-muted-foreground">Symbol</th>
                            <th className="text-center px-3 sm:px-4 py-2 font-medium text-muted-foreground">Dir</th>
                            <th className="text-right px-3 sm:px-4 py-2 font-medium text-muted-foreground">Entry</th>
                            <th className="text-right px-3 sm:px-4 py-2 font-medium text-muted-foreground">Exit</th>
                            <th className="text-right px-3 sm:px-4 py-2 font-medium text-muted-foreground">SL</th>
                            <th className="text-right px-3 sm:px-4 py-2 font-medium text-muted-foreground">TP</th>
                            <th className="text-right px-3 sm:px-4 py-2 font-medium text-muted-foreground">Lot</th>
                            <th className="text-center px-3 sm:px-4 py-2 font-medium text-muted-foreground">Result</th>
                            <th className="text-right px-3 sm:px-4 py-2 font-medium text-muted-foreground">P&L</th>
                            <th className="text-center px-3 sm:px-4 py-2 font-medium text-muted-foreground">Pr #</th>
                          </tr>
                        </thead>
                        <tbody>
                          {journalData.trades.map((t: JournalTrade) => (
                            <tr key={`${t.source}-${t.id}`} className="border-b border-border/50 hover:bg-muted/50">
                              <td className="px-3 sm:px-4 py-2 text-muted-foreground whitespace-nowrap">
                                {t.executed_at?.split('T')[1]?.split('.')[0]?.slice(0, 5) || '-'}
                              </td>
                              <td className="px-3 sm:px-4 py-2 text-center">
                                <span className={cn(
                                  "inline-block text-[10px] font-medium px-1.5 py-0.5 rounded uppercase",
                                  t.source === 'autopilot' ? 'bg-purple-500/10 text-purple-500' :
                                  t.source === 'mt5_connector' ? 'bg-orange-500/10 text-orange-500' :
                                  'bg-blue-500/10 text-blue-500'
                                )}>
                                  {t.source === 'autopilot' ? 'A' : t.source === 'mt5_connector' ? 'M5' : 'M'}
                                </span>
                              </td>
                              <td className="px-3 sm:px-4 py-2 font-medium">{t.symbol}</td>
                              <td className="px-3 sm:px-4 py-2 text-center">
                                <span className={cn("inline-flex items-center gap-1 text-xs font-medium",
                                  t.direction === 'BUY' ? 'text-green-500' : 'text-red-500')}>
                                  {t.direction === 'BUY' ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
                                  {t.direction}
                                </span>
                              </td>
                              <td className="px-3 sm:px-4 py-2 text-right tabular-nums">{t.entry_price?.toFixed(t.symbol?.includes('XAU') ? 2 : 5) ?? '-'}</td>
                              <td className="px-3 sm:px-4 py-2 text-right tabular-nums">{t.exit_price?.toFixed(t.symbol?.includes('XAU') ? 2 : 5) ?? '-'}</td>
                              <td className="px-3 sm:px-4 py-2 text-right tabular-nums text-muted-foreground">{t.stop_loss?.toFixed(t.symbol?.includes('XAU') ? 2 : 5) ?? '-'}</td>
                              <td className="px-3 sm:px-4 py-2 text-right tabular-nums text-muted-foreground">{t.take_profit?.toFixed(t.symbol?.includes('XAU') ? 2 : 5) ?? '-'}</td>
                              <td className="px-3 sm:px-4 py-2 text-right">{t.lot_size}</td>
                              <td className="px-3 sm:px-4 py-2 text-center">
                                <span className={cn("inline-block text-xs font-medium px-1.5 py-0.5 rounded",
                                  t.result === 'TP_HIT' ? 'bg-green-500/10 text-green-500' :
                                  t.result === 'SL_HIT' ? 'bg-red-500/10 text-red-500' :
                                  (t.profit ?? 0) > 0 ? 'bg-green-500/10 text-green-500' :
                                  (t.profit ?? 0) < 0 ? 'bg-red-500/10 text-red-500' :
                                  'bg-muted text-muted-foreground')}>
                                  {t.result === 'TP_HIT' ? 'TP' : t.result === 'SL_HIT' ? 'SL' : t.result || (t.profit !== null ? ((t.profit ?? 0) > 0 ? 'Win' : 'Loss') : 'Open')}
                                </span>
                              </td>
                              <td className={cn("px-3 sm:px-4 py-2 text-right font-medium tabular-nums", (t.profit ?? 0) >= 0 ? "text-green-500" : "text-red-500")}>
                                {(t.profit ?? 0) >= 0 ? '+' : ''}${(t.profit ?? 0).toFixed(2)}
                              </td>
                              <td className="px-3 sm:px-4 py-2 text-center text-muted-foreground text-xs">
                                {t.prompt_number ? `#${t.prompt_number}` : '-'}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}

                  {/* Pagination */}
                  <div className="flex items-center justify-between px-4 pt-3">
                    <span className="text-xs text-muted-foreground">
                      Total: {journalData.summary.total_trades} trades
                    </span>
                    <div className="flex items-center gap-2">
                      <Button
                        variant="outline" size="sm"
                        disabled={!journalData.has_prev}
                        onClick={() => setJournalPage(p => Math.max(1, p - 1))}
                      >
                        <ChevronLeft className="w-3.5 h-3.5" /> Previous
                      </Button>
                      <span className="text-xs text-muted-foreground">Page {journalData.page}</span>
                      <Button
                        variant="outline" size="sm"
                        disabled={!journalData.has_next}
                        onClick={() => setJournalPage(p => p + 1)}
                      >
                        Next <ChevronRight className="w-3.5 h-3.5" />
                      </Button>
                    </div>
                  </div>
                </CardContent>
              </Card>
            </>
          )}
        </div>
      )}
    </div>
  )
}
