export function ComparatorPage() {
  return (
    <section className="space-y-4">
      <h1 className="text-xl font-semibold">Release Comparator</h1>
      <div className="flex flex-wrap gap-2 rounded-lg border border-slate-800 bg-slate-900 p-4">
        <select disabled className="rounded bg-slate-800 px-3 py-2 text-sm text-slate-500">
          <option>Repository</option>
        </select>
        <input disabled placeholder="Base ref (e.g. v2.4.0)" className="rounded bg-slate-800 px-3 py-2 text-sm" />
        <input disabled placeholder="Target ref (e.g. rc-v2.5.0)" className="rounded bg-slate-800 px-3 py-2 text-sm" />
        <button disabled className="rounded bg-indigo-600/50 px-4 py-2 text-sm font-medium">
          Run Inspection
        </button>
      </div>
      <p className="text-sm text-slate-500">
        Comparator UI arrives in milestone M4. This page is the scaffold.
      </p>
    </section>
  )
}
