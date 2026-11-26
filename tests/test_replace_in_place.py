"""Car 3: re-ingesting replaces each subsection in place. New rows are written
first; only then are that subsection's other rows removed; nothing else is
touched. An in-memory table stands in for Supabase (no network)."""
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ingestion"))


class _Q:
    """The PostgREST calls these scripts use, over a list of dict rows."""
    def __init__(self, db, table):
        self.db, self.t, self.op, self.payload, self.f = db, table, "select", None, []
        self.rng, self.count = None, None

    def select(self, *_a, count=None):
        self.op, self.count = "select", count
        return self

    def delete(self):
        self.op = "delete"
        return self

    def upsert(self, payload, on_conflict=""):
        self.op, self.payload, self.key = "upsert", payload, on_conflict.split(",")
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def eq(self, k, v): self.f.append(lambda r: r.get(k) == v); return self
    def neq(self, k, v): self.f.append(lambda r: r.get(k) != v); return self
    def gte(self, k, v): self.f.append(lambda r: (r.get(k) or 0) >= v); return self
    def in_(self, k, vs): self.f.append(lambda r: r.get(k) in vs); return self
    def order(self, *_a, **_k): return self
    def range(self, a, b): self.rng = (a, b); return self

    def execute(self):
        rows = self.db.setdefault(self.t, [])
        if self.t in self.db.get("_fail", ()) and self.op in ("upsert", "insert"):
            raise RuntimeError("write failed")
        if self.op in ("upsert", "insert"):
            for new in (self.payload if isinstance(self.payload, list) else [self.payload]):
                k = getattr(self, "key", None)
                old = next((r for r in rows if k and all(r.get(c) == new.get(c) for c in k)), None)
                if old:
                    old.update(new)
                else:
                    rows.append(dict(new, id=len(rows) + 1000))
            return SimpleNamespace(data=[], count=None)
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if self.op == "delete":
            self.db[self.t] = [r for r in rows if r not in hit]
            return SimpleNamespace(data=hit, count=None)
        if self.rng:
            hit = hit[self.rng[0]:self.rng[1] + 1]
        return SimpleNamespace(data=[dict(r) for r in hit], count=len(hit))


class FakeSupabase:
    def __init__(self):
        self.db = {}

    def table(self, name):
        return _Q(self.db, name)


def _v3(monkeypatch):
    import vectorise_aip_v3 as v3
    fake = FakeSupabase()
    monkeypatch.setattr(v3, "supabase", fake)
    monkeypatch.setattr(v3, "get_embedding", lambda *_a, **_k: [0.0])
    monkeypatch.setattr(v3, "RATE_PAUSE", 0)
    return v3, fake


def _kb(icao, sec, page, chunk, content="old"):
    return {"aip_part": "AD", "reference_tag": icao, "aip_section": f"AD {sec}",
            "source_page": page, "source_chunk": chunk, "content": content}


def test_a_moved_subsection_replaces_its_old_chunks_and_nothing_else(monkeypatch):
    v3, fake = _v3(monkeypatch)
    fake.db["aip_knowledge_base"] = [
        _kb("DNAA", "2.12", 337, 0),                       # last cycle, page 337
        _kb("DNAA", "2.13", 337, 0, "keep: other subsection"),
        _kb("DNKN", "2.12", 741, 0, "keep: other aerodrome"),
        {**_kb("DNAA", "1.1", 20, 0, "keep: other part"), "aip_part": "AD"},
    ]
    res = SimpleNamespace(text="new 2.12", embed_text="DNAA runway", records=[{"a": 1}])
    assert v3.ingest_result("DNAA", "2.12", res, 338, {}) == (1, 1)   # now on page 338
    kb = fake.db["aip_knowledge_base"]
    dnaa_212 = [r for r in kb if r["reference_tag"] == "DNAA" and r["aip_section"] == "AD 2.12"]
    assert [(r["source_page"], r["content"]) for r in dnaa_212] == [(338, "new 2.12")]
    assert sum(r["content"].startswith("keep") for r in kb) == 3


