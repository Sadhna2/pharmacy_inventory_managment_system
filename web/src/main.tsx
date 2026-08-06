import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { ApiError } from "@/lib/api";
import { AuthProvider, useAuth } from "@/lib/auth";
import { Shell } from "@/components/Shell";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Spinner } from "@/components/ui";
import { Login } from "@/pages/Login";
import { Dashboard } from "@/pages/Dashboard";
import { Products } from "@/pages/Products";
import { Movements, Stock } from "@/pages/Stock";
import {
  Adjustments,
  PurchaseOrders,
  SalesOrders,
  Transfers,
} from "@/pages/Operations";
import { Recalls } from "@/pages/Recalls";
import { Audit } from "@/pages/Audit";
import { LeadTimes } from "@/pages/LeadTimes";
import { Anomalies } from "@/pages/Anomalies";
import { Forecast } from "@/pages/Forecast";
import { Reorder } from "@/pages/Reorder";
import { MasterData } from "@/pages/MasterData";
import { Users } from "@/pages/Users";
import { Settings } from "@/pages/Settings";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      /**
       * Retry a fault, never a refusal.
       *
       * A 4xx is the server's considered answer: not permitted, not found,
       * not valid. Asking again cannot change it, so the retry buys nothing
       * and costs the user a wait — and worse, during that wait the query is
       * neither loading nor failed, which is the gap that used to render as
       * "Nothing recorded". 5xx and network faults are genuinely transient
       * and keep their one retry.
       */
      retry: (failureCount, error) => {
        const status = error instanceof ApiError ? error.status : undefined;
        if (status !== undefined && status >= 400 && status < 500) return false;
        return failureCount < 1;
      },
      refetchOnWindowFocus: false,
      /**
       * Report failures instead of parking them.
       *
       * React Query's default network mode pauses a query the moment it
       * believes the browser is offline, and a paused query is a strange
       * state: `status` stays "pending" forever, `error` is never set, and
       * `isLoading` is false because nothing is in flight. A table handed
       * that renders its empty state — so a screen that could not reach the
       * server announces "Nothing recorded", which is a claim about the
       * business and is false. Every list in this app did that, and it is
       * what made error handling look arbitrary: the same failure showed as
       * an error or as an empty list depending on what the online detector
       * happened to think.
       *
       * "always" is the honest setting here because there is no offline
       * story to protect: no service worker, no persisted cache, no queued
       * mutations. Without the API this app has nothing to show, so the
       * request should be made, allowed to fail, and the failure shown.
       */
      networkMode: "always",
    },
    mutations: { networkMode: "always" },
  },
});

function FullPageSpinner() {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-canvas">
      <Spinner className="size-6" />
    </div>
  );
}

function Protected() {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <FullPageSpinner />;
  if (!user) return <Navigate to="/login" replace />;
  // Inside the Shell, so a screen that throws loses the screen and keeps the
  // navigation — the user can walk away from it instead of reloading. Keyed on
  // the path so moving to another screen clears the caught error.
  return (
    <ErrorBoundary resetKey={location.pathname}>
      <Shell />
    </ErrorBoundary>
  );
}

/**
 * What "/" means depends on who you are.
 *
 * The Dashboard is entirely stock figures, and the CUSTOMER role is refused
 * every endpoint behind them — so signing in as a customer landed on a screen
 * of em dashes with nothing on it they were allowed to see, and no indication
 * that Sales, the one screen they *can* use, was a click away. Anyone who can
 * read stock still gets the Dashboard; anyone who cannot is sent to the first
 * screen their role actually opens.
 */
function Home() {
  const { can } = useAuth();
  if (can("stock.view")) return <Dashboard />;
  if (can("so.view")) return <Navigate to="/sales-orders" replace />;
  if (can("po.view")) return <Navigate to="/purchase-orders" replace />;
  if (can("product.view")) return <Navigate to="/products" replace />;
  // No role today lands here. If one ever does, saying so beats a blank page.
  return (
    <div className="flex min-h-[60vh] items-center justify-center px-6 text-center">
      <p className="max-w-sm text-[13px] text-ink-soft">
        Your account has no screens enabled yet. An administrator can grant
        access under Setup → Users.
      </p>
    </div>
  );
}

function App() {
  const { user, loading } = useAuth();

  return (
    <Routes>
      <Route
        path="/login"
        element={
          loading ? (
            <FullPageSpinner />
          ) : user ? (
            <Navigate to="/" replace />
          ) : (
            <Login />
          )
        }
      />
      <Route element={<Protected />}>
        <Route index element={<Home />} />
        <Route path="products" element={<Products />} />
        <Route path="stock" element={<Stock />} />
        <Route path="movements" element={<Movements />} />
        <Route path="purchase-orders" element={<PurchaseOrders />} />
        <Route path="sales-orders" element={<SalesOrders />} />
        <Route path="transfers" element={<Transfers />} />
        <Route path="adjustments" element={<Adjustments />} />
        <Route path="recalls" element={<Recalls />} />
        <Route path="audit" element={<Audit />} />
        <Route path="lead-times" element={<LeadTimes />} />
        <Route path="exceptions" element={<Anomalies />} />
        <Route path="forecast" element={<Forecast />} />
        <Route path="replenishment" element={<Reorder />} />
        <Route path="master-data" element={<MasterData />} />
        <Route path="users" element={<Users />} />
        <Route path="settings" element={<Settings />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        {/* The outer one catches what the routed boundary cannot: a throw in
            the auth provider, the shell chrome, or routing itself. */}
        <ErrorBoundary>
          <AuthProvider>
            <App />
          </AuthProvider>
        </ErrorBoundary>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
