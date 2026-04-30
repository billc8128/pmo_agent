'use client';

// CopyCommand renders a code line with a Copy button. Click → copies
// to clipboard, briefly flashes "Copied!" feedback.

import { useState } from 'react';

export function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // Older browsers / non-secure contexts: fail silently.
    }
  }

  return (
    <div className="flex items-stretch overflow-hidden rounded-md border border-zinc-200 bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-950">
      <pre className="flex-1 overflow-x-auto px-3 py-2 font-mono text-[13px] leading-relaxed text-zinc-800 dark:text-zinc-200">
        {command}
      </pre>
      <button
        onClick={copy}
        className="flex shrink-0 items-center border-l border-zinc-200 bg-white px-3 text-[11px] font-medium text-zinc-600 transition hover:text-zinc-900 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
        aria-label="Copy"
      >
        {copied ? 'Copied!' : 'Copy'}
      </button>
    </div>
  );
}
