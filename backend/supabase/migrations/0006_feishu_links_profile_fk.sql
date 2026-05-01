-- feishu_links.user_id originally referenced auth.users(id), which works
-- for cascade-on-delete but doesn't expose a relationship PostgREST can
-- traverse for embedded selects. Add a parallel FK to profiles(id) so
-- the bot can do `select feishu_links(...).profiles(handle, display_name)`
-- in one round trip.
--
-- profiles.id itself references auth.users(id) so we don't need to keep
-- both FKs — drop the auth one to avoid having two cascade paths on the
-- same column (Postgres complains about that on multi-cascade).

alter table public.feishu_links
    drop constraint feishu_links_user_id_fkey;

alter table public.feishu_links
    add constraint feishu_links_user_id_fkey
        foreign key (user_id) references public.profiles(id) on delete cascade;
