import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { EmptyState, Panel } from "@/components/ui/display";

export function NotFoundPage() {
  return (
    <Panel>
      <EmptyState title="Page not found" description="The page you requested doesn't exist." />
      <div className="flex justify-center pb-6">
        <Button asChild variant="secondary">
          <Link to="/">Back to dashboard</Link>
        </Button>
      </div>
    </Panel>
  );
}
