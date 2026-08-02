import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "@/lib/auth";
import { Shell } from "@/components/Shell";
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
      retry: 1,
      refetchOnWindowFocus: false,
    },
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
  if (loading) return <FullPageSpinner />;
  return user ? <Shell /> : <Navigate to="/login" replace />;
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
        <Route index element={<Dashboard />} />
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
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
