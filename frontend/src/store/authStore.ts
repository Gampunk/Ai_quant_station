import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import axios, { type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'

interface User {
  id: number
  username: string
  name: string
  role: string
}

interface AuthState {
  user: User | null
  accessToken: string | null
  storedRefreshToken: string | null
  isAuthenticated: boolean
  isLoading: boolean
  error: string | null
  
  login: (username: string, password: string) => Promise<void>
  logout: () => void
  refreshAccessToken: () => Promise<void>
  setTokens: (accessToken: string, refreshToken: string) => void
  checkAuth: () => void
}

const API_URL = '/api/auth'

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      user: null,
      accessToken: null,
      storedRefreshToken: null,
      isAuthenticated: false,
      isLoading: false,
      error: null,

      login: async (username: string, password: string) => {
        set({ isLoading: true, error: null })
        try {
          const response = await axios.post(`${API_URL}/login`, {
            username,
            password
          })
          
          const { access_token, refresh_token } = response.data
          
          const payload = JSON.parse(decodeBase64Url(access_token.split('.')[1]))
          
          set({
            accessToken: access_token,
            storedRefreshToken: refresh_token,
            user: {
              id: payload.user_id,
              username: payload.sub,
              name: payload.name || payload.sub,
              role: payload.role || 'trader'
            },
            isAuthenticated: true,
            isLoading: false,
            error: null
          })
        } catch (error: any) {
          set({
            isLoading: false,
            error: error.response?.data?.detail || 'Login failed'
          })
          throw error
        }
      },

      logout: () => {
        revokeOnServer(get().accessToken, get().storedRefreshToken)
        set({
          user: null,
          accessToken: null,
          storedRefreshToken: null,
          isAuthenticated: false
        })
      },

      refreshAccessToken: async () => {
        const storedRefreshToken = get().storedRefreshToken
        if (!storedRefreshToken) {
          get().logout()
          return
        }

        try {
          const response = await axios.post(`${API_URL}/refresh`, {
            refresh_token: storedRefreshToken
          })
          
          const { access_token, refresh_token } = response.data
          
          set({
            accessToken: access_token,
            storedRefreshToken: refresh_token
          })
        } catch {
          get().logout()
        }
      },

      // After a password change the server ends every older session and hands back a new pair.
      setTokens: (accessToken: string, refreshToken: string) => {
        set({ accessToken, storedRefreshToken: refreshToken })
      },

      checkAuth: async () => {
        const { accessToken, storedRefreshToken } = get()
        if (!accessToken || !storedRefreshToken) {
          set({ isAuthenticated: false })
          return
        }

        try {
          const payload = JSON.parse(decodeBase64Url(accessToken.split('.')[1]))
          const exp = payload.exp * 1000
          const now = Date.now()
          
          if (exp < now) {
            await get().refreshAccessToken()
          } else {
            set({ isAuthenticated: true })
          }
        } catch {
          get().logout()
        }
      }
    }),
    {
      name: 'auth-storage',
      storage: createJSONStorage(() => sessionStorage),
      partialize: (state) => ({
        accessToken: state.accessToken,
        storedRefreshToken: state.storedRefreshToken,
        user: state.user,
        isAuthenticated: state.isAuthenticated
      })
    }
  )
)

// Ask the server to revoke both tokens, so they stop working immediately rather
// than when they expire. Best effort: logout always completes locally.
// Uses fetch, not axios, so the 401-refresh interceptor can never fire on logout.
function revokeOnServer(access: string | null, refresh: string | null) {
  if (!access && !refresh) return
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (access) headers.Authorization = `Bearer ${access}`
  fetch('/api/auth/logout', {
    method: 'POST',
    keepalive: true,
    headers,
    body: JSON.stringify({ refresh_token: refresh }),
  }).catch(() => {})
}

function decodeBase64Url(str: string): string {
  try {
    let base64 = str.replace(/-/g, '+').replace(/_/g, '/')
    while (base64.length % 4) base64 += '='
    return atob(base64)
  } catch {
    return atob(str)
  }
}

// Helper to get token from sessionStorage directly
const getTokenFromStorage = (): string | null => {
  try {
    const stored = sessionStorage.getItem('auth-storage')
    if (stored) {
      const parsed = JSON.parse(stored)
      return parsed.state?.accessToken || null
    }
  } catch {
    // unreadable storage: treated as logged out
  }
  return null
}

type RetriableConfig = InternalAxiosRequestConfig & { _authRetried?: boolean }

const fullUrl = (config: { baseURL?: string; url?: string }) => (config.baseURL || '') + (config.url || '')

// These never trigger a refresh: a 401 from them means the login itself failed.
const AUTH_ENDPOINTS = ['/api/auth/login', '/api/auth/refresh', '/api/auth/logout']

// One refresh at a time, shared by every client, so ten requests failing
// together cause one refresh, not ten.
let refreshPromise: Promise<void> | null = null

/**
 * The one place login tokens are attached and refreshed. Used by the global
 * axios and by the dashboard's client in lib/api.ts. Tokens live only in this
 * store; nothing else writes them.
 *
 * On a 401: refresh once, then retry the request once. If the refresh fails the
 * store logs out and the request fails; it is not retried. A retried request
 * that gets 401 again is not refreshed a second time, so it can never loop.
 */
export function attachAuth(instance: AxiosInstance): void {
  instance.interceptors.request.use((config) => {
    const accessToken = useAuthStore.getState().accessToken || getTokenFromStorage()
    if (accessToken && fullUrl(config).startsWith('/api')) {
      config.headers.Authorization = `Bearer ${accessToken}`
    }
    return config
  })

  instance.interceptors.response.use(
    (response) => response,
    async (error) => {
      const config = error.config as RetriableConfig | undefined
      const url = config ? fullUrl(config) : ''
      if (
        error.response?.status !== 401 || !config || config._authRetried ||
        AUTH_ENDPOINTS.some((p) => url.startsWith(p))
      ) {
        return Promise.reject(error)
      }
      if (!useAuthStore.getState().storedRefreshToken) {
        useAuthStore.getState().logout()
        return Promise.reject(error)
      }
      if (!refreshPromise) {
        refreshPromise = useAuthStore.getState().refreshAccessToken().finally(() => { refreshPromise = null })
      }
      await refreshPromise
      // refreshAccessToken logs out instead of throwing when the refresh fails.
      if (!useAuthStore.getState().accessToken) {
        return Promise.reject(error)
      }
      config._authRetried = true
      return instance(config)
    }
  )
}

attachAuth(axios)
