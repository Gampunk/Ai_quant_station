import { useRef } from 'react'

/**
 * A ref that always holds the value from the latest render. For timers and
 * listeners that must call the current version of a function without being
 * restarted every time it is recreated.
 */
export function useLatest<T>(value: T) {
  const ref = useRef(value)
  ref.current = value
  return ref
}
