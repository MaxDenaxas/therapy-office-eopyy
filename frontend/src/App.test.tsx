import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App from "./App";

function stubFetch(impl: () => Promise<Response>) {
  vi.stubGlobal("fetch", vi.fn(impl));
}

describe("App shell", () => {
  it("shows the Greek title and the planned sections as disabled", async () => {
    stubFetch(() => Promise.resolve(new Response("{}", { status: 200 })));
    render(<App />);

    expect(screen.getByRole("heading", { level: 1, name: "Γραφείο Θεραπειών" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Κύριο μενού" });
    for (const name of ["Ασθενείς", "Θεραπευτές", "Πρότυπα εγγράφων", "Περίοδοι", "Έγγραφα", "Ρυθμίσεις"]) {
      expect(nav).toContainElement(screen.getByRole("button", { name }));
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
    await screen.findByText("Λειτουργεί");
  });

  it("reports the server as running when /api/health answers", async () => {
    stubFetch(() =>
      Promise.resolve(
        new Response(JSON.stringify({ status: "ok", version: "0.1.0", environment: "development" }), {
          status: 200,
        }),
      ),
    );
    render(<App />);
    expect(await screen.findByText("Λειτουργεί")).toBeInTheDocument();
  });

  it("reports the server as unavailable when the request fails", async () => {
    stubFetch(() => Promise.reject(new TypeError("network")));
    render(<App />);
    expect(await screen.findByText("Μη διαθέσιμος")).toBeInTheDocument();
  });

  it("treats an HTTP error as unavailable", async () => {
    stubFetch(() => Promise.resolve(new Response("boom", { status: 500 })));
    render(<App />);
    expect(await screen.findByText("Μη διαθέσιμος")).toBeInTheDocument();
  });
});
