import { useMemo, useState } from "react";
import { api, type SearchHit } from "../lib/api";
import { useNavigate } from "react-router-dom";

export default function SearchPage() {
  const nav = useNavigate();

  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(30);
  const [scopeMode, setScopeMode] = useState<"global" | "doc">("global");
  const [docId, setDocId] = useState("");

  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [meta, setMeta] = useState<{ total?: number; took_ms?: number }>({});

  const canSearch = useMemo(() => {
    if (!query.trim()) return false;
    if (scopeMode === "doc" && !docId.trim()) return false;
    return true;
  }, [query, scopeMode, docId]);

  async function onSearch() {
    setErr(null);
    setLoading(true);
    try {
      const res = await api.search({
        q: query.trim(),
        top_k: topK,
        doc_id: scopeMode === "doc" ? docId.trim() : undefined,
      });
      
      setHits(res.results || []);
      setMeta({ total: res.total_hits, took_ms: res.took_ms });
    } catch (e: any) {
      setErr(e?.message || "Search failed");
    } finally {
      setLoading(false);
    }
  }

  function askAboutHit(hit: SearchHit) {
    const question = `What does this say about: "${query.trim()}"?`;
    // pass context + doc_id to QA page
    nav("/qa", {
      state: {
        presetQuestion: question,
        presetDocId: hit.doc_id || (scopeMode === "doc" ? docId.trim() : ""),
        presetContext: hit.snippet || "",
      },
    });
  }

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>Search</h3>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 140px", gap: 12 }}>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder='Search: "deductible", "insured name", "policy effective date"...'
          onKeyDown={(e) => e.key === "Enter" && canSearch && onSearch()}
        />
        <button disabled={!canSearch || loading} onClick={onSearch}>
          {loading ? "Searching..." : "Search"}
        </button>
      </div>

      <div style={{ display: "flex", gap: 12, marginTop: 12, alignItems: "center", flexWrap: "wrap" }}>
        <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
          Scope:
          <select value={scopeMode} onChange={(e) => setScopeMode(e.target.value as any)}>
            <option value="global">Global (all docs)</option>
            <option value="doc">Single document</option>
          </select>
        </label>

        {scopeMode === "doc" && (
          <input
            value={docId}
            onChange={(e) => setDocId(e.target.value)}
            placeholder="doc_id (e.g., s3 key or your internal id)"
            style={{ minWidth: 320 }}
          />
        )}

        <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
          top_k:
          <input
            type="number"
            value={topK}
            min={1}
            max={25}
            onChange={(e) => setTopK(Number(e.target.value))}
            style={{ width: 72 }}
          />
        </label>

        {meta.took_ms != null && (
          <span style={{ opacity: 0.8 }}>
            took {meta.took_ms} ms{meta.total != null ? ` · total ${meta.total}` : ""}
          </span>
        )}
      </div>

      {err && (
        <div style={{ marginTop: 12, padding: 12, border: "1px solid #f99" }}>
          <b>Error:</b> {err}
        </div>
      )}

      <div style={{ marginTop: 16, display: "grid", gap: 12 }}>
        {hits.map((h, idx) => (
          <div key={idx} style={{ border: "1px solid #ddd", borderRadius: 8, padding: 12 }}>            
            
            
            <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
              <div style={{ opacity: 0.85 }}>
                Search content found: 
                <div><b>Doc type: </b>{h.doc_type} · <b>Confidence: </b>{h.confidence}<br /></div>
                {/*<b>doc</b>: {h.doc_id ?? "—"} */} · <b>page</b>: {h.page_num ?? "—"} · <b>score</b>:{" "}
                {h.score?.toFixed?.(3) ?? "—"}
              </div>              
            </div>

            {/*<pre
              style={{
                whiteSpace: "pre-wrap",
                marginTop: 8,
                marginBottom: 0,
                fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                fontSize: 13,
              }}
            >
              {h.snippet || JSON.stringify(h, null, 2)}
            </pre>*/}

            <div>
              <button onClick={() => askAboutHit(h)}>Query this document</button>
            </div>
            
            
            

          </div>
        ))}

        {!loading && hits.length === 0 && (
          <div style={{ opacity: 0.7, marginTop: 8 }}>
            No results yet. Try a keyword like “premium”, “deductible”, “effective date”, “insured”, etc.
          </div>
        )}
      </div>
    </div>
  );
}
