// DateNav — sticky sidebar with anchor links to each date in the
// current timeline. Server Component; pure HTML output.
//
// Visible on md+ screens as a left-floating column. Hidden on
// narrower screens; we include a top-aligned overflow chip strip as
// a fallback (in TimelineLayout). Anchors target #d-YYYY-MM-DD ids
// emitted by DateGroupedTimeline below.

import type { DayGroup } from '@/lib/grouping';

export function DateNav({ days }: { days: DayGroup[] }) {
  if (days.length === 0) return null;
  return (
    <nav
      aria-label="Jump to date"
      className="sticky top-4 hidden self-start text-xs md:block"
    >
      <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
        Jump to date
      </div>
      <ol className="flex flex-col gap-0.5">
        {days.map((d) => (
          <li key={d.dayKey}>
            <a
              href={`#d-${d.dayKey}`}
              className="flex items-baseline justify-between gap-3 rounded px-2 py-1 text-zinc-600 transition hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
            >
              <span className="truncate">{d.dayLabel}</span>
              <span className="text-[10px] text-zinc-400 dark:text-zinc-500">
                {d.turnCount}
              </span>
            </a>
          </li>
        ))}
      </ol>
    </nav>
  );
}

// MobileDateScroll — a horizontal chip strip that appears above the
// timeline on narrow screens. Same anchor mechanism.
export function MobileDateScroll({ days }: { days: DayGroup[] }) {
  if (days.length === 0) return null;
  return (
    <div className="mb-4 -mx-4 overflow-x-auto px-4 md:hidden">
      <div className="inline-flex gap-1">
        {days.map((d) => (
          <a
            key={d.dayKey}
            href={`#d-${d.dayKey}`}
            className="rounded-full bg-zinc-100 px-2.5 py-0.5 text-[11px] font-medium text-zinc-600 hover:bg-zinc-200 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-700 whitespace-nowrap"
          >
            {d.dayLabel}{' '}
            <span className="text-zinc-400 dark:text-zinc-500">{d.turnCount}</span>
          </a>
        ))}
      </div>
    </div>
  );
}
