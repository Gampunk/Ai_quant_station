import { useState, useEffect } from 'react'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useToast } from '@/hooks/use-toast'
import axios from 'axios'

export default function SettingsPage() {
  const { toast } = useToast()
  const [mt5Testing, setMt5Testing] = useState(false)
  const [mt5TestResult, setMt5TestResult] = useState<{success: boolean, message: string} | null>(null)
  const [aiTesting, setAiTesting] = useState(false)
  const [aiTestResult, setAiTestResult] = useState<{success: boolean, message: string} | null>(null)
  const [testProvider, setTestProvider] = useState('nvidia')
  const [testModel, setTestModel] = useState('qwen/qwen3.5-122b-a10b')

  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [passwordLoading, setPasswordLoading] = useState(false)

  const handleChangePassword = async () => {
    if (!currentPassword) { toast({ title: 'Error', description: 'Current password is required', variant: 'destructive' }); return }
    if (!newPassword || newPassword.length < 12) { toast({ title: 'Error', description: 'New password must be at least 12 characters', variant: 'destructive' }); return }
    setPasswordLoading(true)
    try {
      await axios.put('/api/auth/password', { current_password: currentPassword, new_password: newPassword })
      toast({ title: 'Success', description: 'Password changed successfully' })
      setCurrentPassword(''); setNewPassword('')
    } catch (error: any) {
      toast({ title: 'Error', description: error.response?.data?.detail || 'Failed to change password', variant: 'destructive' })
    } finally { setPasswordLoading(false) }
  }

  // The connector is configured on the server. This only checks whether it answers.
  const testMt5Connection = async () => {
    setMt5Testing(true); setMt5TestResult(null)
    try {
      const res = await axios.get('/api/mt5/health')
      const ok = !!res.data?.mt5_initialized
      setMt5TestResult({ success: ok, message: ok ? 'Connector reachable and MT5 terminal connected.' : 'Connector reachable, but the MT5 terminal is not connected.' })
    } catch (error: any) {
      setMt5TestResult({ success: false, message: error.response?.data?.detail || 'MT5 connector unreachable' })
    } finally { setMt5Testing(false) }
  }

  const [availableProviders, setAvailableProviders] = useState<{id: string, name: string, models: string[], has_key: boolean}[]>([])
  const [userKeyInputs, setUserKeyInputs] = useState<Record<string, string>>({})
  const [userKeyStatus, setUserKeyStatus] = useState<Record<string, boolean>>({})
  const [savingKeys, setSavingKeys] = useState(false)
  useEffect(() => { axios.get('/api/ai/providers').then(res => setAvailableProviders(res.data?.providers || [])).catch(() => {}) }, [])
  useEffect(() => { axios.get('/api/ai/user-keys').then(res => setUserKeyStatus(res.data?.providers || {})).catch(() => {}) }, [])
  useEffect(() => {
    const prov = availableProviders.find(p => p.id === testProvider)
    if (prov?.models?.length) setTestModel(prov.models[0])
  }, [testProvider, availableProviders])

  const testConnection = async () => {
    setAiTesting(true); setAiTestResult(null)
    try {
      await axios.post('/api/ai/test', { provider: testProvider, model: testModel })
      setAiTestResult({ success: true, message: `Connection to ${testProvider} successful!` })
      toast({ title: 'Success', description: 'AI connection working' })
    } catch (error: any) { setAiTestResult({ success: false, message: error.response?.data?.detail || 'Connection failed' }); toast({ title: 'Error', description: 'Connection failed', variant: 'destructive' }) }
    finally { setAiTesting(false) }
  }

  return (
    <div className="p-3 sm:p-6 md:p-8 max-w-4xl mx-auto">
      <h1 className="font-heading text-xl sm:text-2xl md:text-3xl font-bold mb-4 sm:mb-6 md:mb-8">Settings</h1>

      <div className="space-y-4 sm:space-y-6">
        {/* Account */}
        <Card>
          <CardHeader className="px-3 sm:px-4 md:px-6 pt-3 sm:pt-4 md:pt-6 pb-2 sm:pb-3">
            <CardTitle className="text-sm sm:text-base md:text-lg">Account</CardTitle>
            <CardDescription className="text-xs sm:text-sm">Change your password</CardDescription>
          </CardHeader>
          <CardContent className="px-3 sm:px-4 md:px-6 pb-3 sm:pb-4 md:pb-6 space-y-3 sm:space-y-4">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 sm:gap-4">
              <div>
                <Label className="text-xs sm:text-sm">Current Password</Label>
                <Input type="password" value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} placeholder="Current password" className="text-sm h-9 sm:h-10" />
              </div>
              <div>
                <Label className="text-xs sm:text-sm">New Password</Label>
                <Input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} placeholder="Min 6 chars" className="text-sm h-9 sm:h-10" />
              </div>
            </div>
            <Button variant="outline" onClick={handleChangePassword} disabled={passwordLoading} className="text-xs sm:text-sm w-full sm:w-auto">
              {passwordLoading ? 'Updating...' : 'Update Password'}
            </Button>
          </CardContent>
        </Card>

        {/* AI Providers */}
        <Card>
          <CardHeader className="px-3 sm:px-4 md:px-6 pt-3 sm:pt-4 md:pt-6 pb-2 sm:pb-3">
            <CardTitle className="text-sm sm:text-base md:text-lg">AI Providers</CardTitle>
            <CardDescription className="text-xs sm:text-sm">Configure your AI API keys</CardDescription>
          </CardHeader>
          <CardContent className="px-3 sm:px-4 md:px-6 pb-3 sm:pb-4 md:pb-6 space-y-3 sm:space-y-4">
            <div className="space-y-3 sm:space-y-4">
              <p className="text-xs text-muted-foreground">Your API keys are encrypted and stored on the server. They are never saved in the browser.</p>
              {availableProviders.map(p => (
                <div key={p.id}>
                  <Label className="text-xs sm:text-sm">{p.name}</Label>
                  <div className="flex gap-2 items-center">
                    <Input
                      type="password"
                      placeholder={userKeyStatus[p.id] ? "•••••••• (saved)" : "Enter your API key..."}
                      value={userKeyInputs[p.id] || ''}
                      onChange={(e) => setUserKeyInputs({...userKeyInputs, [p.id]: e.target.value})}
                      className="text-sm h-9 sm:h-10 bg-background flex-1"
                    />
                    {userKeyStatus[p.id] && <span className="text-xs text-green-500 shrink-0">Saved</span>}
                  </div>
                </div>
              ))}
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 sm:gap-4">
              <div>
                <Label className="text-xs sm:text-sm">Provider</Label>
                <Select value={testProvider} onValueChange={setTestProvider}>
                  <SelectTrigger className="text-sm"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {availableProviders.map((p) => (<SelectItem key={p.id} value={p.id} className="text-sm">{p.name}</SelectItem>))}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label className="text-xs sm:text-sm">Model</Label>
                <Select value={testModel} onValueChange={setTestModel}>
                  <SelectTrigger className="text-sm"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {availableProviders.find(p => p.id === testProvider)?.models.map((m) => (
                      <SelectItem key={m} value={m} className="text-sm text-xs">{m.length > 25 ? m.substring(0, 25) + '...' : m}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
            <div className="flex flex-col sm:flex-row gap-2">
              <Button
                onClick={async () => {
                  setSavingKeys(true)
                  try {
                    const keys: Record<string, string> = {}
                    availableProviders.forEach(p => {
                      if (userKeyInputs[p.id]) keys[p.id] = userKeyInputs[p.id]
                    })
                    if (Object.keys(keys).length === 0) { toast({ title: 'No keys to save', variant: 'destructive' }); return }
                    await axios.post('/api/ai/user-keys', keys)
                    setUserKeyInputs({})
                    const res = await axios.get('/api/ai/user-keys')
                    setUserKeyStatus(res.data?.providers || {})
                    toast({ title: 'Success', description: 'API keys saved securely' })
                  } catch { toast({ title: 'Error', description: 'Failed to save keys', variant: 'destructive' }) }
                  finally { setSavingKeys(false) }
                }}
                disabled={savingKeys}
                size="sm"
                className="text-xs sm:text-sm"
              >{savingKeys ? 'Saving...' : 'Save Keys'}</Button>
              <Button onClick={testConnection} disabled={aiTesting} variant="outline" size="sm" className="text-xs sm:text-sm">{aiTesting ? 'Testing...' : 'Test Connection'}</Button>
            </div>
            {aiTestResult && (
              <div className={`p-2 sm:p-3 rounded text-xs sm:text-sm ${aiTestResult.success ? 'bg-green-500/10 text-green-500' : 'bg-red-500/10 text-red-500'}`}>
                {aiTestResult.message}
              </div>
            )}
          </CardContent>
        </Card>

        {/* MT5 Connector */}
        <Card>
          <CardHeader className="px-3 sm:px-4 md:px-6 pt-3 sm:pt-4 md:pt-6 pb-2 sm:pb-3">
            <CardTitle className="text-sm sm:text-base md:text-lg">MT5 Connection</CardTitle>
            <CardDescription className="text-xs sm:text-sm">
              Every trade goes through the server's MT5 connector. Its address, token and terminal are set on the server, not here.
            </CardDescription>
          </CardHeader>
          <CardContent className="px-3 sm:px-4 md:px-6 pb-3 sm:pb-4 md:pb-6 space-y-3 sm:space-y-4">
            <Button variant="outline" onClick={testMt5Connection} disabled={mt5Testing} size="sm" className="text-xs sm:text-sm">{mt5Testing ? 'Checking...' : 'Check Connection'}</Button>
            {mt5TestResult && (
              <div className={`p-2 sm:p-3 rounded text-xs sm:text-sm ${mt5TestResult.success ? 'bg-green-500/10 text-green-500' : 'bg-red-500/10 text-red-500'}`}>
                {mt5TestResult.message}
              </div>
            )}
          </CardContent>
        </Card>


      </div>
    </div>
  )
}
