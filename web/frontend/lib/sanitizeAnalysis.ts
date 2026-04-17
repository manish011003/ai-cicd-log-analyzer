/**
 * Strips LLM boilerplate about ES/knowledge-base "match" status from analysis text
 * (legacy rows + model variance). Keeps substantive diagnosis only.
 */
export function stripMatchStatusFromAnalysis(text: string): string {
  let t = (text || "").trim();
  if (!t) return t;

  for (let i = 0; i < 6; i++) {
    const split = t.split("\n\n");
    const para = split[0] ?? "";
    const rest = split.slice(1).join("\n\n");
    const candidate = para.trim();
    if (!candidate) {
      t = rest.trim();
      continue;
    }

    const cl = candidate.toLowerCase();
    const hasSimilarityPct = /\bsimilarity\s*\d+\s*%/i.test(candidate);
    const exactMatchLead = /^(?:#+\s*)?(?:\*\*)?\s*exact\s+match\s+found\b/i.test(candidate);

    const isNoise =
      (cl.includes("exact match") && (cl.includes("similarity") || candidate.includes("%"))) ||
      cl.includes("returning previously accepted") ||
      (hasSimilarityPct && /match|returning|accepted/.test(cl)) ||
      exactMatchLead;

    if (isNoise) {
      t = rest.trim();
      continue;
    }
    break;
  }

  return t.trim();
}
