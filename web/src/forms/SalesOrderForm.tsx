/**
 * Raise a sales order for an institutional customer.
 *
 * The branch is worked out, not chosen. Asking for it first was asking the
 * wrong question at the wrong time: nothing checks stock at creation, so a
 * branch that could not supply the order accepted it anyway and the refusal
 * arrived at allocation, by which point a document existed for an order that
 * could never ship. Here the customer and the products come first, the server
 * says which branches together can cover them, and each proposal is raised as
 * an ordinary single-branch order.
 *
 * One order per branch rather than one order drawing on several, because GST
 * registers per state: a branch in another state is a separately registered
 * person, and one document cannot carry two supplier GSTINs or two tax splits.
 *
 * No batch is chosen here either. Allocation is a separate step and picks by
 * earliest expiry (FEFO), so the person taking the order cannot accidentally
 * reserve stock that expires next week.
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Loader2 } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { cn, money, qty } from "@/lib/format";
import { useAuth } from "@/lib/auth";
import { STATES } from "@/lib/states";
import type {
  Customer,
  PlannedOrder,
  SalesOrderPlan,
  Warehouse,
} from "@/lib/types";
import {
  FormError,
  FormGrid,
  LineItems,
  emptyLine,
  type Line,
} from "@/components/form";
import { Badge, Button, Field, Input, Modal, Select } from "@/components/ui";

/** Where the price in the box came from, so the number can be trusted or not. */
interface PriceNote {
  source: "last_charged" | "mrp" | "none";
  last_charged_on?: string | null;
}

/**
 * The sentinel the customer Select uses for "this person is not on the list".
 *
 * A command sitting in a list of data, which is normally worth avoiding — but
 * the alternative is a button beside the field that is only ever wanted at the
 * exact moment someone has just looked down the list and not found the name.
 * That is where they are looking, so that is where it goes. Not a number, so
 * it can never collide with a customer id.
 */
const WALK_IN = "walk-in";

/**
 * Name the person at the counter, without leaving the order.
 *
 * This writes a real customer rather than stapling a name onto the order,
 * because the name has to survive: it is on the invoice, it is who a recall
 * traces to, and the same person coming back next month should be found by
 * typing three letters rather than entered a second time.
 *
 * GSTIN is genuinely optional. A supply to an unregistered person is an
 * ordinary B2C sale — the invoice carries the tax split with no recipient
 * registration on it — so an empty box here is a complete answer, not a
 * skipped field.
 */
