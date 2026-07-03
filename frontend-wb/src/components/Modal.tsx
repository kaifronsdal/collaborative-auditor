/**
 * `Modal` — the workbench's shared overlay dialog. Fixed backdrop, centered
 * card (640px max, 80vh scroll body), close on Escape / backdrop click / `×`.
 * Portals to `document.body` so it escapes the column's `overflow: hidden`.
 *
 * Consumers: `GateCard` (seed/quote review), `ReaderCard` (full transcript).
 */
import { useEffect, type JSX, type ReactNode } from "react";
import { createPortal } from "react-dom";

import "./modal.css";

type Props = {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  width?: string;
};

export default function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  width,
}: Props): JSX.Element | null {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <div className="wb-modal-overlay" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        className="wb-modal"
        style={width ? { maxWidth: width } : undefined}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="wb-modal-head">
          <span className="wb-modal-title">{title}</span>
          <button
            type="button"
            className="wb-modal-x"
            aria-label="close"
            onClick={onClose}
          >
            <i className="bi bi-x-lg" />
          </button>
        </div>
        <div className="wb-modal-body">{children}</div>
        {footer && <div className="wb-modal-foot">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}
