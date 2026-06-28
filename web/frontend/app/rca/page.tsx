import { Suspense } from "react";
import RcaView from "@/components/RcaView";

export const metadata = {
  title: "Root Cause Analysis - CI Failure Analyzer",
  description: "AI root-cause analysis for a Jenkins build failure",
};

export default function RcaPage() {
  return (
    <main className="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <Suspense
        fallback={
          <div className="flex h-screen items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-slate-300 border-t-slate-700 dark:border-slate-700 dark:border-t-slate-200" />
          </div>
        }
      >
        <RcaView />
      </Suspense>
    </main>
  );
}
