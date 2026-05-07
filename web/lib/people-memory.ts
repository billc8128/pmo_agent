export type PeopleMemoryRow = {
  person_key?: string | null;
  profile_id?: string | null;
  feishu_open_id?: string | null;
  display_name?: string | null;
  handle?: string | null;
  pmo_notes?: string | null;
  notes_updated_at?: string | null;
  last_observed_at?: string | null;
  metadata?: unknown;
  created_at?: string | null;
  updated_at?: string | null;
};

export type PublicPersonMemory = {
  viewKey: string;
  displayName: string;
  handle: string | null;
  note: string;
  notesUpdatedAt: string | null;
  lastObservedAt: string | null;
  updatedAt: string | null;
};

export function toPublicPersonMemory(row: PeopleMemoryRow): PublicPersonMemory | null {
  const note = (row.pmo_notes ?? '').trim();
  if (!note) return null;

  const handle = normalizeHandle(row.handle);
  const displayName = normalizeDisplayName(row.display_name, handle);
  const notesUpdatedAt = row.notes_updated_at ?? null;
  return {
    viewKey: `${handle ?? 'unknown'}:${notesUpdatedAt ?? row.updated_at ?? row.created_at ?? 'unknown'}`,
    displayName,
    handle,
    note,
    notesUpdatedAt,
    lastObservedAt: row.last_observed_at ?? null,
    updatedAt: row.updated_at ?? null,
  };
}

export function toPublicPeopleMemory(rows: PeopleMemoryRow[]): PublicPersonMemory[] {
  return rows
    .map((row) => toPublicPersonMemory(row))
    .filter((person): person is PublicPersonMemory => person != null)
    .sort(comparePublicPeople);
}

function comparePublicPeople(a: PublicPersonMemory, b: PublicPersonMemory): number {
  const observed = compareIsoDesc(a.lastObservedAt, b.lastObservedAt);
  if (observed !== 0) return observed;
  return compareIsoDesc(a.notesUpdatedAt ?? a.updatedAt, b.notesUpdatedAt ?? b.updatedAt);
}

function compareIsoDesc(a: string | null | undefined, b: string | null | undefined): number {
  const at = a ? Date.parse(a) : 0;
  const bt = b ? Date.parse(b) : 0;
  return bt - at;
}

function normalizeHandle(value: string | null | undefined): string | null {
  const handle = (value ?? '').trim().replace(/^@+/, '');
  return handle || null;
}

function normalizeDisplayName(
  value: string | null | undefined,
  handle: string | null,
): string {
  const displayName = (value ?? '').trim();
  if (displayName) return displayName;
  if (handle) return `@${handle}`;
  return 'Unknown member';
}
