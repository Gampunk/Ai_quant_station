import { useEffect, useState } from 'react'
import axios from 'axios'

export interface HaltState {
  halted: boolean
  changed_by_name: string | null
  reason: string | null
  created_at: string | null
}

/** A red bar across every page while the kill switch is on. Checks every 30 seconds. */
export default function TradingHaltBanner() {
  const [halt, setHalt] = useState<HaltState | null>(null)

  useEffect(() => {
    const check = () => {
      axios.get('/api/risk/halt').then((r) => setHalt(r.data)).catch(() => { /* keep the last known state */ })
    }
    check()
    const id = setInterval(check, 30000)
    return () => clearInterval(id)
  }, [])

  if (!halt?.halted) return null
  return (
    <div role="alert" className="bg-red-600 text-white text-xs sm:text-sm px-3 py-2">
      <strong>Trading is stopped.</strong> New orders are refused and autopilots are off.
      {' '}Stopped by {halt.changed_by_name || 'an operator'}
      {halt.created_at ? ` at ${halt.created_at.slice(0, 16).replace('T', ' ')} UTC` : ''}
      {halt.reason ? `: ${halt.reason}` : ''}. Closing trades still works.
    </div>
  )
}
