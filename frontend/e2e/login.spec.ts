import { test, expect } from '@playwright/test'
import { adminPassword } from './credentials'

const ADMIN_PW = adminPassword()
const NAV_TIMEOUT = 20000

test.describe('Login Flow', () => {
  test('shows login page and signs in with valid credentials', async ({ page }) => {
    await page.goto('/login')
    await page.fill('#username', 'admin')
    await page.fill('#password', ADMIN_PW)
    await page.click('button:has-text("Sign In")')
    await page.getByRole('link', { name: 'Dashboard' }).waitFor({ state: 'visible', timeout: NAV_TIMEOUT })
  })

  test('shows error on invalid credentials', async ({ page }) => {
    await page.goto('/login')
    await page.fill('#username', 'admin')
    await page.fill('#password', 'wrong_password')
    await page.click('button:has-text("Sign In")')
    await expect(page.getByText('Login failed', { exact: true }).first()).toBeVisible({ timeout: 10000 })
  })

  test('redirects to login when not authenticated', async ({ page }) => {
    await page.goto('/')
    await page.waitForURL('**/login', { timeout: 5000 })
  })
})
