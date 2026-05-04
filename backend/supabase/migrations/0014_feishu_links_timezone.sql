-- Store the Feishu user's timezone captured during OAuth binding.

alter table public.feishu_links
    add column if not exists timezone text not null default 'Asia/Shanghai';
