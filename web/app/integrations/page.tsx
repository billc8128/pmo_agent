import { canManageIntegrations } from '@/lib/integration-permissions';
import {
  buildWebhookUrl,
  EXTERNAL_PROVIDERS,
  type ExternalDeliveryRow,
  type ExternalRepoRow,
  type PublicExternalDelivery,
  type PublicExternalRepo,
  toPublicExternalDelivery,
  toPublicExternalRepo,
} from '@/lib/integrations';
import { adminClient } from '@/lib/supabase-admin';
import { serverComponentClient } from '@/lib/supabase-server';
import { IntegrationsPanel } from './integrations-panel';

export const dynamic = 'force-dynamic';

export default async function IntegrationsPage() {
  const sb = await serverComponentClient();
  const {
    data: { user },
  } = await sb.auth.getUser();

  const { data: profile } = user
    ? await sb
        .from('profiles')
        .select('id, handle')
        .eq('id', user.id)
        .maybeSingle()
    : { data: null };
  const viewer = user ? { id: user.id, handle: profile?.handle ?? null } : null;
  const canManage = canManageIntegrations(viewer);
  const botBaseUrl =
    process.env.BOT_WEBHOOK_BASE_URL ??
    process.env.NEXT_PUBLIC_BOT_WEBHOOK_BASE_URL ??
    '';
  const githubAppInstallUrl =
    process.env.GITHUB_APP_INSTALL_URL ??
    process.env.NEXT_PUBLIC_GITHUB_APP_INSTALL_URL ??
    null;

  if (!user) {
    const providers = EXTERNAL_PROVIDERS.map((provider) => ({
      provider,
      webhookUrl: buildWebhookUrl(botBaseUrl, provider),
      installUrl: provider === 'github' ? githubAppInstallUrl : null,
      repoCount: 0,
      latestDeliveryAt: null,
    }));
    return (
      <main className="mx-auto max-w-5xl px-4 py-8 sm:py-12">
        <IntegrationsPanel
          providers={providers}
          repos={[]}
          deliveries={[]}
          signedIn={false}
          canManage={false}
          loginHref={`/login?next=${encodeURIComponent('/integrations')}`}
          botBaseConfigured={Boolean(botBaseUrl)}
        />
      </main>
    );
  }

  let admin: ReturnType<typeof adminClient>;
  try {
    admin = adminClient();
  } catch (e) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-12">
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
          GitHub / Gitea integrations
        </h1>
        <div className="mt-6 rounded-md border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          <p className="font-medium">Server configuration required</p>
          <p className="mt-1 leading-relaxed">
            {(e as Error).message}
          </p>
        </div>
      </main>
    );
  }
  const [{ data: repoRows, error: repoError }, { data: deliveryRows, error: deliveryError }] =
    await Promise.all([
      admin
        .from('external_repos')
        .select(
          'id, provider, repo_full_name, project_root, created_by, created_at, updated_at, profiles:created_by(handle, display_name)',
        )
        .order('provider')
        .order('repo_full_name'),
      admin
        .from('external_webhook_deliveries')
        .select('provider, delivery_id, event_type, received_at, event_id, ignored_reason, raw_body')
        .order('received_at', { ascending: false })
        .limit(20),
    ]);

  if (repoError) {
    throw new Error(`failed to load external repos: ${repoError.message}`);
  }
  if (deliveryError) {
    throw new Error(`failed to load webhook deliveries: ${deliveryError.message}`);
  }

  const repos = ((repoRows ?? []) as ExternalRepoRow[])
    .map(toPublicExternalRepo)
    .filter((repo): repo is PublicExternalRepo => repo != null);
  const deliveries = ((deliveryRows ?? []) as ExternalDeliveryRow[])
    .map(toPublicExternalDelivery)
    .filter((delivery): delivery is PublicExternalDelivery => delivery != null);

  const providers = EXTERNAL_PROVIDERS.map((provider) => {
    const providerDeliveries = deliveries.filter((d) => d.provider === provider);
    return {
      provider,
      webhookUrl: buildWebhookUrl(botBaseUrl, provider),
      installUrl: provider === 'github' ? githubAppInstallUrl : null,
      repoCount: repos.filter((repo) => repo.provider === provider).length,
      latestDeliveryAt: providerDeliveries[0]?.receivedAt ?? null,
    };
  });

  return (
    <main className="mx-auto max-w-5xl px-4 py-8 sm:py-12">
      <IntegrationsPanel
        providers={providers}
        repos={repos}
        deliveries={deliveries}
        signedIn={Boolean(user)}
        canManage={canManage}
        loginHref={`/login?next=${encodeURIComponent('/integrations')}`}
        botBaseConfigured={Boolean(botBaseUrl)}
      />
    </main>
  );
}
