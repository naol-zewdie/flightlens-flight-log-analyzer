import { useEffect, useState } from "react";

export interface ServerInfo {
  status: string;
  version: string;
  read_only?: boolean;
}

let info: Promise<ServerInfo> | null = null;

/** /api/health, fetched once per page load. */
export function serverInfo(): Promise<ServerInfo> {
  info ??= fetch("/api/health")
    .then((r) => r.json() as Promise<ServerInfo>)
    .catch(() => ({ status: "unknown", version: "" }));
  return info;
}

/** True on a read-only demo server: uploads, edits and deletes are turned off. */
export function useReadOnly(): boolean {
  const [ro, setRo] = useState(false);
  useEffect(() => {
    let alive = true;
    serverInfo().then((i) => alive && setRo(!!i.read_only));
    return () => {
      alive = false;
    };
  }, []);
  return ro;
}