function WalkInPanel({
  onCreated,
  onCancel,
}: {
  onCreated: (customer: Customer) => void;
  onCancel: () => void;
}) {
  const { user } = useAuth();
  const [name, setName] = useState("");
  const [gstin, setGstin] = useState("");
  const [stateCode, setStateCode] = useState("");
  const [phone, setPhone] = useState("");
  const [email, setEmail] = useState("");
  const [failed, setFailed] = useState<string | null>(null);

  const warehouses = useQuery({
    queryKey: ["warehouses", "active"],
    queryFn: () => api.get<Warehouse[]>("/api/v1/warehouses?is_active=true"),
  });

  /**
   * The buyer's state, which decides CGST + SGST against IGST.
   *
   * Defaulted to the branch the operator works at: a walk-in is someone
   * standing in the shop, so the shop's state is right nearly every time. It
   * stays a picker rather than a fixed label because the exception — a visitor
   * from another state producing their own GSTIN — is exactly the case where
   * getting it wrong puts the wrong tax on the invoice.
   *
   * A manager has no home branch, so this falls through to the central
   * warehouse — the same two steps the server takes when the field is left
   * out, deliberately, because the box has to say what is about to happen.
   * Leaving `stateCode` empty and letting the Select show its first option
   * instead put "AN — Andaman & Nicobar Islands" in front of a Mumbai
   * counter, unasked and about to be submitted.
   */
  const home =
    warehouses.data?.find((w) => w.id === user?.warehouse_id) ??
    warehouses.data?.find((w) => w.is_central);
  const effective = stateCode || home?.state_code || "";
  const prefix = STATES.find((s) => s.code === effective)?.gstPrefix;

  const create = useMutation({
    mutationFn: () =>
      api.post<Customer>("/api/v1/customers/walk-in", {
        name: name.trim(),
        gstin: gstin.trim() ? gstin.trim().toUpperCase() : null,
        // Only when it has been chosen. Left out, the server fills it from the
        // operator's own branch — one rule, on the side that can be trusted to
        // still be applying it when this form is not the caller.
        state_code: stateCode || null,
        phone: phone.trim() || null,
        email: email.trim() || null,
      }),
    onSuccess: onCreated,
    onError: (err) =>
      setFailed(
        err instanceof ApiError
          ? err.problem.detail
          : "Could not save this customer",
      ),
  });

  return (
    <div className="rounded-lg border border-line bg-muted/40 p-4">
      <p className="mb-3 text-[13px] font-medium text-ink">
        New walk-in customer
      </p>
      <FormError message={failed} />
      <FormGrid>
        <Field label="Name" required>
          <Input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="As it should read on the invoice"
          />
        </Field>
        <Field
          label="State"
          hint={`Decides the tax split${home ? ` — ${home.name} by default` : ""}`}
        >
          <Select
            value={effective}
            disabled={!effective}
            onChange={(e) => setStateCode(e.target.value)}
          >
            {/*
              The warehouses are still in flight, so there is no default to
              show yet. A Select with an unmatched value shows its first
              option, and the first state alphabetically is not a sensible
              thing to have offered anybody.
            */}
            {!effective && <option value="">Working out the branch…</option>}
            {STATES.map((s) => (
              <option key={s.code} value={s.code}>
                {s.code} — {s.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label="GSTIN"
          hint={
            prefix
              ? `Optional — a ${effective} registration starts ${prefix}`
              : "Optional — leave blank for an unregistered buyer"
          }
        >
          <Input
            value={gstin}
            onChange={(e) => setGstin(e.target.value.toUpperCase())}
            placeholder={prefix ? `${prefix}AABCS9876P1Z_` : "15 characters"}
          />
        </Field>
        {/*
          The only record of how to reach this buyer. An institution is on file
          with an account manager and a purchase order behind it; the person at
          the counter is not, so if a batch they were sold is recalled next
          month these two fields are the whole means of telling them. Optional,
          because a counter sale cannot be held up over a phone number nobody
          wants to give — but asked for, because nobody fills them in later.
        */}
        <Field label="Phone" hint="Optional — how to reach them about a recall">
          <Input
            type="tel"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="+91 98204 33127"
          />
        </Field>
        <Field label="Email" hint="Optional — where the invoice can be sent">
          <Input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="name@example.com"
          />
        </Field>
      </FormGrid>
      <div className="mt-3 flex gap-2">
        <Button
          variant="primary"
          size="sm"
          disabled={!name.trim()}
          loading={create.isPending}
          onClick={() => {
            setFailed(null);
            create.mutate();
          }}
        >
          Save and select
        </Button>
        <Button size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

export function SalesOrderForm({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [customerId, setCustomerId] = useState("");
  const [addingWalkIn, setAddingWalkIn] = useState(false);
  const [notes, setNotes] = useState("");
  const [lines, setLines] = useState<Line[]>([emptyLine()]);
  const [priceNotes, setPriceNotes] = useState<Record<number, PriceNote>>({});
  const [plan, setPlan] = useState<SalesOrderPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** Branch id -> the order raised from its proposal. */
  const [raised, setRaised] = useState<Record<number, string>>({});
  /**
   * Branch ids the operator has kept.
   *
   * All of them, until they say otherwise. The plan is a proposal to review,
   * and the common answer is "yes, all three" — starting with none ticked
   * would make the ordinary case the one that takes the most clicking.
   */
  const [chosen, setChosen] = useState<Set<number>>(new Set());
  /**
   * Recommendation's branch id -> the branch actually picked for it.
   *
   * Keyed by the recommendation rather than the choice, so a card keeps its
   * identity when it is re-pointed and the plan never has to be recomputed.
   */
  const [branchFor, setBranchFor] = useState<Record<number, number>>({});

  const customers = useQuery({
    queryKey: ["customers", "active"],
    queryFn: () => api.get<Customer[]>("/api/v1/customers?is_active=true"),
    enabled: open,
  });

  const reset = () => {
    setCustomerId("");
    setAddingWalkIn(false);
    setNotes("");
    setLines([emptyLine()]);
    setPriceNotes({});
    setPlan(null);
    setError(null);
    setRaised({});
    setChosen(new Set());
    setBranchFor({});
  };

  useEffect(() => {
    if (open) reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const usable = lines.filter((l) => l.product && Number(l.values.qty) > 0);

  /**
   * Fill the price box with what this customer last paid.
   *
   * Left blank it defaulted to MRP — the list price, which is the one number
   * an institutional buyer is certainly not paying. The field stays editable;
   * this only decides what is in it before anyone types.
   */
  const suggestPrice = async (key: number, productId: number, buyer: string) => {
    try {
      const suggestion = await api.get<{
        unit_price: string;
        source: PriceNote["source"];
        last_charged_on: string | null;
      }>(
        `/api/v1/sales-orders/suggested-price?customer_id=${buyer}&product_id=${productId}`,
      );
      setPriceNotes((notes) => ({
        ...notes,
        [key]: {
          source: suggestion.source,
          last_charged_on: suggestion.last_charged_on,
        },
      }));
      setLines((current) =>
        current.map((l) =>
          // Only if the box is still empty. A price typed while the request
          // was in flight is the operator's decision and outranks a suggestion.
          l.key === key && !l.values.price
            ? {
                ...l,
                values: {
                  ...l.values,
                  // The API sends a Decimal, so "38.5000" — four places is
                  // what the column stores, not what a price looks like.
                  price: String(Number(suggestion.unit_price)),
                },
              }
            : l,
        ),
      );
    } catch {
      // A suggestion that cannot be fetched is not an error worth showing —
      // the field is editable and the server falls back to MRP regardless.
    }
  };

  const onLinesChange = (next: Line[]) => {
    setPlan(null); // the plan describes the lines as they were
    setLines(next);
    if (!customerId) return;
    next.forEach((line) => {
      const previous = lines.find((l) => l.key === line.key);
      if (line.product && line.product.id !== previous?.product?.id) {
        void suggestPrice(line.key, line.product.id, customerId);
      }
    });
  };

  const onCustomerChange = (id: string) => {
    setCustomerId(id);
    setPlan(null);
    // Prices are per customer, so every one already on screen is now a
    // suggestion for the wrong buyer.
    setPriceNotes({});
    if (!id) return;
    lines.forEach((line) => {
      if (line.product) void suggestPrice(line.key, line.product.id, id);
    });
  };

  const planning = useMutation({
    mutationFn: () =>
      api.post<SalesOrderPlan>("/api/v1/sales-orders/plan", {
        customer_id: Number(customerId),
        lines: usable.map((l) => ({
          product_id: l.product!.id,
          qty_ordered: l.values.qty,
          unit_price: l.values.price || null,
        })),
      }),
    onSuccess: (result) => {
      setError(null);
      setRaised({});
      setPlan(result);
      setChosen(new Set(result.orders.map((o) => o.warehouse_id)));
      setBranchFor({});
    },
    onError: (err) =>
      setError(
        err instanceof ApiError ? err.problem.detail : "Could not plan this order",
      ),
  });

  const create = useMutation({
    // Both ids, because they can differ: `card` is the recommendation this
    // came from and never changes, `order` is where it is actually going
    // after any override. Keying progress on the card is what lets a
    // re-pointed proposal still be marked raised.
    mutationFn: ({ order }: { card: number; order: PlannedOrder }) =>
      api.post<{ so_number: string }>("/api/v1/sales-orders", {
        customer_id: Number(customerId),
        warehouse_id: order.warehouse_id,
        notes: notes.trim() || null,
        lines: order.lines.map((l) => ({
          product_id: l.product_id,
          qty_ordered: l.quantity,
          unit_price: l.unit_price,
        })),
      }),
    onSuccess: (raisedOrder, { card }) => {
      setError(null);
      setRaised((done) => ({ ...done, [card]: raisedOrder.so_number }));
      qc.invalidateQueries({ queryKey: ["sales-orders"] });
    },
    onError: (err) =>
      setError(
        err instanceof ApiError ? err.problem.detail : "Could not raise this order",
      ),
  });

  const orders = plan?.orders ?? [];

  /**
   * A proposed order as it currently stands, after any branch override.
   *
   * The lines never change — only where they ship from, and what that does to
   * the tax split and the total. Both come from the alternative itself, which
   * the server costed against the same lines, so the figure on the card is
   * the figure on the invoice rather than a guess made here.
   */
  const resolve = (order: PlannedOrder): PlannedOrder => {
    const pickedId = branchFor[order.warehouse_id];
    if (pickedId === undefined || pickedId === order.warehouse_id) return order;
    const alt = order.alternatives.find((a) => a.warehouse_id === pickedId);
    if (!alt) return order;
    return {
      ...order,
      warehouse_id: alt.warehouse_id,
      warehouse_name: alt.warehouse_name,
      state_code: alt.state_code,
      is_interstate: alt.is_interstate,
      subtotal: alt.subtotal,
      tax_total: alt.tax_total,
      grand_total: alt.grand_total,
    };
  };

  const outstanding = orders.filter(
    (o) => chosen.has(o.warehouse_id) && !raised[o.warehouse_id],
  );
  const anyRaised = orders.some((o) => raised[o.warehouse_id]);

  /**
   * One at a time, and stopping at the first refusal.
   *
   * Sequential rather than parallel because these orders draw on the same
   * shelves: fired together, two of them can both pass the stock check and
   * only one survive allocation. And a refusal stops the rest, because the
   * usual reason — the credit limit, which counts every open order — makes
   * every order after it wrong too. What was already raised stays raised and
   * says so; nothing is silently rolled back.
   */
  const raiseChosen = async () => {
    setError(null);
    for (const proposed of outstanding) {
      try {
        await create.mutateAsync({
          card: proposed.warehouse_id,
          order: resolve(proposed),
        });
      } catch {
        break;
      }
    }
  };

  const toggle = (warehouseId: number) =>
    setChosen((current) => {
      const next = new Set(current);
      if (!next.delete(warehouseId)) next.add(warehouseId);
      return next;
    });

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title="New sales order"
      description="Pick the customer and what they want; the branches are worked out from stock on hand."
      footer={
        <>
          <Button onClick={onClose}>{anyRaised ? "Done" : "Cancel"}</Button>
          {!plan ? (
            <Button
              variant="primary"
              loading={planning.isPending}
              disabled={!customerId || usable.length === 0}
              onClick={() => planning.mutate()}
            >
              Work out the branches
            </Button>
          ) : (
            outstanding.length > 0 && (
              <Button
                variant="primary"
                loading={create.isPending}
                onClick={raiseChosen}
              >
                {outstanding.length === 1
                  ? "Create this order"
                  : `Create ${outstanding.length} orders`}
              </Button>
            )
          )}
        </>
      }
    >
      <div className="space-y-4">
        <FormError message={error} />

        <FormGrid>
          <Field label="Customer" required>
            <Select
              value={customerId}
              onChange={(e) =>
                e.target.value === WALK_IN
                  ? setAddingWalkIn(true)
                  : onCustomerChange(e.target.value)
              }
            >
              <option value="">Select a customer…</option>
              <option value={WALK_IN}>＋ Walk-in customer…</option>
              {customers.data?.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                  {c.is_institutional ? "" : " (retail)"}
                </option>
              ))}
            </Select>
          </Field>

          <Field label="Notes">
            <Input
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Ward, department, delivery instructions"
            />
          </Field>
        </FormGrid>

        {addingWalkIn && (
          <WalkInPanel
            onCancel={() => setAddingWalkIn(false)}
            onCreated={(created) => {
              setAddingWalkIn(false);
              // Into the list before it is selected, so the Select has an
              // option to match and does not fall back to its placeholder for
              // the moment between the POST and the refetch landing.
              qc.setQueryData<Customer[]>(["customers", "active"], (list) =>
                [...(list ?? []), created].sort((a, b) =>
                  a.name.localeCompare(b.name),
                ),
              );
              void qc.invalidateQueries({ queryKey: ["customers"] });
              onCustomerChange(String(created.id));
            }}
          />
        )}

        <LineItems
          lines={lines}
          onChange={onLinesChange}
          columns={[
            { name: "qty", header: "Quantity", type: "number", placeholder: "0" },
            {
              name: "price",
              header: "Unit price ₹",
              type: "number",
              placeholder: "MRP",
              width: "8rem",
            },
          ]}
          note={(line) => {
            const note = priceNotes[line.key];
            if (!note || !line.product) return null;
            if (note.source === "last_charged") {
              return `Price last charged to this customer${
                note.last_charged_on ? ` on ${note.last_charged_on}` : ""
              } — edit if it has been renegotiated.`;
            }
            if (note.source === "mrp") {
              return "No history with this customer, so this is the list price (MRP).";
            }
            return "No price on record for this product — enter one.";
          }}
        />

        {plan && (
          <Plan
            plan={plan}
            raised={raised}
            chosen={chosen}
            onToggle={toggle}
            branchFor={branchFor}
            onBranchChange={(card, warehouseId) =>
              setBranchFor((current) => ({ ...current, [card]: warehouseId }))
            }
            busyAt={create.isPending ? create.variables?.card : undefined}
          />
        )}
      </div>
    </Modal>
  );
}

/* ------------------------------------------------------------------- the plan */

function Plan({
  plan,
  raised,
  chosen,
  onToggle,
  branchFor,
  onBranchChange,
  busyAt,
}: {
  plan: SalesOrderPlan;
  raised: Record<number, string>;
  chosen: Set<number>;
  onToggle: (warehouseId: number) => void;
  branchFor: Record<number, number>;
  onBranchChange: (card: number, warehouseId: number) => void;
  busyAt?: number;
}) {
  return (
    <div className="space-y-3">
      {plan.shortfalls.length > 0 && (
        <div className="flex items-start gap-2.5 rounded-lg border border-warn/25 bg-warn-soft px-3 py-2.5">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" />
          <div className="min-w-0 space-y-1">
            <p className="text-[13px] font-medium text-warn-strong">
              Not everything can be supplied
            </p>
            {plan.shortfalls.map((s) => (
              <p key={s.product_id} className="text-[13px] text-ink-soft">
                {s.product_name} — the chain holds {qty(s.planned)} of the{" "}
                {qty(s.requested)} asked for.
              </p>
            ))}
            <p className="text-[12px] text-ink-faint">
              The orders below cover what is available. The rest needs
              replenishing before it can be sold.
            </p>
          </div>
        </div>
      )}

      {plan.orders.length === 0 ? (
        <p className="rounded-lg border border-line bg-muted/40 px-3 py-2.5 text-[13px] text-ink-soft">
          No branch holds any of this, so there is nothing to raise.
        </p>
      ) : (
        <>
          <p className="text-[13px] text-ink-soft">
            {plan.orders.length === 1
              ? "One branch can supply all of this. Review it and create it."
              : `No single branch holds everything, so this becomes ${plan.orders.length} orders — each branch supplies its own and invoices under its own GST registration. Untick any you do not want.`}
          </p>

          {plan.orders.map((order, i) => (
            <ProposedOrder
              key={order.warehouse_id}
              order={order}
              index={i}
              total={plan.orders.length}
              soNumber={raised[order.warehouse_id]}
              chosen={chosen.has(order.warehouse_id)}
              onToggle={() => onToggle(order.warehouse_id)}
              pickedBranch={branchFor[order.warehouse_id] ?? order.warehouse_id}
              onBranchChange={(id) => onBranchChange(order.warehouse_id, id)}
              busy={busyAt === order.warehouse_id}
            />
          ))}
        </>
      )}
    </div>
  );
}

function ProposedOrder({
  order,
  index,
  total,
  soNumber,
  chosen,
  onToggle,
  pickedBranch,
  onBranchChange,
  busy,
}: {
  order: PlannedOrder;
  index: number;
  total: number;
  soNumber?: string;
  chosen: boolean;
  onToggle: () => void;
  pickedBranch: number;
  onBranchChange: (warehouseId: number) => void;
  busy: boolean;
}) {
  const done = Boolean(soNumber);
  const picked =
    pickedBranch === order.warehouse_id
      ? null
      : order.alternatives.find((a) => a.warehouse_id === pickedBranch);
  const shipsFrom = picked ?? order;
  const overridden = picked !== null;
  return (
    <div
      className={cn(
        "rounded-lg border transition-colors",
        done ? "border-ok/30 bg-ok-soft/40" : chosen ? "border-line" : "border-line",
        // Unticked, it stays legible but stops competing for attention: it is
        // still part of the answer, just not part of what is about to happen.
        !chosen && !done && "opacity-55",
      )}
    >
      <label
        className={cn(
          "flex flex-wrap items-center justify-between gap-2 border-b border-line px-3 py-2.5",
          !done && "cursor-pointer",
        )}
      >
        <div className="flex min-w-0 items-center gap-2.5">
          {done ? (
            <Check className="size-4 shrink-0 text-ok-strong" />
          ) : (
            <input
              type="checkbox"
              checked={chosen}
              onChange={onToggle}
              disabled={busy}
              className="size-4 shrink-0 accent-brand"
              aria-label={`Create the order from ${order.warehouse_name}`}
            />
          )}
          <div className="min-w-0">
            <p className="truncate text-[13px] font-medium text-ink">
              {total > 1 && (
                <span className="text-ink-faint">
                  Order {index + 1} of {total} ·{" "}
                </span>
              )}
              {shipsFrom.warehouse_name}
            </p>
            <p className="text-[11px] text-ink-faint">
              {done
                ? `Raised as ${soNumber}`
                : overridden
                  ? `${shipsFrom.state_code} — you chose this instead of ${order.warehouse_name}`
                  : `${shipsFrom.state_code} — recommended`}
            </p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {busy && <Loader2 className="size-4 animate-spin text-ink-faint" />}
          <Badge tone={shipsFrom.is_interstate ? "info" : "neutral"}>
            {shipsFrom.is_interstate ? "IGST" : "CGST + SGST"}
          </Badge>
          <span className="text-[13px] font-medium tnum text-ink">
            {money(shipsFrom.grand_total)}
          </span>
        </div>
      </label>

      {/*
        A recommendation, not a ruling.

        The planner reads stock; it does not know that a branch is short
        staffed today, or that this customer collects from one in person. So
        where more than one branch could take these lines in full, the choice
        is offered — and only those, because a branch that could cover four of
        five lines is a different plan, not an alternative to this order.
      */}
      {!done && order.alternatives.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
          <span className="text-[12px] text-ink-faint">Ship from</span>
          <Select
            value={String(pickedBranch)}
            disabled={busy}
            onChange={(e) => onBranchChange(Number(e.target.value))}
            className="h-8 max-w-[22rem] flex-1 text-[13px]"
          >
            <option value={order.warehouse_id}>
              {order.warehouse_name} — recommended
            </option>
            {order.alternatives.map((a) => (
              <option key={a.warehouse_id} value={a.warehouse_id}>
                {a.warehouse_name} ({a.state_code} ·{" "}
                {a.is_interstate ? "IGST" : "CGST + SGST"})
              </option>
            ))}
          </Select>
        </div>
      )}

      <div className="divide-y divide-line">
        {order.lines.map((line) => (
          <div
            key={line.product_id}
            className="flex items-baseline justify-between gap-3 px-3 py-1.5"
          >
            <span className="min-w-0 truncate text-[13px] text-ink-soft">
              {line.product_name}
            </span>
            <span className="shrink-0 text-[13px] tnum text-ink">
              {qty(line.quantity)} × {money(line.unit_price)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
