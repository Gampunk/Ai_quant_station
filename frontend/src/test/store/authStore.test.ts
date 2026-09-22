import { describe, it, expect, beforeEach, vi } from 'vitest'
import { useAuthStore } from '@/store/authStore'

vi.mock('axios', () => {
  const handlers: { request: Function[]; responseSuccess: Function[]; responseError: Function[] } = {
    request: [],
    responseSuccess: [],
    responseError: [],
  }

  const mockAxios: any = Object.assign(
    vi.fn(() => Promise.resolve({ data: 'ok' })),
    {
      create: vi.fn(() => mockAxios),
      post: vi.fn(),
      get: vi.fn(),
      defaults: {},
      interceptors: {
        request: {
          use: vi.fn((fn: Function) => { handlers.request.push(fn) }),
        },
        response: {
          use: vi.fn((fn: Function, fn2?: Function) => {
            handlers.responseSuccess.push(fn)
            if (fn2) handlers.responseError.push(fn2)
          }),
        },
      },
    }
  )

  ;(mockAxios as any).__test_handlers = handlers
  return { default: mockAxios }
})

const mockAccessToken = [
  btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' })),
  btoa(JSON.stringify({
    sub: 'testuser',
    user_id: 1,
    role: 'trader',
    name: 'Test User',
    exp: Math.floor(Date.now() / 1000) + 3600,
  })),
  'fakesignature',
].join('.')

const mockExpiredToken = [
  btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' })),
  btoa(JSON.stringify({
    sub: 'testuser',
    user_id: 1,
    role: 'trader',
    name: 'Test User',
    exp: Math.floor(Date.now() / 1000) - 3600,
  })),
  'fakesignature',
].join('.')

function resetStore() {
  useAuthStore.setState({
    user: null,
    accessToken: null,
    storedRefreshToken: null,
    isAuthenticated: false,
    isLoading: false,
    error: null,
  })
  sessionStorage.clear()
}

