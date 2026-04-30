-- device_label: free-text label that identifies which machine
-- uploaded the turn. Sourced from the ingest function (it knows
-- which PAT was used, and tokens have a label).
--
-- Existing rows are left NULL. The web layer renders the chip only
-- when this column is set, so historical data isn't decorated with
-- guessed labels.

alter table public.turns
    add column if not exists device_label text;

comment on column public.turns.device_label is
    'Free-text identifier of the machine that uploaded the turn. '
    'Mirror of the tokens.label belonging to the PAT used at ingest time.';
