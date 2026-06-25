import { type JSX, type ReactNode, useEffect, useRef, useState } from "react";

type Props = {
  children: ReactNode;
  /** Pixel height above which content is clipped behind a [more] toggle. */
  maxHeight?: number;
};

/**
 * Clips tall content to `maxHeight` with a sticky [more]/[less] toggle, in the
 * style of inspect-view's ExpandablePanel. Measures the natural height of
 * children via ResizeObserver so content that streams in and grows past the
 * threshold collapses live. Below the threshold the wrapper is inert (no clip,
 * no toggle).
 */
export function CollapsibleContent({ children, maxHeight = 280 }: Props): JSX.Element {
  const innerRef = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    const el = innerRef.current;
    if (!el) return;
    // Observe the unclamped inner box so growth is detected even while the
    // outer .cc-clip has a fixed max-height (whose own box would not resize).
    const measure = (): void => setOverflows(el.scrollHeight - maxHeight > 1);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [maxHeight]);

  const collapsed = overflows && !expanded;

  const toggle = (): void => {
    if (!expanded) {
      setExpanded(true);
      return;
    }
    // Collapsing a panel taller than the viewport would strand the user
    // mid-scroll; pull the panel's bottom edge back into view afterwards.
    const el = innerRef.current;
    const tall = !!el && el.getBoundingClientRect().height > window.innerHeight;
    setExpanded(false);
    if (tall) {
      requestAnimationFrame(() =>
        el.scrollIntoView({ block: "end", behavior: "smooth" }),
      );
    }
  };

  return (
    <div className="cc-wrap" data-collapsed={collapsed || undefined}>
      <div
        className="cc-clip"
        style={collapsed ? { maxHeight, overflow: "hidden", contain: "layout paint" } : undefined}
      >
        <div ref={innerRef}>{children}</div>
      </div>
      {overflows && (
        <button type="button" className="cc-toggle" onClick={toggle}>
          {expanded ? "less" : "more"}
        </button>
      )}
    </div>
  );
}
