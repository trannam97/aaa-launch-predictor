import { GameCard } from "@/components/GameCard";
import { ApiUnavailableError, listGames } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  let games;
  try {
    games = await listGames();
  } catch (error) {
    if (error instanceof ApiUnavailableError) {
      return (
        <div className="empty">
          <p>{error.message}</p>
          <p>
            Start it with <code>uvicorn app.main:app --reload</code> from <code>/backend</code>.
          </p>
        </div>
      );
    }
    throw error;
  }

  if (games.length === 0) {
    return (
      <div className="empty">
        <p>No games are being tracked yet.</p>
        <p>
          Add one with{" "}
          <code>curl -X POST localhost:8000/games -H &apos;content-type: application/json&apos; -d
            &apos;&#123;&quot;steam_appid&quot;: 1174180&#125;&apos;</code>
        </p>
      </div>
    );
  }

  return (
    <div className="grid">
      {games.map((game) => (
        <GameCard key={game.steam_appid} game={game} />
      ))}
    </div>
  );
}
