A Jenkins build has failed. Below are the filtered error logs.

**Job:** {job_name} #{build_number}
**Stage:** {stage_name}

## Filtered Error Logs
```text
{filtered_logs}
```

Respond using exactly these markdown headings, in this order.

## Analysis
A short paragraph (2–4 sentences) explaining what went wrong and why, with enough technical context for the developer to understand the issue. Ground your reasoning in the logs above. No bullet points here.

## Step-by-Step Fix
For each step, output a level-3 heading followed by one short paragraph and (when needed) a single fenced code block at the left margin.

```text
### Step 1: <short title>
<one or two sentences describing the action>

```bash
<single command or short script>
```

### Step 2: <short title>
<one or two sentences>

```java
<file edit or snippet>
```
```

Rules:
- Do NOT use a numbered list (`1.`, `2.`, …) for the steps. Use `### Step N:` headings instead — the renderer mis-parses fenced code that is nested inside numbered list items.
- Do NOT indent code fences. They must start at column 1.
- Tag every fence with a real language (`bash`, `java`, `groovy`, `yaml`, `json`, `dockerfile`, `text`, etc.) — never leave the tag blank and never use `hljs` as the tag.
- Skip the code fence entirely if a step is purely descriptive.

## Verify
One sentence and (optionally) one fenced code block with a single command or check that confirms the fix worked.

Total response under 400 words.
