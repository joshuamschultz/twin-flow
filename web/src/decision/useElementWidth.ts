import { useLayoutEffect, useRef, useState } from "react";
import type { RefObject } from "react";

/** The width of an element, kept up to date with ResizeObserver. `fallback` is used before
 * the first measure and when there is no layout (server render, tests). */
export function useElementWidth<T extends HTMLElement>(
  fallback = 640,
): [RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const w = Math.floor(el.getBoundingClientRect().width);
      if (w > 0) setWidth(w);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}
