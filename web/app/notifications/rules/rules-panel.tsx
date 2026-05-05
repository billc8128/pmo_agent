'use client';

import Link from 'next/link';
import { useRef, useState, useTransition } from 'react';
import type { PublicNotificationRule } from '@/lib/notification-rules';
import {
  archiveNotificationRule,
  createNotificationRule,
  setNotificationRuleEnabled,
  updateNotificationRule,
} from './actions';

type Props = {
  publicRules: PublicNotificationRule[];
  ownRules: PublicNotificationRule[];
  signedIn: boolean;
  loginHref: string;
};

export function RulesPanel({
  publicRules,
  ownRules,
  signedIn,
  loginHref,
}: Props) {
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
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

  function beginEdit(rule: PublicNotificationRule) {
    setEditingId(requireRuleId(rule));
    setDraft(rule.description);
    setError(null);
  }

  return (
    <div className="space-y-10">
      <section className="grid gap-5 border-b border-zinc-200 pb-8 dark:border-zinc-800 md:grid-cols-[minmax(0,1fr)_20rem]">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
            Notification rules
          </h1>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">
            Public watch rules people have asked the PMO bot to follow. Add a
            sentence in natural language; the bot uses it as the contract for
            proactive notifications.
          </p>
        </div>

        {signedIn ? (
          <form
            ref={formRef}
            action={(formData) =>
              run(() => createNotificationRule(formData), () => {
                formRef.current?.reset();
              })
            }
            className="rounded-md border border-zinc-200 bg-zinc-50 p-3 dark:border-zinc-800 dark:bg-zinc-900"
          >
            <label
              htmlFor="description"
              className="text-xs font-medium uppercase tracking-wider text-zinc-500 dark:text-zinc-400"
            >
              Add rule
            </label>
            <textarea
              id="description"
              name="description"
              rows={3}
              maxLength={240}
              placeholder="vibelive 播放器进展告诉我"
              className="mt-2 block w-full resize-none rounded border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 outline-none transition placeholder:text-zinc-400 focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100 dark:focus:border-zinc-500"
            />
            <button
              type="submit"
              disabled={pending}
              className="mt-3 rounded bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-zinc-800 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-200"
            >
              {pending ? 'Saving...' : 'Add rule'}
            </button>
          </form>
        ) : (
          <div className="rounded-md border border-zinc-200 bg-zinc-50 p-4 dark:border-zinc-800 dark:bg-zinc-900">
            <p className="text-sm text-zinc-600 dark:text-zinc-300">
              Sign in to add your own watch rule.
            </p>
            <Link
              href={loginHref}
              className="mt-3 inline-flex rounded bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-zinc-800 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-200"
            >
              Sign in
            </Link>
          </div>
        )}
      </section>

      {error && (
        <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
          {error}
        </p>
      )}

      {ownRules.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
            Your rules
          </h2>
          <ul className="mt-3 divide-y divide-zinc-200 rounded-md border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
            {ownRules.map((rule) => (
              <li key={rule.viewKey} className="p-3">
                {editingId === rule.subscriptionId ? (
                  <form
                    action={(formData) =>
                      run(() => updateNotificationRule(formData), () => {
                        setEditingId(null);
                        setDraft('');
                      })
                    }
                    className="space-y-2"
                  >
                    <input type="hidden" name="id" value={rule.subscriptionId ?? ''} />
                    <textarea
                      name="description"
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      rows={2}
                      maxLength={240}
                      className="block w-full resize-none rounded border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 outline-none transition focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
                    />
                    <div className="flex gap-2">
                      <button
                        type="submit"
                        disabled={pending}
                        className="rounded bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
                      >
                        Save
                      </button>
                      <button
                        type="button"
                        onClick={() => setEditingId(null)}
                        className="rounded border border-zinc-300 px-3 py-1.5 text-xs text-zinc-600 dark:border-zinc-700 dark:text-zinc-400"
                      >
                        Cancel
                      </button>
                    </div>
                  </form>
                ) : (
                  <RuleRow
                    rule={rule}
                    canManage
                    pending={pending}
                    onEdit={() => beginEdit(rule)}
                    onToggle={() =>
                      run(() =>
                        setNotificationRuleEnabled(requireRuleId(rule), !rule.enabled),
                      )
                    }
                    onArchive={() => {
                      if (confirm('Archive this rule? Notification history stays intact.')) {
                        run(() => archiveNotificationRule(requireRuleId(rule)));
                      }
                    }}
                  />
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <div className="flex items-end justify-between gap-4">
          <div>
            <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
              Active public rules
            </h2>
            <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-500">
              Paused or archived rules are not shown here.
            </p>
          </div>
          <span className="text-xs text-zinc-400 dark:text-zinc-500">
            {publicRules.length} active
          </span>
        </div>

        {publicRules.length === 0 ? (
          <div className="mt-4 rounded-md border border-dashed border-zinc-300 px-4 py-8 text-center dark:border-zinc-700">
            <p className="text-sm text-zinc-500 dark:text-zinc-400">
              No active notification rules yet.
            </p>
          </div>
        ) : (
          <ul className="mt-4 divide-y divide-zinc-200 rounded-md border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
            {publicRules.map((rule) => (
              <li key={rule.viewKey} className="p-3">
                <RuleRow
                  rule={rule}
                  canManage={false}
                  pending={pending}
                  onEdit={() => beginEdit(rule)}
                  onToggle={() =>
                    run(() =>
                      setNotificationRuleEnabled(requireRuleId(rule), !rule.enabled),
                    )
                  }
                  onArchive={() => {
                    if (confirm('Archive this rule? Notification history stays intact.')) {
                      run(() => archiveNotificationRule(requireRuleId(rule)));
                    }
                  }}
                />
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function RuleRow({
  rule,
  canManage,
  pending,
  onEdit,
  onToggle,
  onArchive,
}: {
  rule: PublicNotificationRule;
  canManage: boolean;
  pending: boolean;
  onEdit: () => void;
  onToggle: () => void;
  onArchive: () => void;
}) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <p className="break-words text-sm font-medium leading-relaxed text-zinc-900 dark:text-zinc-100">
          {rule.description}
        </p>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-zinc-500 dark:text-zinc-500">
          <span>{ownerLabel(rule)}</span>
          <span aria-hidden="true">·</span>
          <span>{new Date(rule.createdAt).toLocaleString()}</span>
          {rule.ownedByViewer && (
            <>
              <span aria-hidden="true">·</span>
              <span className="font-medium text-zinc-700 dark:text-zinc-300">
                yours
              </span>
            </>
          )}
          {!rule.enabled && (
            <>
              <span aria-hidden="true">·</span>
              <span className="font-medium text-amber-700 dark:text-amber-300">
                paused
              </span>
            </>
          )}
        </div>
      </div>
      {canManage && (
        <div className="flex shrink-0 flex-wrap gap-2">
          <button
            type="button"
            onClick={onEdit}
            disabled={pending}
            className="rounded border border-zinc-300 px-2 py-1 text-xs text-zinc-600 transition hover:border-zinc-400 hover:text-zinc-900 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400 dark:hover:border-zinc-500 dark:hover:text-zinc-100"
          >
            Edit
          </button>
          <button
            type="button"
            onClick={onToggle}
            disabled={pending}
            className="rounded border border-zinc-300 px-2 py-1 text-xs text-zinc-600 transition hover:border-zinc-400 hover:text-zinc-900 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400 dark:hover:border-zinc-500 dark:hover:text-zinc-100"
          >
            {rule.enabled ? 'Pause' : 'Resume'}
          </button>
          <button
            type="button"
            onClick={onArchive}
            disabled={pending}
            className="rounded border border-red-200 px-2 py-1 text-xs text-red-600 transition hover:border-red-300 hover:text-red-700 disabled:opacity-50 dark:border-red-950 dark:text-red-300 dark:hover:border-red-900"
          >
            Archive
          </button>
        </div>
      )}
    </div>
  );
}

function ownerLabel(rule: PublicNotificationRule): string {
  if (rule.ownerDisplayName && rule.ownerHandle) {
    return `${rule.ownerDisplayName} / @${rule.ownerHandle}`;
  }
  if (rule.ownerHandle) return `@${rule.ownerHandle}`;
  if (rule.ownerDisplayName) return rule.ownerDisplayName;
  return 'Unknown user';
}

function requireRuleId(rule: PublicNotificationRule): string {
  if (!rule.subscriptionId) {
    throw new Error('rule is not manageable by this viewer');
  }
  return rule.subscriptionId;
}
