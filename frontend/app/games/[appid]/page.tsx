import Link from "next/link";
import { notFound } from "next/navigation";

import { Sparkline } from "@/components/Sparkline";
import { StatusBadge } from "@/components/StatusBadge";
import {
  ApiUnavailableError,
  formatNumber,
  formatPrice,
  formatReleaseDate,
  getGame,
  type Snapshot,
} from "@/lib/api";

export const dynamic = "force-dynamic";

function platforms(game: { on_windows: boolean; on_mac: boolean; on_linux: boolean }) {
  const names = [
    game.on_windows ? "Windows" : null,
    game.on_mac ? "macOS" : null,
    game.on_linux ? "Linux" : null,
  ].filter(Boolean);
  return names.length > 0 ? names.join(", ") : "Unknown";
}

/** Snapshots arrive newest-first; a chart needs them oldest-first. */
function series(snapshots: Snapshot[], key: "concurrent_players" | "positive_pct"): number[] {
  return snapshots
    .slice()
    .reverse()
    .map((snapshot) => snapshot[key])
    .filter((value): value is number => value !== null);
}

export default async function GameDetailPage({
  params,
}: {
  params: Promise<{ appid: string }>;
}) {
  const { appid } = await params;
  const parsed = Number(appid);
  if (!Number.isInteger(parsed) || parsed <= 0) notFound();

  let game;
  try {
    game = await getGame(parsed);
  } catch (error) {
    if (error instanceof ApiUnavailableError) {
      return <div className="empty">{error.message}</div>;
    }
    throw error;
  }
  if (!game) notFound();

  const latest = game.latest_snapshot;
  const players = series(game.snapshots, "concurrent_players");
  const sentiment = series(game.snapshots, "positive_pct");

  return (
    <article>
      <Link className="back-link" href="/">
        ← All tracked games
      </Link>

      <div className="panel">
        <h2>Status</h2>
        <StatusBadge status={game.status} />
        {game.status.note ? <p className="badge-note">{game.status.note}</p> : null}
        <h3 style={{ margin: "1rem 0 0.25rem", fontSize: "1.15rem" }}>{game.name}</h3>
        <p className="meta">
          {formatReleaseDate(game)} ·{" "}
          {game.developers.length > 0 ? game.developers.join(", ") : "Developer unknown"}
          {game.publishers.length > 0 ? ` · published by ${game.publishers.join(", ")}` : ""}
        </p>
        {game.short_description ? <p>{game.short_description}</p> : null}
      </div>

      <div className="panel">
        <h2>Latest reading</h2>
        <ul className="stat-row">
          <li>
            <span className="stat-label">User reviews</span>
            <span className="stat-value">
              {latest?.review_total ? formatNumber(latest.review_total) : "—"}
            </span>
          </li>
          <li>
            <span className="stat-label">Positive</span>
            <span className="stat-value">
              {latest?.positive_pct !== null && latest?.positive_pct !== undefined
                ? `${latest.positive_pct}%`
                : "—"}
            </span>
          </li>
          <li>
            <span className="stat-label">Steam summary</span>
            <span className="stat-value">{latest?.review_score_desc ?? "—"}</span>
          </li>
          <li>
            {/* Critic and user scores stay separate numbers, never blended. */}
            <span className="stat-label">Critic score</span>
            <span className="stat-value">{game.metacritic_score ?? "—"}</span>
          </li>
          <li>
            <span className="stat-label">Playing now</span>
            <span className="stat-value">
              {formatNumber(latest?.concurrent_players ?? null) ?? "—"}
            </span>
          </li>
          <li>
            <span className="stat-label">Price</span>
            <span className="stat-value">
              {game.is_free
                ? "Free"
                : (formatPrice(latest?.price_final_cents ?? null, game.price_currency) ?? "—")}
              {latest?.discount_percent ? ` (−${latest.discount_percent}%)` : ""}
            </span>
          </li>
          <li>
            <span className="stat-label">Platforms</span>
            <span className="stat-value">{platforms(game)}</span>
          </li>
        </ul>
      </div>

      {players.length > 1 || sentiment.length > 1 ? (
        <div className="panel">
          <h2>Trend</h2>
          {players.length > 1 ? (
            <>
              <span className="stat-label">Concurrent players</span>
              <Sparkline values={players} label="Concurrent players" />
            </>
          ) : null}
          {sentiment.length > 1 ? (
            <>
              <span className="stat-label">Positive review share</span>
              <Sparkline values={sentiment} label="Positive review share" />
            </>
          ) : null}
        </div>
      ) : null}

      <div className="panel">
        <h2>Snapshot history</h2>
        <p className="badge-note">
          Readings this project has collected itself. History only starts when a game is added —
          there is no backfill of past concurrent-player data.
        </p>
        <table>
          <thead>
            <tr>
              <th>Captured</th>
              <th>Reviews</th>
              <th>Positive</th>
              <th>Players</th>
              <th>Price</th>
            </tr>
          </thead>
          <tbody>
            {game.snapshots.map((snapshot) => (
              <tr key={snapshot.captured_at}>
                <td>{new Date(snapshot.captured_at).toLocaleString("en-US")}</td>
                <td>{formatNumber(snapshot.review_total) ?? "—"}</td>
                <td>{snapshot.positive_pct !== null ? `${snapshot.positive_pct}%` : "—"}</td>
                <td>{formatNumber(snapshot.concurrent_players) ?? "—"}</td>
                <td>
                  {formatPrice(snapshot.price_final_cents, game.price_currency) ??
                    (game.is_free ? "Free" : "—")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </article>
  );
}
