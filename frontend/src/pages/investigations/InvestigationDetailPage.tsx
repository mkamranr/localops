import { ArrowLeft } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { InvestigationWorkspace } from "@/components/investigation/InvestigationWorkspace";

export function InvestigationDetailPage() {
  const { id } = useParams<{ id: string }>();
  if (!id) return null;
  return (
    <div className="space-y-4">
      <Link to="/investigations" className="inline-flex items-center gap-1.5 text-xs text-muted hover:text-fg">
        <ArrowLeft className="size-3.5" /> All investigations
      </Link>
      <InvestigationWorkspace id={id} />
    </div>
  );
}
