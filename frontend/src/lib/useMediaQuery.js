import { useCallback, useSyncExternalStore } from 'react'

/** True while the CSS media query matches. */
export function useMediaQuery(query) {
  const subscribe = useCallback(
    (onChange) => {
      const mql = window.matchMedia(query)
      mql.addEventListener('change', onChange)
      return () => mql.removeEventListener('change', onChange)
    },
    [query],
  )
  return useSyncExternalStore(subscribe, () => window.matchMedia(query).matches)
}

/** Tailwind's `lg` breakpoint: desktop layouts (table, side drawer). */
export function useIsDesktop() {
  return useMediaQuery('(min-width: 1024px)')
}
