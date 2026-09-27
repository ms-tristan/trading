import { cn } from "@/lib/cn";
import { stateLabel } from "@/lib/states";
import { isAttentionState } from "@/lib/ranking";
import type { ProfileView } from "@/lib/types";

export interface LiveTradingNoticeProps {
  /**
   * `allow_live_trading` of `GET /api/settings`; `null` when the settings could
   * not be read, in which case the notice says so instead of guessing.
   */
  allowLiveTrading: boolean | null;
  /** The live profiles of the real-trading section. */
  profiles: ProfileView[];
  className?: string;
}

/**
 * Plain statement of the live-trading gate inside the real-trading section.
 *
 * It answers the two questions an operator asks when a live profile does not
 * trade: is live trading enabled at all, and which profiles were refused (with
 * the reason the engine published).
 */
export function LiveTradingNotice({
  allowLiveTrading,
  profiles,
  className,
}: LiveTradingNoticeProps) {
  const refused = profiles.filter((profile) => isAttentionState(profile.state));

  let headline: string;
  let explanation: string;

  if (allowLiveTrading === null) {
    headline = "Live trading status unknown";
    explanation =
      "The platform settings could not be read, so the state of allow_live_trading is unknown. Paper trading is unaffected.";
  } else if (allowLiveTrading) {
    headline = "Live trading is enabled";
    explanation =
      profiles.length === 0
        ? "No live profile is configured yet; enable one when the operator token and the exchange keys are in place."
        : `${profiles.length} live ${profiles.length === 1 ? "profile is" : "profiles are"} configured; the venue is funded with real money.`;
  } else {
    headline = "Live trading is not enabled";
    explanation =
      "The platform reports allow_live_trading = false, so no live profile can start or keep trading; a live profile stays queued or blocked until an operator enables the gate.";
  }

  return (
    <div
      role="note"
      className={cn(
        "mb-2 min-w-0 rounded-lg border border-border bg-muted/40 p-3 text-base",
        className,
      )}
    >
      <p className="font-semibold">{headline}</p>
      <p className="text-muted-foreground">{explanation}</p>
      {refused.length > 0 ? (
        <>
          <p className="mt-2 font-medium">
            {refused.length === 1 ? "Refused profile" : "Refused profiles"}
          </p>
          <ul className="list-disc pl-5 text-muted-foreground">
            {refused.map((profile) => (
              <li key={profile.id}>
                <span className="text-foreground">{profile.name}</span> — {stateLabel(profile.state)}
                {profile.state_reason ? `: ${profile.state_reason}` : ""}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}
