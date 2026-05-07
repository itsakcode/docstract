export type Json = Record<string, any>;

async function postJson<T>(url: string, body: Json): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const text = await res.text();
  let data: any;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }

  if (!res.ok) {
    throw new Error(data?.message || `HTTP ${res.status}`);
  }
  return data as T;
}

// --------------- Search types

export type SearchRequest = {
  q: string;
  top_k?: number;
  doc_id?: string;
  filters?: Json;
};

export type SearchHit = {
  score?: number;
  doc_id?: string;
  chunk_id?: string;
  doc_type?: string;
  confidence?: number;
  pdf_s3_uri?: string;
  page_num?: number;           // renamed from page
  snippet?: string;            // this holds the text snippet
  extracted_fields?: Json;     // optional structured fields
};

export type SearchResponse = {
  results: SearchHit[];
  total_hits?: number;
  took_ms?: number;
};

// --------------- QA types

export type QaRequest = {
  question: string;
  doc_id?: string;
  context?: string;
  top_k?: number;
};

export type QaResponse = {
  answer: string;
  citations?: Array<{
    doc_id?: string;
    page?: number;
    chunk_id?: string;
    text?: string;
    score?: number;
  }>;
  took_ms?: number;
};

// --------------- API calls

export const api = {
  search: (req: SearchRequest) =>
    postJson<SearchResponse>(import.meta.env.VITE_SEARCH_URL, req),
  qa: (req: QaRequest) =>
    postJson<QaResponse>(import.meta.env.VITE_QA_URL, req),
};
