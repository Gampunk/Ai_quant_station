import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import axios from 'axios'
import api from '@/lib/api'

// A fake transport, so no test ever reaches the network. Real calls made these
// tests depend on a connection being refused quickly, which stopped being true
// once WSL switched to mirrored networking: unused ports now hang instead.
function fakeTransport() {
  return vi.fn(async (config: any) => ({
    data: { retried: true }, status: 200, statusText: 'OK', headers: {}, config,
  }))
}

describe('API client', () => {
  beforeEach(() => {
    sessionStorage.clear()
  })

  it('has correct base URL', () => {
    expect(api.defaults.baseURL).toBe('/api')
  })

  it('exports default axios instance with HTTP methods', () => {
    expect(api).toBeDefined()
    expect(typeof api.get).toBe('function')
    expect(typeof api.post).toBe('function')
    expect(typeof api.put).toBe('function')
    expect(typeof api.delete).toBe('function')
  })

  it('has request interceptors registered', () => {
    expect(api.interceptors.request).toBeDefined()
    expect(typeof api.interceptors.request.use).toBe('function')
  })

  it('has response interceptors registered', () => {
    expect(api.interceptors.response).toBeDefined()
    expect(typeof api.interceptors.response.use).toBe('function')
  })

  describe('request interceptor', () => {
    let handlers: any[]

    beforeEach(() => {
      handlers = (api.interceptors.request as any).handlers
    })

    function extractRequestHandler(): Function {
      const handler = handlers[0]?.fulfilled
      if (!handler) throw new Error('No request handler found')
      return handler
    }

    it('adds Bearer token from sessionStorage for /api requests', () => {
      sessionStorage.setItem('auth-storage', JSON.stringify({
        state: { accessToken: 'test-token' },
      }))
      const handler = extractRequestHandler()
      const config: any = { url: '/api/mt5/positions', headers: {} }
      const result = handler(config)
      expect(result.headers.Authorization).toBe('Bearer test-token')
    })

    it('does not add Bearer when sessionStorage has no token', () => {
      const handler = extractRequestHandler()
      const config: any = { url: '/api/mt5/positions', headers: {} }
      const result = handler(config)
      expect(result.headers.Authorization).toBeUndefined()
    })

    it('does not add Bearer when auth-storage key is missing', () => {
      sessionStorage.setItem('other-key', JSON.stringify({ token: 'x' }))
      const handler = extractRequestHandler()
      const config: any = { url: '/api/mt5/positions', headers: {} }
      const result = handler(config)
      expect(result.headers.Authorization).toBeUndefined()
    })
  })

  describe('response interceptor', () => {
    afterEach(() => { vi.restoreAllMocks() })

    let handlers: any[]

    beforeEach(() => {
      handlers = (api.interceptors.response as any).handlers
    })

    function extractErrorHandler(): Function {
      const handler = handlers[0]?.rejected
      if (!handler) throw new Error('No response error handler found')
      return handler
    }

    it('passes through non-401 errors unchanged', async () => {
      const handler = extractErrorHandler()
      const error = { response: { status: 403 }, config: {} }
      await expect(handler(error)).rejects.toBe(error)
    })

    it('passes through 401 when no sessionStorage data', async () => {
      const handler = extractErrorHandler()
      const error = { response: { status: 401 }, config: {} }
      await expect(handler(error)).rejects.toBe(error)
    })

    it('passes through 401 when no refresh token in storage', async () => {
      sessionStorage.setItem('auth-storage', JSON.stringify({
        state: { accessToken: 'tok' },
      }))
      const handler = extractErrorHandler()
      const error = { response: { status: 401 }, config: {} }
      await expect(handler(error)).rejects.toBe(error)
    })

    it('refreshes token and retries on 401', async () => {
      sessionStorage.setItem('auth-storage', JSON.stringify({
        state: { accessToken: 'old-tok', storedRefreshToken: 'refresh-me' },
      }))
      const post = vi.spyOn(axios, 'post').mockResolvedValue({
        data: { access_token: 'new-tok', refresh_token: 'new-refresh' },
      } as any)
      const adapter = fakeTransport()

      const handler = extractErrorHandler()
      const result = await handler({ response: { status: 401 }, config: { adapter } })

      expect(post).toHaveBeenCalledWith('/api/auth/refresh', { refresh_token: 'refresh-me' })
      const stored = JSON.parse(sessionStorage.getItem('auth-storage')!)
      expect(stored.state.accessToken).toBe('new-tok')
      expect(stored.state.storedRefreshToken).toBe('new-refresh')
      expect(adapter).toHaveBeenCalledOnce()
      expect(result.data).toEqual({ retried: true })
    })

    it('removes auth-storage and redirects on refresh failure', async () => {
      sessionStorage.setItem('auth-storage', JSON.stringify({
        state: { accessToken: 'old-tok', storedRefreshToken: 'bad-refresh' },
      }))
      vi.spyOn(axios, 'post').mockRejectedValue(new Error('refresh rejected'))
      Object.defineProperty(window, 'location', { value: { href: '/current' }, writable: true })

      const handler = extractErrorHandler()
      await handler({ response: { status: 401 }, config: { adapter: fakeTransport() } })

      expect(sessionStorage.getItem('auth-storage')).toBeNull()
      expect(window.location.href).toBe('/login')
    })
  })
})
