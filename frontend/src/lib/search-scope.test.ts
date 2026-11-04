import { beforeEach, describe, expect, it } from "vitest";
import {
  loadStoredScope,
  parseScopeParam,
  scopeParam,
  searchQueryString,
  storeScope,
  toggleScopeItem,
} from "./search-scope";

describe("search scope", () => {
  beforeEach(() => localStorage.clear());

  it("round-trips through the URL parameter and drops junk", () => {
    const items = parseScopeParam(
      "customer:1,folder:4,bogus,source:x,folder:4,source:9",
    );
    expect(items).toEqual([
      { kind: "customer", id: 1 },
      { kind: "folder", id: 4 },
      { kind: "source", id: 9 },
    ]);
    expect(scopeParam(items)).toBe("customer:1,folder:4,source:9");
    expect(parseScopeParam(null)).toEqual([]);
  });

  it("builds the API query string", () => {
    const qs = searchQueryString("timeout error", {
      items: [
        { kind: "customer", id: 1, name: "Acme" },
        { kind: "folder", id: 4, name: "prod" },
        { kind: "folder", id: 5, name: "web" },
      ],
      path: " iis/ ",
    });
    const params = new URLSearchParams(qs);
    expect(params.get("q")).toBe("timeout error");
    expect(params.getAll("customer_id")).toEqual(["1"]);
    expect(params.getAll("folder_id")).toEqual(["4", "5"]);
    expect(params.get("path")).toBe("iis/");
    expect(
      new URLSearchParams(searchQueryString("x", { items: [], path: "" })).has(
        "path",
      ),
    ).toBe(false);
  });

  it("toggles items", () => {
    const acme = { kind: "customer" as const, id: 1, name: "Acme" };
    expect(toggleScopeItem([], acme)).toEqual([acme]);
    expect(toggleScopeItem([acme], { ...acme, name: "renamed" })).toEqual([]);
  });

  it("persists in localStorage and ignores corrupt data", () => {
    storeScope({
      items: [{ kind: "source", id: 3, name: "web01" }],
      path: "app",
    });
    expect(loadStoredScope()).toEqual({
      items: [{ kind: "source", id: 3, name: "web01" }],
      path: "app",
    });
    localStorage.setItem("perchtail.searchScope", "{not json");
    expect(loadStoredScope()).toEqual({ items: [], path: "" });
    localStorage.setItem(
      "perchtail.searchScope",
      JSON.stringify({ items: [{ kind: "evil", id: 1 }] }),
    );
    expect(loadStoredScope().items).toEqual([]);
  });
});
