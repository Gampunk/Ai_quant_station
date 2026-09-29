import { useEffect, useState } from 'react'
import axios from 'axios'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useToast } from '@/hooks/use-toast'
import { useAuthStore } from '@/store/authStore'

type NumberKey = 'autopilot_risk_pct' | 'max_trade_risk_pct' | 'max_open_positions' | 'daily_loss_pct' |
  'min_margin_level' | 'max_pending_distance_pct'

interface RiskSettings extends Record<NumberKey, number> {
  id: number
  require_stop_loss: boolean
  created_at: string | null
  changed_by_name: string | null
  reason: string | null
}

interface RiskStatus {
  equity: number | null
  start_of_day_equity: number | null
  daily_loss_pct: number | null
  margin_level: number | null
  open_positions: number
}

interface Decision {
  id: number
  created_at: string
  source: string
  action: string
  symbol: string
  reason_code: string | null
  message: string | null
}

const FIELDS: { key: NumberKey; label: string; help: string }[] = [
  { key: 'autopilot_risk_pct', label: 'Autopilot risk per trade (%)', help: 'The autopilot sizes each trade so hitting the stop loses this share of equity.' },
  { key: 'max_trade_risk_pct', label: 'Most any trade may risk (%)', help: 'Applies to every order, from any page.' },
  { key: 'daily_loss_pct', label: 'Daily loss limit (%)', help: 'No new orders after falling this far below the day’s starting equity. 0 turns it off.' },
  { key: 'max_open_positions', label: 'Most trades open at once', help: '0 means no limit.' },
  { key: 'min_margin_level', label: 'Lowest margin level (%)', help: 'No new orders while the margin level is below this. 0 turns it off.' },
  { key: 'max_pending_distance_pct', label: 'Pending order distance (%)', help: 'How far a pending order’s price may be from the market. 0 turns it off.' },
]

const fmt = (n: number | null | undefined, digits = 2) => (n === null || n === undefined ? '—' : n.toFixed(digits))

