export type IntegrationViewer = {
  id: string;
  handle: string | null;
};

export function canManageIntegrations(viewer: IntegrationViewer | null): boolean {
  if (!viewer) return false;
  const allowedIds = splitEnvList(process.env.PMO_INTEGRATION_ADMIN_USER_IDS);
  const allowedHandles = splitEnvList(process.env.PMO_INTEGRATION_ADMIN_HANDLES)
    .map((h) => h.replace(/^@/, '').toLowerCase());

  // Internal default for 2.0a: any signed-in PMO user can maintain repo
  // mappings. Deployments that need stricter control can set either env var.
  if (allowedIds.length === 0 && allowedHandles.length === 0) return true;
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
