import { useEffect, useState } from 'react'
import { useSpring } from 'framer-motion'

/**
 * Springs an integer value smoothly toward `target`. Returns the rounded
 * intermediate value for display in stat tiles. Updates settle quickly
 * (damping 30, stiffness 220) — perceptible animation without lag.
 */
export function useCountUp(target: number): number {
  const spring = useSpring(target, { damping: 30, stiffness: 220 })
  const [display, setDisplay] = useState(target)

  useEffect(() => {
    spring.set(target)
  }, [target, spring])

  useEffect(() => {
    const unsubscribe = spring.on('change', v => setDisplay(Math.round(v)))
    return unsubscribe
  }, [spring])

  return display
}
