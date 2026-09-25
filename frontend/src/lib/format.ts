/** Compact, unambiguous display helpers. Times are shown in UTC to match the API. */

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

export function formatInstant(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");
}

export function validity(from: string, until: string | null): string {
  return `${formatInstant(from)} → ${until ? formatInstant(until) : "open"}`;
}

export function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}…` : id;
}
