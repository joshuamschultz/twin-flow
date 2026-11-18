// One tooltip for the whole view. Opens on hover, keyboard focus and tap (DESIGN.md 8.4).
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type FocusEvent,
  type MouseEvent,
  type ReactNode,
} from "react";

type Tip = { text: string; x: number; y: number } | null;
type TipApi = {
  show: (text: string, x: number, y: number) => void;
  hide: () => void;
};

const TipContext = createContext<TipApi>({ show: () => {}, hide: () => {} });

export function TooltipHost({ children }: { children: ReactNode }) {
  const [tip, setTip] = useState<Tip>(null);
  const show = useCallback(
    (text: string, x: number, y: number) => setTip({ text, x, y }),
    [],
  );
  const hide = useCallback(() => setTip(null), []);
  const api = useMemo(() => ({ show, hide }), [show, hide]);
  const left = tip
    ? Math.max(8, Math.min(tip.x + 12, window.innerWidth - 272))
    : 0;
  return (
    <TipContext.Provider value={api}>
      {children}
      {tip && (
        <div
          className="tf-tip"
          role="tooltip"
          style={{ left, top: tip.y + 14 }}
        >
          {tip.text}
        </div>
      )}
    </TipContext.Provider>
  );
}

export function useTip(): TipApi {
  return useContext(TipContext);
}

/** Props that give any element a tooltip on hover, focus and tap. */
export function useTipProps() {
  const { show, hide } = useTip();
  return useCallback(
    (text: string) => ({
      onMouseMove: (e: MouseEvent) => show(text, e.clientX, e.clientY),
      onMouseLeave: hide,
      onFocus: (e: FocusEvent) => {
        const r = (e.currentTarget as Element).getBoundingClientRect();
        show(text, r.left + r.width / 2, r.bottom);
      },
      onBlur: hide,
      onClick: (e: MouseEvent) => show(text, e.clientX, e.clientY),
    }),
    [show, hide],
  );
}
