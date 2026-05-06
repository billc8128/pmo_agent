'use client';

import { useRef, useState, useTransition } from 'react';
import type { RefObject } from 'react';
import { CopyCommand } from '@/app/_components/copy-command';
import type {
  ExternalProvider,
  PublicExternalDelivery,
  PublicExternalRepo,
} from '@/lib/integrations';
import {
  createExternalRepoMapping,
  deleteExternalRepoMapping,
} from './actions';

type ProviderSummary = {
  provider: ExternalProvider;
  webhookUrl: string;
  repoCount: number;
  latestDeliveryAt: string | null;
};

type Props = {
  providers: ProviderSummary[];
  repos: PublicExternalRepo[];
  deliveries: PublicExternalDelivery[];
  signedIn: boolean;
  canManage: boolean;
  loginHref: string;
  botBaseConfigured: boolean;
};

export function IntegrationsPanel({
  providers,
  repos,
  deliveries,
  signedIn,
  canManage,
  loginHref,
  botBaseConfigured,
}: Props) {
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const formRef = useRef<HTMLFormElement>(null);

  function run(action: () => Promise<void>, after?: () => void) {
    setError(null);
    startTransition(async () => {
      try {
        await action();
        after?.();
      } catch (e) {
        setError((e as Error).message);
      }
    });
  }

  return (
    <div className="space-y-9">
      <header className="border-b border-zinc-200 pb-7 dark:border-zinc-800">
        <p className="text-xs font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-500">
          System sources
        </p>
        <div className="mt-2 grid gap-4 md:grid-cols-[minmax(0,1fr)_18rem]">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
              GitHub / Gitea integrations
            </h1>
            <p className="mt-2 max-w-2xl text-sm leading-relaxed text-zinc-600 dark:text-zinc-400">
              Connect code hosting events to the PMO agent. Repositories mapped
              here become shared project sources; notification rules can then
              subscribe to PRs, merges, releases, and comments from those repos.
            </p>
          </div>
          <div className="rounded-md border border-zinc-200 bg-zinc-50 p-3 text-xs text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400">
            <p className="font-medium text-zinc-800 dark:text-zinc-200">
              Access model
            </p>
            <p className="mt-1 leading-relaxed">
              Everyone can see connected repos. Signed-in maintainers can add or
              remove mappings; provider secrets stay in the bot deployment.
            </p>
          </div>
        </div>
      </header>

      {error && (
        <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
          {error}
        </p>
      )}

      {!botBaseConfigured && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          Set <code className="font-mono text-xs">BOT_WEBHOOK_BASE_URL</code> on
          the web deployment to show absolute webhook URLs.
        </div>
      )}

      <section>
        <div className="mb-3 flex items-end justify-between gap-4">
          <div>
            <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
              Providers
            </h2>
            <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-500">
              Add these webhook URLs in GitHub or Gitea repository settings.
            </p>
          </div>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          {providers.map((provider) => (
            <ProviderBlock key={provider.provider} provider={provider} />
          ))}
        </div>
      </section>

      <section className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div>
          <div className="mb-3 flex items-end justify-between gap-4">
            <div>
              <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
                Connected repositories
              </h2>
              <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-500">
                Registry of repositories connected to the PMO agent.
              </p>
            </div>
            <span className="text-xs text-zinc-400 dark:text-zinc-500">
              {repos.length} mapped
            </span>
          </div>

          {repos.length === 0 ? (
            <div className="rounded-md border border-dashed border-zinc-300 px-4 py-8 text-center dark:border-zinc-700">
              <p className="text-sm text-zinc-500 dark:text-zinc-400">
                No repositories mapped yet.
              </p>
            </div>
          ) : (
            <ul className="divide-y divide-zinc-200 rounded-md border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
              {repos.map((repo) => (
                <li key={repo.id} className="p-3">
                  <RepoRow
                    repo={repo}
                    canManage={canManage}
                    pending={pending}
                    onDelete={() => {
                      if (confirm(`Remove mapping for ${repo.repoFullName}?`)) {
                        run(() => deleteExternalRepoMapping(repo.id));
                      }
                    }}
                  />
                </li>
              ))}
            </ul>
          )}
        </div>

        <AddRepoPanel
          signedIn={signedIn}
          canManage={canManage}
          pending={pending}
          loginHref={loginHref}
          formRef={formRef}
          onSubmit={(formData) =>
            run(() => createExternalRepoMapping(formData), () => {
              formRef.current?.reset();
            })
          }
        />
      </section>

      <section>
        <div className="mb-3 flex items-end justify-between gap-4">
          <div>
            <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
              Recent accepted deliveries
            </h2>
            <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-500">
              Shows webhooks that reached the bot. Raw payloads are never
              exposed in the browser.
            </p>
          </div>
        </div>

        {deliveries.length === 0 ? (
          <div className="rounded-md border border-dashed border-zinc-300 px-4 py-8 text-center dark:border-zinc-700">
            <p className="text-sm text-zinc-500 dark:text-zinc-400">
              No accepted webhook deliveries yet.
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-zinc-200 rounded-md border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
            {deliveries.map((delivery) => (
              <li key={delivery.viewKey} className="grid gap-2 p-3 text-sm sm:grid-cols-[8rem_minmax(0,1fr)_9rem] sm:items-center">
                <div className="font-medium text-zinc-900 dark:text-zinc-100">
                  {providerLabel(delivery.provider)}
                </div>
                <div className="min-w-0">
                  <p className="truncate text-zinc-700 dark:text-zinc-300">
                    {delivery.eventType}
                    {delivery.repoFullName ? ` · ${delivery.repoFullName}` : ''}
                  </p>
                  <p className="mt-0.5 font-mono text-[11px] text-zinc-400 dark:text-zinc-500">
                    {delivery.deliveryIdShort}
                  </p>
                </div>
                <div className="text-left text-xs text-zinc-500 dark:text-zinc-500 sm:text-right">
                  <p>{formatDate(delivery.receivedAt)}</p>
                  <p className={delivery.linkedToEvent ? 'text-emerald-700 dark:text-emerald-300' : 'text-zinc-400 dark:text-zinc-500'}>
                    {delivery.linkedToEvent ? 'event created' : 'archived only'}
                  </p>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="rounded-md border border-zinc-200 bg-zinc-50 p-4 dark:border-zinc-800 dark:bg-zinc-900">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
          Actor attribution
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-zinc-600 dark:text-zinc-400">
          GitHub/Gitea usernames can still be mapped to PMO users later, but
          that mapping only improves phrases like “my PRs” or “Albert merged”.
          It is not how the PMO agent gets repository access.
        </p>
      </section>
    </div>
  );
}

function ProviderBlock({ provider }: { provider: ProviderSummary }) {
  return (
    <div className="rounded-md border border-zinc-200 p-3 dark:border-zinc-800">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            {providerLabel(provider.provider)}
          </h3>
          <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-500">
            {provider.repoCount} repo{provider.repoCount === 1 ? '' : 's'} mapped
          </p>
        </div>
        <span className={provider.latestDeliveryAt ? statusClass('ok') : statusClass('idle')}>
          {provider.latestDeliveryAt ? 'receiving' : 'waiting'}
        </span>
      </div>
      <div className="mt-3">
        <CopyCommand command={provider.webhookUrl} />
      </div>
      <p className="mt-2 text-xs text-zinc-500 dark:text-zinc-500">
        {provider.latestDeliveryAt
          ? `Last delivery ${formatDate(provider.latestDeliveryAt)}`
          : 'No accepted delivery yet'}
      </p>
    </div>
  );
}

function RepoRow({
  repo,
  canManage,
  pending,
  onDelete,
}: {
  repo: PublicExternalRepo;
  canManage: boolean;
  pending: boolean;
  onDelete: () => void;
}) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded border border-zinc-200 px-1.5 py-0.5 text-[11px] font-medium uppercase text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
            {repo.provider}
          </span>
          <p className="break-words text-sm font-medium text-zinc-900 dark:text-zinc-100">
            {repo.repoFullName}
          </p>
        </div>
        <p className="mt-1 break-all font-mono text-xs text-zinc-500 dark:text-zinc-500">
          {repo.projectRoot}
        </p>
        <p className="mt-1 text-[11px] text-zinc-400 dark:text-zinc-500">
          Added {formatDate(repo.createdAt)}
          {creatorLabel(repo) ? ` by ${creatorLabel(repo)}` : ''}
        </p>
      </div>
      {canManage && (
        <button
          type="button"
          onClick={onDelete}
          disabled={pending}
          className="w-fit shrink-0 rounded border border-red-200 px-2 py-1 text-xs text-red-600 transition hover:border-red-300 hover:text-red-700 disabled:opacity-50 dark:border-red-950 dark:text-red-300 dark:hover:border-red-900"
        >
          Remove
        </button>
      )}
    </div>
  );
}

function AddRepoPanel({
  signedIn,
  canManage,
  pending,
  loginHref,
  formRef,
  onSubmit,
}: {
  signedIn: boolean;
  canManage: boolean;
  pending: boolean;
  loginHref: string;
  formRef: RefObject<HTMLFormElement | null>;
  onSubmit: (formData: FormData) => void;
}) {
  if (!signedIn) {
    return (
      <div className="rounded-md border border-zinc-200 bg-zinc-50 p-4 dark:border-zinc-800 dark:bg-zinc-900">
        <p className="text-sm text-zinc-600 dark:text-zinc-300">
          Sign in to manage repository mappings.
        </p>
        <a
          href={loginHref}
          className="mt-3 inline-flex rounded bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-zinc-800 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-200"
        >
          Sign in
        </a>
      </div>
    );
  }
  if (!canManage) {
    return (
      <div className="rounded-md border border-zinc-200 bg-zinc-50 p-4 text-sm text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400">
        You can view integrations, but this deployment limits changes to
        integration maintainers.
      </div>
    );
  }
  return (
    <form
      ref={formRef}
      action={onSubmit}
      className="rounded-md border border-zinc-200 bg-zinc-50 p-4 dark:border-zinc-800 dark:bg-zinc-900"
    >
      <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
        Add repository
      </h2>
      <label className="mt-3 block text-xs font-medium text-zinc-600 dark:text-zinc-400">
        Provider
        <select
          name="provider"
          className="mt-1 block w-full rounded border border-zinc-300 bg-white px-2 py-2 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
        >
          <option value="github">GitHub</option>
          <option value="gitea">Gitea</option>
        </select>
      </label>
      <label className="mt-3 block text-xs font-medium text-zinc-600 dark:text-zinc-400">
        Repository
        <input
          name="repoFullName"
          placeholder="owner/repo"
          className="mt-1 block w-full rounded border border-zinc-300 bg-white px-2 py-2 text-sm text-zinc-900 outline-none placeholder:text-zinc-400 focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
        />
      </label>
      <label className="mt-3 block text-xs font-medium text-zinc-600 dark:text-zinc-400">
        Project root
        <input
          name="projectRoot"
          placeholder="/Users/a/Desktop/vibelive"
          className="mt-1 block w-full rounded border border-zinc-300 bg-white px-2 py-2 font-mono text-sm text-zinc-900 outline-none placeholder:text-zinc-400 focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
        />
      </label>
      <button
        type="submit"
        disabled={pending}
        className="mt-4 rounded bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-zinc-800 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-200"
      >
        {pending ? 'Saving...' : 'Save mapping'}
      </button>
      <p className="mt-3 text-xs leading-relaxed text-zinc-500 dark:text-zinc-500">
        Saving the same provider/repo again updates its project root.
      </p>
    </form>
  );
}

function providerLabel(provider: ExternalProvider): string {
  return provider === 'github' ? 'GitHub' : 'Gitea';
}

function creatorLabel(repo: PublicExternalRepo): string {
  if (repo.creatorDisplayName && repo.creatorHandle) {
    return `${repo.creatorDisplayName} / @${repo.creatorHandle}`;
  }
  if (repo.creatorHandle) return `@${repo.creatorHandle}`;
  return repo.creatorDisplayName ?? '';
}

function formatDate(value: string): string {
  return new Date(value).toLocaleString();
}

function statusClass(kind: 'ok' | 'idle'): string {
  const base = 'rounded-full px-2 py-0.5 text-[11px] font-medium';
  if (kind === 'ok') {
    return `${base} bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300`;
  }
  return `${base} bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400`;
}
