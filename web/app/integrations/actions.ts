'use server';

import { revalidatePath } from 'next/cache';
import { canManageIntegrations } from '@/lib/integration-permissions';
import { validateExternalRepoInput } from '@/lib/integrations';
import { adminClient } from '@/lib/supabase-admin';
import { serverActionClient } from '@/lib/supabase-server';

const INTEGRATIONS_PATH = '/integrations';

export async function createExternalRepoMapping(formData: FormData) {
  const user = await requireIntegrationManager();
  const { provider, repoFullName, projectRoot } = validateExternalRepoInput(
    String(formData.get('provider') ?? ''),
    String(formData.get('repoFullName') ?? ''),
    String(formData.get('projectRoot') ?? ''),
  );

  const { error } = await adminClient()
    .from('external_repos')
    .upsert(
      {
        provider,
        repo_full_name: repoFullName,
        project_root: projectRoot,
        created_by: user.id,
        updated_at: new Date().toISOString(),
      },
      { onConflict: 'provider,repo_full_name' },
    );
  if (error) {
    throw new Error(`failed to save repo mapping: ${error.message}`);
  }
  revalidatePath(INTEGRATIONS_PATH);
}

export async function deleteExternalRepoMapping(id: string) {
  await requireIntegrationManager();
  const repoId = id.trim();
  if (!repoId) throw new Error('repo mapping id is required');

  const { error } = await adminClient()
    .from('external_repos')
    .delete()
    .eq('id', repoId);
  if (error) {
    throw new Error(`failed to delete repo mapping: ${error.message}`);
  }
  revalidatePath(INTEGRATIONS_PATH);
}

export async function refreshIntegrationStatus() {
  await requireSignedInViewer();
  revalidatePath(INTEGRATIONS_PATH);
}

async function requireSignedInViewer() {
  const sb = await serverActionClient();
  const {
    data: { user },
  } = await sb.auth.getUser();
  if (!user) {
    throw new Error('not signed in');
  }
  return user;
}

async function requireIntegrationManager() {
  const user = await requireSignedInViewer();

  const sb = await serverActionClient();
  const { data: profile } = await sb
    .from('profiles')
    .select('id, handle')
    .eq('id', user.id)
    .maybeSingle();
  const viewer = { id: user.id, handle: profile?.handle ?? null };
  if (!canManageIntegrations(viewer)) {
    throw new Error('you do not have permission to manage integrations');
  }
  return viewer;
}
