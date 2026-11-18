import { describe, expect, it } from "vitest";
import { createSseParser, type SseMessage } from "@/api/sse";

describe("createSseParser", () => {
  it("parses complete messages across arbitrary chunk boundaries", () => {
    const out: SseMessage[] = [];
    const feed = createSseParser((m) => out.push(m));
    const stream =
      'retry: 3000\n\nid: 1\nevent: agent.started\ndata: {"seq":1}\n\n: keepalive\n\n' +
      'id: 2\r\nevent: agent.completed\r\ndata: {"seq":\r\ndata: 2}\r\n\r\nevent: end\ndata: {}\n\n';
    for (let i = 0; i < stream.length; i += 7) feed(stream.slice(i, i + 7));
    expect(out).toEqual([
      { id: "1", event: "agent.started", data: '{"seq":1}' },
      { id: "2", event: "agent.completed", data: '{"seq":\n2}' },
      { id: undefined, event: "end", data: "{}" },
    ]);
  });

  it("keeps incomplete messages buffered", () => {
    const out: SseMessage[] = [];
    const feed = createSseParser((m) => out.push(m));
    feed("id: 5\nevent: x\ndata: 1\n");
    expect(out).toHaveLength(0);
    feed("\n");
    expect(out).toEqual([{ id: "5", event: "x", data: "1" }]);
  });
});
