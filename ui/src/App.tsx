import { NavLink, Route, Routes } from "react-router-dom";
import SearchPage from "./pages/SearchPage";
import QaPage from "./pages/QaPage";

export default function App() {
  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: 16 }}>
      <header style={{ display: "flex", alignItems: "baseline", gap: 16 }}>
        <h2 style={{ margin: 0 }}>AWS IDP Demo</h2>
        <nav style={{ display: "flex", gap: 12 }}>
          <NavLink to="/search">Search</NavLink>
          <NavLink to="/qa">Q&A</NavLink>
        </nav>
      </header>

      <hr />

      <Routes>
        <Route path="/" element={<SearchPage />} />
        <Route path="/search" element={<SearchPage />} />
        <Route path="/qa" element={<QaPage />} />
      </Routes>
    </div>
  );
}
