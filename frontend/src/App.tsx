import ServerStatus from "./components/ServerStatus";

// Sections planned in docs/ARCHITECTURE.md. Not implemented yet: shown disabled.
const SECTIONS = [
  "Ασθενείς",
  "Θεραπευτές",
  "Πρότυπα εγγράφων",
  "Περίοδοι",
  "Έγγραφα",
  "Ρυθμίσεις",
];

export default function App() {
  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl flex-col gap-1 px-4 py-4">
          <h1 className="text-xl font-semibold">Γραφείο Θεραπειών</h1>
          <p className="text-sm text-slate-600">
            Διαχείριση βεβαιώσεων θεραπειών ΕΟΠΥΥ
          </p>
        </div>
      </header>

      <div className="mx-auto flex max-w-5xl gap-6 px-4 py-6">
        <nav aria-label="Κύριο μενού" className="w-48 shrink-0">
          <ul className="flex flex-col gap-1">
            {SECTIONS.map((name) => (
              <li key={name}>
                <button
                  type="button"
                  disabled
                  className="w-full rounded px-3 py-2 text-left text-sm text-slate-400"
                >
                  {name}
                </button>
              </li>
            ))}
          </ul>
        </nav>

        <main className="flex-1 rounded border border-slate-200 bg-white p-6">
          <h2 className="mb-2 text-lg font-medium">Καλώς ήρθατε</h2>
          <p className="mb-4 text-slate-700">
            Η εφαρμογή βρίσκεται υπό ανάπτυξη. Οι οθόνες θα προστεθούν σταδιακά.
          </p>
          <ServerStatus />
        </main>
      </div>
    </div>
  );
}
