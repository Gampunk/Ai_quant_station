import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useEffect, useState } from 'react'
import { useToast } from '@/hooks/use-toast'
import api from '@/lib/api'

interface AccountInfo {
  balance: number
  equity: number
  margin: number
  free_margin: number
  margin_level: number
  profit: number
}

interface Position {
  ticket: number
  symbol: string
  direction: string
  volume: number
  profit: number
}

export default function DashboardPage() {
  const { toast } = useToast()
  const [account, setAccount] = useState<AccountInfo | null>(null)
  const [positions, setPositions] = useState<Position[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const fetchData = async () => {
      try {
        const posRes = await api.get('/mt5/positions')
        const d = posRes.data || {}
        setAccount({
          balance: d.balance || 0,
          equity: d.equity || 0,
          margin: d.margin || 0,
          free_margin: d.free_margin || 0,
          margin_level: d.margin_level || 0,
          profit: d.total_profit || 0
        })
        setPositions(d.positions || [])
      } catch (error) {
        toast({ title: "Connection Error", description: "Could not load MT5 data", variant: 'destructive' })
      } finally {
        setLoading(false)
      }
    }
    fetchData()
  // eslint-disable-next-line react-hooks/exhaustive-deps -- loads once on mount
  }, [])

  if (loading) {
    return (
      <div className="p-4 sm:p-6 md:p-8 flex items-center justify-center min-h-[300px] sm:min-h-[400px]">
        <div className="text-muted-foreground text-sm sm:text-base">Loading dashboard...</div>
      </div>
    )
  }

  return (
    <div className="p-3 sm:p-6 md:p-8">
      <h1 className="font-heading text-xl sm:text-2xl md:text-3xl font-bold mb-4 sm:mb-6 md:mb-8">Dashboard</h1>

      {/* Metrics grid - responsive: 1 col on mobile, 2 on tablet, 4 on desktop */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4 mb-6 sm:mb-8">
        <Card>
          <CardHeader className="pb-1 sm:pb-2 px-3 sm:px-4 pt-3 sm:pt-4">
            <CardTitle className="text-xs sm:text-sm font-medium text-muted-foreground">Balance</CardTitle>
          </CardHeader>
          <CardContent className="px-3 sm:px-4 pb-3 sm:pb-4">
            <div className="text-lg sm:text-xl md:text-2xl font-bold">
              ${account?.balance?.toFixed(2) || '0.00'}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-1 sm:pb-2 px-3 sm:px-4 pt-3 sm:pt-4">
            <CardTitle className="text-xs sm:text-sm font-medium text-muted-foreground">Equity</CardTitle>
          </CardHeader>
          <CardContent className="px-3 sm:px-4 pb-3 sm:pb-4">
            <div className="text-lg sm:text-xl md:text-2xl font-bold">
              ${account?.equity?.toFixed(2) || '0.00'}
            </div>
            <div className={`text-xs sm:text-sm ${(account?.profit || 0) >= 0 ? 'text-green-500' : 'text-red-500'}`}>
              {account?.profit ? `$${account.profit.toFixed(2)}` : '$0.00'}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-1 sm:pb-2 px-3 sm:px-4 pt-3 sm:pt-4">
            <CardTitle className="text-xs sm:text-sm font-medium text-muted-foreground">Margin</CardTitle>
          </CardHeader>
          <CardContent className="px-3 sm:px-4 pb-3 sm:pb-4">
            <div className="text-lg sm:text-xl md:text-2xl font-bold">
              ${account?.margin?.toFixed(2) || '0.00'}
            </div>
            <div className="text-xs sm:text-sm text-muted-foreground">
              Free: ${account?.free_margin?.toFixed(2) || '0.00'}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-1 sm:pb-2 px-3 sm:px-4 pt-3 sm:pt-4">
            <CardTitle className="text-xs sm:text-sm font-medium text-muted-foreground">Margin Level</CardTitle>
          </CardHeader>
          <CardContent className="px-3 sm:px-4 pb-3 sm:pb-4">
            <div className="text-lg sm:text-xl md:text-2xl font-bold">
              {account?.margin_level?.toFixed(1) || '0'}%
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Open Positions */}
      <Card>
        <CardHeader className="px-3 sm:px-4 md:px-6 pt-3 sm:pt-4 md:pt-6 pb-2 sm:pb-3">
          <CardTitle className="text-sm sm:text-base md:text-lg">Open Positions ({positions.length})</CardTitle>
        </CardHeader>
        <CardContent className="px-3 sm:px-4 md:px-6 pb-3 sm:pb-4 md:pb-6">
          {positions.length === 0 ? (
            <p className="text-muted-foreground text-center py-6 sm:py-8 text-sm sm:text-base">
              No open positions. Start trading in the Terminal.
            </p>
          ) : (
            <div className="space-y-2">
              {positions.map((pos) => (
                <div key={pos.ticket} className="flex items-center justify-between p-2 sm:p-3 bg-muted rounded-lg gap-2">
                  <div className="min-w-0">
                    <span className="font-medium text-sm sm:text-base">{pos.symbol}</span>
                    <span className={`ml-1 sm:ml-2 text-xs sm:text-sm ${pos.direction === 'BUY' ? 'text-green-500' : 'text-red-500'}`}>
                      {pos.direction}
                    </span>
                    <span className="ml-1 sm:ml-2 text-xs sm:text-sm text-muted-foreground">x{pos.volume}</span>
                  </div>
                  <div className={`shrink-0 text-sm sm:text-base ${pos.profit >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                    ${(pos.profit || 0).toFixed(2)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
