import { useLocation } from "react-router-dom";
import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";

type NavState = {
  presetQuestion?: string;
  presetDocId?: string;
  presetContext?: string;
};

export default function QaPage() {
  const loc = useLocation();
  const state = (loc.state || {}) as NavState;

  const [question, setQuestion] = useState(state.presetQuestion || "");
  const [scopeMode, setScopeMode] = useState<"global" | "doc">(state.presetDocId ? "doc" : "global");
  const [docId, setDocId] = useState(state.presetDocId || "");
  const [context, setContext] = useState(state.presetContext || "");
  const [topK, setTopK] = useState(6);

  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [answer, setAnswer] = useState<string>("");
  const [citations, setCitations] = useState<any[]>([]);
  const [tookMs, setTookMs] = useState<number | undefined>(undefined);

  useEffect(() => {
    // If navigated from search with context, pre-fill but don’t auto-run.
  }, []);

  const canAsk = useMemo(() => {
    if (!question.trim()) return false;
    if (scopeMode === "doc" && !docId.trim()) return false;
    return true;
  }, [question, scopeMode, docId]);

  async function onAsk() {
    setErr(null);
    setLoading(true);
    try {
      const res = await api.qa({
        question: question.trim(),
        doc_id: scopeMode === "doc" ? docId.trim() : undefined,
        context: context.trim() ? context : undefined,
        top_k: topK,
      });
      setAnswer(res.answer || "");
      setCitations(res.citations || []);
      setTookMs(res.took_ms);
    } catch (e: any) {
      setErr(e?.message || "QA failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>Q&A</h3>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 140px", gap: 12 }}>
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder='Ask: "What is the deductible?", "Who is the named insured?"'
          onKeyDown={(e) => e.key === "Enter" && canAsk && onAsk()}
        />
        <button disabled={!canAsk || loading} onClick={onAsk}>
          {loading ? "Asking..." : "Ask"}
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
            placeholder="doc_id"
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

        {tookMs != null && <span style={{ opacity: 0.8 }}>took {tookMs} ms</span>}
      </div>

      <div style={{ marginTop: 12 }}>
        <label style={{ display: "block", marginBottom: 6, opacity: 0.85 }}>
          Optional context (paste snippet / selected search result)
        </label>
        <textarea
          value={context}
          onChange={(e) => setContext(e.target.value)}
          rows={5}
          placeholder="If you paste a snippet here, QA can ground to it (optional)."
          style={{ width: "100%" }}
        />
      </div>

      {err && (
        <div style={{ marginTop: 12, padding: 12, border: "1px solid #f99" }}>
          <b>Error:</b> {err}
        </div>
      )}

      {answer && (
        <div style={{ marginTop: 16, border: "1px solid #ddd", borderRadius: 8, padding: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
            <b>Answer</b>
          </div>
          <div style={{ marginTop: 8, whiteSpace: "pre-wrap" }}>{answer}</div>

          {citations?.length > 0 && (
            <>
              <hr />
              <b>Citations</b>
              <div style={{ display: "grid", gap: 10, marginTop: 10 }}>
                {citations.map((c, i) => (
                  <div key={i} style={{ border: "1px solid #eee", borderRadius: 8, padding: 10 }}>
                    <div style={{ opacity: 0.85 }}>
                      <b>doc</b>: {c.doc_id ?? "—"} · <b>page</b>: {c.page ?? "—"} · <b>score</b>:{" "}
                      {c.score?.toFixed?.(3) ?? "—"}
                    </div>
                    {c.text && (
                      <pre style={{ whiteSpace: "pre-wrap", margin: "8px 0 0 0", fontSize: 13 }}>
                        {c.text}
                      </pre>
                    )}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
