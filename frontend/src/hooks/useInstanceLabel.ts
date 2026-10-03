import { useEffect, useState } from 'react'
import axios from 'axios'

/** The server's instance label ("Version 2" by default), to tell side-by-side deployments apart. */
export function useInstanceLabel(): string {
  const [label, setLabel] = useState('')
  useEffect(() => {
    axios.get('/api/instance').then((r) => setLabel(r.data?.label || '')).catch(() => { /* label stays empty */ })
  }, [])
  return label
}