export default function RiskSettingsCard() {
  const { toast } = useToast()
  const isAdmin = useAuthStore((s) => s.user?.role === 'admin')
  const [current, setCurrent] = useState<RiskSettings | null>(null)
  const [draft, setDraft] = useState<Record<string, string | boolean>>({})
  const [reason, setReason] = useState('')
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState<RiskStatus | null>(null)
  const [history, setHistory] = useState<RiskSettings[]>([])
  const [refusals, setRefusals] = useState<Decision[]>([])

  const load = async () => {
    try {
      const res = await axios.get('/api/risk/settings')
      const s: RiskSettings = res.data.settings
      setCurrent(s)
      setDraft({ ...Object.fromEntries(FIELDS.map((f) => [f.key, String(s[f.key])])), require_stop_loss: s.require_stop_loss })
    } catch { /* shown as unavailable */ }
    axios.get('/api/risk/settings/history', { params: { limit: 5 } }).then((r) => setHistory(r.data.versions || [])).catch(() => {})
    axios.get('/api/risk/decisions', { params: { outcome: 'refused', limit: 10 } }).then((r) => setRefusals(r.data.decisions || [])).catch(() => {})
    axios.get('/api/risk/status').then((r) => setStatus(r.data)).catch(() => setStatus(null))
  }
  useEffect(() => { load() }, [])

  const save = async () => {
    if (!current) return
    const changes: Record<string, number | boolean> = {}
    for (const f of FIELDS) {
      const value = Number(draft[f.key])
      if (Number.isNaN(value)) { toast({ title: 'Error', description: `${f.label} must be a number`, variant: 'destructive' }); return }
      if (value !== current[f.key]) changes[f.key] = value
    }
    if (draft.require_stop_loss !== current.require_stop_loss) changes.require_stop_loss = !!draft.require_stop_loss
    if (Object.keys(changes).length === 0) { toast({ title: 'Nothing changed' }); return }
    if (!reason.trim()) { toast({ title: 'Error', description: 'Say why you are changing the limits', variant: 'destructive' }); return }
    setSaving(true)
    try {
      await axios.put('/api/risk/settings', { reason, ...changes })
      toast({ title: 'Saved', description: 'New limits apply to the next order' })
      setReason('')
      await load()
    } catch (error: any) {
      toast({ title: 'Error', description: error.response?.data?.detail || 'Could not save the limits', variant: 'destructive' })
    } finally { setSaving(false) }
  }

  return (
    <Card>
      <CardHeader className="px-3 sm:px-4 md:px-6 pt-3 sm:pt-4 md:pt-6 pb-2 sm:pb-3">
        <CardTitle className="text-sm sm:text-base md:text-lg">Risk Limits</CardTitle>
        <CardDescription className="text-xs sm:text-sm">
          Every new order is checked against these first. Closing a trade is never blocked.
          {!isAdmin && ' Only an admin can change them.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="px-3 sm:px-4 md:px-6 pb-3 sm:pb-4 md:pb-6 space-y-4">
        {!current ? (
          <p className="text-xs text-muted-foreground">Risk limits unavailable.</p>
        ) : (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 sm:gap-4">
              {FIELDS.map((f) => (
                <div key={f.key}>
                  <Label className="text-xs sm:text-sm">{f.label}</Label>
                  <Input type="number" step="any" value={String(draft[f.key] ?? '')} disabled={!isAdmin}
                    onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })} className="text-sm h-9 sm:h-10" />
                  <p className="text-[11px] text-muted-foreground mt-1">{f.help}</p>
                </div>
              ))}
            </div>
            <label className="flex items-center gap-2 text-xs sm:text-sm">
              <input type="checkbox" checked={!!draft.require_stop_loss} disabled={!isAdmin}
                onChange={(e) => setDraft({ ...draft, require_stop_loss: e.target.checked })} />
              Every order needs a stop loss (the autopilot always uses one)
            </label>
            {isAdmin && (
              <div className="flex flex-col sm:flex-row gap-2">
                <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why are you changing them?"
                  className="text-sm h-9 sm:h-10 flex-1" />
                <Button onClick={save} disabled={saving} size="sm" className="text-xs sm:text-sm">{saving ? 'Saving...' : 'Save Limits'}</Button>
              </div>
            )}
            <p className="text-[11px] text-muted-foreground">
              Version {current.id}{current.changed_by_name ? `, set by ${current.changed_by_name}` : ''}{current.reason ? `: ${current.reason}` : ''}
            </p>
          </>
        )}

        <div className="rounded border p-2 sm:p-3 text-xs space-y-1">
          <p className="font-medium">Today</p>
          {status ? (
            <>
              <p>Equity {fmt(status.equity)} (started the day at {fmt(status.start_of_day_equity)}), down {fmt(status.daily_loss_pct)}%</p>
              <p>{status.open_positions} trades open, margin level {status.margin_level ? `${fmt(status.margin_level, 0)}%` : 'not in use'}</p>
            </>
          ) : <p className="text-muted-foreground">Connector unreachable.</p>}
        </div>

        {history.length > 1 && (
          <div className="text-xs space-y-1">
            <p className="font-medium">Recent changes</p>
            {history.map((v) => (
              <p key={v.id} className="text-muted-foreground">
                v{v.id} {v.created_at?.slice(0, 16).replace('T', ' ')} {v.changed_by_name || 'system'}: {v.reason}
              </p>
            ))}
          </div>
        )}

        {refusals.length > 0 && (
          <div className="text-xs space-y-1">
            <p className="font-medium">Recently refused orders</p>
            {refusals.map((d) => (
              <p key={d.id} className="text-muted-foreground">
                {d.created_at.slice(0, 16).replace('T', ' ')} {d.source} {d.action} {d.symbol}: {d.message}
              </p>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
