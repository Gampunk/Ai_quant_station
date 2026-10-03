import { create } from 'zustand'
import { persist } from 'zustand/middleware'

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

interface LoadedDataInfo {
  source: string
  symbol: string
  startDate: string
  endDate: string
  candles: number
}

interface AIAnalystState {
  messages: Message[]
  provider: string
  model: string
  persona: string
  symbol: string | undefined
  customSymbol: string
  loadData: 'yahoo' | 'mt5' | 'none'
  dataPeriod: string
  timeframe: string
  loadedData: LoadedDataInfo | null
  liveMode: boolean
  feedbackSet: number[]

  setMessages: (messages: Message[]) => void
  addMessage: (message: Message) => void
  setProvider: (provider: string) => void
  setModel: (model: string) => void
  setPersona: (persona: string) => void
  setSymbol: (symbol: string | undefined) => void
  setCustomSymbol: (customSymbol: string) => void
  setLoadData: (loadData: 'yahoo' | 'mt5' | 'none') => void
  setDataPeriod: (dataPeriod: string) => void
  setTimeframe: (timeframe: string) => void
  setLoadedData: (loadedData: LoadedDataInfo | null) => void
  setLiveMode: (liveMode: boolean) => void
  addFeedback: (idx: number) => void
  clearMessages: () => void
  clearAll: () => void
}

export const useAIAnalystStore = create<AIAnalystState>()(
  persist(
    (set) => ({
      messages: [],
      provider: 'nvidia',
      model: 'qwen/qwen3.5-122b-a10b',
      persona: 'technical_analyst',
      symbol: undefined,
      customSymbol: '',
      loadData: 'none',
      dataPeriod: '1mo',
          timeframe: '1m',
      loadedData: null,
      liveMode: false,
      feedbackSet: [],

      setMessages: (messages) => set({ messages }),
      addMessage: (message) => set((state) => ({ messages: [...state.messages, message] })),
      setProvider: (provider) => set({ provider }),
      setModel: (model) => set({ model }),
      setPersona: (persona) => set({ persona }),
      setSymbol: (symbol) => set({ symbol }),
      setCustomSymbol: (customSymbol) => set({ customSymbol }),
      setLoadData: (loadData) => set({ loadData }),
      setDataPeriod: (dataPeriod) => set({ dataPeriod }),
      setTimeframe: (timeframe) => set({ timeframe }),
      setLoadedData: (loadedData) => set({ loadedData }),
      setLiveMode: (liveMode) => set({ liveMode }),
      addFeedback: (idx) =>
        set((state) => ({
          feedbackSet: state.feedbackSet.includes(idx)
            ? state.feedbackSet
            : [...state.feedbackSet, idx],
        })),
      clearMessages: () => set({ messages: [], feedbackSet: [] }),
      clearAll: () =>
        set({
          messages: [],
          provider: 'nvidia',
          model: 'qwen/qwen3.5-122b-a10b',
          persona: 'technical_analyst',
          symbol: undefined,
          customSymbol: '',
          loadData: 'none',
          dataPeriod: '1mo',
      timeframe: '1m',
          loadedData: null,
          liveMode: false,
          feedbackSet: [],
        }),
    }),
    {
      name: 'ai-analyst-storage',
      partialize: (state) => ({
        messages: state.messages,
        provider: state.provider,
        model: state.model,
        persona: state.persona,
        symbol: state.symbol,
        customSymbol: state.customSymbol,
        loadData: state.loadData,
        dataPeriod: state.dataPeriod,
        timeframe: state.timeframe,
        loadedData: state.loadedData,
        liveMode: state.liveMode,
        feedbackSet: state.feedbackSet,
      }),
    }
  )
)
