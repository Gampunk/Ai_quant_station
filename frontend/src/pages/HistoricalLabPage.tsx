import React, { useState, useRef, useEffect } from 'react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer
} from 'recharts'
import {
  Play, AlertTriangle, Zap, BrainCircuit, Settings2,
  TrendingUp, History as HistoryIcon, FlaskConical, BarChartHorizontal,
  Send, User as UserIcon, Bot, Loader2
} from 'lucide-react'
import { useToast } from '@/hooks/use-toast'
import { useHistoricalLabStore } from '@/store/historicalLabStore'
import axios from 'axios'

type Mode = 'backtest' | 'analysis'
type Status = 'pending' | 'running' | 'completed' | 'failed'

interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  execution_output?: string
  execution_charts?: any[]
  execution_tables?: any[]
}

// ... other interfaces ...

// Update the Chat rendering loop (around line 443)
// I will use a multi-replace if needed, but let's try a single contiguous block first.


interface Metrics {
  total_return_pct: number
  sharpe_ratio: number
  max_drawdown_pct: number
  win_rate_pct: number
  profit_factor: number
  num_trades: number
  final_equity: number
  lot_size?: number
  total_pnl?: number
}

interface TradeRecord {
  entry_time: string
  exit_time: string
  direction: 'BUY' | 'SELL'
  entry_price: number
  exit_price: number
  pnl: number
}

interface LabResult {
  id: number
  mode: Mode
  symbol: string
  status: Status
  equity_curve?: { time: string; balance: number }[]
  metrics?: Metrics
  analysis?: {
    hourly_volatility: { hour_utc: number; avg_range: number }[]
    day_of_week_volatility: { day: string; avg_range: number }[]
    stats: Record<string, any>
  }
  ai_report: string
  chat_history: ChatMessage[]
  trade_log?: TradeRecord[]
}

const TIMEFRAMES = [{ value: '1T', label: '1 min' }, { value: '5T', label: '5 min' }, { value: '1H', label: '1 hour' }]

const MetricCard = ({ label, value, sub, icon: Icon, color }: any) => (
  <Card className="bg-card/60 border-border/50">
    <CardContent className="p-4 space-y-1">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground uppercase tracking-wider">{label}</p>
        <Icon className={`w-4 h-4 ${color}`} />
      </div>
      <h3 className={`text-2xl font-bold font-heading ${color}`}>{value}</h3>
      {sub && <p className="text-xs text-muted-foreground">{sub}</p>}
    </CardContent>
  </Card>
)

