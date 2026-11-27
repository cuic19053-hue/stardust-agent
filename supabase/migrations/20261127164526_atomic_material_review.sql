-- Version compare and publish the student's text and RAG index in one transaction.
alter table public.documents add column if not exists review_revision bigint not null default 0;

create function public.advance_document_review_revision()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
  new.review_revision := old.review_revision + 1;
  return new;
end
$$;

revoke all on function public.advance_document_review_revision() from public, anon;
create trigger document_review_revision before update on public.documents
for each row execute function public.advance_document_review_revision();

create function public.rag_apply_document_review(
  p_document_id text, p_expected_revision bigint, p_text text, p_status text, p_confirmed boolean, p_chunks jsonb
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  doc public.documents%rowtype;
begin
  select d.* into doc
  from public.documents d
  join public.projects p on p.id = d.project_id
  where d.id = p_document_id and p.owner_id = (select auth.uid()) and d.kind <> 'template'
  for update of d;
  if not found or doc.review_revision is distinct from p_expected_revision then
    raise exception 'document revised or unavailable' using errcode = 'PT409';
  end if;
  if p_text is null or length(p_text) > 200000 or p_confirmed is null
     or p_status is null or (p_status <> 'ready' and p_status <> doc.extraction_status)
     or (p_confirmed and (p_status <> 'ready' or btrim(p_text) = ''))
     or (not p_confirmed and p_chunks is distinct from '[]'::jsonb) then
    raise exception 'invalid document review' using errcode = '22023';
  end if;
  if not p_confirmed then
    delete from public.chunks where document_id = doc.id;
  end if;
  update public.documents
  set extracted_text = p_text, extraction_status = p_status, confirmed = p_confirmed,
      confirmation_origin = case when p_confirmed then 'user' else '' end
  where id = doc.id
  returning * into doc;
  if p_confirmed then
    perform public.rag_replace_project_chunks(
      doc.id, encode(pg_catalog.sha256(pg_catalog.convert_to(p_text, 'UTF8')), 'hex'), p_chunks
    );
  end if;
  return to_jsonb(doc);
end
$$;

revoke all on function public.rag_apply_document_review(text, bigint, text, text, boolean, jsonb) from public, anon;
grant execute on function public.rag_apply_document_review(text, bigint, text, text, boolean, jsonb) to authenticated;
