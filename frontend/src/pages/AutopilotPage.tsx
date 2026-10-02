import { useState, useEffect } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useAutopilotStore } from '@/store/autopilotStore'
import { useToast } from '@/hooks/use-toast'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import axios from 'axios'

interface LogEntry {
  timestamp: string
  level: string
  message: string
}

interface LogHistoryEntry {
  id: number
  timestamp: string
  level: string
  message: string
  cycle_number: number | null
}

interface AutopilotStatus {
  enabled: boolean
  running: boolean
  settings: {
    enabled: boolean
    interval_seconds: number
    default_lot: number
    max_trades_per_day: number
    cooldown_minutes: number
    max_daily_loss: number
    mt5_terminal_path: string | null
    mt5_connector_url: string | null
    symbol: string
    provider: string
    model: string
    mt5_connected: boolean
  } | null
  stats: {
    total_runs: number
    trades_executed: number
    skipped_count: number
    error_count: number
    last_run: string | null
  }
  logs: LogEntry[]
}

interface PromptStatsItem {
  prompt_number: number
  prompt_text: string
  total_trades: number
  wins: number
  losses: number
  win_rate: number
  total_profit: number
  avg_profit: number
  display_name?: string
}

export default function AutopilotPage() {
  const { toast } = useToast()
  const [status, setStatus] = useState<AutopilotStatus | null>(null)

  const [promptStats, setPromptStats] = useState<PromptStatsItem[]>([])
  const [selectedPromptFilter, setSelectedPromptFilter] = useState<number | null>(null)
  const [promptTrades, setPromptTrades] = useState<any[]>([])
  const [promptTradesLoading, setPromptTradesLoading] = useState(false)
  const [loading, setLoading] = useState(true)

  // Log History state
  const [historyLogs, setHistoryLogs] = useState<LogHistoryEntry[]>([])
  const [historyTotal, setHistoryTotal] = useState(0)
  const [historyPage, setHistoryPage] = useState(1)
  const [historyLevelFilter, setHistoryLevelFilter] = useState('all')
  const [historyLoading, setHistoryLoading] = useState(false)
  const [cycleHistory, setCycleHistory] = useState<any[]>([])
  const [cycleHistoryTotal, setCycleHistoryTotal] = useState(0)
  const [cycleHistoryLoading, setCycleHistoryLoading] = useState(false)
  const [cycleHistoryPage, setCycleHistoryPage] = useState(0)

  useEffect(() => {
    if (selectedPromptFilter == null) { setPromptTrades([]); return }
    setPromptTradesLoading(true)
    axios.get('/api/autopilot/results', {
      params: { limit: 100, prompt_number: selectedPromptFilter }
    })
      .then(res => setPromptTrades(res.data || []))
      .catch(() => console.warn('Failed to fetch prompt trades'))
      .finally(() => setPromptTradesLoading(false))
  }, [selectedPromptFilter])

  // Prompts State
  const [defaultPrompts, setDefaultPrompts] = useState<{id: string, text: string}[]>([])
  const [personalPrompts, setPersonalPrompts] = useState<{id: string, text: string}[]>([])
  const [newPromptText, setNewPromptText] = useState('')
  const [editingPromptId, setEditingPromptId] = useState<string | null>(null)
  const [promptSearch, setPromptSearch] = useState('')

  // Available providers (dynamic)
  interface ProviderOption {
    id: string
    name: string
    models: string[]
  }
  const [providers, setProviders] = useState<ProviderOption[]>([])
  const [providerModels, setProviderModels] = useState<string[]>([])

  // Settings form
  const intervalVal = useAutopilotStore((s) => s.intervalVal)
  const setIntervalVal = useAutopilotStore((s) => s.setIntervalVal)
  const lotSize = useAutopilotStore((s) => s.lotSize)
  const setLotSize = useAutopilotStore((s) => s.setLotSize)
  const symbol = useAutopilotStore((s) => s.symbol)
  const setSymbol = useAutopilotStore((s) => s.setSymbol)
  const provider = useAutopilotStore((s) => s.provider)
  const setProvider = useAutopilotStore((s) => s.setProvider)
  const model = useAutopilotStore((s) => s.model)
  const setModel = useAutopilotStore((s) => s.setModel)
  const terminalPath = useAutopilotStore((s) => s.terminalPath)
  const setTerminalPath = useAutopilotStore((s) => s.setTerminalPath)
  const connectorUrl = useAutopilotStore((s) => s.connectorUrl)
  const setConnectorUrl = useAutopilotStore((s) => s.setConnectorUrl)
  const selectedPromptIds = useAutopilotStore((s) => s.selectedPromptIds)
  const setSelectedPromptIds = useAutopilotStore((s) => s.setSelectedPromptIds)
  const maxTradesPerDay = useAutopilotStore((s) => s.maxTradesPerDay)
  const setMaxTradesPerDay = useAutopilotStore((s) => s.setMaxTradesPerDay)
  const maxDailyLoss = useAutopilotStore((s) => s.maxDailyLoss)
  const setMaxDailyLoss = useAutopilotStore((s) => s.setMaxDailyLoss)
  const [mt5Connected, setMt5Connected] = useState(false)

  useEffect(() => {
    fetchProviders()
    fetchStatus()
    fetchPromptStats()
    fetchPrompts()
    fetchCycleHistory(0)
    // Poll only for status/running state, not settings
    const intervalId: ReturnType<typeof setInterval> = setInterval(() => {
      axios.get('/api/autopilot/status').then(res => {
        const data = res.data
        setStatus(data)
        setMt5Connected(data.settings?.mt5_connected || false)
      }).catch(console.error)
    }, 5000)
    const cycleIntervalId: ReturnType<typeof setInterval> = setInterval(() => fetchCycleHistory(), 30000)
    return () => { clearInterval(intervalId); clearInterval(cycleIntervalId) }
  }, [])

  async function fetchCycleHistory(page = cycleHistoryPage) {
    setCycleHistoryLoading(true)
    try {
      const res = await axios.get('/api/autopilot/cycles', { params: { limit: 50, skip: page * 50 } })
      setCycleHistory(res.data?.cycles || [])
      setCycleHistoryTotal(res.data?.total || 0)
      setCycleHistoryPage(page)
    } catch (error) {
      console.error('Failed to fetch autopilot cycle history', error)
    } finally {
      setCycleHistoryLoading(false)
    }
  }

  // Sync model list when provider changes
  useEffect(() => {
    const prov = providers.find(p => p.id === provider)
    setProviderModels(prov?.models || [])
  }, [provider, providers])

  // Fetch log history
  useEffect(() => {
    fetchHistoryLogs()
  }, [historyPage, historyLevelFilter])

  const fetchHistoryLogs = async () => {
    setHistoryLoading(true)
    try {
      const params = new URLSearchParams({ page: String(historyPage), per_page: '50' })
      if (historyLevelFilter !== 'all') params.set('level', historyLevelFilter)
      const res = await axios.get(`/api/autopilot/logs?${params}`)
      setHistoryLogs(res.data.logs || [])
      setHistoryTotal(res.data.total || 0)
    } catch {
      // silently fail
    } finally {
      setHistoryLoading(false)
    }
  }

  const fetchProviders = async () => {
    try {
      const res = await axios.get('/api/ai/providers')
      setProviders(res.data?.providers || [])
    } catch (error) {
      console.error('Failed to fetch providers:', error)
    }
  }

  const fetchPrompts = async () => {
    try {
      const res = await axios.get('/api/autopilot/prompts')
      setDefaultPrompts(res.data?.default_prompts || [])
      setPersonalPrompts(res.data?.personal_prompts || [])
      setSelectedPromptIds((res.data?.selected_ids || []).map(String))
    } catch (error) {
      console.error('Failed to fetch prompts:', error)
    }
  }

  const fetchStatus = async () => {
    try {
      const res = await axios.get('/api/autopilot/status')
      setStatus(res.data)
      if (res.data?.settings) {
        setIntervalVal(String(res.data.settings.interval_seconds))
        setLotSize(String(res.data.settings.default_lot))
        setSymbol(res.data.settings.symbol)
        setProvider(res.data.settings.provider)
        setModel(res.data.settings.model)
        setTerminalPath(res.data.settings.mt5_terminal_path || '')
        setConnectorUrl(res.data.settings.mt5_connector_url || '')
        setMt5Connected(res.data.settings.mt5_connected || false)
        setMaxTradesPerDay(String(res.data.settings.max_trades_per_day ?? 10))
        setMaxDailyLoss(String(res.data.settings.max_daily_loss ?? -50))
        if (res.data.settings.selected_prompts) {
          setSelectedPromptIds(res.data.settings.selected_prompts.map(String))
        }
      }
    } catch (error) {
      console.error('Failed to fetch status:', error)
    } finally {
      setLoading(false)
    }
  }

  const fetchPromptStats = async () => {
    try {
      const res = await axios.get('/api/autopilot/prompt-stats')
      setPromptStats(res.data || [])
    } catch (error) {
      console.error('Failed to fetch prompt stats:', error)
    }
  }

  const handleStart = async () => {
    await saveSettings()
    try {
      await axios.post('/api/autopilot/start')
      fetchStatus()
    } catch (error: any) {
      toast({ title: 'Start Failed', description: error.response?.data?.detail || error.message, variant: 'destructive' })
    }
  }

  const handleStop = async () => {
    try {
      await axios.post('/api/autopilot/stop')
      fetchStatus()
    } catch (error) {
      console.error('Failed to stop autopilot:', error)
    }
  }

  const saveSettings = async () => {
    try {
      await axios.post('/api/autopilot/settings', {
        interval_seconds: parseInt(intervalVal),
        default_lot: parseFloat(lotSize),
        max_trades_per_day: parseInt(maxTradesPerDay) || 10,
        cooldown_minutes: 5,
        max_daily_loss: parseFloat(maxDailyLoss) || -50,
        mt5_terminal_path: terminalPath,
        mt5_connector_url: connectorUrl || null,
        symbol: symbol,
        provider: provider,
        model: model,
        selected_prompts: selectedPromptIds.map(id => id.startsWith('custom_') ? id : parseInt(id))
      })
    } catch (error) {
      console.error('Failed to save settings:', error)
    }
  }

  const connectMT5 = async () => {
    try {
      const res = await axios.post('/api/autopilot/connect-mt5', null, {
        params: {
          terminal_path: terminalPath || null,
          connector_url: connectorUrl || null
        }
      })
      if (res.data.success) {
        setMt5Connected(true)
        toast({ title: 'MT5 Connected', description: 'Connected successfully' })
      } else {
        toast({ title: 'MT5 Connection Failed', description: res.data.message, variant: 'destructive' })
      }
      fetchStatus()
    } catch (error: any) {
      toast({ title: 'MT5 Connection Error', description: error.response?.data?.detail || error.message, variant: 'destructive' })
    }
  }

  const handleAddPrompt = async () => {
    if (!newPromptText.trim()) return
    try {
      await axios.post('/api/autopilot/prompts', { content: newPromptText })
      setNewPromptText('')
      fetchPrompts()
    } catch (error) {
      console.error('Failed to add prompt:', error)
    }
  }

  const handleDeletePrompt = async (id: string) => {
    if (!confirm('Are you sure you want to delete this prompt?')) return
    try {
      await axios.delete(`/api/autopilot/prompts/${id}`)
      fetchPrompts()
    } catch (error) {
      console.error('Failed to delete prompt:', error)
    }
  }

  const handleUpdatePrompt = async (id: string, text: string) => {
    try {
      await axios.put(`/api/autopilot/prompts/${id}`, { content: text })
      setEditingPromptId(null)
      fetchPrompts()
    } catch (error) {
      console.error('Failed to update prompt:', error)
    }
  }

  const togglePromptSelection = (id: string) => {
    const current = useAutopilotStore.getState().selectedPromptIds
    setSelectedPromptIds(
      current.includes(id) ? current.filter(i => i !== id) : [...current, id]
    )
  }

  const selectAllPrompts = () => {
    const allIds = [...defaultPrompts.map(p => p.id), ...personalPrompts.map(p => p.id)]
    setSelectedPromptIds(allIds)
  }

  const clearPromptSelection = () => {
    setSelectedPromptIds([])
  }

  const getLogColor = (level: string) => {
    switch (level) {
      case 'SUCCESS': return 'text-green-400'
      case 'ERROR': return 'text-red-400'
      default: return 'text-gray-300'
    }
  }

  if (loading) {
    return (
      <div className="p-8 flex items-center justify-center min-h-[400px]">
        <div className="text-muted-foreground">Loading...</div>
      </div>
    )
  }

  return (
    <div className="p-8">
      <div className="flex items-center justify-between mb-8">
        <h1 className="font-heading text-3xl font-bold">Autopilot</h1>
        <div className="flex items-center gap-4">
          <div className={`px-4 py-2 rounded-full ${status?.enabled ? 'bg-green-500/20 text-green-400' : 'bg-gray-500/20 text-gray-400'}`}>
            {status?.enabled ? 'Running' : 'Stopped'}
          </div>
          {status?.enabled ? (
            <Button variant="destructive" onClick={handleStop}>Stop</Button>
          ) : (
            <Button onClick={handleStart}>Start Autopilot</Button>
          )}
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Total Cycles</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{status?.stats.total_runs || 0}</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Trades Executed</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{status?.stats.trades_executed || 0}</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Skipped</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-yellow-500">{status?.stats.skipped_count || 0}</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Errors</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-red-500">{status?.stats.error_count || 0}</div>
          </CardContent>
        </Card>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
        {/* Settings */}
        <Card>
          <CardHeader>
            <CardTitle>Settings</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* MT5 Connection */}
            <div className="p-4 bg-muted/30 rounded-lg">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">MT5 Terminal</span>
                  <span className={`px-2 py-1 rounded text-xs ${mt5Connected ? 'bg-green-500/20 text-green-400' : 'bg-gray-500/20 text-gray-400'}`}>
                    {mt5Connected ? 'Connected' : 'Disconnected'}
                  </span>
                </div>
                <Button
                  size="sm"
                  variant={mt5Connected ? "outline" : "default"}
                  onClick={connectMT5}
                  disabled={status?.enabled}
                >
                  {mt5Connected ? 'Reconnect' : 'Connect'}
                </Button>
              </div>
              <Input
                placeholder="C:\Program Files\MetaTrader 5\terminal64.exe"
                value={terminalPath}
                onChange={(e) => setTerminalPath(e.target.value)}
                disabled={status?.enabled}
                className="text-xs"
              />
              <p className="text-xs text-muted-foreground mt-1">
                Leave empty for default MT5 terminal
              </p>
              {/* MT5 Connector URL */}
              <label className="text-sm text-muted-foreground mt-3 block">MT5 Connector URL</label>
              <Input
                placeholder="http://192.168.1.100:5001"
                value={connectorUrl}
                onChange={(e) => setConnectorUrl(e.target.value)}
                disabled={status?.enabled}
                className="text-xs"
              />
              <p className="text-xs text-muted-foreground mt-1">
                IP:Port of MT5 Connector service (leave empty for localhost)
              </p>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-sm text-muted-foreground">Cycle Interval</label>
                <Select 
                  value={intervalVal} 
                  onValueChange={setIntervalVal}
                  disabled={status?.enabled}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="60">1 minute</SelectItem>
                    <SelectItem value="180">3 minutes</SelectItem>
                    <SelectItem value="300">5 minutes</SelectItem>
                    <SelectItem value="600">10 minutes</SelectItem>
                    <SelectItem value="900">15 minutes</SelectItem>
                    <SelectItem value="1800">30 minutes</SelectItem>
                    <SelectItem value="3600">1 hour</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div>
                <label className="text-sm text-muted-foreground">Lot Size</label>
                <Input
                  type="number"
                  step="0.01"
                  value={lotSize}
                  onChange={(e) => setLotSize(e.target.value)}
                  disabled={status?.enabled}
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-sm text-muted-foreground">Max Trades / Day</label>
                <Input
                  type="number"
                  min="1"
                  step="1"
                  value={maxTradesPerDay}
                  onChange={(e) => setMaxTradesPerDay(e.target.value)}
                  disabled={status?.enabled}
                />
              </div>
              <div>
                <label className="text-sm text-muted-foreground">Max Daily Loss ($)</label>
                <Input
                  type="number"
                  step="1"
                  value={maxDailyLoss}
                  onChange={(e) => setMaxDailyLoss(e.target.value)}
                  disabled={status?.enabled}
                />
              </div>
            </div>

            <div>
              <label className="text-sm text-muted-foreground">Symbol</label>
              <Input
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                disabled={status?.enabled}
              />
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-sm text-muted-foreground">AI Provider</label>
                <Select value={provider} onValueChange={setProvider} disabled={status?.enabled}>
                  <SelectTrigger>
                    <SelectValue placeholder="Select provider" />
                  </SelectTrigger>
                  <SelectContent>
                    {providers.map((p) => (
                      <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <label className="text-sm text-muted-foreground">Model</label>
                <Select value={model} onValueChange={setModel} disabled={status?.enabled}>
                  <SelectTrigger>
                    <SelectValue placeholder="Select model" />
                  </SelectTrigger>
                  <SelectContent>
                    {providerModels.map((m) => (
                      <SelectItem key={m} value={m}>{m}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            <Button onClick={saveSettings} variant="outline" className="w-full">
              Save Settings
            </Button>
          </CardContent>
        </Card>

        {/* Prompt Management */}
        <Card className="lg:row-span-2 flex flex-col">
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle>Strategy Prompts</CardTitle>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" className="h-7 text-[10px]" onClick={selectAllPrompts}>All</Button>
              <Button variant="outline" size="sm" className="h-7 text-[10px]" onClick={clearPromptSelection}>None</Button>
            </div>
          </CardHeader>
          <CardContent className="flex-1 flex flex-col space-y-4 overflow-hidden">
            <div className="space-y-2">
              <Input 
                placeholder="Search prompts..." 
                value={promptSearch}
                onChange={(e) => setPromptSearch(e.target.value)}
                className="h-8 text-xs"
              />
              <div className="flex items-center gap-2 text-[10px] text-muted-foreground px-1">
                <span className="flex items-center gap-1">
                  <div className="w-2 h-2 rounded-full bg-blue-500"></div> Default
                </span>
                <span className="flex items-center gap-1">
                  <div className="w-2 h-2 rounded-full bg-purple-500"></div> Personal
                </span>
                <span className="ml-auto">{selectedPromptIds.length} selected</span>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto pr-2 space-y-2 max-h-[500px]">
              {/* Personal Prompts First */}
              {personalPrompts
                .filter(p => (p.text || '').toLowerCase().includes(promptSearch.toLowerCase()))
                .map(p => (
                <div key={p.id} className={`p-3 rounded-lg border text-xs transition-colors ${selectedPromptIds.includes(p.id) ? 'bg-purple-500/10 border-purple-500/30' : 'bg-muted/30 border-transparent'}`}>
                  <div className="flex items-start gap-3">
                    <input 
                      type="checkbox" 
                      checked={selectedPromptIds.includes(p.id)}
                      onChange={() => togglePromptSelection(p.id)}
                      className="mt-1 accent-purple-500"
                    />
                    <div className="flex-1">
                      {editingPromptId === p.id ? (
                        <div className="space-y-2">
                          <textarea 
                            className="w-full bg-background border rounded p-2 min-h-[60px]"
                            value={p.text}
                            onChange={(e) => {
                              const newText = e.target.value
                              setPersonalPrompts(prev => prev.map(item => item.id === p.id ? {...item, text: newText} : item))
                            }}
                          />
                          <div className="flex gap-2">
                            <Button size="sm" onClick={() => handleUpdatePrompt(p.id, p.text)}>Save</Button>
                            <Button size="sm" variant="ghost" onClick={() => setEditingPromptId(null)}>Cancel</Button>
                          </div>
                        </div>
                      ) : (
                        <>
                          <p className="leading-relaxed">{p.text}</p>
                          <div className="flex gap-2 mt-2 opacity-50 hover:opacity-100">
                            <button onClick={() => setEditingPromptId(p.id)} className="hover:text-blue-400">Edit</button>
                            <button onClick={() => handleDeletePrompt(p.id)} className="hover:text-red-400">Delete</button>
                          </div>
                        </>
                      )}
                    </div>
                  </div>
                </div>
              ))}

              {/* Default Prompts */}
              {defaultPrompts
                .filter(p => (p.text || '').toLowerCase().includes(promptSearch.toLowerCase()) || (p.id || '').includes(promptSearch))
                .map(p => (
                <div key={p.id} className={`p-3 rounded-lg border text-xs transition-colors ${selectedPromptIds.includes(p.id) ? 'bg-blue-500/10 border-blue-500/30' : 'bg-muted/30 border-transparent'}`}>
                  <div className="flex items-start gap-3">
                    <input 
                      type="checkbox" 
                      checked={selectedPromptIds.includes(p.id)}
                      onChange={() => togglePromptSelection(p.id)}
                      className="mt-1 accent-blue-500"
                    />
                    <div className="flex-1">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="font-bold text-blue-400">#{p.id}</span>
                      </div>
                      <p className="leading-relaxed text-muted-foreground">{p.text}</p>
                    </div>
                  </div>
                </div>
              ))}
            </div>

            <div className="pt-4 border-t border-border">
              <label className="text-xs font-medium mb-2 block">Add Personal Strategy</label>
              <textarea 
                placeholder="Enter your custom trading prompt..." 
                className="w-full bg-muted/30 border-border rounded-lg p-3 text-xs min-h-[80px] focus:ring-1 focus:ring-purple-500 outline-none"
                value={newPromptText}
                onChange={(e) => setNewPromptText(e.target.value)}
              />
              <Button className="w-full mt-2" onClick={handleAddPrompt} disabled={!newPromptText.trim()}>
                Add to Library
              </Button>
            </div>
          </CardContent>
        </Card>

        {/* Logs */}
        <Card>
          <CardHeader>
            <CardTitle>Live Logs</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="h-[300px] overflow-y-auto font-mono text-xs space-y-1">
              {status?.logs && status.logs.length > 0 ? (
                status.logs.map((log, idx) => (
                  <div key={idx} className="flex gap-2">
                    <span className="text-muted-foreground">[{log.timestamp}]</span>
                    <span className={getLogColor(log.level)}>{log.level}</span>
                    <span className="text-gray-300">{log.message}</span>
                  </div>
                ))
              ) : (
                <div className="text-muted-foreground text-center py-8">No logs yet</div>
              )}
            </div>
          </CardContent>
        </Card>

        {/* Log History */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle>Log History</CardTitle>
            <Select value={historyLevelFilter} onValueChange={v => { setHistoryLevelFilter(v); setHistoryPage(1) }}>
              <SelectTrigger className="w-28 h-8 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Levels</SelectItem>
                <SelectItem value="INFO">INFO</SelectItem>
                <SelectItem value="SUCCESS">SUCCESS</SelectItem>
                <SelectItem value="WARNING">WARNING</SelectItem>
                <SelectItem value="ERROR">ERROR</SelectItem>
              </SelectContent>
            </Select>
          </CardHeader>
          <CardContent>
            {historyLoading ? (
              <div className="text-muted-foreground text-center py-8">Loading...</div>
            ) : historyLogs.length === 0 ? (
              <div className="text-muted-foreground text-center py-8">No history logs found.</div>
            ) : (
              <>
                <div className="h-[400px] overflow-y-auto font-mono text-xs space-y-1">
                  {historyLogs.map((log) => (
                    <div key={log.id} className="flex gap-2 py-1 border-b border-border/20">
                      <span className="text-muted-foreground shrink-0 w-[32px]">[{log.timestamp.slice(11, 19)}]</span>
                      <span className={getLogColor(log.level) + ' shrink-0 w-[60px]'}>{log.level}</span>
                      <span className="text-muted-foreground shrink-0 w-[40px] text-right">{log.cycle_number ? `#${log.cycle_number}` : ''}</span>
                      <span className="text-gray-300 break-words">{log.message}</span>
                    </div>
                  ))}
                </div>
                <div className="flex items-center justify-between mt-3">
                  <span className="text-xs text-muted-foreground">Total: {historyTotal} entries</span>
                  <div className="flex items-center gap-2">
                    <Button variant="outline" size="sm" disabled={historyPage <= 1}
                      onClick={() => setHistoryPage(p => p - 1)}>
                      <ChevronLeft className="w-3 h-3" /> Prev
                    </Button>
                    <span className="text-xs text-muted-foreground">Page {historyPage}</span>
                    <Button variant="outline" size="sm" disabled={historyPage * 50 >= historyTotal}
                      onClick={() => setHistoryPage(p => p + 1)}>
                      Next <ChevronRight className="w-3 h-3" />
                    </Button>
                  </div>
                </div>
              </>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Autopilot Cycle History */}
      <Card className="mt-8">
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Autopilot Cycle History</CardTitle>
            <p className="text-xs text-muted-foreground mt-1">Each scheduled check, including skips, linked to AI calls, execution, broker events, and eventual trade outcome.</p>
          </div>
          <Button variant="outline" size="sm" onClick={() => fetchCycleHistory()} disabled={cycleHistoryLoading}>
            {cycleHistoryLoading ? 'Refreshing…' : `Refresh (${cycleHistoryTotal})`}
          </Button>
        </CardHeader>
        <CardContent>
          {cycleHistoryLoading && cycleHistory.length === 0 ? (
            <div className="text-muted-foreground text-center py-6">Loading cycle history…</div>
          ) : cycleHistory.length === 0 ? (
            <div className="text-muted-foreground text-center py-6">No cycle records yet. New checks are recorded after this version is deployed.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead><tr className="border-b border-border text-left">
                  <th className="py-2 px-2">Started</th><th className="py-2 px-2">Cycle ID</th>
                  <th className="py-2 px-2">Symbol</th><th className="py-2 px-2">Outcome</th>
                  <th className="py-2 px-2">Reason</th><th className="py-2 px-2">Trade P&amp;L</th>
                </tr></thead>
                <tbody>{cycleHistory.map((cycle) => (
                  <tr key={cycle.cycle_id} className="border-b border-border/30 align-top">
                    <td className="py-2 px-2 whitespace-nowrap">{cycle.started_at ? new Date(cycle.started_at).toLocaleString() : '—'}</td>
                    <td className="py-2 px-2 font-mono text-xs" title={cycle.cycle_id}>{cycle.cycle_id.slice(0, 12)}…</td>
                    <td className="py-2 px-2">{cycle.symbol}</td>
                    <td className="py-2 px-2">{cycle.outcome || cycle.status}</td>
                    <td className="py-2 px-2 max-w-[320px] whitespace-normal text-xs">{cycle.outcome_reason || '—'}</td>
                    <td className={`py-2 px-2 ${cycle.realized_profit == null ? '' : cycle.realized_profit >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                      {cycle.realized_profit == null ? 'Open / n.a.' : `$${Number(cycle.realized_profit).toFixed(2)}`}
                    </td>
                  </tr>
                ))}</tbody>
              </table>
              <div className="mt-3 max-h-[420px] overflow-y-auto space-y-2">
                {cycleHistory.map((cycle) => (
                  <details key={`${cycle.cycle_id}-details`} className="rounded border border-border/50 p-3">
                    <summary className="cursor-pointer text-xs font-medium">Cycle #{cycle.cycle_number} · {cycle.outcome || cycle.status} · {cycle.prompt_text ? cycle.prompt_text.slice(0, 100) : 'No prompt selected'}</summary>
                    <div className="mt-3 space-y-2 text-xs">
                      <div className="font-mono break-all text-muted-foreground">Cycle ID: {cycle.cycle_id}</div>
                      {cycle.setup && <pre className="whitespace-pre-wrap rounded bg-muted/40 p-2">Setup: {JSON.stringify(cycle.setup, null, 2)}</pre>}
                      <ol className="border-l border-border pl-4 space-y-2">
                        {(cycle.timeline || []).map((event: any, index: number) => (
                          <li key={`${event.stage}-${index}`}>
                            <div className="font-medium">{event.timestamp ? new Date(event.timestamp).toLocaleString() : 'Time unavailable'} · {event.stage}{event.outcome ? ` · ${event.outcome}` : ''}</div>
                            {event.details && <pre className="mt-1 whitespace-pre-wrap break-words text-muted-foreground">{JSON.stringify(event.details, null, 2)}</pre>}
                          </li>
                        ))}
                      </ol>
                    </div>
                  </details>
                ))}
              </div>
              <div className="mt-3 flex items-center justify-between">
                <p className="text-xs text-muted-foreground">Showing {cycleHistory.length ? cycleHistoryPage * 50 + 1 : 0}–{cycleHistoryPage * 50 + cycleHistory.length} of {cycleHistoryTotal} cycles.</p>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" disabled={cycleHistoryPage === 0 || cycleHistoryLoading} onClick={() => fetchCycleHistory(cycleHistoryPage - 1)}>Previous</Button>
                  <Button variant="outline" size="sm" disabled={(cycleHistoryPage + 1) * 50 >= cycleHistoryTotal || cycleHistoryLoading} onClick={() => fetchCycleHistory(cycleHistoryPage + 1)}>Next</Button>
                </div>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Strategy Scoreboard */}
      <Card className="mt-8">
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>Strategy Scoreboard</CardTitle>
          {selectedPromptFilter != null && (
            <Button variant="ghost" size="sm" onClick={() => setSelectedPromptFilter(null)}>
              Clear Filter
            </Button>
          )}
        </CardHeader>
        <CardContent>
          {promptStats.length === 0 ? (
            <p className="text-muted-foreground text-center py-8">No completed trades yet</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-border">
                    <th className="text-left py-3 px-2">#</th>
                    <th className="text-left py-3 px-2">Strategy</th>
                    <th className="text-right py-3 px-2">Trades</th>
                    <th className="text-right py-3 px-2">Wins</th>
                    <th className="text-right py-3 px-2">Losses</th>
                    <th className="text-right py-3 px-2">Win Rate</th>
                    <th className="text-right py-3 px-2">Total P&L</th>
                    <th className="text-right py-3 px-2">Avg P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {promptStats.map((s) => (
                    <tr
                      key={s.prompt_number}
                      className={`border-b border-border/30 hover:bg-muted/50 cursor-pointer ${selectedPromptFilter === s.prompt_number ? 'bg-blue-500/10' : ''}`}
                      onClick={() => setSelectedPromptFilter(selectedPromptFilter === s.prompt_number ? null : s.prompt_number)}
                    >
                      <td className="py-3 px-2 font-bold text-blue-400">{s.display_name || `#${s.prompt_number}`}</td>
                      <td className="py-3 px-2 text-xs max-w-[200px] truncate">{s.prompt_text}</td>
                      <td className="py-3 px-2 text-right">{s.total_trades}</td>
                      <td className="py-3 px-2 text-right text-green-500">{s.wins}</td>
                      <td className="py-3 px-2 text-right text-red-500">{s.losses}</td>
                      <td className="py-3 px-2 text-right">
                        <span className={s.win_rate >= 60 ? 'text-green-500' : s.win_rate >= 40 ? 'text-yellow-500' : 'text-red-500'}>
                          {s.win_rate}%
                        </span>
                      </td>
                      <td className={`py-3 px-2 text-right font-medium ${s.total_profit >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                        ${s.total_profit.toFixed(2)}
                      </td>
                      <td className={`py-3 px-2 text-right ${s.avg_profit >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                        ${s.avg_profit.toFixed(2)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Selected Prompt Trades Dialog */}
      <Dialog open={selectedPromptFilter != null} onOpenChange={(open) => { if (!open) setSelectedPromptFilter(null) }}>
        <DialogContent className="max-w-5xl max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle>Trades for Prompt #{selectedPromptFilter}</DialogTitle>
          </DialogHeader>
          <div className="flex-1 overflow-auto">
            {promptTradesLoading ? (
              <p className="text-muted-foreground text-center py-8">Loading trades...</p>
            ) : promptTrades.length === 0 ? (
              <p className="text-muted-foreground text-center py-8">No trades for this prompt</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-border">
                      <th className="text-left py-3 px-2">Time</th>
                      <th className="text-left py-3 px-2">Symbol</th>
                      <th className="text-left py-3 px-2">Dir</th>
                      <th className="text-right py-3 px-2">Entry</th>
                      <th className="text-right py-3 px-2">Exit</th>
                      <th className="text-right py-3 px-2">SL</th>
                      <th className="text-right py-3 px-2">TP</th>
                      <th className="text-right py-3 px-2">Lot</th>
                      <th className="text-right py-3 px-2">Ticket</th>
                      <th className="text-left py-3 px-2">Result</th>
                      <th className="text-right py-3 px-2">P&L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {promptTrades.map((trade: any) => (
                      <tr key={trade.id} className="border-b border-border/30 hover:bg-muted/50">
                        <td className="py-3 px-2 text-xs">{trade.executed_at?.slice(11, 19) || '-'}</td>
                        <td className="py-3 px-2 font-medium">{trade.symbol}</td>
                        <td className={`py-3 px-2 ${trade.direction === 'BUY' ? 'text-green-500' : 'text-red-500'}`}>
                          {trade.direction}
                        </td>
                        <td className="py-3 px-2 text-right">{trade.entry_price?.toFixed(trade.symbol?.includes('XAU') ? 2 : 5) ?? '-'}</td>
                        <td className="py-3 px-2 text-right">{trade.exit_price?.toFixed(trade.symbol?.includes('XAU') ? 2 : 5) ?? '-'}</td>
                        <td className="py-3 px-2 text-right text-red-400">{trade.stop_loss?.toFixed(trade.symbol?.includes('XAU') ? 2 : 5) ?? '-'}</td>
                        <td className="py-3 px-2 text-right text-green-400">{trade.take_profit?.toFixed(trade.symbol?.includes('XAU') ? 2 : 5) ?? '-'}</td>
                        <td className="py-3 px-2 text-right">{trade.lot_size}</td>
                        <td className="py-3 px-2">{trade.mt5_ticket || '-'}</td>
                        <td className="py-3 px-2">
                          <span className={trade.result === 'TP_HIT' ? 'text-green-400' : trade.result === 'SL_HIT' ? 'text-red-400' : trade.result === 'MANUAL_CLOSE' ? 'text-yellow-400' : 'text-gray-400'}>
                            {trade.result === 'TP_HIT' ? 'TP' : trade.result === 'SL_HIT' ? 'SL' : trade.result === 'MANUAL_CLOSE' ? 'Manual' : trade.result || 'Open'}
                          </span>
                        </td>
                        <td className={`py-3 px-2 text-right font-medium ${(trade.profit ?? 0) >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                          {trade.profit != null ? `$${trade.profit.toFixed(2)}` : '-'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>

    </div>
  )
}
