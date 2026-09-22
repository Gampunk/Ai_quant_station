import { test, expect, Page } from '@playwright/test'
import { adminPassword } from './credentials'

const NAV_TIMEOUT = 20000
let _cachedToken: string | null = null

async function getToken(): Promise<string> {
  if (_cachedToken) return _cachedToken
  for (let attempt = 0; attempt < 3; attempt++) {
    if (attempt > 0) await new Promise(r => setTimeout(r, 1000))
    const resp = await fetch('http://localhost:8002/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'admin', password: adminPassword() }),
    })
    if (resp.ok) {
      const data: any = await resp.json()
      _cachedToken = data.access_token
      return _cachedToken!
    }
  }
  throw new Error('Failed to get auth token')
}

async function login(page: Page) {
  const token = await getToken()
  const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')))
  const authState = JSON.stringify({
    state: {
      accessToken: token,
      storedRefreshToken: token,
      user: { username: payload.sub, userId: payload.user_id, role: payload.role, name: payload.name },
      isAuthenticated: true,
    },
    version: 0,
  })
  await page.goto('/login')
  await page.waitForURL('**/login', { timeout: 5000 })
  await page.evaluate((s) => { sessionStorage.setItem('auth-storage', s) }, authState)
  await page.goto('/')
  await page.waitForURL('**/', { timeout: NAV_TIMEOUT })
}

test.describe('Backtest Page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page)
  })

  test('loads backtest page with run button', async ({ page }) => {
    await page.getByRole('link', { name: /Backtest/ }).click()
    await page.waitForURL('**/backtest')
    await expect(page.getByRole('heading', { name: /Prompt Backtest/ })).toBeVisible()
    await expect(page.getByRole('button', { name: /Run/ })).toBeVisible()
  })
})
