/** Map entity types to colors – case-insensitive. */
const TYPE_COLORS: Record<string, string> = {
  person: "#60a5fa",
  organization: "#f472b6",
  org: "#f472b6",
  role: "#a78bfa",
  project: "#a78bfa",
  event: "#34d399",
  meeting: "#34d399",
  topic: "#9da3b4",
};

const DEFAULT_COLOR = "#6b7186";

export function typeColor(type: string): string {
  return TYPE_COLORS[type.toLowerCase()] ?? DEFAULT_COLOR;
}
