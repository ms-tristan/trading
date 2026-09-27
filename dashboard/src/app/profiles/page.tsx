import Link from "next/link";

import { ModeSection } from "@/components/overview/ModeSection";
import { profileMatchesStrategy } from "@/components/strategies/strategyMeta";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { SectionHeader } from "@/components/ui/SectionHeader";
import { fetchProfiles } from "@/lib/api";
import { splitByMode } from "@/lib/ranking";
import { EMPTY_PROFILES, type ProfileView } from "@/lib/types";

/** Live data on every request: the profile list is never prerendered. */
export const dynamic = "force-dynamic";

export interface ProfilesIndexPageProps {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}

/**
 * Validate the optional `?strategy=` filter.
 *
 * The strategy cards link with a catalogue id, which may be spelled as the id
 * (`momentum`) or as the freqtrade class name (`MomentumStrategy`); the matching
 * itself lives in `profileMatchesStrategy`. An empty parameter means "no filter".
 */
export function normaliseStrategyFilter(value: string | string[] | undefined): string | null {
  const candidate = Array.isArray(value) ? value[0] : value;
  const trimmed = typeof candidate === "string" ? candidate.trim() : "";
  return trimmed === "" ? null : trimmed;
}

/** Keep the profiles that hold `strategy`, or all of them when it is `null`. */
export function filterByStrategy(profiles: ProfileView[], strategy: string | null): ProfileView[] {
  if (strategy === null) {
    return profiles;
  }
  return profiles.filter((profile) => profileMatchesStrategy(strategy, profile.strategy));
}

/**
 * Profile index.
 *
 * It renders the **ranked** list of `GET /api/profiles` - the API ranks by
 * portfolio value, descending - split by trading mode exactly like the overview,
 * through the very same `ModeSection`: same columns, same aggregate line, same
 * `aria-sort` headers. The optional `?strategy=` filter is what the strategy
 * cards link to, and the filter narrows both sections at once.
 *
 * Re-sorting happens inside the tables, on an explicit click on a sortable
 * header. It is deliberately *not* a server-side re-sort: the `#` column is the
 * portfolio-value rank of the API order, and re-ordering the rows before they
 * reach the table would renumber that rank.
 */
export default async function ProfilesPage({ searchParams }: ProfilesIndexPageProps) {
  const params = searchParams !== undefined ? await searchParams : {};
  const strategy = normaliseStrategyFilter(params?.strategy);

  const { data, error } = await fetchProfiles(EMPTY_PROFILES);

  const filtered = filterByStrategy(data.profiles, strategy);
  const { paper, live } = splitByMode(filtered);

  return (
    <div className="grid grid-cols-12 gap-2">
      <SectionHeader
        className="col-span-12"
        title="Profiles"
        subtitle={`${filtered.length} ${
          filtered.length === 1 ? "profile" : "profiles"
        } in the API ranking (portfolio value, descending)`}
        actions={
          strategy === null ? undefined : (
            <Link
              href="/profiles"
              className="rounded-md border border-border px-2 py-1 text-sm transition-smooth hover:border-ring"
            >
              Clear filter
            </Link>
          )
        }
      />

      <ErrorBanner
        className="col-span-12"
        error={error}
        title="The profile ranking could not be refreshed"
      />

      {strategy === null ? null : (
        <p className="col-span-12 text-sm text-muted-foreground">
          Filtered on strategy <code className="font-mono text-foreground">{strategy}</code>:{" "}
          {filtered.length} of {data.profiles.length} profiles.
        </p>
      )}

      <ModeSection
        className="col-span-12"
        id="paper-trading"
        title="Paper trading"
        profiles={paper}
        emptyMessage={
          strategy === null
            ? "No paper profile is configured yet."
            : `No paper profile holds the strategy ${strategy}.`
        }
      />

      <ModeSection
        className="col-span-12"
        id="real-trading"
        title="Real trading"
        profiles={live}
        emptyMessage={
          strategy === null
            ? "No live profile is configured yet."
            : `No live profile holds the strategy ${strategy}.`
        }
      />
    </div>
  );
}
