import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AAA Launch Predictor",
  description:
    "Commercial performance forecasts for upcoming Triple-A releases on Steam. Not affiliated with Valve.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="site-header">
            <h1>AAA Launch Predictor</h1>
            <p>
              Commercial performance forecasts for Triple-A releases, measured against
              budget-tier expectations — not a judgment of a game&rsquo;s creative quality.
            </p>
          </header>
          <main>{children}</main>
          <footer className="site-footer">
            <p>
              Not affiliated with, endorsed by, or connected to Valve Corporation. Steam data is
              retrieved via Valve&rsquo;s public Web API. Outcomes here reflect sales and
              engagement relative to a game&rsquo;s budget tier, and are attributed to business
              and market conditions — budget, marketing, timing, platform strategy — not to the
              work of individual developers or artists.
            </p>
            <p>
              Predictions are estimates generated from public data and can be wrong. Where an
              outcome cannot be confidently resolved, it stays marked provisional rather than
              guessed.
            </p>
          </footer>
        </div>
      </body>
    </html>
  );
}
