import axios from 'axios'
import { attachAuth } from '@/store/authStore'

// The dashboard's client. Tokens are attached and refreshed by the same code as
// every other request (attachAuth in the auth store). It used to have its own
// copy, which wrote refreshed tokens straight into storage behind the store's
// back and, when a refresh failed, left without revoking anything.
const api = axios.create({
  baseURL: '/api',
})

attachAuth(api)

export default api
