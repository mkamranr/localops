import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ApiError, refreshAccessToken } from "@/api/client";
import { authApi } from "@/api/endpoints";
import { Spinner } from "@/components/ui/display";
import { AppLayout } from "@/layouts/AppLayout";
import { COMING_SOON } from "@/layouts/navigation";
import { AgentsPage } from "@/pages/AgentsPage";
import { AssistantPage } from "@/pages/AssistantPage";
import { AuditLogsPage } from "@/pages/AuditLogsPage";
import { ComingSoonPage } from "@/pages/ComingSoonPage";
import { DashboardPage } from "@/pages/DashboardPage";
import { InfrastructurePage } from "@/pages/infrastructure/InfrastructurePage";
import { InvestigationsPage } from "@/pages/investigations/InvestigationsPage";
import { LoginPage } from "@/pages/LoginPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { AccountPage } from "@/pages/settings/AccountPage";
import { ModelsPage } from "@/pages/settings/ModelsPage";
import { RolesPage } from "@/pages/settings/RolesPage";
import { SettingsLayout } from "@/pages/settings/SettingsLayout";
import { TenantsPage } from "@/pages/settings/TenantsPage";
import { UsersPage } from "@/pages/settings/UsersPage";
import { useAuthStore } from "@/stores/authStore";

// Load the heavier pages (graphs, incidents, reports) on demand.
const DependenciesPage = lazy(() =>
  import("@/pages/infrastructure/DependenciesPage").then((m) => ({ default: m.DependenciesPage })),
);
const ResourceDetailPage = lazy(() =>
  import("@/pages/infrastructure/ResourceDetailPage").then((m) => ({ default: m.ResourceDetailPage })),
);
const IncidentsPage = lazy(() => import("@/pages/incidents/IncidentsPage").then((m) => ({ default: m.IncidentsPage })));
const IncidentDetailPage = lazy(() =>
  import("@/pages/incidents/IncidentDetailPage").then((m) => ({ default: m.IncidentDetailPage })),
);
const ReportsPage = lazy(() => import("@/pages/reports/ReportsPage").then((m) => ({ default: m.ReportsPage })));
const ReportDetailPage = lazy(() => import("@/pages/reports/ReportsPage").then((m) => ({ default: m.ReportDetailPage })));
const ApprovalsPage = lazy(() => import("@/pages/approvals/ApprovalsPage").then((m) => ({ default: m.ApprovalsPage })));
const ApprovalDetailPage = lazy(() =>
  import("@/pages/approvals/ApprovalsPage").then((m) => ({ default: m.ApprovalDetailPage })),
);
const PoliciesPage = lazy(() => import("@/pages/settings/PoliciesPage").then((m) => ({ default: m.PoliciesPage })));
const InvestigationDetailPage = lazy(() =>
  import("@/pages/investigations/InvestigationDetailPage").then((m) => ({ default: m.InvestigationDetailPage })),
);
const IntegrationsPage = lazy(() => import("@/pages/IntegrationsPage").then((m) => ({ default: m.IntegrationsPage })));
const lazyPage = (node: ReactNode) => <Suspense fallback={<Spinner />}>{node}</Suspense>;

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: false,
      retry: (count, err) =>
        count < 2 && !(err instanceof ApiError && err.status >= 400 && err.status < 500),
    },
  },
});

/** Restore a session from the refresh cookie on page load. */
function useSessionRestore() {
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const store = useAuthStore.getState();
      if (await refreshAccessToken()) {
        try {
          const me = await authApi.me();
          if (!cancelled) store.setUser(me);
          return;
        } catch {
          /* fall through */
        }
      }
      if (!cancelled) store.clear();
    })();
    return () => {
      cancelled = true;
    };
  }, []);
}

function RequireAuth({ children }: { children: ReactNode }) {
  const status = useAuthStore((s) => s.status);
  const location = useLocation();
  if (status === "initialising") return <Spinner label="Restoring session…" />;
  if (status === "anonymous") return <Navigate to="/login" replace state={{ from: location }} />;
  return <>{children}</>;
}

function RequirePermission({ permission, children }: { permission: string; children: ReactNode }) {
  const allowed = useAuthStore((s) => s.hasPermission(permission));
  return allowed ? <>{children}</> : <Navigate to="/settings/account" replace />;
}

export function AppRoutes() {
  useSessionRestore();
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <AppLayout />
          </RequireAuth>
        }
      >
        <Route
          index
          element={
            <RequirePermission permission="dashboard.read">
              <DashboardPage />
            </RequirePermission>
          }
        />
        <Route
          path="audit"
          element={
            <RequirePermission permission="audit.read">
              <AuditLogsPage />
            </RequirePermission>
          }
        />
        <Route
          path="assistant"
          element={
            <RequirePermission permission="incident.investigate">
              <AssistantPage />
            </RequirePermission>
          }
        />
        <Route
          path="investigations"
          element={
            <RequirePermission permission="incident.read">
              <InvestigationsPage />
            </RequirePermission>
          }
        />
        <Route
          path="investigations/:id"
          element={
            <RequirePermission permission="incident.read">{lazyPage(<InvestigationDetailPage />)}</RequirePermission>
          }
        />
        <Route path="incidents" element={<RequirePermission permission="incident.read">{lazyPage(<IncidentsPage />)}</RequirePermission>} />
        <Route path="incidents/:id" element={<RequirePermission permission="incident.read">{lazyPage(<IncidentDetailPage />)}</RequirePermission>} />
        <Route path="approvals" element={<RequirePermission permission="approval.read">{lazyPage(<ApprovalsPage />)}</RequirePermission>} />
        <Route path="approvals/:id" element={<RequirePermission permission="approval.read">{lazyPage(<ApprovalDetailPage />)}</RequirePermission>} />
        <Route path="reports" element={<RequirePermission permission="incident.read">{lazyPage(<ReportsPage />)}</RequirePermission>} />
        <Route path="reports/:id" element={<RequirePermission permission="incident.read">{lazyPage(<ReportDetailPage />)}</RequirePermission>} />
        <Route
          path="agents"
          element={
            <RequirePermission permission="agent.read">
              <AgentsPage />
            </RequirePermission>
          }
        />
        <Route path="infrastructure" element={<RequirePermission permission="resource.read"><InfrastructurePage /></RequirePermission>} />
        <Route path="infrastructure/:id" element={<RequirePermission permission="resource.read">{lazyPage(<ResourceDetailPage />)}</RequirePermission>} />
        <Route path="dependencies" element={<RequirePermission permission="resource.read">{lazyPage(<DependenciesPage />)}</RequirePermission>} />
        <Route path="integrations" element={<RequirePermission permission="integration.read">{lazyPage(<IntegrationsPage />)}</RequirePermission>} />
        <Route path="settings" element={<SettingsLayout />}>
          <Route index element={<Navigate to="account" replace />} />
          <Route path="account" element={<AccountPage />} />
          <Route path="users" element={<RequirePermission permission="user.read"><UsersPage /></RequirePermission>} />
          <Route path="roles" element={<RequirePermission permission="role.read"><RolesPage /></RequirePermission>} />
          <Route path="models" element={<RequirePermission permission="settings.read"><ModelsPage /></RequirePermission>} />
          <Route path="policies" element={<RequirePermission permission="settings.read">{lazyPage(<PoliciesPage />)}</RequirePermission>} />
          <Route path="tenants" element={<RequirePermission permission="tenant.manage"><TenantsPage /></RequirePermission>} />
        </Route>
        {COMING_SOON.map((item) => (
          <Route key={item.path} path={item.path.slice(1)} element={<ComingSoonPage item={item} />} />
        ))}
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
