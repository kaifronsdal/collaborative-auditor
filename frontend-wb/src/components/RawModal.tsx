import type { JSX } from "react";
import { JSONPanel } from "@tsmono/react/components";
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
      <JSONPanel data={value} className="raw-modal-body" />
    </Modal>
  );
}
