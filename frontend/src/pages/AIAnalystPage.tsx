import { useState, useEffect, useRef } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import axios from 'axios'
import { createChart, CandlestickSeries, CandlestickData, Time } from 'lightweight-charts'
import { DataPreviewTable } from '@/components/ui/data-preview-table'
import { MiniChart } from '@/components/ui/mini-chart'
import { useToast } from '@/hooks/use-toast'
import { useLatest } from '@/hooks/useLatest'
import { useAIAnalystStore } from '@/store/aiAnalystStore'

interface AIProvider {
  id: string
  name: string
  models: string[]
}



interface Message {
  role: 'user' | 'assistant'
  content: string
  detected_setup?: any
  detected_action?: any
  data_preview?: string
  detected_chart?: any
  execution_output?: string
  execution_charts?: Array<{title: string, data: number[], color: string, type: string}>
  execution_tables?: Array<{title: string, columns?: string[], rows: any[][]}>
  chat_memory_id?: number
  rag_context?: string
}




interface SymbolOption {
  symbol: string
  type?: string
  name?: string
}

interface CandleData {
  time: number
  open: number
  high: number
  low: number
  close: number
}

function getDigitsForSymbol(symbol: string): number {
  const s = symbol.toUpperCase()
  if (/JPY/.test(s)) return 3
  if (/XAU|XAG|XPT|XPD/.test(s)) return 2
  if (/BTC|ETH|XRP|SOL|ADA|DOT|LINK|AVAX|MATIC|LTC|BCH|UNI/.test(s)) return 2
  if (/US30|SPX500|NAS100|DAX|FTSE|CAC|NI225|HK50|AUS200|UK100/.test(s)) return 2
  if (/COCOA|COFFEE|SUGAR|CORN|WHEAT|Soybean|OIL|NGAS/.test(s)) return 2
  if (/USD|EUR|GBP|AUD|NZD|CAD|CHF/.test(s)) return 5
  return 2
}