def test_a_shorter_ad222_drops_its_old_tail(monkeypatch):
    v3, fake = _v3(monkeypatch)
    fake.db["aip_knowledge_base"] = [_kb("DNAA", "2.22", 340, i) for i in range(5)]
    text = "2.22.1 General\nA\n2.22.2 Arrivals\nB\n2.22.3 Departures\nC\n"
    monkeypatch.setattr(v3, "EMBED_CHAR_LIMIT", 10)                    # one chunk each
    res = SimpleNamespace(text=text, embed_text="DNAA procedures", records=[])
    kb, _ = v3.ingest_result("DNAA", "2.22", res, 340, {})
    rows = sorted(r["source_chunk"] for r in fake.db["aip_knowledge_base"])
    assert kb == 3 and rows == [0, 1, 2]


def test_nothing_is_removed_when_a_new_chunk_was_not_written(monkeypatch):
    v3, fake = _v3(monkeypatch)
    fake.db["aip_knowledge_base"] = [_kb("DNAA", "2.12", 337, 0)]
    fake.db["_fail"] = {"aip_knowledge_base"}
    res = SimpleNamespace(text="new", embed_text="x", records=[])
    assert v3.ingest_result("DNAA", "2.12", res, 338, {}) == (0, 0)
    assert fake.db["aip_knowledge_base"] == [_kb("DNAA", "2.12", 337, 0)]


def test_structured_records_lose_their_old_tail(monkeypatch):
    v3, fake = _v3(monkeypatch)
    fake.db["aip_structured"] = [
        {"icao_code": "DNAA", "subsection": "2.18", "record_index": i, "record": {"old": i}}
        for i in range(4)] + [
        {"icao_code": "DNAA", "subsection": "2.19", "record_index": 3, "record": {}}]
    assert v3.struct_save("DNAA", "2.18", [{"svc": "TWR"}, {"svc": "APP"}]) == 2
    rows = fake.db["aip_structured"]
    assert sorted(r["record_index"] for r in rows if r["subsection"] == "2.18") == [0, 1]
    assert any(r["subsection"] == "2.19" for r in rows)


def test_a_skipped_subsection_with_old_rows_is_named_stale(monkeypatch):
    v3, fake = _v3(monkeypatch)
    fake.db["aip_knowledge_base"] = [_kb("DNAK", "2.19", 390, 0)]
    assert v3.STALE_MARK in v3._stale_note("DNAK", "2.19")
    assert v3._stale_note("DNAK", "2.20") == ""


def test_facts_no_longer_written_are_pruned_within_their_scope_only(monkeypatch):
    import build_fact_index as bfi
    fake = FakeSupabase()
    monkeypatch.setattr(bfi, "_client_ref", lambda: fake)
    row = lambda kind, sid, sub, ent, lab: {"scope_kind": kind, "scope_id": sid,  # noqa: E731
                                            "subsection": sub, "entity": ent, "label": lab}
    fake.db["aip_facts"] = [dict(r, id=i) for i, r in enumerate([
        row("AD", "DNAA", "2.18", "TWR", "Frequency"),
        row("AD", "DNAA", "2.18", "FIS", "Frequency"),      # withdrawn this cycle
        row("AD", "DNKN", "2.18", "FIS", "Frequency"),      # another aerodrome
        row("ENR_AIRSPACE", "KANO", "2.1", "", "Class"),    # ENR 2.1, same number as AD 2.1
    ])]
    written = [row("AD", "DNAA", "2.18", "TWR", "Frequency")]
    assert bfi._prune_stale(written, lambda q: q.eq("scope_kind", "AD").eq("scope_id", "DNAA")) == 1
    left = {(r["scope_id"], r["entity"]) for r in fake.db["aip_facts"]}
    assert left == {("DNAA", "TWR"), ("DNKN", "FIS"), ("KANO", "")}
    assert bfi._prune_stale([], lambda q: q.eq("subsection", "2.1").neq("scope_kind", "AD")) == 1
    assert {r["scope_id"] for r in fake.db["aip_facts"]} == {"DNAA", "DNKN"}
