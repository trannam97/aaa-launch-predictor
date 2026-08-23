import Link from "next/link";

import { StatusBadge } from "@/components/StatusBadge";
import { formatNumber, formatReleaseDate, type GameSummary } from "@/lib/api";

export function GameCard({ game }: { game: GameSummary }) {
  const snapshot = game.latest_snapshot;
  const studio = game.developers[0] ?? game.publishers[0] ?? "Studio unknown";

  return (
    <Link className="card" href={`/games/${game.steam_appid}`}>
      {game.header_image ? (
        // Steam serves header images from several CDN hosts, so this stays a
        // plain <img> rather than a next/image allowlist to keep in sync.
        <img className="card-image" src={game.header_image} alt="" loading="lazy" />
      ) : (
        <div className="card-image" />
      )}
      <div className="card-body">
        <h2 className="card-title">{game.name}</h2>
        <p className="meta">
          {formatReleaseDate(game)} · {studio}
        </p>
        <StatusBadge status={game.status} />
        <p className="meta">
          {snapshot?.review_score_desc && snapshot.review_total
            ? `${snapshot.review_score_desc} · ${formatNumber(snapshot.review_total)} user reviews`
            : "No user review data yet"}
          {snapshot?.concurrent_players !== null && snapshot?.concurrent_players !== undefined
            ? ` · ${formatNumber(snapshot.concurrent_players)} playing now`
            : ""}
        </p>
      </div>
    </Link>
  );
}