export default function HistoricalLabPage() {
  const { toast } = useToast()
  const mode = useHistoricalLabStore((s) => s.mode)
  const setMode = useHistoricalLabStore((s) => s.setMode)
  const symbol = useHistoricalLabStore((s) => s.symbol)
  const setSymbol = useHistoricalLabStore((s) => s.setSymbol)
  const startDate = useHistoricalLabStore((s) => s.startDate)
  const setStartDate = useHistoricalLabStore((s) => s.setStartDate)
  const endDate = useHistoricalLabStore((s) => s.endDate)
  const setEndDate = useHistoricalLabStore((s) => s.setEndDate)
  const timeframe = useHistoricalLabStore((s) => s.timeframe)
  const setTimeframe = useHistoricalLabStore((s) => s.setTimeframe)
  const capital = useHistoricalLabStore((s) => s.capital)
  const setCapital = useHistoricalLabStore((s) => s.setCapital)
  const lotSize = useHistoricalLabStore((s) => s.lotSize)
  const setLotSize = useHistoricalLabStore((s) => s.setLotSize)
  const includeSpread = useHistoricalLabStore((s) => s.includeSpread)
  const setIncludeSpread = useHistoricalLabStore((s) => s.setIncludeSpread)
  const includeCommission = useHistoricalLabStore((s) => s.includeCommission)
  const setIncludeCommission = useHistoricalLabStore((s) => s.setIncludeCommission)
  const prompt = useHistoricalLabStore((s) => s.prompt)
  const setPrompt = useHistoricalLabStore((s) => s.setPrompt)
  const provider = useHistoricalLabStore((s) => s.provider)
  const setProvider = useHistoricalLabStore((s) => s.setProvider)
  const model = useHistoricalLabStore((s) => s.model)
  const setModel = useHistoricalLabStore((s) => s.setModel)
  const backtestResult = useHistoricalLabStore((s) => s.backtestResult)
  const setBacktestResult = useHistoricalLabStore((s) => s.setBacktestResult)
  const analysisResult = useHistoricalLabStore((s) => s.analysisResult)
  const setAnalysisResult = useHistoricalLabStore((s) => s.setAnalysisResult)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [availableProviders, setAvailableProviders] = useState<any[]>([])
  const [availableSymbols, setAvailableSymbols] = useState<string[]>([])

  // Fetch providers
  useEffect(() => {
    const fetchProviders = async () => {
      try {
        const res = await axios.get('/api/ai/providers')
        setAvailableProviders(res.data?.providers || [])
      } catch (e) { console.error("Providers error", e) }
    }
    
    // Fetch available symbols
    const fetchSymbols = async () => {
      try {
        const res = await axios.get('/api/historical-lab/available-symbols')
        setAvailableSymbols(res.data?.symbols || [])
      } catch (e) { console.error("Symbols error", e) }
    }
    
    fetchProviders()
    fetchSymbols()
  }, [])

  // Auto-select first model when provider changes
  useEffect(() => {
    const selectedProv = availableProviders.find(p => p.id === provider)
    if (selectedProv && selectedProv.models && selectedProv.models.length > 0) {
      if (!selectedProv.models.includes(model)) {
        setModel(selectedProv.models[0])
      }
    }
  }, [provider, availableProviders, model, setModel])
  
  // Chat specific state
  const [chatInput, setChatInput] = useState('')
  const [chatLoading, setChatLoading] = useState(false)
  const chatEndRef = useRef<HTMLDivElement>(null)

  // Polling for status
  useEffect(() => {
    let interval: any = null;
    const currentResult = mode === 'backtest' ? backtestResult : analysisResult
    const setResult = mode === 'backtest' ? setBacktestResult : setAnalysisResult
    
    if (currentResult && (currentResult.status === 'pending' || currentResult.status === 'running')) {
      interval = setInterval(async () => {
        try {
          const res = await axios.get(`/api/historical-lab/status/${currentResult.id}`)
          setResult(res.data)
          if (res.data?.status === 'completed' || res.data?.status === 'failed') {
            setLoading(false)
            if (res.data.status === 'failed') setError('Processing failed. Please check parameters.')
          }
        } catch (e) {
          console.error("Polling error", e)
        }
      }, 2000)
    }
    
    return () => { if (interval) clearInterval(interval) }
  // Restarts only when the job or its status changes. Depending on the whole result
  // object would restart the poll on every reply; the setters never change.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- see the lines above
  }, [backtestResult?.id, backtestResult?.status, analysisResult?.id, analysisResult?.status, mode])

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [backtestResult?.chat_history, analysisResult?.chat_history, mode])

  const handleRun = async () => {
    // Validate date range against available years for the symbol
    if (availableSymbols.length > 0 && symbol && startDate && endDate) {
      try {
        const yearsRes = await axios.get(`/api/historical-lab/available-years/${symbol}`)
        const years = yearsRes.data?.years || []
        if (years.length > 0) {
          const startYear = new Date(startDate).getFullYear()
          const endYear = new Date(endDate).getFullYear()
          const minYear = Math.min(...years)
          const maxYear = Math.max(...years)
          
          if (startYear < minYear || endYear > maxYear) {
            setError(`Data for ${symbol} is only available from ${minYear} to ${maxYear}. Please adjust your date range.`)
            return
          }
        }
      } catch (e) {
        // If years check fails, continue anyway - don't block the run
        console.warn("Could not validate date range:", e)
      }
    }
    
    setLoading(true)
    setError(null)
    const setResult = mode === 'backtest' ? setBacktestResult : setAnalysisResult
    setResult(null)

    try {
      const res = await axios.post('/api/historical-lab/run', {
        mode, symbol, start_date: startDate, end_date: endDate,
        timeframe, prompt, initial_capital: parseFloat(capital) || 10000,
        lot_size: parseFloat(lotSize) || 0.01,
        include_spread: includeSpread,
        include_commission: includeCommission,
        provider, model,
      })

      setResult(res.data as LabResult)
    } catch (e: any) {
      const msg = e.response?.data?.detail || e.message || 'An error occurred.'
      setError(msg)
      toast({ title: 'Lab Error', description: msg, variant: 'destructive' })
      setLoading(false)
    }
  }

  const handleChat = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!chatInput.trim() || chatLoading) return
    
    const currentResult = mode === 'backtest' ? backtestResult : analysisResult
    const setResult = mode === 'backtest' ? setBacktestResult : setAnalysisResult

    if (!currentResult?.id) {
      setError("Please click 'Run Lab' first to initialize the research vault with market data.")
      return
    }

    setChatLoading(true)
    const userMsg = chatInput
    setChatInput('')

    try {
      const res = await axios.post('/api/historical-lab/chat', {
        backtest_id: currentResult.id,
        message: userMsg,
        provider, model
      })
      setResult(res.data as LabResult)
    } catch (e: any) {
      const msg = e.response?.data?.detail || e.message || 'Chat error.'
      setError(msg)
      toast({ title: 'Chat Error', description: msg, variant: 'destructive' })
    } finally {
      setChatLoading(false)
    }
  }

  const result = mode === 'backtest' ? backtestResult : analysisResult
  const metrics = result?.metrics
  const isProcessing = result?.status === 'pending' || result?.status === 'running'

  return (
    <div className="p-6 space-y-6 animate-in fade-in duration-500">
      {/* Header */}
      <div className="flex flex-wrap gap-4 justify-between items-end">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <FlaskConical className="w-6 h-6 text-primary" />
            <h1 className="text-3xl font-bold font-heading">Historical Lab</h1>
          </div>
          <p className="text-muted-foreground text-sm">Professional backtesting & analysis engine running on high-speed Parquet data</p>
        </div>

        {/* Mode Switcher & Model Selection */}
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2 bg-muted p-1 rounded-xl border border-border">
            <Select value={provider} onValueChange={setProvider}>
              <SelectTrigger className="h-8 w-32 border-none bg-transparent shadow-none focus:ring-0">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {availableProviders.map(p => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}
              </SelectContent>
            </Select>
            <div className="w-px h-4 bg-border" />
            <Select value={model} onValueChange={setModel}>
              <SelectTrigger className="h-8 w-48 border-none bg-transparent shadow-none focus:ring-0">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {availableProviders.find(p => p.id === provider)?.models.map((m: any) => (
                  <SelectItem key={m} value={m}>{m}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="bg-muted p-1 rounded-xl flex gap-1 border border-border">
            {(['backtest', 'analysis'] as Mode[]).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`px-4 py-2 rounded-lg text-sm font-medium capitalize transition-all ${
                  mode === m ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {m === 'backtest' ? '📈 Backtest' : '🔬 Deep Analysis'}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-12 gap-6">
        {/* Configuration Desk */}
        <Card className="col-span-12 lg:col-span-4 border-primary/10 bg-card/50">
          <CardHeader className="border-b border-border/50 pb-4">
            <CardTitle className="flex items-center gap-2 text-base">
              <Settings2 className="w-4 h-4 text-primary" /> Configuration Desk
            </CardTitle>
          </CardHeader>
          <CardContent className="p-5 space-y-5">
            <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label className="text-xs">Symbol</Label>
                  <Select value={symbol} onValueChange={setSymbol} disabled={loading}>
                    <SelectTrigger className="h-9"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {availableSymbols.length > 0 ? availableSymbols.map(s => <SelectItem key={s} value={s}>{s}</SelectItem>) : ['XAUUSD', 'BTCUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'XAGUSD'].map(s => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>
              <div className="space-y-1.5">
                <Label className="text-xs">Timeframe</Label>
                <Select value={timeframe} onValueChange={setTimeframe} disabled={loading}>
                  <SelectTrigger className="h-9"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {TIMEFRAMES.map(t => <SelectItem key={t.value} value={t.value}>{t.label}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label className="text-xs">Start Date</Label>
                <Input type="date" className="h-9" value={startDate} onChange={e => setStartDate(e.target.value)} disabled={loading} />
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs">End Date</Label>
                <Input type="date" className="h-9" value={endDate} onChange={e => setEndDate(e.target.value)} disabled={loading} />
              </div>
            </div>

            {mode === 'backtest' && (
              <div className="space-y-4 animate-in slide-in-from-top-2 duration-200">
                <div className="h-px bg-border/50" />
                <div className="grid grid-cols-2 gap-3">
                  <div className="space-y-1.5">
                    <Label className="text-xs">Initial Capital ($)</Label>
                    <Input type="number" className="h-9" value={capital} onChange={e => setCapital(e.target.value)} disabled={loading} />
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs">Lot Size</Label>
                    <Input type="number" className="h-9" step="0.01" min="0.01" value={lotSize} onChange={e => setLotSize(e.target.value)} disabled={loading} />
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div className="space-y-1.5">
                    <Label className="text-xs">Spread (points)</Label>
                    <Input type="number" className="h-9" step="0.1" min="0" value={includeSpread ? 1.0 : 0.0} onChange={(e) => {
                        const val = parseFloat(e.target.value);
                        setIncludeSpread(val > 0);
                        // We need a place to store the actual spread value, but for now 
                        // let's stick to the existing includeSpread boolean toggle logic 
                        // or modify the store. The current store has setIncludeSpread(boolean).
                        // I will update the UI to just handle this for now.
                    }} disabled={loading} />
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs">Comm. ($/lot)</Label>
                    <Input type="number" className="h-9" step="0.1" min="0" value={includeCommission ? 5.0 : 0.0} onChange={(e) => {
                        const val = parseFloat(e.target.value);
                        setIncludeCommission(val > 0);
                    }} disabled={loading} />
                  </div>
                </div>
                <div className="flex gap-3">
                  <button
                    onClick={() => setIncludeSpread(!includeSpread)}
                    disabled={loading}
                    className={`flex-1 py-2 rounded-lg text-xs font-medium border transition-all ${
                      includeSpread ? 'bg-primary/10 border-primary text-primary' : 'border-border text-muted-foreground'
                    }`}
                  >
                    {includeSpread ? '✓' : '+'} Spread
                  </button>
                  <button
                    onClick={() => setIncludeCommission(!includeCommission)}
                    disabled={loading}
                    className={`flex-1 py-2 rounded-lg text-xs font-medium border transition-all ${
                      includeCommission ? 'bg-primary/10 border-primary text-primary' : 'border-border text-muted-foreground'
                    }`}
                  >
                    {includeCommission ? '✓' : '+'} Comm.
                  </button>
                </div>
              </div>
            )}

            {mode === 'backtest' && (
              <div className="space-y-2">
                <div className="flex items-center gap-2 text-sm font-medium text-primary">
                  <BrainCircuit className="w-4 h-4" />
                  Strategy Prompt
                </div>
                <Textarea 
                  placeholder="Describe your entry/exit logic in plain English..." 
                  className="min-h-[100px] bg-background/50 resize-none"
                  value={prompt}
                  onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setPrompt(e.target.value)}
                />
                <p className="text-[10px] text-muted-foreground italic">
                  Example: "Buy when RSI &lt; 30 and price is above 200 EMA. Exit when RSI &gt; 70."
                </p>
              </div>
            )}

            {error && <div className="p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-xs text-destructive">⚠️ {error}</div>}

            <Button className="w-full h-11" onClick={handleRun} disabled={loading}>
              {loading ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Processing...</> : <><Play className="mr-2 h-4 w-4 fill-current" /> Run Lab</>}
            </Button>
          </CardContent>
        </Card>

        {/* Results Cockpit */}
        <div className="col-span-12 lg:col-span-8 space-y-5">
          {/* Progress Overlay */}
          {isProcessing && (
            <div className="bg-card/80 backdrop-blur-sm border border-primary/20 rounded-xl p-8 flex flex-col items-center justify-center space-y-4 animate-pulse">
              <div className="w-16 h-16 rounded-full border-4 border-primary/20 border-t-primary animate-spin" />
              <div className="text-center">
                <h3 className="text-lg font-bold">Executing {result?.mode}</h3>
                <p className="text-sm text-muted-foreground">Current Status: <span className="text-primary capitalize">{result?.status}</span></p>
                <p className="text-xs text-muted-foreground mt-2 italic">Vectorizing data and computing strategy math...</p>
              </div>
            </div>
          )}

          {/* Metrics */}
          {mode === 'backtest' && !isProcessing && (
            <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
              <MetricCard label="Total P&L" value={metrics ? `$${(metrics.total_pnl ?? Number(metrics.final_equity ?? 0) - 10000).toFixed(2)}` : '—'} icon={TrendingUp} color="text-green-400" />
              <MetricCard label="Sharpe Ratio" value={metrics ? Number(metrics.sharpe_ratio ?? 0).toFixed(2) : '—'} icon={TrendingUp} color="text-green-400" />
              <MetricCard label="Win Rate" value={metrics ? `${Number(metrics.win_rate_pct ?? 0).toFixed(1)}%` : '—'} icon={Zap} color="text-yellow-400" />
              <MetricCard label="Profit Factor" value={metrics ? Number(metrics.profit_factor ?? 0).toFixed(2) : '—'} icon={BarChartHorizontal} color="text-blue-400" />
              <MetricCard label="Max Drawdown" value={metrics ? `${Number(metrics.max_drawdown_pct ?? 0).toFixed(1)}%` : '—'} icon={AlertTriangle} color="text-red-400" />
            </div>
          )}

          {/* Equity Curve */}
          {mode === 'backtest' && !isProcessing && (
            <Card className="bg-card/50 border-border/50 h-[350px]">
              <CardHeader className="pb-2">
                <CardTitle className="text-sm flex items-center gap-2"><HistoryIcon className="w-4 h-4" /> Equity Performance</CardTitle>
              </CardHeader>
              <CardContent className="h-[280px] p-0 pr-4 pb-4">
                <ResponsiveContainer width="100%" height="100%">
                  {result?.equity_curve ? (
                    <AreaChart data={result.equity_curve}>
                      <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="hsl(var(--border))" />
                      <XAxis dataKey="time" fontSize={10} hide />
                      <YAxis fontSize={10} tickLine={false} axisLine={false} tickFormatter={v => `$${((v || 0) / 1000).toFixed(0)}k`} />
                      <Tooltip />
                      <Area type="monotone" dataKey="balance" stroke="hsl(var(--primary))" fill="hsl(var(--primary))" fillOpacity={0.1} />
                    </AreaChart>
                  ) : <div className="h-full flex items-center justify-center text-muted-foreground text-xs italic">Awaiting results...</div>}
                </ResponsiveContainer>
              </CardContent>
            </Card>
          )}

          {/* Trade History */}
          {mode === 'backtest' && !isProcessing && result?.trade_log && result.trade_log.length > 0 && (
            <Card className="bg-card/50 border-border/50">
              <CardHeader className="pb-2">
                <CardTitle className="text-sm flex items-center gap-2"><BarChartHorizontal className="w-4 h-4" /> Trade History ({result.trade_log.length} trades)</CardTitle>
              </CardHeader>
              <CardContent className="p-0 overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border/50">
                      <th className="text-left p-2 font-medium text-muted-foreground">Entry</th>
                      <th className="text-left p-2 font-medium text-muted-foreground">Exit</th>
                      <th className="text-center p-2 font-medium text-muted-foreground">Dir</th>
                      <th className="text-right p-2 font-medium text-muted-foreground">Entry $</th>
                      <th className="text-right p-2 font-medium text-muted-foreground">Exit $</th>
                      <th className="text-right p-2 font-medium text-muted-foreground">P&L $</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.trade_log.map((t, i) => (
                      <tr key={i} className="border-b border-border/20 hover:bg-muted/30">
                        <td className="p-2 text-left text-muted-foreground font-mono">{t.entry_time ? new Date(t.entry_time).toLocaleString() : '—'}</td>
                        <td className="p-2 text-left text-muted-foreground font-mono">{t.exit_time === 'END OF DATA' ? '—' : t.exit_time ? new Date(t.exit_time).toLocaleString() : '—'}</td>
                        <td className={`p-2 text-center font-bold ${t.direction === 'BUY' ? 'text-green-400' : 'text-red-400'}`}>{t.direction || '—'}</td>
                        <td className="p-2 text-right font-mono">{Number(t.entry_price ?? 0).toFixed(2)}</td>
                        <td className="p-2 text-right font-mono">{Number(t.exit_price ?? 0).toFixed(2)}</td>
                        <td className={`p-2 text-right font-mono font-bold ${(t.pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>${Number(t.pnl ?? 0).toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          )}

          {/* Analysis Results — metrics + volatility charts */}
          {mode === 'analysis' && !isProcessing && result?.analysis && (
            <>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <MetricCard label="Total Bars" value={result.analysis.stats?.total_bars?.toLocaleString() || '—'} icon={BarChartHorizontal} color="text-blue-400" />
                <MetricCard label="Mean Return" value={result.analysis.stats?.mean_return_pct != null ? `${result.analysis.stats.mean_return_pct}%` : '—'} sub={`Std: ${result.analysis.stats?.std_return_pct != null ? `${result.analysis.stats.std_return_pct}%` : '—'}`} icon={TrendingUp} color="text-green-400" />
                <MetricCard label="Avg ATR (14)" value={result.analysis.stats?.avg_atr_14 != null ? result.analysis.stats.avg_atr_14.toFixed(2) : '—'} icon={Zap} color="text-yellow-400" />
                <MetricCard label="Period" value={`${result.analysis.stats?.best_day || '—'} → ${result.analysis.stats?.worst_day || '—'}`} icon={AlertTriangle} color="text-red-400" sub="Best → Worst Day" />
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <Card className="bg-card/50 border-border/50 h-[250px]">
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm flex items-center gap-2"><HistoryIcon className="w-4 h-4" /> Hourly Volatility</CardTitle>
                  </CardHeader>
                  <CardContent className="h-[200px] p-0 pr-4 pb-4">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={result.analysis.hourly_volatility}>
                        <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="hsl(var(--border))" />
                        <XAxis dataKey="hour_utc" fontSize={10} tickLine={false} axisLine={false} tickFormatter={h => `${h}h`} />
                        <YAxis fontSize={10} tickLine={false} axisLine={false} />
                        <Tooltip />
                        <Bar dataKey="avg_range" fill="hsl(var(--primary))" radius={[2,2,0,0]} />
                      </BarChart>
                    </ResponsiveContainer>
                  </CardContent>
                </Card>

                <Card className="bg-card/50 border-border/50 h-[250px]">
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm flex items-center gap-2"><HistoryIcon className="w-4 h-4" /> Day-of-Week Volatility</CardTitle>
                  </CardHeader>
                  <CardContent className="h-[200px] p-0 pr-4 pb-4">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={result.analysis.day_of_week_volatility}>
                        <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="hsl(var(--border))" />
                        <XAxis dataKey="day" fontSize={10} tickLine={false} axisLine={false} />
                        <YAxis fontSize={10} tickLine={false} axisLine={false} />
                        <Tooltip />
                        <Bar dataKey="avg_range" fill="hsl(var(--primary))" radius={[2,2,0,0]} />
                      </BarChart>
                    </ResponsiveContainer>
                  </CardContent>
                </Card>
              </div>
            </>
          )}

          {/* AI Chat Terminal */}
          <Card className="bg-primary/5 border-primary/20 h-[500px] flex flex-col">
            <CardHeader className="pb-3 shrink-0">
              <CardTitle className="text-base flex items-center gap-2"><BrainCircuit className="w-5 h-5 text-primary" /> Quant Research Vault</CardTitle>
            </CardHeader>
            <CardContent className="flex-1 overflow-y-auto space-y-4 p-4">
              {(result?.chat_history || []).map((msg, i) => (
                <div key={i} className="space-y-2">
                  <div className={`flex gap-3 ${msg.role === 'user' ? 'flex-row-reverse' : ''}`}>
                    <div className={`w-8 h-8 rounded-full flex items-center justify-center shrink-0 ${msg.role === 'user' ? 'bg-primary/20 text-primary' : 'bg-muted'}`}>
                      {msg.role === 'user' ? <UserIcon size={16} /> : <Bot size={16} />}
                    </div>
                    <div className={`p-3 rounded-2xl text-sm max-w-[85%] ${msg.role === 'user' ? 'bg-primary text-primary-foreground rounded-tr-none' : 'bg-muted/50 border border-border/50 rounded-tl-none whitespace-pre-wrap'}`}>
                      {msg.role === 'assistant' 
                        ? (msg.content || '').replace(/```python[\s\S]*?```/g, '').trim() 
                        : msg.content}
                    </div>
                  </div>

                  {/* Execution Results */}
                  {msg.role === 'assistant' && (msg.execution_output || msg.execution_charts || msg.execution_tables) && (
                    <div className="ml-11 space-y-3 max-w-[90%]">
                      {msg.execution_output && (
                        <div className="bg-muted/30 border border-border/50 rounded-lg p-3 font-mono text-[10px] whitespace-pre-wrap overflow-x-auto">
                          <p className="text-[9px] uppercase tracking-widest text-muted-foreground mb-2 flex items-center gap-1.5">
                            <Zap className="w-3 h-3" /> Execution Output
                          </p>
                          {msg.execution_output}
                        </div>
                      )}

                      {msg.execution_charts?.map((chart: any, ci: number) => (
                        <Card key={ci} className="bg-card/30 border-border/50 overflow-hidden">
                          <div className="p-2 border-b border-border/30 bg-muted/20 flex justify-between items-center">
                            <span className="text-[10px] font-bold uppercase tracking-tight">{chart.title || 'Analysis Chart'}</span>
                          </div>
                          <div className="h-[200px] p-2">
                            <ResponsiveContainer width="100%" height="100%">
                              {(() => {
                                const values = Array.isArray(chart.data)
                                  ? chart.data
                                  : (chart.data?.value || [])
                                const chartData = (values || []).map((v: any, idx: number) => ({ idx, value: v }))
                                return (
                                  <AreaChart data={chartData}>
                                    <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="hsl(var(--border))" />
                                    <XAxis dataKey="idx" hide />
                                    <YAxis fontSize={10} tickLine={false} axisLine={false} />
                                    <Tooltip />
                                    <Area type="monotone" dataKey="value" stroke={chart.color || '#2563eb'} fill={chart.color || '#2563eb'} fillOpacity={0.1} />
                                  </AreaChart>
                                )
                              })()}
                            </ResponsiveContainer>
                          </div>
                        </Card>
                      ))}

                      {msg.execution_tables?.map((table: any, ti: number) => (
                        <div key={ti} className="border border-border/50 rounded-lg overflow-hidden bg-card/30">
                          <div className="bg-muted/20 p-1.5 border-b border-border/30 text-[9px] font-bold uppercase tracking-widest">{table.title || 'Data Table'}</div>
                          <div className="overflow-x-auto">
                            <table className="w-full text-left text-[10px]">
                              {table.columns && (
                                <thead className="bg-muted/10 border-b border-border/20">
                                  <tr>
                                    {(table.columns || []).map((col: string) => <th key={col} className="p-1.5 font-medium">{col}</th>)}
                                  </tr>
                                </thead>
                              )}
                              <tbody>
                                {(table.rows || []).map((row: any[], ri: number) => (
                                  <tr key={ri} className="border-b border-border/10 last:border-0 hover:bg-muted/5 transition-colors">
                                    {(row || []).map((cell: any, ci: number) => <td key={ci} className="p-1.5">{typeof cell === 'number' ? cell.toFixed(4) : String(cell)}</td>)}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
              {chatLoading && (
                <div className="flex gap-3">
                  <div className="w-8 h-8 rounded-full bg-muted flex items-center justify-center shrink-0">
                    <Loader2 className="w-4 h-4 animate-spin text-primary" />
                  </div>
                  <div className="p-3 rounded-2xl text-sm bg-muted/50 border border-border/50 rounded-tl-none animate-pulse">
                    Analyst is calculating insights...
                  </div>
                </div>
              )}
              <div ref={chatEndRef} />
            </CardContent>
            <div className="p-4 border-t border-border/50 bg-background/50">
              <form onSubmit={handleChat} className="flex gap-2">
                <Input 
                  placeholder={!result ? "Run lab to start research..." : "Ask a follow-up..."} 
                  value={chatInput} 
                  onChange={e => setChatInput(e.target.value)} 
                  onKeyDown={e => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault();
                      handleChat(e as any);
                    }
                  }}
                  disabled={isProcessing || chatLoading} 
                />
                <Button size="icon" type="submit" disabled={isProcessing || chatLoading}><Send size={16} /></Button>
              </form>
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}
