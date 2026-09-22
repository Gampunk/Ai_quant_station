import '@testing-library/jest-dom'
import { vi } from 'vitest'

// No unit test may reach the network. A real request can hang, or pass or fail
// depending on what happens to be listening on this machine. Tests that care
// about a request assert on this mock instead.
vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(null, { status: 200 }))))
