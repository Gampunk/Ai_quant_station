import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

type AnyFn = (...args: any[]) => any
import axios from 'axios'
import api from '@/lib/api'
import { useAuthStore } from '@/store/authStore'

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

    function extractRequestHandler(): AnyFn {
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
      useAuthStore.setState({ accessToken: null, storedRefreshToken: null, isAuthenticated: false, user: null })
    })

    function extractErrorHandler(): AnyFn {
      const handler = handlers[0]?.rejected
      if (!handler) throw new Error('No response error handler found')
      return handler
    }

    it('passes through non-401 errors unchanged', async () => {
      const handler = extractErrorHandler()
      const error = { response: { status: 403 }, config: { baseURL: '/api', url: '/mt5/positions', headers: {} } }
      await expect(handler(error)).rejects.toBe(error)
    })

    it('logs out on 401 when there is no refresh token', async () => {
      useAuthStore.setState({ accessToken: 'tok', isAuthenticated: true })
      const handler = extractErrorHandler()
      const error = { response: { status: 401 }, config: { baseURL: '/api', url: '/mt5/positions', headers: {} } }
      await expect(handler(error)).rejects.toBe(error)
      expect(useAuthStore.getState().isAuthenticated).toBe(false)
    })

    it('refreshes through the auth store and retries once', async () => {
      useAuthStore.setState({ accessToken: 'old-tok', storedRefreshToken: 'refresh-me', isAuthenticated: true })
      const post = vi.spyOn(axios, 'post').mockResolvedValue({
        data: { access_token: 'new-tok', refresh_token: 'new-refresh' },
      } as any)
      const adapter = fakeTransport()

      const handler = extractErrorHandler()
      const result = await handler({ response: { status: 401 }, config: { baseURL: '/api', url: '/mt5/positions', headers: {}, adapter } })

      expect(post).toHaveBeenCalledWith('/api/auth/refresh', { refresh_token: 'refresh-me' })
      expect(useAuthStore.getState().accessToken).toBe('new-tok')
      expect(useAuthStore.getState().storedRefreshToken).toBe('new-refresh')
      expect(adapter).toHaveBeenCalledOnce()
      expect(adapter.mock.calls[0][0].headers.Authorization).toBe('Bearer new-tok')
      expect(result.data).toEqual({ retried: true })
    })

    it('logs out and does not retry when the refresh fails', async () => {
      useAuthStore.setState({ accessToken: 'old-tok', storedRefreshToken: 'bad-refresh', isAuthenticated: true })
      vi.spyOn(axios, 'post').mockRejectedValue(new Error('refresh rejected'))
      const adapter = fakeTransport()

      const handler = extractErrorHandler()
      const error = { response: { status: 401 }, config: { baseURL: '/api', url: '/mt5/positions', headers: {}, adapter } }
      await expect(handler(error)).rejects.toBe(error)

      expect(adapter).not.toHaveBeenCalled()
      expect(useAuthStore.getState().isAuthenticated).toBe(false)
      expect(useAuthStore.getState().accessToken).toBeNull()
    })
  })
})
