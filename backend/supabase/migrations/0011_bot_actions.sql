-- Idempotency + audit + lock log for bot write tools.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE bot_actions (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id         text NOT NULL,
    chat_id            text NOT NULL,
    sender_open_id     text NOT NULL,
    logical_key        text NOT NULL,
    attempt_count      int NOT NULL DEFAULT 1,
    action_type        text NOT NULL,
    status             text NOT NULL CHECK (
                           status IN (
                             'pending',
                             'success',
                             'failed',
                             'undone',
                             'reconciled_unknown'
                           )
                         ),
    logical_key_locked boolean NOT NULL DEFAULT true,
    args               jsonb NOT NULL,
    target_id          text,
    target_kind        text,
    result             jsonb,
    error              text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT bot_actions_message_action_uniq
      UNIQUE (message_id, action_type)
);

CREATE INDEX bot_actions_target_idx ON bot_actions (target_kind, target_id);

CREATE INDEX bot_actions_pending_idx ON bot_actions (status, updated_at)
  WHERE status = 'pending';

CREATE INDEX bot_actions_chat_sender_recent_idx
  ON bot_actions (chat_id, sender_open_id, created_at DESC);

CREATE UNIQUE INDEX bot_actions_logical_locked_uniq
  ON bot_actions (logical_key)
  WHERE logical_key_locked = true
    AND status IN ('pending', 'success', 'reconciled_unknown');

ALTER TABLE bot_actions ENABLE ROW LEVEL SECURITY;
