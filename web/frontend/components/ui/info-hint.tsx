"use client";

import { useId, useState } from "react";

interface Props {
  /** The explanatory text revealed on hover / focus. */
  text: string;
  /** Optional accessible label for the trigger (defaults to "More info"). */
  label?: string;
  /** Tooltip placement relative to the icon. */
  side?: "top" | "bottom" | "left" | "right";
  className?: string;
}

const SIDE_CLASSES: Record<NonNullable<Props["side"]>, string> = {
  top: "bottom-full left-1/2 mb-2 -translate-x-1/2",
  bottom: "top-full left-1/2 mt-2 -translate-x-1/2",
  left: "right-full top-1/2 mr-2 -translate-y-1/2",
  right: "left-full top-1/2 ml-2 -translate-y-1/2",
};

/**
 * A small "ⓘ" affordance that reveals helper copy on hover or keyboard focus.
 * Replaces inline explainer paragraphs so the UI stays clean but the guidance
 * is still one hover away.
 */
export default function InfoHint({ text, label = "More info", side = "top", className = "" }: Props) {
  const [open, setOpen] = useState(false);
  const tooltipId = useId();

  return (
    <span className={`relative inline-flex items-center ${className}`}>
      <button
        type="button"
        aria-label={label}
        aria-describedby={open ? tooltipId : undefined}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={(e) => {
          e.preventDefault();
          setOpen((v) => !v);
        }}
        className="flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 text-[10px] font-semibold leading-none text-slate-500 transition-colors hover:border-slate-400 hover:text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 dark:border-slate-600 dark:text-slate-400 dark:hover:border-slate-500 dark:hover:text-slate-200"
      >
        i
      </button>
      {open && (
        <span
          id={tooltipId}
          role="tooltip"
          className={`absolute z-50 w-64 rounded-lg border border-slate-200 bg-white px-3 py-2 text-left text-[11px] font-normal normal-case leading-snug tracking-normal text-slate-600 shadow-lg dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300 ${SIDE_CLASSES[side]}`}
        >
          {text}
        </span>
      )}
    </span>
  );
}
