import { useState, useEffect } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { useToast } from '@/hooks/use-toast'
import { useHistoryStore } from '@/store/historyStore'
import axios from 'axios'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'

interface Trade { ticket: number; symbol: string; direction: string; volume: number; price: number; profit: number; time: string; comment: string }

interface StrategyScore {
  prompt_text: string
  symbol: string
  direction: string | null
  source: string
  total_trades: number
  winning_trades: number
  total_pnl: number
  win_rate: number
  status?: 'winner' | 'neutral' | 'needs_work'
  avg_confidence: number | null
  avg_profit: number | null
  avg_loss: number | null
  profit_factor: number | null
  last_used: string | null
}

interface RewriteResult {
  original: string
  rewritten: string
  stats: { win_rate: number; total_trades: number; winning_trades: number; total_pnl: number }
  provider: string
  model: string
  losers_analyzed: number
  winners_analyzed: number
}

const STATUS_BADGE: Record<string, { label: string; cls: string }> = {
  winner: { label: 'Winner', cls: 'bg-green-500/15 text-green-500' },
  needs_work: { label: 'Needs Work', cls: 'bg-red-500/15 text-red-500' },
  neutral: { label: 'Neutral', cls: 'bg-muted text-muted-foreground' },
}

interface ModelPerf {
  provider: string
  model: string
  trades: number
  wins: number
  win_rate: number
  total_pnl: number
}

interface TimelinePoint {
  date: string
  trades: number
  wins: number
  win_rate: number
}

interface RagLogEntry {
  symbol: string | null
  similar_count: number
  top_count: number
  losers_count: number
  context_chars: number
  created_at: string | null
}

interface Plateau {
  type: string
  symbol?: string | null
  status?: string
  reason: string
}

interface RagHealth {
  config: {
    similar_count: number
    top_count: number
    losers_count: number
    embedding_model: string
    code_block_stripping: boolean
    min_trades_for_best: number
    min_trades_for_flag: number
    flag_threshold: number
    min_embeddings_per_symbol: number
    sim_variance_min: number
    recent_rag_logs: RagLogEntry[]
  }
  plateaus: Plateau[]
  per_symbol_stats: Array<{
    symbol: string
    total_embeddings: number
    avg_win_rate: number | null
    distinct_prompts: number
  }>
}

const PLATEAU_LABEL: Record<string, string> = {
  embedding: 'Embedding',
  embedding_variance: 'Embedding variance',
  scoreboard: 'Scoreboard',
  prompt_improvement: 'Prompt improvement',
  model_routing: 'Model routing',
}

