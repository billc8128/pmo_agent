// Shape of public.profiles, mirroring backend/supabase/migrations/0001_initial.sql.
export type Profile = {
  id: string;            // uuid
  handle: string;
  display_name: string | null;
  created_at: string;    // ISO timestamp
};

// Shape of public.turns. Matches the backend schema verbatim so we
// can `from('turns').select('*')` without remapping.
export type Turn = {
  id: number;
  user_id: string;
  agent: 'claude_code' | 'codex' | string;  // string fallback for forward-compat
  agent_session_id: string;
  project_path: string | null;
  project_root: string | null;
  turn_index: number;
  user_message: string;
  agent_response_full: string | null;
  agent_summary: string | null;
  device_label: string | null;
  user_message_at: string;
  agent_response_at: string | null;
  created_at: string;
  updated_at: string;
};
