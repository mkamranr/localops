import { API_BASE, refreshAccessToken } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";

/**
 * Server-Sent Events over fetch().
 *
 * The browser's EventSource cannot send an Authorization header, and the access token
 * lives only in memory (never in a cookie or URL), so the stream is read with fetch and
 * parsed here. Reconnects resume from the last received id via Last-Event-ID.
 */

export interface SseMessage {
  id?: string;
  event: string;
  data: string;
}

/** Incremental parser: feed text chunks, receive complete messages. */
export function createSseParser(onMessage: (message: SseMessage) => void) {
  let buffer = "";
  return (chunk: string) => {
    buffer += chunk.replace(/\r\n?/g, "\n");
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let id: string | undefined;
      let event = "message";
      const data: string[] = [];
      let hasField = false;
      for (const line of block.split("\n")) {
        if (!line || line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        let value = colon === -1 ? "" : line.slice(colon + 1);
        if (value.startsWith(" ")) value = value.slice(1);
        if (field === "id") id = value;
        else if (field === "event") event = value;
        else if (field === "data") data.push(value);
        else continue;
        hasField = true;
      }
      if (hasField && (data.length || event !== "message")) {
        onMessage({ id, event, data: data.join("\n") });
      }
    }
  };
}

export interface StreamOptions {
  after?: number;
  signal: AbortSignal;
  onMessage: (message: SseMessage) => void;
  onStatus?: (status: "connecting" | "live" | "reconnecting") => void;
  /** Called once when the server signals the end of the stream. */
  onEnd?: (data: string) => void;
}

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(t);
      resolve();
    });
  });

export async function streamEvents(path: string, opts: StreamOptions): Promise<void> {
  let lastId = opts.after ?? 0;
  let failures = 0;
  while (!opts.signal.aborted) {
    opts.onStatus?.(failures ? "reconnecting" : "connecting");
    let ended = false;
    try {
      const send = () => {
        const token = useAuthStore.getState().accessToken;
        const headers: Record<string, string> = { Accept: "text/event-stream" };
        if (token) headers.Authorization = `Bearer ${token}`;
        if (lastId) headers["Last-Event-ID"] = String(lastId);
        return fetch(`${API_BASE}${path}`, {
          headers,
          credentials: "same-origin",
          signal: opts.signal,
          cache: "no-store",
        });
      };
      let res = await send();
      if (res.status === 401 && (await refreshAccessToken())) res = await send();
      if (res.status === 401 || res.status === 403 || res.status === 404) {
        opts.onEnd?.(JSON.stringify({ error: res.status }));
        return;
      }
      if (!res.ok || !res.body) throw new Error(`stream failed (${res.status})`);
      opts.onStatus?.("live");
      failures = 0;
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      const parse = createSseParser((m) => {
        if (m.event === "end") {
          ended = true;
          opts.onEnd?.(m.data);
          return;
        }
        if (m.id && /^\d+$/.test(m.id)) lastId = Math.max(lastId, Number(m.id));
        opts.onMessage(m);
      });
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        parse(value);
        if (ended) {
          await reader.cancel();
          return;
        }
      }
    } catch (err) {
      if (opts.signal.aborted) return;
      failures += 1;
      void err;
    }
    if (ended || opts.signal.aborted) return;
    await sleep(Math.min(1000 * 2 ** Math.min(failures, 4), 15_000), opts.signal);
  }
}
