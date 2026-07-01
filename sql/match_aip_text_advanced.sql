DROP FUNCTION IF EXISTS match_aip_text_advanced(vector, text, text, text, text, int);

CREATE FUNCTION match_aip_text_advanced (
  query_embedding vector(1536),
  match_filter_part text,
  match_filter_reference text,
  match_procedure_type text DEFAULT '',
  match_runway text DEFAULT '',
  match_limit int DEFAULT 8
)
RETURNS TABLE (
  content text,
  aip_section text,
  reference_tag text,
  chart_url text,
  similarity float
)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
BEGIN
  RETURN QUERY
  SELECT
    aip.content::text,
    aip.aip_section::text,
    aip.reference_tag::text,
    aip.chart_url::text,
    1 - (aip.embedding <=> query_embedding) AS similarity
  FROM aip_knowledge_base aip
  WHERE aip.aip_part = match_filter_part
    AND aip.reference_tag = match_filter_reference
  ORDER BY
    (CASE
      WHEN match_procedure_type = '' OR match_procedure_type IS NULL THEN 0
      WHEN aip.procedure_type ILIKE '%' || match_procedure_type || '%' THEN 0
      ELSE 1
    END),
    (CASE
      WHEN match_runway = '' OR match_runway IS NULL THEN 0
      WHEN aip.runway = match_runway THEN 0
      ELSE 1
    END),
    aip.embedding <=> query_embedding
  LIMIT match_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION match_aip_text_advanced(vector, text, text, text, text, int)
  TO service_role, anon, authenticated;
