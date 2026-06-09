import SettingsPanel from "@/components/SettingsPanel";

export const metadata = {
  title: "Settings - CI Failure Analyzer",
  description: "Inspect the worker's structural log filter configuration",
};

export default function SettingsPage() {
  return (
    <main className="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <SettingsPanel />
    </main>
  );
}
