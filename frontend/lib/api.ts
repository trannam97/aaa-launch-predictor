/**
 * Typed client for the Phase 0 backend API.
 *
 * Everything here runs on the server (React Server Components), so the
 * backend URL never has to be reachable from the browser.
 */

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

export type LifecycleStatus =
  | "pre_launch"
  | "tracking"
  | "failed_to_meet_expectations"
  | "resolved"
  | "unresolved_insufficient_data";

export type Outcome = "flop" | "underperform" | "success" | "breakout";

export type StatusKind = "forecast" | "tracking" | "provisional" | "resolved" | "unresolved";

export interface StatusBadge {
  label: string;
  kind: StatusKind;
  /** True while an outcome is unsettled — the UI must not present it as final. */
  provisional: boolean;
  note: string | null;
}

export interface Snapshot {
  captured_at: string;
  price_final_cents: number | null;
  discount_percent: number | null;
  review_total: number | null;
  review_positive: number | null;
  review_negative: number | null;
  review_score_desc: string | null;
  positive_pct: number | null;
  concurrent_players: number | null;
  metacritic_score: number | null;
}

export interface GameSummary {
  steam_appid: number;
  name: string;
  header_image: string | null;
  release_date: string | null;
  release_date_raw: string | null;
  coming_soon: boolean;
  developers: string[];
  publishers: string[];
  genres: string[];
  price_initial_cents: number | null;
  price_currency: string | null;
  is_free: boolean;
  lifecycle_status: LifecycleStatus;
  status: StatusBadge;
  predicted_outcome: Outcome | null;
  predicted_confidence: number | null;
  resolved_outcome: Outcome | null;
  metacritic_score: number | null;
  latest_snapshot: Snapshot | null;
  last_ingested_at: string | null;
}

export interface GameDetail extends GameSummary {
  short_description: string | null;
  metacritic_url: string | null;
  on_windows: boolean;
  on_mac: boolean;
  on_linux: boolean;
  snapshots: Snapshot[];
}

export class ApiUnavailableError extends Error {}

async function getJson<T>(path: string): Promise<T> {
  let response: Response;
  try {
    // Predictions are cached server-side, but the dashboard should show the
    // latest ingested data rather than a stale render.
    response = await fetch(`${API_BASE_URL}${path}`, { cache: "no-store" });
  } catch (error) {
    throw new ApiUnavailableError(
      `Could not reach the API at ${API_BASE_URL}. Is the backend running?`,
      { cause: error },
    );
  }

  if (response.status === 404) {
    return null as T;
  }
  if (!response.ok) {
    throw new ApiUnavailableError(`API returned HTTP ${response.status} for ${path}`);
  }
  return (await response.json()) as T;
}

export function listGames(): Promise<GameSummary[]> {
  return getJson<GameSummary[]>("/games");
}

export function getGame(appid: number): Promise<GameDetail | null> {
  return getJson<GameDetail | null>(`/games/${appid}`);
}

export function formatPrice(cents: number | null, currency: string | null): string | null {
  if (cents === null) return null;
  const amount = (cents / 100).toFixed(2);
  return currency ? `${amount} ${currency}` : amount;
}

export function formatReleaseDate(game: Pick<GameSummary, "release_date" | "release_date_raw">) {
  // Steam's own string is shown when it exists: "Q4 2026" carries the real
  // precision, where the parsed date would imply a specific day.
  return game.release_date_raw ?? game.release_date ?? "Release date unknown";
}

export function formatNumber(value: number | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  return value.toLocaleString("en-US");
}
