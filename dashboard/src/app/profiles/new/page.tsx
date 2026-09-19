import type { JSX } from 'react';

import { ServerCrash } from 'lucide-react';

import { CreateProfileForm } from '@/components/profiles/create-profile-form';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorBanner } from '@/components/ui/error-banner';
import { fetchCatalog } from '@/lib/api';
import { failureReport } from '@/lib/api-failure';
import { serverApiBaseUrl } from '@/lib/config';
import type { CatalogPayload } from '@/lib/types';

/**
 * Profile creation route.
 *
 * This is a Server Component: it fetches `GET /api/catalog` once — with
 * `cache: 'no-store'`, pinned by the API client — and renders the whole form
 * (asset, strategy, timeframe, mode) from that payload, so no picker list is
 * ever hard-coded in the dashboard. Nothing polls here: the form submits once
 * and the overview it navigates back to is the surface that refreshes.
 *
 * A failure of the catalogue call renders the unreachable panel below instead of
 * throwing: an API outage never becomes a Next.js error screen, and the panel
 * speaks the shared operator-facing copy (headline plus raw detail) rather than
 * echoing a proxy status. The operator token is not fetched here — it lives in
 * the browser `sessionStorage` and is read by the form itself at submit time,
 * because a Server Component never sees the browser storage.
 */

/**
 * Props of the route.
 *
 * The route takes no parameter; the interface exists so the page keeps the shape
 * of its siblings.
 */
// eslint-disable-next-line @typescript-eslint/no-empty-object-type
export interface CreateProfilePageProps {}

/** Never prerender: the catalogue is read live from the monitoring server. */
export const dynamic = 'force-dynamic';

/** The page heading and its one-line explanation. */
function PageHeading() {
  return (
    <div className="flex flex-col gap-sm">
      <h1 className="font-mono text-xl font-semibold text-foreground">New profile</h1>
      <p className="text-sm text-muted-foreground">
        Pick a tradable pair, a strategy, a timeframe and a mode. The engine starts the profile as
        soon as it is written to the profiles configuration file.
      </p>
    </div>
  );
}

/**
 * First paint rendered when the catalogue cannot be read.
 *
 * It is an ordinary page render, not an error screen: the operator gets the
 * mapped failure headline plus its raw detail, the origin the dashboard tried to
 * call (the `API_ORIGIN` configuration) and what to check — and no form that
 * could not be submitted anyway.
 */
function ApiUnreachable({ failure, baseUrl }: { failure: unknown; baseUrl: string }) {
  const report = failureReport(failure);
  return (
    <div className="flex flex-col gap-xl">
      <h1 className="font-mono text-xl font-semibold text-foreground">New profile</h1>
      <ErrorBanner message={report.headline} detail={report.detail} />
      <EmptyState
        title="API unreachable"
        description={`The monitoring API is unreachable: no catalogue payload was returned by ${baseUrl}, so the profile form was not started. Check that the Python monitoring server is running and that API_ORIGIN points at it.`}
        icon={<ServerCrash className="size-5" />}
      />
    </div>
  );
}

/** Creation route: the catalogue is fetched on the server, the form is a client island. */
export default async function CreateProfilePage(): Promise<JSX.Element> {
  const baseUrl = serverApiBaseUrl();

  let catalog: CatalogPayload;
  try {
    catalog = await fetchCatalog({ baseUrl });
  } catch (error) {
    return <ApiUnreachable failure={error} baseUrl={baseUrl} />;
  }

  return (
    <div className="flex flex-col gap-2xl">
      <PageHeading />
      <CreateProfileForm catalog={catalog} operatorToken="" />
    </div>
  );
}
