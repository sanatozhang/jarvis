"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Animated number — rolls from the previous value to the next on change,
 * like an instrument readout settling. Respects prefers-reduced-motion.
 */
export function CountUp({
  value,
  duration = 900,
  className,
  style,
}: {
  value: number;
  duration?: number;
  className?: string;
  style?: React.CSSProperties;
}) {
  const [display, setDisplay] = useState(value);
  const prev = useRef(value);

  useEffect(() => {
    const reduce =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const from = prev.current;
    const to = value;
    if (reduce || from === to) {
      setDisplay(to);
      prev.current = to;
      return;
    }
    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
      setDisplay(Math.round(from + (to - from) * eased));
      if (t < 1) raf = requestAnimationFrame(tick);
      else prev.current = to;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, duration]);

  return (
    <span className={className} style={style}>
      {display.toLocaleString()}
    </span>
  );
}
