export type IntegrationViewer = {
  id: string;
  handle: string | null;
};

export function canManageIntegrations(viewer: IntegrationViewer | null): boolean {
  if (!viewer) return false;
  const allowedIds = splitEnvList(process.env.PMO_INTEGRATION_ADMIN_USER_IDS);
  const allowedHandles = splitEnvList(process.env.PMO_INTEGRATION_ADMIN_HANDLES)
    .map((h) => h.replace(/^@/, '').toLowerCase());

  if (allowedIds.length === 0 && allowedHandles.length === 0) return false;
  return (
    allowedIds.includes(viewer.id) ||
    Boolean(viewer.handle && allowedHandles.includes(viewer.handle.toLowerCase()))
  );
}

function splitEnvList(value: string | undefined): string[] {
  return (value ?? '')
    .split(',')
    .map((v) => v.trim())
    .filter(Boolean);
}
