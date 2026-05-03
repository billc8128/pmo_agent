alter table public.feishu_links
    add column if not exists feishu_mobile text;

create index if not exists feishu_links_mobile_idx
    on public.feishu_links (feishu_mobile)
    where feishu_mobile is not null;
