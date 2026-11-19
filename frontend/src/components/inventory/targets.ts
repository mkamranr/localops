import type { IntegrationType, Target } from "@/types/api";

/**
 * One target per line:  `host[:port] [display name]`  or, for HTTP checks,
 * `https://url [display name]`. Blank lines and `#` comments are ignored.
 */
export function parseTargets(
  text: string,
  type: IntegrationType,
): { targets: Target[]; errors: string[] } {
  const targets: Target[] = [];
  const errors: string[] = [];
  text.split(/\r?\n/).forEach((raw, index) => {
    const line = raw.trim();
    if (!line || line.startsWith("#")) return;
    const [first = "", ...rest] = line.split(/\s+/);
    const name = rest.join(" ").trim() || null;
    if (type === "http") {
      if (!/^https?:\/\/\S+$/i.test(first)) {
        errors.push(`Line ${index + 1}: "${first}" is not an http(s) URL`);
        return;
      }
      targets.push({ host: first, name });
      return;
    }
    let host = first;
    let port: number | null = null;
    const bracketed = /^\[([^\]]+)\](?::(\d+))?$/.exec(first); // [ipv6]:port
    const hostPort = /^([^:]+):(\d+)$/.exec(first); // host:port (not bare IPv6)
    if (bracketed) {
      host = bracketed[1] ?? "";
      port = bracketed[2] ? Number(bracketed[2]) : null;
    } else if (hostPort) {
      host = hostPort[1] ?? "";
      port = Number(hostPort[2]);
    }
    if (port !== null && (port < 1 || port > 65535)) {
      errors.push(`Line ${index + 1}: port ${port} is out of range`);
      return;
    }
    if (!/^[A-Za-z0-9._:-]+$/.test(host) || host.startsWith("-")) {
      errors.push(`Line ${index + 1}: "${host}" is not a valid hostname or IP address`);
      return;
    }
    targets.push({ host, port, name });
  });
  return { targets, errors };
}

export function formatTargets(targets: Target[]): string {
  return targets
    .map((t) => {
      const host = t.port ? (t.host.includes(":") ? `[${t.host}]:${t.port}` : `${t.host}:${t.port}`) : t.host;
      return t.name ? `${host} ${t.name}` : host;
    })
    .join("\n");
}