export default function HistoryPage() {
  const { toast } = useToast()
  const [trades, setTrades] = useState<Trade[]>([])
  const [loading, setLoading] = useState(true)
  const hours = useHistoryStore((s) => s.hours)
  const setHours = useHistoryStore((s) => s.setHours)

  const [scores, setScores] = useState<StrategyScore[]>([])
  const [scoresLoading, setScoresLoading] = useState(true)

  const [rewriteTarget, setRewriteTarget] = useState<StrategyScore | null>(null)
  const [rewriteLoading, setRewriteLoading] = useState(false)
  const [rewriteResult, setRewriteResult] = useState<RewriteResult | null>(null)
  const [approveLoading, setApproveLoading] = useState(false)

  const [modelPerf, setModelPerf] = useState<ModelPerf[]>([])
  const [timeline, setTimeline] = useState<TimelinePoint[]>([])
  const [ragHealth, setRagHealth] = useState<RagHealth | null>(null)
  const [ragOpen, setRagOpen] = useState(true)

  useEffect(() => { fetchHistory() }, [hours])

  useEffect(() => { fetchScores(); fetchInsights() }, [])

  const fetchHistory = async () => {
    setLoading(true)
    try { setTrades((await axios.get(`/api/mt5/history?hours=${hours}`)).data?.deals || []) }
    catch { toast({ title: "Error", description: "Failed to fetch trade history", variant: 'destructive' }) }
    finally { setLoading(false) }
  }

  const fetchScores = async () => {
    setScoresLoading(true)
    try {
      const res = await axios.get('/api/analytics/strategy-scores')
      setScores(res.data || [])
    } catch { /* scores optional */ }
    finally { setScoresLoading(false) }
  }

  const fetchInsights = async () => {
    try {
      const [mp, tl] = await Promise.all([
        axios.get('/api/analytics/model-performance'),
        axios.get('/api/analytics/accuracy-timeline?days=30'),
      ])
      setModelPerf(mp.data || [])
      setTimeline(tl.data?.timeline || [])
    } catch { /* insights optional */ }
    try {
      const rh = await axios.get('/api/rag-health')
      setRagHealth(rh.data)
    } catch { /* rag health optional */ }
  }

  const handleRewrite = async (s: StrategyScore) => {
    setRewriteTarget(s)
    setRewriteResult(null)
    setRewriteLoading(true)
    try {
      const res = await axios.post('/api/analytics/prompts/rewrite', {
        prompt_text: s.prompt_text,
        symbol: s.symbol,
      })
      setRewriteResult(res.data)
    } catch (err: any) {
      toast({ title: 'Rewrite failed', description: err?.response?.data?.detail || 'AI call failed', variant: 'destructive' })
      setRewriteTarget(null)
    } finally { setRewriteLoading(false) }
  }

  const handleApprove = async () => {
    if (!rewriteResult) return
    setApproveLoading(true)
    try {
      await axios.post('/api/autopilot/prompts', { content: rewriteResult.rewritten })
      toast({ title: 'Rewrite approved', description: 'Saved as a personal prompt — it enters rotation as a new prompt. Original untouched.' })
      setRewriteTarget(null)
      setRewriteResult(null)
      fetchScores()
    } catch (err: any) {
      toast({ title: 'Save failed', description: err?.response?.data?.detail || 'Could not save prompt', variant: 'destructive' })
    } finally { setApproveLoading(false) }
  }

  const needsWork = scores.filter(s => s.status === 'needs_work')

  const totalProfit = trades.reduce((sum, t) => sum + t.profit, 0)
  const wins = trades.filter(t => t.profit > 0).length
  const winRate = trades.length > 0 ? (wins / trades.length) * 100 : 0

  return (
    <div className="p-3 sm:p-6 md:p-8">
      <h1 className="font-heading text-xl sm:text-2xl md:text-3xl font-bold mb-4 sm:mb-6 md:mb-8">Trade History</h1>

      <div className="flex flex-wrap gap-2 sm:gap-4 mb-4 sm:mb-6">
        <Select value={hours} onValueChange={setHours}>
          <SelectTrigger className="w-32 sm:w-40 text-sm"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="0">All Time</SelectItem>
            <SelectItem value="24">Last 24h</SelectItem>
            <SelectItem value="168">Last Week</SelectItem>
            <SelectItem value="720">Last Month</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 sm:gap-4 mb-4 sm:mb-6 md:mb-8">
        <Card>
          <CardHeader className="pb-1 px-3 pt-3 sm:px-4 sm:pt-4">
            <CardTitle className="text-[10px] sm:text-xs md:text-sm font-medium text-muted-foreground">Total P&L</CardTitle>
          </CardHeader>
          <CardContent className="px-3 pb-3 sm:px-4 sm:pb-4">
            <div className={`text-sm sm:text-lg md:text-2xl font-bold ${totalProfit >= 0 ? 'text-green-500' : 'text-red-500'}`}>
              ${totalProfit.toFixed(2)}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1 px-3 pt-3 sm:px-4 sm:pt-4">
            <CardTitle className="text-[10px] sm:text-xs md:text-sm font-medium text-muted-foreground">Total Trades</CardTitle>
          </CardHeader>
          <CardContent className="px-3 pb-3 sm:px-4 sm:pb-4">
            <div className="text-sm sm:text-lg md:text-2xl font-bold">{trades.length}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1 px-3 pt-3 sm:px-4 sm:pt-4">
            <CardTitle className="text-[10px] sm:text-xs md:text-sm font-medium text-muted-foreground">Wins</CardTitle>
          </CardHeader>
          <CardContent className="px-3 pb-3 sm:px-4 sm:pb-4">
            <div className="text-sm sm:text-lg md:text-2xl font-bold text-green-500">{wins}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1 px-3 pt-3 sm:px-4 sm:pt-4">
            <CardTitle className="text-[10px] sm:text-xs md:text-sm font-medium text-muted-foreground">Win Rate</CardTitle>
          </CardHeader>
          <CardContent className="px-3 pb-3 sm:px-4 sm:pb-4">
            <div className="text-sm sm:text-lg md:text-2xl font-bold">{winRate.toFixed(1)}%</div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="px-3 sm:px-4 md:px-6 pt-3 sm:pt-4 md:pt-6 pb-2 sm:pb-3">
          <CardTitle className="text-sm sm:text-base md:text-lg">Closed Trades</CardTitle>
        </CardHeader>
        <CardContent className="px-3 sm:px-4 md:px-6 pb-3 sm:pb-4 md:pb-6">
          {loading ? (
            <p className="text-muted-foreground text-center py-6 sm:py-8 text-sm">Loading...</p>
          ) : trades.length === 0 ? (
            <p className="text-muted-foreground text-center py-6 sm:py-8 text-sm">No closed trades found</p>
          ) : (
            <div className="overflow-x-auto -mx-3 sm:mx-0">
              <table className="w-full text-xs sm:text-sm">
                <thead>
                  <tr className="border-b border-border">
                    <th className="text-left py-2 px-1 sm:px-2 font-medium text-muted-foreground">Time</th>
                    <th className="text-left py-2 px-1 sm:px-2 font-medium text-muted-foreground">Symbol</th>
                    <th className="text-left py-2 px-1 sm:px-2 font-medium text-muted-foreground hidden sm:table-cell">Dir</th>
                    <th className="text-right py-2 px-1 sm:px-2 font-medium text-muted-foreground">Vol</th>
                    <th className="text-right py-2 px-1 sm:px-2 font-medium text-muted-foreground hidden md:table-cell">Price</th>
                    <th className="text-right py-2 px-1 sm:px-2 font-medium text-muted-foreground">P&L</th>
                    <th className="text-left py-2 px-1 sm:px-2 font-medium text-muted-foreground hidden lg:table-cell">Comment</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((trade) => (
                    <tr key={trade.ticket} className="border-b border-border/50 hover:bg-muted/50">
                      <td className="py-2 px-1 sm:px-2 text-[10px] sm:text-xs whitespace-nowrap">{trade.time}</td>
                      <td className="py-2 px-1 sm:px-2 font-medium">{trade.symbol}</td>
                      <td className={`py-2 px-1 sm:px-2 hidden sm:table-cell ${trade.direction === 'BUY' ? 'text-green-500' : 'text-red-500'}`}>{trade.direction}</td>
                      <td className="py-2 px-1 sm:px-2 text-right">{trade.volume}</td>
                      <td className="py-2 px-1 sm:px-2 text-right hidden md:table-cell">{(trade.price || 0).toFixed(5)}</td>
                      <td className={`py-2 px-1 sm:px-2 text-right font-medium ${(trade.profit ?? 0) >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                        ${(trade.profit ?? 0).toFixed(2)}
                      </td>
                      <td className="py-2 px-1 sm:px-2 text-xs text-muted-foreground truncate max-w-[80px] sm:max-w-[150px] hidden lg:table-cell">{trade.comment}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="mt-4 sm:mt-6">
        <CardHeader className="px-3 sm:px-4 md:px-6 pt-3 sm:pt-4 md:pt-6 pb-2 sm:pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-sm sm:text-base md:text-lg">Strategy Analytics</CardTitle>
            <button onClick={() => { fetchScores(); fetchInsights() }} className="text-xs text-muted-foreground hover:text-foreground" disabled={scoresLoading}>
              {scoresLoading ? '...' : 'Refresh'}
            </button>
          </div>
        </CardHeader>
        <CardContent className="px-3 sm:px-4 md:px-6 pb-3 sm:pb-4 md:pb-6">
          {scores.length === 0 ? (
            <p className="text-muted-foreground text-center py-4 text-sm">
              {scoresLoading ? 'Loading...' : 'No strategy data yet. Scores are calculated hourly from closed trades.'}
            </p>
          ) : (
            <div className="overflow-x-auto -mx-3 sm:mx-0">
              <table className="w-full text-xs sm:text-sm">
                <thead>
                  <tr className="border-b border-border">
                    <th className="text-left py-2 px-1 sm:px-2 font-medium text-muted-foreground">Prompt</th>
                    <th className="text-left py-2 px-1 sm:px-2 font-medium text-muted-foreground">Symbol</th>
                    <th className="text-left py-2 px-1 sm:px-2 font-medium text-muted-foreground hidden sm:table-cell">Status</th>
                    <th className="text-right py-2 px-1 sm:px-2 font-medium text-muted-foreground">Trades</th>
                    <th className="text-right py-2 px-1 sm:px-2 font-medium text-muted-foreground">Win Rate</th>
                    <th className="text-right py-2 px-1 sm:px-2 font-medium text-muted-foreground">P&L</th>
                    <th className="text-right py-2 px-1 sm:px-2 font-medium text-muted-foreground hidden md:table-cell">Avg Profit</th>
                    <th className="text-right py-2 px-1 sm:px-2 font-medium text-muted-foreground hidden md:table-cell">Avg Loss</th>
                  </tr>
                </thead>
                <tbody>
                  {scores.map((s, idx) => (
                    <tr key={idx} className="border-b border-border/50 hover:bg-muted/50">
                      <td className="py-2 px-1 sm:px-2 text-[10px] sm:text-xs max-w-[120px] sm:max-w-[200px] truncate" title={s.prompt_text}>
                        {s.prompt_text.substring(0, 40)}...
                      </td>
                      <td className="py-2 px-1 sm:px-2 font-medium">{s.symbol}</td>
                      <td className="py-2 px-1 sm:px-2 hidden sm:table-cell">
                        {s.status && (
                          <span className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-medium ${STATUS_BADGE[s.status]?.cls || STATUS_BADGE.neutral.cls}`}>
                            {STATUS_BADGE[s.status]?.label || s.status}
                          </span>
                        )}
                      </td>
                      <td className="py-2 px-1 sm:px-2 text-right">{s.total_trades}</td>
                      <td className={`py-2 px-1 sm:px-2 text-right font-medium ${s.win_rate >= 50 ? 'text-green-500' : 'text-red-500'}`}>
                        {s.win_rate.toFixed(1)}%
                      </td>
                      <td className={`py-2 px-1 sm:px-2 text-right font-medium ${s.total_pnl >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                        ${s.total_pnl.toFixed(2)}
                      </td>
                      <td className="py-2 px-1 sm:px-2 text-right text-green-500 hidden md:table-cell">
                        {s.avg_profit ? `$${s.avg_profit.toFixed(2)}` : '-'}
                      </td>
                      <td className="py-2 px-1 sm:px-2 text-right text-red-500 hidden md:table-cell">
                        {s.avg_loss ? `$${s.avg_loss.toFixed(2)}` : '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4 sm:mt-6">
        <Card>
          <CardHeader className="px-3 sm:px-4 md:px-6 pt-3 sm:pt-4 md:pt-6 pb-2 sm:pb-3">
            <CardTitle className="text-sm sm:text-base md:text-lg">Model Performance</CardTitle>
          </CardHeader>
          <CardContent className="px-3 sm:px-4 md:px-6 pb-3 sm:pb-4 md:pb-6">
            {modelPerf.length === 0 ? (
              <p className="text-muted-foreground text-center py-4 text-sm">
                No closed trades with model info yet.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs sm:text-sm">
                  <thead>
                    <tr className="border-b border-border">
                      <th className="text-left py-2 px-1 sm:px-2 font-medium text-muted-foreground">Provider</th>
                      <th className="text-left py-2 px-1 sm:px-2 font-medium text-muted-foreground">Model</th>
                      <th className="text-right py-2 px-1 sm:px-2 font-medium text-muted-foreground">Trades</th>
                      <th className="text-right py-2 px-1 sm:px-2 font-medium text-muted-foreground">Win Rate</th>
                      <th className="text-right py-2 px-1 sm:px-2 font-medium text-muted-foreground">P&L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {modelPerf.map((m, idx) => (
                      <tr key={idx} className="border-b border-border/50 hover:bg-muted/50">
                        <td className="py-2 px-1 sm:px-2 font-medium">{m.provider}</td>
                        <td className="py-2 px-1 sm:px-2 text-[10px] sm:text-xs truncate max-w-[140px]" title={m.model}>{m.model}</td>
                        <td className="py-2 px-1 sm:px-2 text-right">{m.trades}</td>
                        <td className={`py-2 px-1 sm:px-2 text-right font-medium ${m.win_rate >= 50 ? 'text-green-500' : 'text-red-500'}`}>
                          {m.win_rate.toFixed(1)}%
                        </td>
                        <td className={`py-2 px-1 sm:px-2 text-right font-medium ${m.total_pnl >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                          ${m.total_pnl.toFixed(2)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="px-3 sm:px-4 md:px-6 pt-3 sm:pt-4 md:pt-6 pb-2 sm:pb-3">
            <CardTitle className="text-sm sm:text-base md:text-lg">30-Day Win Rate</CardTitle>
          </CardHeader>
          <CardContent className="px-3 sm:px-4 md:px-6 pb-3 sm:pb-4 md:pb-6">
            {timeline.length === 0 ? (
              <p className="text-muted-foreground text-center py-4 text-sm">
                No closed trades in the last 30 days.
              </p>
            ) : (
              <div className="h-[220px] w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={timeline} margin={{ top: 5, right: 10, left: -20, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                    <XAxis dataKey="date" tick={{ fontSize: 10 }} tickFormatter={(d: string) => d.slice(5)} />
                    <YAxis domain={[0, 100]} tick={{ fontSize: 10 }} unit="%" />
                    <Tooltip formatter={(v: any) => [`${v}%`, 'Win rate']} labelFormatter={(l: any) => `Date: ${l}`} />
                    <Line type="monotone" dataKey="win_rate" stroke="#22c55e" strokeWidth={2} dot={false} name="Win rate" />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {ragHealth && (
        <Card className="mt-4 sm:mt-6">
          <CardHeader className="px-3 sm:px-4 md:px-6 pt-3 sm:pt-4 md:pt-6 pb-2 sm:pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-sm sm:text-base md:text-lg">🧠 RAG Health</CardTitle>
              <button
                onClick={() => setRagOpen(!ragOpen)}
                className="text-xs text-muted-foreground hover:text-foreground"
              >
                {ragOpen ? 'Hide' : 'Show'}
              </button>
            </div>
          </CardHeader>
          {ragOpen && (
            <CardContent className="px-3 sm:px-4 md:px-6 pb-3 sm:pb-4 md:pb-6">
              <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                <div className="rounded-md border border-border bg-muted/30 p-3">
                  <p className="text-xs font-medium text-muted-foreground mb-2">Config</p>
                  <dl className="space-y-1 text-xs">
                    {[
                      ['Similar (X)', ragHealth.config.similar_count],
                      ['Top (Y)', ragHealth.config.top_count],
                      ['Losers (Z)', ragHealth.config.losers_count],
                      ['Embedding model', ragHealth.config.embedding_model],
                      ['Code-block stripping', ragHealth.config.code_block_stripping ? 'ON' : 'OFF'],
                      ['Min trades — winner/best', ragHealth.config.min_trades_for_best],
                      ['Min trades — flag', ragHealth.config.min_trades_for_flag],
                      ['Flag threshold', `${(ragHealth.config.flag_threshold * 100).toFixed(0)}%`],
                      ['Min embeddings/symbol', ragHealth.config.min_embeddings_per_symbol],
                      ['Sim variance min', ragHealth.config.sim_variance_min],
                    ].map(([k, v], i) => (
                      <div key={i} className="flex justify-between gap-2">
                        <dt className="text-muted-foreground">{k}</dt>
                        <dd className="font-medium text-right">{v}</dd>
                      </div>
                    ))}
                  </dl>
                  <p className="text-xs font-medium text-muted-foreground mt-3 mb-1">
                    Last [RAG] calls ({ragHealth.config.recent_rag_logs.length})
                  </p>
                  <div className="max-h-40 overflow-y-auto space-y-1 pr-1">
                    {ragHealth.config.recent_rag_logs.length === 0 ? (
                      <p className="text-muted-foreground text-xs">No RAG calls logged yet.</p>
                    ) : ragHealth.config.recent_rag_logs.map((l, i) => (
                      <div key={i} className="text-[10px] text-muted-foreground border-b border-border/50 pb-1">
                        <span className="text-foreground font-medium">{l.symbol}</span>
                        {': '}{l.similar_count} similar, {l.top_count} top, {l.losers_count} losers
                        {' · '}{l.context_chars} chars
                        {l.created_at ? ` · ${l.created_at.slice(11, 16)} UTC` : ''}
                      </div>
                    ))}
                  </div>
                </div>

                <div className="rounded-md border border-border bg-muted/30 p-3">
                  <p className="text-xs font-medium text-muted-foreground mb-2">Per-Symbol Stats</p>
                  {ragHealth.per_symbol_stats.length === 0 ? (
                    <p className="text-muted-foreground text-xs">No data yet.</p>
                  ) : (
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead>
                          <tr className="border-b border-border">
                            <th className="text-left py-1 pr-2 font-medium text-muted-foreground">Symbol</th>
                            <th className="text-right py-1 px-2 font-medium text-muted-foreground">Embeds</th>
                            <th className="text-right py-1 px-2 font-medium text-muted-foreground">Avg WR</th>
                            <th className="text-right py-1 pl-2 font-medium text-muted-foreground">Prompts</th>
                          </tr>
                        </thead>
                        <tbody>
                          {ragHealth.per_symbol_stats.map((r) => (
                            <tr key={r.symbol} className="border-b border-border/50">
                              <td className="py-1 pr-2 font-medium">{r.symbol}</td>
                              <td className="py-1 px-2 text-right">{r.total_embeddings}</td>
                              <td className={`py-1 px-2 text-right font-medium ${r.avg_win_rate == null ? 'text-muted-foreground' : r.avg_win_rate >= 0.5 ? 'text-green-500' : 'text-red-500'}`}>
                                {r.avg_win_rate == null ? '—' : `${(r.avg_win_rate * 100).toFixed(0)}%`}
                              </td>
                              <td className="py-1 pl-2 text-right">{r.distinct_prompts}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>

                <div className="rounded-md border border-border bg-muted/30 p-3">
                  <p className="text-xs font-medium text-muted-foreground mb-2">
                    Active Plateaus ({ragHealth.plateaus.length})
                  </p>
                  {ragHealth.plateaus.length === 0 ? (
                    <p className="text-xs text-green-500">All clear — no plateaus detected.</p>
                  ) : (
                    <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
                      {ragHealth.plateaus.map((p, i) => (
                        <div key={i} className="rounded border border-red-500/20 bg-red-500/5 p-2">
                          <span className="inline-block rounded bg-red-500/15 text-red-500 px-1.5 py-0.5 text-[10px] font-medium">
                            {PLATEAU_LABEL[p.type] || p.type}{p.symbol ? ` · ${p.symbol}` : ''}
                          </span>
                          <p className="mt-1 text-[11px] text-muted-foreground">{p.reason}</p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </CardContent>
          )}
        </Card>
      )}

      {needsWork.length > 0 && (
        <Card className="mt-4 sm:mt-6 border-red-500/30">
          <CardHeader className="px-3 sm:px-4 md:px-6 pt-3 sm:pt-4 md:pt-6 pb-2 sm:pb-3">
            <CardTitle className="text-sm sm:text-base md:text-lg text-red-500">
              Needs Improvement ({needsWork.length})
            </CardTitle>
          </CardHeader>
          <CardContent className="px-3 sm:px-4 md:px-6 pb-3 sm:pb-4 md:pb-6">
            <p className="text-xs text-muted-foreground mb-3">
              Prompts with 5+ trades and a win rate under 40%. Let AI propose a rewritten version —
              you review it side by side, and only your approval saves it as a NEW personal prompt
              (the original is never replaced).
            </p>
            <div className="space-y-3">
              {needsWork.map((s, idx) => (
                <div key={idx} className="rounded-md border border-red-500/20 bg-red-500/5 p-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <p className="text-xs text-muted-foreground truncate" title={s.prompt_text}>
                        {s.symbol} · {s.prompt_text}
                      </p>
                      <p className="mt-1 text-xs">
                        <span className="text-red-500 font-medium">{s.win_rate.toFixed(1)}% win rate</span>
                        <span className="text-muted-foreground"> · {s.winning_trades}/{s.total_trades} wins · ${s.total_pnl.toFixed(2)} P&L</span>
                      </p>
                    </div>
                    <Button
                      size="sm"
                      variant="outline"
                      className="text-xs shrink-0"
                      onClick={() => handleRewrite(s)}
                      disabled={rewriteLoading && rewriteTarget?.prompt_text === s.prompt_text}
                    >
                      {rewriteLoading && rewriteTarget?.prompt_text === s.prompt_text ? 'Rewriting...' : 'Suggest Rewrite'}
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      <Dialog open={!!rewriteTarget} onOpenChange={(open) => { if (!open && !approveLoading) { setRewriteTarget(null); setRewriteResult(null) } }}>
        <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>AI Prompt Rewrite Suggestion</DialogTitle>
          </DialogHeader>
          {rewriteLoading ? (
            <p className="text-sm text-muted-foreground py-6 text-center">
              Analyzing losing trades and generating a rewrite...
            </p>
          ) : rewriteResult ? (
            <div className="space-y-4">
              <div className="rounded-md border border-border bg-muted/40 p-3">
                <p className="text-xs font-medium text-muted-foreground mb-1">
                  STATS: {rewriteResult.stats.winning_trades}/{rewriteResult.stats.total_trades} wins
                  ({rewriteResult.stats.win_rate.toFixed(1)}%) · ${rewriteResult.stats.total_pnl.toFixed(2)} P&L
                  · analyzed {rewriteResult.losers_analyzed} losing, {rewriteResult.winners_analyzed} winning trades
                </p>
              </div>
              <div>
                <p className="text-xs font-medium text-red-500 mb-1">ORIGINAL (underperforming — not replaced)</p>
                <div className="rounded-md border border-red-500/20 bg-red-500/5 p-3 text-xs whitespace-pre-wrap max-h-48 overflow-y-auto">
                  {rewriteResult.original}
                </div>
              </div>
              <div>
                <p className="text-xs font-medium text-green-500 mb-1">PROPOSED REWRITE (saved only if you approve)</p>
                <div className="rounded-md border border-green-500/20 bg-green-500/5 p-3 text-xs whitespace-pre-wrap max-h-48 overflow-y-auto">
                  {rewriteResult.rewritten}
                </div>
              </div>
              <DialogFooter className="gap-2">
                <Button variant="outline" size="sm" onClick={() => { setRewriteTarget(null); setRewriteResult(null) }} disabled={approveLoading}>
                  Dismiss
                </Button>
                <Button size="sm" onClick={handleApprove} disabled={approveLoading}>
                  {approveLoading ? 'Saving...' : 'Approve & Save as Personal Prompt'}
                </Button>
              </DialogFooter>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
    </div>
  )
}