describe('authStore', () => {
  beforeEach(() => {
    resetStore()
    vi.clearAllMocks()
  })

  describe('initial state', () => {
    it('has correct default values', () => {
      const state = useAuthStore.getState()
      expect(state.user).toBeNull()
      expect(state.accessToken).toBeNull()
      expect(state.storedRefreshToken).toBeNull()
      expect(state.isAuthenticated).toBe(false)
      expect(state.isLoading).toBe(false)
      expect(state.error).toBeNull()
    })
  })

  describe('login', () => {
    it('sets authenticated state on success', async () => {
      const axios = (await import('axios')).default
      vi.mocked(axios.post).mockResolvedValueOnce({
        data: { access_token: mockAccessToken, refresh_token: 'new_refresh' },
      })

      await useAuthStore.getState().login('testuser', 'password')

      const state = useAuthStore.getState()
      expect(state.isAuthenticated).toBe(true)
      expect(state.isLoading).toBe(false)
      expect(state.accessToken).toBe(mockAccessToken)
      expect(state.storedRefreshToken).toBe('new_refresh')
      expect(state.user).toMatchObject({
        id: 1,
        username: 'testuser',
        name: 'Test User',
        role: 'trader',
      })
    })

    it('sets error on login failure', async () => {
      const axios = (await import('axios')).default
      vi.mocked(axios.post).mockRejectedValueOnce({
        response: { data: { detail: 'Invalid credentials' } },
      })

      await expect(
        useAuthStore.getState().login('testuser', 'wrong')
      ).rejects.toThrow()

      const state = useAuthStore.getState()
      expect(state.isAuthenticated).toBe(false)
      expect(state.isLoading).toBe(false)
      expect(state.error).toBe('Invalid credentials')
    })

    it('sets generic error when response has no detail', async () => {
      const axios = (await import('axios')).default
      vi.mocked(axios.post).mockRejectedValueOnce({})

      await expect(
        useAuthStore.getState().login('testuser', 'password')
      ).rejects.toThrow()

      const state = useAuthStore.getState()
      expect(state.error).toBe('Login failed')
    })
  })

  describe('logout', () => {
    it('clears all auth state', () => {
      useAuthStore.setState({
        user: { id: 1, username: 'testuser', name: 'Test', role: 'trader' },
        accessToken: 'token',
        storedRefreshToken: 'refresh',
        isAuthenticated: true,
      })

      useAuthStore.getState().logout()

      const state = useAuthStore.getState()
      expect(state.user).toBeNull()
      expect(state.accessToken).toBeNull()
      expect(state.storedRefreshToken).toBeNull()
      expect(state.isAuthenticated).toBe(false)
    })

    it('asks the server to revoke both tokens', () => {
      const fetchMock = vi.mocked(fetch)
      fetchMock.mockClear()
      sessionStorage.clear()
      useAuthStore.setState({ accessToken: 'the-access', storedRefreshToken: 'the-refresh', isAuthenticated: true })

      useAuthStore.getState().logout()

      expect(fetchMock).toHaveBeenCalledOnce()
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
      expect(url).toBe('/api/auth/logout')
      expect(init.method).toBe('POST')
      expect((init.headers as Record<string, string>).Authorization).toBe('Bearer the-access')
      expect(JSON.parse(init.body as string)).toEqual({ refresh_token: 'the-refresh' })
    })

    it('prefers newer tokens written straight to storage', () => {
      const fetchMock = vi.mocked(fetch)
      fetchMock.mockClear()
      // Login fills the store, which persists itself. Later the dashboard's API
      // client refreshes and writes newer tokens straight to storage.
      useAuthStore.setState({ accessToken: 'stale-access', storedRefreshToken: 'stale-refresh' })
      sessionStorage.setItem('auth-storage', JSON.stringify({
        state: { accessToken: 'newer-access', storedRefreshToken: 'newer-refresh' },
      }))

      useAuthStore.getState().logout()

      const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
      expect(JSON.parse(init.body as string)).toEqual({ refresh_token: 'newer-refresh' })
      sessionStorage.clear()
    })

    it('makes no request when there is nothing to revoke', () => {
      const fetchMock = vi.mocked(fetch)
      fetchMock.mockClear()
      sessionStorage.clear()
      useAuthStore.setState({ accessToken: null, storedRefreshToken: null })

      useAuthStore.getState().logout()

      expect(fetchMock).not.toHaveBeenCalled()
    })
  })

  describe('refreshAccessToken', () => {
    it('refreshes token on success', async () => {
      useAuthStore.setState({
        storedRefreshToken: 'old_refresh',
        isAuthenticated: true,
      })

      const axios = (await import('axios')).default
      vi.mocked(axios.post).mockResolvedValueOnce({
        data: { access_token: 'new_access', refresh_token: 'new_refresh' },
      })

      await useAuthStore.getState().refreshAccessToken()

      const state = useAuthStore.getState()
      expect(state.accessToken).toBe('new_access')
      expect(state.storedRefreshToken).toBe('new_refresh')
      expect(state.isAuthenticated).toBe(true)
    })

    it('logs out when no refresh token exists', async () => {
      useAuthStore.setState({
        storedRefreshToken: null,
        isAuthenticated: true,
      })

      await useAuthStore.getState().refreshAccessToken()

      const state = useAuthStore.getState()
      expect(state.isAuthenticated).toBe(false)
    })

    it('logs out on refresh failure', async () => {
      useAuthStore.setState({
        storedRefreshToken: 'bad_refresh',
        isAuthenticated: true,
      })

      const axios = (await import('axios')).default
      vi.mocked(axios.post).mockRejectedValueOnce(new Error('Network error'))

      await useAuthStore.getState().refreshAccessToken()

      const state = useAuthStore.getState()
      expect(state.isAuthenticated).toBe(false)
    })
  })

  describe('checkAuth', () => {
    it('sets isAuthenticated=false when no tokens', async () => {
      useAuthStore.setState({
        accessToken: null,
        storedRefreshToken: null,
        isAuthenticated: true,
      })

      await useAuthStore.getState().checkAuth()

      expect(useAuthStore.getState().isAuthenticated).toBe(false)
    })

    it('sets isAuthenticated=true when token is valid', async () => {
      useAuthStore.setState({
        accessToken: mockAccessToken,
        storedRefreshToken: 'refresh',
        isAuthenticated: false,
      })

      await useAuthStore.getState().checkAuth()

      expect(useAuthStore.getState().isAuthenticated).toBe(true)
    })

    it('refreshes token when expired', async () => {
      useAuthStore.setState({
        accessToken: mockExpiredToken,
        storedRefreshToken: 'refresh',
        isAuthenticated: false,
      })

      const axios = (await import('axios')).default
      vi.mocked(axios.post).mockResolvedValueOnce({
        data: { access_token: 'new_access', refresh_token: 'new_refresh' },
      })

      await useAuthStore.getState().checkAuth()

      const state = useAuthStore.getState()
      expect(state.accessToken).toBe('new_access')
      expect(state.storedRefreshToken).toBe('new_refresh')
    })

    it('logs out when token is malformed', async () => {
      useAuthStore.setState({
        accessToken: 'not.a.valid.jwt',
        storedRefreshToken: 'refresh',
        isAuthenticated: true,
      })

      await useAuthStore.getState().checkAuth()

      expect(useAuthStore.getState().isAuthenticated).toBe(false)
    })
  })

  describe('axios request interceptor', () => {
    let requestHandler: Function

    beforeEach(async () => {
      const axios = (await import('axios')).default as any
      requestHandler = axios.__test_handlers.request[0]
    })

    it('adds Bearer token for /api requests when token exists in store', () => {
      useAuthStore.setState({ accessToken: 'test-token' })
      const config: any = { url: '/api/mt5/positions', headers: {} }
      const result = requestHandler(config)
      expect(result.headers.Authorization).toBe('Bearer test-token')
    })

    it('does not add Bearer token for non-/api URLs', () => {
      useAuthStore.setState({ accessToken: 'test-token' })
      const config: any = { url: '/login', headers: {} }
      const result = requestHandler(config)
      expect(result.headers.Authorization).toBeUndefined()
    })

    it('does not add Bearer token when no token exists in store or storage', () => {
      const config: any = { url: '/api/trade/order', headers: {} }
      const result = requestHandler(config)
      expect(result.headers.Authorization).toBeUndefined()
    })

    it('falls back to sessionStorage when token not in store', () => {
      sessionStorage.setItem('auth-storage', JSON.stringify({
        state: { accessToken: 'stored-token' },
      }))
      const config: any = { url: '/api/ai/chat', headers: {} }
      const result = requestHandler(config)
      expect(result.headers.Authorization).toBe('Bearer stored-token')
    })

    it('always returns the config object', () => {
      const config: any = { url: '/api/test', headers: {} }
      const result = requestHandler(config)
      expect(result).toBe(config)
    })
  })

  describe('axios response interceptor', () => {
    let responseSuccessHandler: Function
    let responseErrorHandler: Function

    beforeEach(async () => {
      const axios = (await import('axios')).default as any
      responseSuccessHandler = axios.__test_handlers.responseSuccess[0]
      responseErrorHandler = axios.__test_handlers.responseError[0]
    })

    it('passes through successful responses unchanged', () => {
      const response = { data: 'ok' }
      const result = responseSuccessHandler(response)
      expect(result).toBe(response)
    })

    it('passes through non-401 errors unchanged', async () => {
      const error = { response: { status: 403 }, config: { url: '/api/test' } }
      await expect(responseErrorHandler(error)).rejects.toBe(error)
    })

    it('logs out on 401 when no refresh token exists', async () => {
      useAuthStore.setState({
        storedRefreshToken: null,
        isAuthenticated: true,
        user: { id: 1, username: 'test', name: 'T', role: 'trader' },
      })

      const error = { response: { status: 401 }, config: { url: '/api/test' } }
      await expect(responseErrorHandler(error)).rejects.toBe(error)
      expect(useAuthStore.getState().isAuthenticated).toBe(false)
      expect(useAuthStore.getState().user).toBeNull()
    })

    it('retries original request even when refresh API call fails', async () => {
      useAuthStore.setState({
        storedRefreshToken: 'bad-token',
        isAuthenticated: true,
        user: { id: 1, username: 'test', name: 'T', role: 'trader' },
      })

      const axios = (await import('axios')).default as any
      axios.post.mockRejectedValueOnce(new Error('Refresh failed'))

      const error = { response: { status: 401 }, config: { url: '/api/test' } }
      const result = await responseErrorHandler(error)
      // refreshAccessToken() catches errors internally and never throws,
      // so the interceptor always proceeds to retry the original request
      expect(result).toEqual({ data: 'ok' })
      expect(useAuthStore.getState().isAuthenticated).toBe(false)
    })

    it('refreshes token and retries on 401 when refresh token exists', async () => {
      useAuthStore.setState({ storedRefreshToken: 'refresh-me' })
      const axios = (await import('axios')).default as any
      axios.post.mockResolvedValueOnce({
        data: { access_token: 'new-access', refresh_token: 'new-refresh' },
      })

      const error = {
        response: { status: 401 },
        config: { url: '/api/test', headers: {} },
      }
      await responseErrorHandler(error)
      expect(useAuthStore.getState().accessToken).toBe('new-access')
      expect(useAuthStore.getState().storedRefreshToken).toBe('new-refresh')
    })
  })
})
