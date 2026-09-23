import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface AutopilotState {
  intervalVal: string
  lotSize: string
  symbol: string
  provider: string
  model: string
  selectedPromptIds: string[]
  maxTradesPerDay: string
  maxDailyLoss: string

  setIntervalVal: (v: string) => void
  setLotSize: (v: string) => void
  setSymbol: (v: string) => void
  setProvider: (v: string) => void
  setModel: (v: string) => void
  setSelectedPromptIds: (v: string[]) => void
  setMaxTradesPerDay: (v: string) => void
  setMaxDailyLoss: (v: string) => void
}

export const useAutopilotStore = create<AutopilotState>()(
  persist(
    (set) => ({
      intervalVal: '300',
      lotSize: '0.10',
      symbol: 'XAUUSD',
      provider: 'nvidia',
      model: 'qwen/qwen3.5-122b-a10b',
      selectedPromptIds: [],
      maxTradesPerDay: '10',
      maxDailyLoss: '-50',

      setIntervalVal: (v) => set({ intervalVal: v }),
      setLotSize: (v) => set({ lotSize: v }),
      setSymbol: (v) => set({ symbol: v }),
      setProvider: (v) => set({ provider: v }),
      setModel: (v) => set({ model: v }),
      setSelectedPromptIds: (v) => set({ selectedPromptIds: v }),
      setMaxTradesPerDay: (v) => set({ maxTradesPerDay: v }),
      setMaxDailyLoss: (v) => set({ maxDailyLoss: v }),
    }),
    {
      name: 'autopilot-storage',
      partialize: (state) => ({
        intervalVal: state.intervalVal,
        lotSize: state.lotSize,
        symbol: state.symbol,
        provider: state.provider,
        model: state.model,
        selectedPromptIds: state.selectedPromptIds,
        maxTradesPerDay: state.maxTradesPerDay,
        maxDailyLoss: state.maxDailyLoss,
      }),
    }
  )
)