export default function AIAnalystPage() {
  const { toast } = useToast()
  const chartContainerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<any>(null)
  const seriesRef = useRef<any>(null)

  // Persisted state (survives tab switches via localStorage)
  const messages = useAIAnalystStore((s) => s.messages)
  const addMessage = useAIAnalystStore((s) => s.addMessage)
  const provider = useAIAnalystStore((s) => s.provider)
  const setProvider = useAIAnalystStore((s) => s.setProvider)
  const model = useAIAnalystStore((s) => s.model)
  const setModel = useAIAnalystStore((s) => s.setModel)
  const persona = useAIAnalystStore((s) => s.persona)
  const setPersona = useAIAnalystStore((s) => s.setPersona)
  const symbol = useAIAnalystStore((s) => s.symbol)
  const setSymbol = useAIAnalystStore((s) => s.setSymbol)
  const customSymbol = useAIAnalystStore((s) => s.customSymbol)
  const setCustomSymbol = useAIAnalystStore((s) => s.setCustomSymbol)
  const loadData = useAIAnalystStore((s) => s.loadData)
  const setLoadData = useAIAnalystStore((s) => s.setLoadData)
  const dataPeriod = useAIAnalystStore((s) => s.dataPeriod)
  const setDataPeriod = useAIAnalystStore((s) => s.setDataPeriod)
  const timeframe = useAIAnalystStore((s) => s.timeframe)
  const setTimeframe = useAIAnalystStore((s) => s.setTimeframe)
  const loadedData = useAIAnalystStore((s) => s.loadedData)
  const setLoadedData = useAIAnalystStore((s) => s.setLoadedData)
  const liveMode = useAIAnalystStore((s) => s.liveMode)
  const setLiveMode = useAIAnalystStore((s) => s.setLiveMode)
  const clearMessages = useAIAnalystStore((s) => s.clearMessages)
  const feedbackSet = useAIAnalystStore((s) => s.feedbackSet)
  const addFeedback = useAIAnalystStore((s) => s.addFeedback)

  // Transient state (does NOT persist)
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [candleData, setCandleData] = useState<CandleData[]>([])
  const [availableSymbols, setAvailableSymbols] = useState<SymbolOption[]>([])
  const [symbolsLoading, setSymbolsLoading] = useState(false)
  const [dataLoading, setDataLoading] = useState(false)
  const [availableProviders, setAvailableProviders] = useState<AIProvider[]>([])
  const [executingTrade, setExecutingTrade] = useState(false)
  const [tradeExecMsgIdx, setTradeExecMsgIdx] = useState<number | null>(null)
  const [symbolDigits, setSymbolDigits] = useState(5)

  const getSymbolValue = () => {
    if (symbol === 'custom' && customSymbol.trim()) {
      return customSymbol.trim().toUpperCase()
    }
    if (symbol === undefined || symbol === '' || symbol === 'none') {
      return ''
    }
    return symbol
  }

  // The symbol in use, for effects that should rerun when it changes.
  const currentSymbol = getSymbolValue()

  // Fetch providers from backend on mount
  useEffect(() => {
    const fetchProviders = async () => {
      try {
        const res = await axios.get('/api/ai/providers')
        if (res.data?.providers) {
          setAvailableProviders(res.data.providers)
        }
      } catch (error) {
        console.error('Failed to fetch AI providers:', error)
      }
    }
    fetchProviders()
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

  // The timer calls the latest handleLoadData without being restarted on every render.
  const loadLatest = useLatest(() => handleLoadData())
  useEffect(() => {
    let interval: any
    if (liveMode && loadData !== 'none' && currentSymbol) {
      interval = setInterval(() => {
        loadLatest.current()
      }, 60000)
    }
    return () => {
      if (interval) clearInterval(interval)
    }
  }, [liveMode, loadData, currentSymbol, dataPeriod, timeframe, loadLatest])

  useEffect(() => {
    if (!chartContainerRef.current) return

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { color: '#1a1a1a' },
        textColor: '#d1d5db'
      },
      grid: {
        vertLines: { color: '#2a2a2a' },
        horzLines: { color: '#2a2a2a' }
      },
      crosshair: {
        mode: 1,
        vertLine: {
          color: '#6b7280',
          labelBackgroundColor: '#374151',
          width: 1,
          style: 2
        },
        horzLine: {
          color: '#6b7280',
          labelBackgroundColor: '#374151',
          width: 1,
          style: 2
        }
      },
      rightPriceScale: {
        borderColor: '#374151'
      },
      timeScale: {
        borderColor: '#374151',
        timeVisible: true,
        secondsVisible: false
      }
    })

    const initialDigits = getDigitsForSymbol(getSymbolValue())
    const candlestickSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderUpColor: '#22c55e',
      borderDownColor: '#ef4444',
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
      priceFormat: {
        type: 'price',
        precision: initialDigits,
        minMove: 1 / Math.pow(10, initialDigits)
      }
    })

    chartRef.current = chart
    seriesRef.current = candlestickSeries

    const handleResize = () => {
      if (chartContainerRef.current) {
        chart.applyOptions({ 
          width: chartContainerRef.current.clientWidth,
          height: chartContainerRef.current.clientHeight 
        })
      }
    }

    handleResize()
    window.addEventListener('resize', handleResize)

    return () => {
      window.removeEventListener('resize', handleResize)
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
  // Built once; the effect that follows updates the price format when the symbol changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- the chart is created once on mount
  }, [])

  useEffect(() => {
    if (seriesRef.current && candleData.length > 0) {
      const validData = candleData.filter(d => {
        const timeVal = Number(d.time)
        const openVal = Number(d.open)
        const highVal = Number(d.high)
        const lowVal = Number(d.low)
        const closeVal = Number(d.close)
        
        return (
          timeVal > 0 &&
          openVal > 0 &&
          highVal > 0 &&
          lowVal > 0 &&
          closeVal > 0 &&
          !isNaN(timeVal) &&
          !isNaN(openVal) &&
          !isNaN(highVal) &&
          !isNaN(lowVal) &&
          !isNaN(closeVal)
        )
      })

      const formattedData: CandlestickData<Time>[] = validData.map(d => ({
        time: Number(d.time) as Time,
        open: Number(d.open),
        high: Number(d.high),
        low: Number(d.low),
        close: Number(d.close)
      }))
      
      if (formattedData.length > 0) {
        seriesRef.current.setData(formattedData)
        chartRef.current?.timeScale().fitContent()
      }
    }
  }, [candleData])

  // Update priceFormat on the chart series when symbol changes
  useEffect(() => {
    if (seriesRef.current) {
      const minMove = 1 / Math.pow(10, symbolDigits)
      seriesRef.current.applyOptions({
        priceFormat: {
          type: 'price',
          precision: symbolDigits,
          minMove
        }
      })
    }
  }, [symbolDigits])

  // Auto-reload data on re-mount if we have persisted symbol+source
  useEffect(() => {
    if (loadData !== 'none' && getSymbolValue() && candleData.length === 0) {
      handleLoadData()
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- runs once on mount, to reload a symbol kept from the last visit
  }, [])

  // Derive decimal precision from symbol name
  useEffect(() => {
    if (currentSymbol) {
      setSymbolDigits(getDigitsForSymbol(currentSymbol))
    }
  }, [currentSymbol])

  const MAX_CONVERSATION_TURNS = 20  // Sliding window: only last N turns sent to AI

const detectRequiredCandles = (query: string): number => {
    const lower = query.toLowerCase()
    if (/daily|weekly|d1|w1|1d\b|previous\s*day|yesterday/i.test(lower)) return 30000
    if (/4h\b|4[-\s]?hour|four\s*hour|h4\b|4hrs?\b/i.test(lower)) return 10000
    if (/1h\b|1[-\s]?hour|one\s*hour|hourly|h1\b|1hrs?\b/i.test(lower)) return 5000
    if (/30m\b|30[-\s]?min|m30|thirty\s*min/i.test(lower)) return 3000
    return 10000
}

  const handleLoadData = async (count?: number) => {
    if (loadData === 'none' || !getSymbolValue()) {
      return
    }

    setDataLoading(true)
    try {
      let data: CandleData[] = []
      const candleCount = count || 10000

      if (loadData === 'yahoo') {
        const res = await axios.get(`/api/data/yahoo/${getSymbolValue()}?period=${dataPeriod}`)
        if (res.data?.success && res.data.data?.length > 0) {
          data = res.data.data.map((d: any) => ({
            time: typeof d.time === 'number' ? d.time : Math.floor(new Date(d.time).getTime() / 1000),
            open: d.open || 0,
            high: d.high || 0,
            low: d.low || 0,
            close: d.close || 0
          }))
          const startDate = data[0].time
          const endDate = data[data.length - 1].time
          setLoadedData({
            source: loadData,
            symbol: getSymbolValue(),
            startDate: new Date(startDate * 1000).toLocaleDateString(),
            endDate: new Date(endDate * 1000).toLocaleDateString(),
            candles: data.length
          })
        }
      } else if (loadData === 'mt5') {
        const res = await axios.post('/api/mt5/data/latest', {
          symbol: getSymbolValue(),
          timeframe: timeframe,
          count: candleCount
        })
        if (res.data?.success && res.data.data?.length > 0) {
          data = res.data.data.map((d: any) => ({
            time: typeof d.time === 'number' ? d.time : Math.floor(new Date(d.time).getTime() / 1000),
            open: d.open || 0,
            high: d.high || 0,
            low: d.low || 0,
            close: d.close || 0
          }))
          const startDate = data[0].time
          const endDate = data[data.length - 1].time
          setLoadedData({
            source: loadData,
            symbol: getSymbolValue(),
            startDate: new Date(startDate * 1000).toLocaleDateString(),
            endDate: new Date(endDate * 1000).toLocaleDateString(),
            candles: data.length
          })
        }
      }

      setCandleData(data)
    } catch (error) {
      console.error('Failed to load data:', error)
      setLoadedData(null)
      setCandleData([])
      toast({ title: 'Data Load Failed', description: `Could not fetch data for ${getSymbolValue()}`, variant: 'destructive' })
    } finally {
      setDataLoading(false)
    }
  }

  // Clear symbol+data when source changes (Yahoo vs MT5 symbols differ)
  useEffect(() => {
    setSymbol(undefined)
    setLoadedData(null)
    setCandleData([])
  }, [loadData, setSymbol, setLoadedData])

  useEffect(() => {
    const fetchSymbols = async () => {
      if (loadData === 'none') {
        setAvailableSymbols([])
        return
      }

      setSymbolsLoading(true)
      try {
        if (loadData === 'yahoo') {
          const res = await axios.get('/api/data/yahoo/symbols')
          setAvailableSymbols(res.data?.symbols || [])
        } else if (loadData === 'mt5') {
          const res = await axios.get('/api/mt5/symbols')
          if (res.data?.success) {
            setAvailableSymbols(res.data?.symbols || [])
          } else {
            setAvailableSymbols([])
          }
        }
      } catch (error) {
        console.error('Failed to fetch symbols:', error)
        setAvailableSymbols([])
      } finally {
        setSymbolsLoading(false)
      }
    }

    fetchSymbols()
  }, [loadData])

  const handleSend = async () => {
    if (!input.trim()) return

    const userMessage: Message = { role: 'user', content: input }
    addMessage(userMessage)
    setInput('')
    setLoading(true)

    try {
      // Auto-fetch more data if user's query needs higher TF
      let currentCandleData = candleData
      if (loadData === 'mt5' && getSymbolValue()) {
        const required = detectRequiredCandles(input)
        if (required > currentCandleData.length) {
          setDataLoading(true)
          const res = await axios.post('/api/mt5/data/latest', {
            symbol: getSymbolValue(),
            timeframe: timeframe,
            count: required
          })
          if (res.data?.success && res.data.data?.length > 0) {
            currentCandleData = res.data.data.map((d: any) => ({
              time: typeof d.time === 'number' ? d.time : Math.floor(new Date(d.time).getTime() / 1000),
              open: d.open || 0,
              high: d.high || 0,
              low: d.low || 0,
              close: d.close || 0
            }))
            setCandleData(currentCandleData)
            const startDate = currentCandleData[0].time
            const endDate = currentCandleData[currentCandleData.length - 1].time
            setLoadedData({
              source: loadData,
              symbol: getSymbolValue(),
              startDate: new Date(startDate * 1000).toLocaleDateString(),
              endDate: new Date(endDate * 1000).toLocaleDateString(),
              candles: currentCandleData.length
            })
          }
          setDataLoading(false)
        }
      }

      // Sliding window: only last N turns sent to AI (store keeps full history for display)
      const recentMessages = [...messages, userMessage].slice(-MAX_CONVERSATION_TURNS)
      const allMessages = recentMessages.map(m => ({
        role: m.role,
        content: m.content
      }))

      const res = await axios.post('/api/ai/chat', {
        messages: allMessages,
        provider,
        model,
        persona,
        symbol: loadData !== 'none' ? getSymbolValue() : null,
        load_market_data: loadData !== 'none' ? loadData : null,
        data_period: dataPeriod,
        timeframe: timeframe,
        candle_data: currentCandleData.length > 0 ? currentCandleData : null,
        debug_rag: true
      })

      const assistantMessage: Message = {
        role: 'assistant',
        content: res.data.message,
        detected_setup: res.data.detected_setup,
        detected_action: res.data.detected_action,
        data_preview: res.data.data_preview,
        detected_chart: res.data.detected_chart,
        execution_output: res.data.execution_output,
        execution_charts: res.data.execution_charts,
        execution_tables: res.data.execution_tables,
        chat_memory_id: res.data.chat_memory_id,
        rag_context: res.data.rag_context
      }

      addMessage(assistantMessage)
    } catch (error: any) {
      console.error('Chat error:', error)
      const status = error.response?.status
      let errorMsg = error.response?.data?.detail || error.message || 'An error occurred'
      if (status === 429) {
        errorMsg = 'AI request limit reached. Please wait a moment before sending another message.'
      }
      addMessage({ 
        role: 'assistant', 
        content: `Error: ${errorMsg}` 
      })
    } finally {
      setLoading(false)
      setDataLoading(false)
    }
  }

  const handleFeedback = async (idx: number, isHelpful: boolean) => {
    if (feedbackSet.includes(idx)) return
    try {
      await axios.post('/api/analytics/feedback', {
        chat_memory_id: messages[idx]?.chat_memory_id,
        is_helpful: isHelpful
      })
      addFeedback(idx)
    } catch (error) {
      console.error('Feedback submission failed:', error)
    }
  }

  const handleTradeExecute = async (setup: any, msgIdx?: number) => {
    if (executingTrade) return
    setExecutingTrade(true)
    setTradeExecMsgIdx(msgIdx ?? null)
    try {
      const payload: any = {
        symbol: setup.symbol,
        action: setup.direction,
        volume: setup.lot_size,
        price: setup.order_type === 'market' ? null : setup.entry_price,
        sl: setup.stop_loss,
        tp: setup.take_profit
      }
      // Link trade to the AI chat message that suggested it
      if (msgIdx !== undefined && messages[msgIdx]?.chat_memory_id) {
        payload.chat_memory_id = messages[msgIdx].chat_memory_id
      }
      await axios.post('/api/trade/order', payload)
      toast({ title: "Trade Executed", description: `${setup.direction} ${setup.symbol} x${setup.lot_size}` })
    } catch (error: any) {
      toast({ title: "Trade Failed", description: error.response?.data?.detail || error.message, variant: 'destructive' })
    } finally {
      setExecutingTrade(false)
      setTradeExecMsgIdx(null)
    }
  }

  const filterMessageContent = (content: string) => {
    // Remove markdown code blocks (python and json)
    return (content || '')
      .replace(/```python[\s\S]*?(?:```|$)/g, '')
      .replace(/```json[\s\S]*?(?:```|$)/g, '')
      .trim()
  }

  return (
    <div className="h-screen flex flex-col overflow-hidden">
      <div className="p-4 pb-2">
        <h1 className="font-heading text-2xl font-bold">AI Analyst</h1>
      </div>

      <div className="flex-1 overflow-y-auto px-4 pb-4 space-y-4">
        <Card className="w-full">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-4 flex-wrap">
              <div>
                <Select value={provider} onValueChange={setProvider}>
                  <SelectTrigger className="w-32">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {availableProviders.map((p) => (
                      <SelectItem key={p.id} value={p.id}>
                        {p.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div>
                <Select value={model} onValueChange={setModel}>
                  <SelectTrigger className="w-48">
                    <SelectValue placeholder="Model" />
                  </SelectTrigger>
                  <SelectContent>
                    {availableProviders.find(p => p.id === provider)?.models?.map((m) => (
                      <SelectItem key={m} value={m}>
                        {m}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div>
                <Select value={persona} onValueChange={setPersona}>
                  <SelectTrigger className="w-44">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="technical_analyst">Technical Analyst</SelectItem>
                    <SelectItem value="risk_manager">Risk Manager</SelectItem>
                    <SelectItem value="quant">Quant / Systematic</SelectItem>
                    <SelectItem value="swing_trader">Swing Trader</SelectItem>
                    <SelectItem value="scalper">Scalper</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className="border-l pl-4">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    if (messages.length === 0) return
                    if (window.confirm('Clear all chat messages?')) {
                      clearMessages()
                    }
                  }}
                >
                  Clear Chat
                </Button>
              </div>
              
              <div className="flex items-center gap-2 border-l pl-4">
                <span className="text-sm text-muted-foreground">Data:</span>
                <Select value={loadData} onValueChange={(v: 'yahoo' | 'mt5' | 'none') => setLoadData(v)}>
                  <SelectTrigger className="w-24">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">None</SelectItem>
                    <SelectItem value="yahoo">Yahoo</SelectItem>
                    <SelectItem value="mt5">MT5</SelectItem>
                  </SelectContent>
                </Select>
                
                {loadData !== 'none' && (
                  <>
                    <Select 
                      value={symbol || ''} 
                      onValueChange={(val) => setSymbol(val)}
                    >
                      <SelectTrigger className="w-32 h-8" disabled={symbolsLoading}>
                        <SelectValue placeholder={symbolsLoading ? "Loading..." : "Symbol"} />
                      </SelectTrigger>
                      <SelectContent>
                        {availableSymbols.length > 0 ? (
                          <>
                            <SelectItem value="custom">Custom...</SelectItem>
                            {availableSymbols.map((sym) => (
                              <SelectItem key={sym.symbol} value={sym.symbol}>
                                {sym.name || sym.symbol}
                              </SelectItem>
                            ))}
                          </>
                        ) : (
                          <div className="p-2 text-sm text-muted-foreground">No symbols</div>
                        )}
                      </SelectContent>
                    </Select>
                    {symbol === 'custom' && (
                      <Input
                        placeholder="Symbol"
                        value={customSymbol}
                        onChange={(e) => setCustomSymbol(e.target.value.toUpperCase())}
                        className="w-20 h-8"
                      />
                    )}
                    {loadData === 'yahoo' && (
                      <Select value={dataPeriod} onValueChange={setDataPeriod}>
                        <SelectTrigger className="w-20 h-8">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="1d">1D</SelectItem>
                          <SelectItem value="1w">1W</SelectItem>
                          <SelectItem value="1mo">1M</SelectItem>
                          <SelectItem value="3mo">3M</SelectItem>
                        </SelectContent>
                      </Select>
                    )}
                    {loadData === 'mt5' && (
                      <Select value={timeframe} onValueChange={(v) => { setTimeframe(v); setCandleData([]); setLoadedData(null) }}>
                        <SelectTrigger className="w-20 h-8">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="1m">M1</SelectItem>
                          <SelectItem value="5m">M5</SelectItem>
                          <SelectItem value="15m">M15</SelectItem>
                          <SelectItem value="30m">M30</SelectItem>
                          <SelectItem value="1h">H1</SelectItem>
                          <SelectItem value="4h">H4</SelectItem>
                          <SelectItem value="1d">D1</SelectItem>
                          <SelectItem value="1w">W1</SelectItem>
                        </SelectContent>
                      </Select>
                    )}

                    <Button 
                      size="sm" 
                      variant="outline"
                      onClick={() => handleLoadData()}
                      disabled={!getSymbolValue() || dataLoading}
                    >
                      {dataLoading ? 'Loading...' : 'Load'}
                    </Button>
                    
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={liveMode}
                        onChange={(e) => setLiveMode(e.target.checked)}
                        className="w-4 h-4 rounded"
                      />
                      <span className="text-xs">Live</span>
                    </label>
                  </>
                )}
              </div>
            </div>

            {loadedData && (
              <div className="flex items-center gap-3 mt-2 text-sm text-green-400 bg-green-500/10 rounded px-3 py-1.5 w-fit">
                <span>{loadedData.symbol}</span>
                <span className="text-muted-foreground">|</span>
                <span>{loadedData.startDate} → {loadedData.endDate}</span>
                <span className="text-muted-foreground">|</span>
                <span>{loadedData.candles} candles</span>
                <Button 
                  size="sm" 
                  variant="ghost" 
                  className="h-6 w-6 p-0 ml-1"
                  onClick={() => { setLoadedData(null); setCandleData([]) }}
                >
                  ✕
                </Button>
              </div>
            )}
          </CardHeader>
        </Card>

        <Card className="w-full min-h-[350px]">
          <CardContent className="p-0">
            <div 
              ref={chartContainerRef} 
              className="w-full h-[350px]"
            />
          </CardContent>
        </Card>

        <div className="space-y-3">
          {messages.length === 0 ? (
            <p className="text-muted-foreground text-center py-8">
              Ask the AI analyst about a symbol...
            </p>
          ) : (
            messages.map((msg, idx) => (
              <div key={idx} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                <div className={`max-w-[85%] p-4 rounded-lg ${msg.role === 'user' ? 'bg-primary text-primary-foreground' : 'bg-muted'}`}>
                  <p className="whitespace-pre-wrap text-sm">
                    {msg.role === 'assistant' ? filterMessageContent(msg.content) : msg.content}
                  </p>
                  
                  {msg.detected_setup && (
                    <div className="mt-3 p-3 bg-background rounded border border-green-500/30">
                      <p className="font-bold text-green-500 text-sm mb-2">🎯 Trade Setup</p>
                      <p className="text-sm">{msg.detected_setup.direction} {msg.detected_setup.symbol}</p>
                      <p className="text-xs text-muted-foreground">
                        Entry: {msg.detected_setup.entry_price} | SL: {msg.detected_setup.stop_loss} | TP: {msg.detected_setup.take_profit}
                      </p>
                      <Button size="sm" className="mt-2" onClick={() => handleTradeExecute(msg.detected_setup, idx)} disabled={executingTrade && tradeExecMsgIdx === idx}>
                        {executingTrade && tradeExecMsgIdx === idx ? 'Executing...' : 'Execute Trade'}
                      </Button>
                    </div>
                  )}

                  {msg.data_preview && (
                    <DataPreviewTable data={msg.data_preview} />
                  )}

                  {msg.detected_chart && (
                    <MiniChart
                      title={msg.detected_chart.title}
                      data={msg.detected_chart}
                      color={msg.detected_chart.color}
                    />
                  )}

                  {msg.execution_charts && msg.execution_charts.length > 0 && (
                    <div className="mt-3 space-y-3">
                      {msg.execution_charts.map((chart, idx) => (
                        <MiniChart
                          key={`exec-chart-${idx}`}
                          title={chart.title}
                          data={chart}
                          color={chart.color || '#2563eb'}
                        />
                      ))}
                    </div>
                  )}

                  {msg.execution_tables && msg.execution_tables.length > 0 && (
                    <div className="mt-3 space-y-3">
                      {msg.execution_tables.map((table, idx) => (
                        <Card key={`exec-table-${idx}`} className="bg-background/50 border-muted">
                          <CardHeader className="py-2 px-3">
                            <CardTitle className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                              {table.title}
                            </CardTitle>
                          </CardHeader>
                          <CardContent className="p-0">
                            <div className="max-h-[400px] overflow-auto rounded-md border border-border/50">
                              <table className="w-full text-xs border-collapse relative">
                                {table.columns && table.columns.length > 0 && (
                                  <thead className="sticky top-0 z-10">
                                    <tr>
                                      {table.columns.map((col, colIdx) => (
                                        <th
                                          key={colIdx}
                                          className="border-b border-border bg-muted/95 backdrop-blur-sm px-3 py-2 text-left font-semibold text-foreground shadow-sm"
                                        >
                                          {col}
                                        </th>
                                      ))}
                                    </tr>
                                  </thead>
                                )}
                                <tbody>
                                  {(table.rows || []).map((row, rowIdx) => (
                                    <tr key={rowIdx} className="border-b border-border/30 hover:bg-primary/5 transition-colors">
                                      {(row || []).map((cell, cellIdx) => (
                                        <td
                                          key={cellIdx}
                                          className="border-r border-border/10 px-3 py-1.5 text-foreground last:border-r-0"
                                        >
                                          {cell !== null && cell !== undefined ? String(cell) : ''}
                                        </td>
                                      ))}
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          </CardContent>
                        </Card>
                      ))}
                    </div>
                  )}

                  {msg.execution_output && (
                    <div className="mt-3 p-3 bg-zinc-950 rounded border border-zinc-800 font-mono text-xs">
                      <p className="text-zinc-500 mb-1 border-b border-zinc-800 pb-1">Execution Output:</p>
                      <pre className="text-zinc-300 overflow-x-auto whitespace-pre-wrap">{msg.execution_output}</pre>
                    </div>
                  )}

                  {msg.rag_context && (
                    <details className="mt-3 group">
                      <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground select-none">
                        🧠 RAG context used ({msg.rag_context.length} chars)
                      </summary>
                      <pre className="mt-2 p-3 bg-background rounded border border-border/50 text-xs text-muted-foreground overflow-x-auto whitespace-pre-wrap max-h-64 overflow-y-auto">{msg.rag_context}</pre>
                    </details>
                  )}



                  
                  {msg.role === 'assistant' && (
                    <div className="mt-2 pt-2 border-t border-border/30 flex justify-end gap-2">
                      {feedbackSet.includes(idx) ? (
                        <span className="text-xs text-muted-foreground">Thanks!</span>
                      ) : (
                        <>
                          <button onClick={() => handleFeedback(idx, true)} className="text-xs text-muted-foreground hover:text-green-500 transition-colors" title="Helpful">👍</button>
                          <button onClick={() => handleFeedback(idx, false)} className="text-xs text-muted-foreground hover:text-red-500 transition-colors" title="Not helpful">👎</button>
                        </>
                      )}
                    </div>
                  )}
                </div>
              </div>
            ))
          )}
          {loading && (
            <div className="flex justify-start">
              <div className="max-w-[85%] p-4 rounded-lg bg-muted border border-primary/20 animate-pulse">
                <div className="flex items-center gap-3">
                  <div className="flex space-x-1">
                    <div className="w-1.5 h-1.5 bg-primary rounded-full animate-bounce [animation-delay:-0.3s]"></div>
                    <div className="w-1.5 h-1.5 bg-primary rounded-full animate-bounce [animation-delay:-0.15s]"></div>
                    <div className="w-1.5 h-1.5 bg-primary rounded-full animate-bounce"></div>
                  </div>
                  <span className="text-sm font-medium text-primary">AI is performing deep quantitative analysis...</span>
                </div>
                <p className="mt-2 text-xs text-muted-foreground">
                  Evaluating {candleData.length}+ candles, checking technical indicators, and scanning for trade setups.
                  This may take a moment for long-form reasoning.
                </p>
              </div>
            </div>
          )}
        </div>

        <div className="flex gap-2 sticky bottom-0 bg-background py-2">
          <Input
            placeholder="Ask about a symbol..."
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSend()}
            disabled={loading}
            className="flex-1"
          />
          <Button onClick={handleSend} disabled={loading}>
            {loading ? '...' : 'Send'}
          </Button>
        </div>
      </div>
    </div>
  )
}