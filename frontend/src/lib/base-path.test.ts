import { describe, expect, it } from "vitest";
import {
  basePathHint,
  basePathPlaceholder,
  canPickFolder,
  suggestRootLabel,
} from "./base-path";

describe("base path helpers", () => {
  it("uses a protocol-appropriate placeholder", () => {
    expect(basePathPlaceholder("smb")).toContain("Logs");
    expect(basePathPlaceholder("smb")).not.toMatch(/^[A-Z]:/);
    expect(basePathPlaceholder("winrm")).toMatch(/^[A-Z]:\\/);
    expect(basePathPlaceholder("ssh")).toMatch(/^\//);
  });

  it("warns against drive paths on SMB", () => {
    expect(basePathHint("smb")).toContain("not a drive path");
  });

  it("offers the picker only for remote protocols", () => {
    expect(canPickFolder("ssh")).toBe(true);
    expect(canPickFolder("winrm")).toBe(true);
    expect(canPickFolder("smb")).toBe(true);
    expect(canPickFolder("agent")).toBe(false);
    expect(canPickFolder("local")).toBe(false);
  });

  it("suggests a unique label from the path", () => {
    expect(suggestRootLabel("D:\\Logs\\IIS", [])).toBe("IIS");
    expect(suggestRootLabel("/var/log/nginx/", ["nginx"])).toBe("nginx-2");
    expect(suggestRootLabel("C:\\", [])).toBe("folder");
    expect(suggestRootLabel("/srv/@weird", [])).toBe("weird");
  });
});
