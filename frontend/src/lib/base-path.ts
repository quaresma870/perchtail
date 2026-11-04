import type { Protocol } from "./types";

// What a source's Base path means differs per protocol, and the wrong kind
// of path (a drive path on SMB, a relative one on WinRM) used to fail only
// at connection time (#159). These drive the editor's placeholder and hint.

export function basePathPlaceholder(protocol: Protocol): string {
  switch (protocol) {
    case "smb":
      return "Logs or Logs\\App";
    case "winrm":
      return "D:\\Logs\\App";
    case "agent":
      return "documented for reference — the agent enforces its own root";
    default:
      return "/var/log/appname";
  }
}

export function basePathHint(protocol: Protocol): string {
  switch (protocol) {
    case "ssh":
      return "Absolute path on the host. If the account is chrooted, it is relative to the chroot (usually /).";
    case "smb":
      return "The share name, optionally with a folder inside it — not a drive path like C:\\Logs. Administrative shares (C$, ADMIN$) are refused: share exactly the log folder.";
    case "winrm":
      return "Absolute Windows path on the host, on any drive (e.g. D:\\Logs\\App).";
    case "local":
      return "Must be under a directory the server operator allows via LOCAL_SOURCE_ROOTS — see docs/source-setup.md.";
    default:
      return "";
  }
}

/** Protocols whose Base path can be picked by browsing the host. */
export function canPickFolder(protocol: Protocol): boolean {
  return protocol === "ssh" || protocol === "smb" || protocol === "winrm";
}

/** A label for a new root, from the last segment of its path. */
export function suggestRootLabel(path: string, taken: string[]): string {
  const segment =
    path
      .split(/[\\/]+/)
      .filter((s) => s && !/^[A-Za-z]:$/.test(s))
      .pop() ?? "";
  const base =
    segment
      .replace(/[^A-Za-z0-9 ._-]/g, "")
      .replace(/^[^A-Za-z0-9]+/, "")
      .slice(0, 60) || "folder";
  const lower = new Set(taken.map((t) => t.toLowerCase()));
  if (!lower.has(base.toLowerCase())) return base;
  for (let i = 2; ; i++) {
    const candidate = `${base}-${i}`;
    if (!lower.has(candidate.toLowerCase())) return candidate;
  }
}
