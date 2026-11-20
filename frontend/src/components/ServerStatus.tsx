import { useEffect, useState } from "react";
import { fetchHealth } from "../api/health";

type State = "checking" | "ok" | "down";

const LABELS: Record<State, string> = {
  checking: "Έλεγχος…",
  ok: "Λειτουργεί",
  down: "Μη διαθέσιμος",
};

const STYLES: Record<State, string> = {
  checking: "bg-slate-100 text-slate-700",
  ok: "bg-emerald-100 text-emerald-800",
  down: "bg-red-100 text-red-800",
};

export default function ServerStatus() {
  const [state, setState] = useState<State>("checking");

  useEffect(() => {
    const controller = new AbortController();
    fetchHealth(controller.signal)
      .then(() => setState("ok"))
      .catch(() => {
        if (!controller.signal.aborted) setState("down");
      });
    return () => controller.abort();
  }, []);

  return (
    <p className="text-sm text-slate-600">
      Κατάσταση διακομιστή:{" "}
      <span
        role="status"
        className={`rounded-full px-2 py-0.5 font-medium ${STYLES[state]}`}
      >
        {LABELS[state]}
      </span>
    </p>
  );
}
