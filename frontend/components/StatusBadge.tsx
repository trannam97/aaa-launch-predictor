import type { StatusBadge as StatusBadgeData } from "@/lib/api";

/**
 * Renders a game's current status.
 *
 * Provisional states (an outcome that hasn't resolved yet) get a dashed
 * outline so a visitor can tell at a glance that nothing has been concluded.
 * Every settled outcome — flop through breakout — renders identically.
 */
export function StatusBadge({ status }: { status: StatusBadgeData }) {
  const className = status.provisional ? "badge badge--provisional" : "badge";
  return (
    <span className={className} title={status.note ?? undefined}>
      {status.label}
      {status.provisional ? " · provisional" : null}
    </span>
  );
}
