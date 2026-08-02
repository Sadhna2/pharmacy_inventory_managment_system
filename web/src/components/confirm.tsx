/**
 * Confirmation dialog for the actions that leave a permanent mark.
 *
 * Cancelling an order, reversing a ledger entry and retiring a product all
 * share a shape: say plainly what is about to happen, sometimes demand a
 * written reason, and surface the server's refusal in place rather than
 * closing and losing the user's context.
 *
 * The reason field is not decoration. A reversal without one is an unexplained
 * hole in the ledger six months later, which is exactly what the append-only
 * design exists to prevent.
 */

import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Button, Field, Modal, Textarea } from "./ui";

interface ReasonSpec {
  label: string;
  placeholder: string;
  /** Server enforces a 3-character floor; mirror it so the button explains itself. */
  minLength?: number;
}

export function ConfirmDialog({
  open,
  onClose,
  title,
  description,
  confirmLabel,
  danger,
  reason,
  path,
  method = "post",
  body,
  invalidate,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  confirmLabel: string;
  danger?: boolean;
  /** Omit for actions that need no explanation, e.g. cancelling a draft. */
  reason?: ReasonSpec;
  path: string;
  method?: "post" | "patch" | "delete";
  /** Extra fields merged into the request alongside any reason. */
  body?: Record<string, unknown>;
  invalidate: string[];
  /** Context worth showing before the user commits — stock on hand, totals. */
  children?: ReactNode;
}) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setText("");
    setMessage(null);
  }, [open]);

  const mutation = useMutation({
    mutationFn: () => {
      const payload = reason ? { ...body, reason: text.trim() } : body;
      if (method === "delete") return api.del(path);
      return api[method](path, payload ?? {});
    },
    onSuccess: () => {
      [...invalidate, "balances", "stock", "movements"].forEach((key) =>
        qc.invalidateQueries({ queryKey: [key] }),
      );
      onClose();
    },
    onError: (err) =>
      setMessage(
        err instanceof ApiError
          ? err.problem.detail || "Could not complete this"
          : "Could not reach the server",
      ),
  });

  const min = reason?.minLength ?? 3;
  const ready = !reason || text.trim().length >= min;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      description={description}
      footer={
        <>
          <Button onClick={onClose}>Back</Button>
          <Button
            variant={danger ? "danger" : "primary"}
            loading={mutation.isPending}
            disabled={!ready}
            onClick={() => mutation.mutate()}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {message && (
          <div className="rounded-lg border border-danger/20 bg-danger-soft px-3 py-2 text-[13px] text-danger">
            {message}
          </div>
        )}

        {children}

        {reason && (
          <Field
            label={reason.label}
            required
            hint={
              text.trim().length < min
                ? `At least ${min} characters — this is kept permanently`
                : undefined
            }
          >
            <Textarea
              rows={3}
              value={text}
              placeholder={reason.placeholder}
              onChange={(e) => setText(e.target.value)}
            />
          </Field>
        )}
      </div>
    </Modal>
  );
}
