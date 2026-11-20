import { Lock, ShieldCheck, WifiOff, Workflow } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { authApi } from "@/api/endpoints";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/display";
import { Field, Input } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";

const TENANT_KEY = "localops.lastTenant";

export function LoginPage() {
  const status = useAuthStore((s) => s.status);
  const navigate = useNavigate();
  const location = useLocation();
  const [tenant, setTenant] = useState(() => localStorage.getItem(TENANT_KEY) ?? "default");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (status === "authenticated") return <Navigate to="/" replace />;

  const from = (location.state as { from?: { pathname: string } } | null)?.from?.pathname ?? "/";

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const me = await authApi.login(tenant.trim().toLowerCase(), username, password);
      localStorage.setItem(TENANT_KEY, me.tenant.slug);
      const target = me.must_change_password ? "/settings/account" : from;
      const fallback = me.permissions.includes("dashboard.read") ? target : "/settings/account";
      navigate(target === "/" ? fallback : target, { replace: true });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="bg-grid grid min-h-full lg:grid-cols-[1.1fr_1fr]">
      <section className="relative hidden flex-col justify-between overflow-hidden border-r border-border p-12 lg:flex">
        <div className="flex items-center gap-3">
          <img src="/logo-mark.png" alt="" className="size-10" />
          <span className="text-xl font-semibold tracking-tight">LocalOps</span>
        </div>
        <div className="max-w-lg space-y-6">
          <h1 className="text-4xl leading-tight font-semibold tracking-tight">
            AI-powered infrastructure operations,{" "}
            <span className="bg-gradient-to-r from-blue-400 to-violet-400 bg-clip-text text-transparent">
              under your control.
            </span>
          </h1>
          <p className="text-muted">
            Investigate incidents, analyse root causes and run approved remediation with self-hosted
            models in connected or fully air-gapped environments.
          </p>
          <ul className="space-y-3 text-sm">
            {[
              { icon: Workflow, text: "Multi-agent investigation orchestrated by LangGraph" },
              { icon: ShieldCheck, text: "Deterministic policies and human approval for every change" },
              { icon: WifiOff, text: "Runs fully offline with local models" },
            ].map(({ icon: Icon, text }) => (
              <li key={text} className="flex items-center gap-3 text-muted">
                <span className="grid size-8 place-items-center rounded-lg border border-border-strong bg-surface-2">
                  <Icon className="size-4 text-blue-400" />
                </span>
                {text}
              </li>
            ))}
          </ul>
        </div>
        <p className="text-xs text-subtle">Apache License 2.0 · Open source</p>
      </section>

      <section className="flex items-center justify-center p-6">
        <form onSubmit={submit} className="panel w-full max-w-sm space-y-5 p-8 shadow-2xl shadow-black/40">
          <div className="space-y-1">
            <div className="mb-4 grid size-10 place-items-center rounded-xl bg-primary/15">
              <Lock className="size-5 text-blue-400" />
            </div>
            <h2 className="text-xl font-semibold">Sign in</h2>
            <p className="text-sm text-muted">Use your LocalOps account.</p>
          </div>
          {error && <Alert>{error}</Alert>}
          <Field
            label="Tenant"
            htmlFor="tenant"
            hint={
              <>
                Platform administrators sign in to tenant <span className="font-mono">system</span>.
              </>
            }
          >
            <Input
              id="tenant"
              value={tenant}
              onChange={(e) => setTenant(e.target.value)}
              autoComplete="organization"
              required
            />
          </Field>
          <Field label="Username" htmlFor="username">
            <Input
              id="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
              required
            />
          </Field>
          <Field label="Password" htmlFor="password">
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </Field>
          <Button type="submit" className="w-full" size="lg" loading={submitting}>
            Sign in
          </Button>
        </form>
      </section>
    </div>
  );
}
