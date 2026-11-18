import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { streamEvents } from "@/api/sse";
import type { InvestigationEvent } from "@/types/api";

export type StreamState = "connecting" | "live" | "reconnecting" | "ended";

/** Events that change the investigation record itself (status, question, result). */
const REFRESH_ON = /^(investigation\.|clarification\.|report\.)/;

/**
 * Live event feed for one investigation: replays history, then follows new events.
 * Also refreshes the investigation query whenever its status may have changed.
 */
export function useInvestigationStream(id: string | undefined, enabled = true) {
  const queryClient = useQueryClient();
  const [events, setEvents] = useState<InvestigationEvent[]>([]);
  const [state, setState] = useState<StreamState>("connecting");

  useEffect(() => {
    if (!id || !enabled) return;
    setEvents([]);
    setState("connecting");
    const controller = new AbortController();
    void streamEvents(`/investigations/${id}/stream`, {
      signal: controller.signal,
      onStatus: setState,
      onMessage: (m) => {
        let event: InvestigationEvent;
        try {
          event = JSON.parse(m.data) as InvestigationEvent;
        } catch {
          return;
        }
        setEvents((prev) => {
          const last = prev.at(-1);
          if (!last || last.seq < event.seq) return [...prev, event];
          if (prev.some((e) => e.seq === event.seq)) return prev;
          return [...prev, event].sort((x, y) => x.seq - y.seq);
        });
        if (REFRESH_ON.test(event.event_type)) {
          void queryClient.invalidateQueries({ queryKey: ["investigation", id] });
          void queryClient.invalidateQueries({ queryKey: ["investigations"] });
        }
      },
      onEnd: () => {
        setState("ended");
        void queryClient.invalidateQueries({ queryKey: ["investigation", id] });
      },
    });
    return () => controller.abort();
  }, [id, enabled, queryClient]);

  return { events, state };
}
