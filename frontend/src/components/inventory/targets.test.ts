import { describe, expect, it } from "vitest";
import { formatTargets, parseTargets } from "@/components/inventory/targets";

describe("parseTargets", () => {
  it("parses hosts, ports, IPv6 and names", () => {
    const { targets, errors } = parseTargets(
      "app-01.corp.local\n10.0.0.12:2222 Database server\n# comment\n\n[fd00::5]:22 v6 host\nfd00::6",
      "linux_ssh",
    );
    expect(errors).toEqual([]);
    expect(targets).toEqual([
      { host: "app-01.corp.local", port: null, name: null },
      { host: "10.0.0.12", port: 2222, name: "Database server" },
      { host: "fd00::5", port: 22, name: "v6 host" },
      { host: "fd00::6", port: null, name: null },
    ]);
  });

  it("rejects bad hosts, ports and option-like input", () => {
    const { targets, errors } = parseTargets("-oProxyCommand=x\nhost:70000\nbad/host", "linux_ssh");
    expect(targets).toEqual([]);
    expect(errors).toHaveLength(3);
  });

  it("requires URLs for HTTP checks", () => {
    const ok = parseTargets("https://intranet.local/health Intranet", "http");
    expect(ok.targets).toEqual([{ host: "https://intranet.local/health", name: "Intranet" }]);
    expect(parseTargets("intranet.local", "http").errors).toHaveLength(1);
  });

  it("round-trips through formatTargets", () => {
    const text = "10.0.0.12:2222 Database server\n[fd00::5]:22\napp-01";
    expect(formatTargets(parseTargets(text, "linux_ssh").targets)).toBe(text);
  });
});
