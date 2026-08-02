import { useState, type FormEvent } from "react";
import { useAuth } from "@/lib/auth";
import { ApiError } from "@/lib/api";
import { Button, Field, Input } from "@/components/ui";

/** Dev accounts, surfaced so a reviewer can get in without hunting the seed. */
const DEMO_ACCOUNTS = [
  { email: "admin@pharmacy.co.in", role: "Administrator", note: "Full access" },
  { email: "manager@pharmacy.co.in", role: "Manager", note: "Approvals, costs" },
  { email: "staff@pharmacy.co.in", role: "Staff", note: "Andheri branch only" },
];

/**
 * One-click sign-in for local work only. `import.meta.env.DEV` is statically
 * replaced at build time, so the production bundle contains neither this value
 * nor the panel below — a deployed instance ships no credential at all.
 */
const IS_DEV = import.meta.env.DEV;
const DEMO_PASSWORD = IS_DEV
  ? (import.meta.env.VITE_DEMO_PASSWORD ?? "ChangeMe@123")
  : "";

export function Login() {
  const { signIn } = useAuth();
  const [email, setEmail] = useState(IS_DEV ? "manager@pharmacy.co.in" : "");
  const [password, setPassword] = useState(DEMO_PASSWORD);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await signIn(email, password);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.problem.detail : "Unable to sign in",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-dvh flex-col items-center justify-center bg-canvas px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-7 flex flex-col items-center text-center">
          <div className="mb-3 flex size-11 items-center justify-center rounded-xl bg-brand shadow-sm">
            <span className="text-lg font-bold text-white">℞</span>
          </div>
          <h1 className="text-lg font-semibold tracking-tight">
            Pharmacy Inventory
          </h1>
          <p className="mt-1 text-[13px] text-ink-soft">
            Sign in to the multi-branch chain
          </p>
        </div>

        <form onSubmit={onSubmit} className="card space-y-4 p-5 shadow-sm">
          <Field label="Email" required>
            <Input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="username"
              required
              autoFocus
            />
          </Field>

          <Field label="Password" required>
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </Field>

          {error && (
            <div
              role="alert"
              className="rounded-lg border border-danger/20 bg-danger-soft px-3 py-2 text-[13px] text-danger"
            >
              {error}
            </div>
          )}

          <Button
            type="submit"
            variant="primary"
            loading={busy}
            className="w-full"
          >
            Sign in
          </Button>
        </form>

        {IS_DEV && (
          <div className="mt-5 rounded-lg border border-line bg-muted/50 p-3.5">
            <p className="mb-2 text-[11px] font-semibold tracking-widest text-ink-faint uppercase">
              Demo accounts
            </p>
            <ul className="space-y-1">
              {DEMO_ACCOUNTS.map((account) => (
                <li key={account.email}>
                  <button
                    type="button"
                    onClick={() => {
                      setEmail(account.email);
                      setPassword(DEMO_PASSWORD);
                    }}
                    className="flex w-full items-baseline justify-between gap-2 rounded-md px-2 py-1 text-left transition-colors hover:bg-surface"
                  >
                    <span className="truncate text-[13px] font-medium text-ink">
                      {account.role}
                    </span>
                    <span className="shrink-0 text-[11px] text-ink-faint">
                      {account.note}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
