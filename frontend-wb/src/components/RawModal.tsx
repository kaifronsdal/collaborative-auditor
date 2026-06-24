import type { JSX } from "react";
import { Modal } from "@tsmono/react/components/Modal";

type Props = {
  value: unknown;
  onClose: () => void;
  title?: string;
};

export function RawModal({ value, onClose, title }: Props): JSX.Element {
  return (
    <Modal
      show
      onHide={onClose}
      title={title ?? "raw"}
      width="min(960px, 92vw)"
      bodyClassName="raw-modal"
      padded={false}
    >
      <pre className="raw-modal-body">{JSON.stringify(value, null, 2)}</pre>
    </Modal>
  );
}
