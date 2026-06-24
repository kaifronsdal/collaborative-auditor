/** Thin wrappers over bootstrap-icons (`bi-*`), matching the class names
 *  inspect-view uses (see @tsmono/react/icons/applicationIcons). The font
 *  CSS is loaded once in main.tsx. */
import type { JSX } from "react";

type IconProps = { size?: number; className?: string };

function bi(icon: string, size: number | undefined, className: string | undefined): JSX.Element {
  const cls = className ? `bi bi-${icon} ${className}` : `bi bi-${icon}`;
  return <i className={cls} style={size !== undefined ? { fontSize: size } : undefined} />;
}

export function Chevron({ size = 12, open, className }: IconProps & { open?: boolean }): JSX.Element {
  // Single glyph rotated rather than swapping right/down so the CSS
  // transition from the old SVG implementation still applies.
  const cls = className ? `bi bi-chevron-right ${className}` : "bi bi-chevron-right";
  return (
    <i
      className={cls}
      style={{
        fontSize: size,
        display: "inline-block",
        transform: open ? "rotate(90deg)" : undefined,
        transition: "transform .12s",
      }}
    />
  );
}

export function IconPlay({ size = 13, className }: IconProps): JSX.Element {
  return bi("play-fill", size, className);
}

export function IconPause({ size = 13, className }: IconProps): JSX.Element {
  return bi("pause-fill", size, className);
}

export function IconStop({ size = 12, className }: IconProps): JSX.Element {
  return bi("stop-fill", size, className);
}

export function IconSend({ size = 14, className }: IconProps): JSX.Element {
  return bi("arrow-up", size, className);
}

export function IconClose({ size = 12, className }: IconProps): JSX.Element {
  return bi("x-lg", size, className);
}

export function IconStep({ size = 12, className }: IconProps): JSX.Element {
  return bi("skip-end-fill", size, className);
}

/** Status dot — no bootstrap glyph for a colored status dot; stays as a CSS circle. */
export function StatusDot({ state, size = 8 }: { state: "ok" | "err" | "pending"; size?: number }): JSX.Element {
  return <span className={`status-dot-svg sd-${state}`} style={{ width: size, height: size }} />;
}
