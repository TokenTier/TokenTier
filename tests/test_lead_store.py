"""Store/API aggregation of lead_usage events (the main session's own token usage)."""
import datetime as dt
import json
import os
import unittest

from dash_helpers import PRICING, TZ, Clock, at, end, ev, start, toks, write
from test_dashboard_store import Base
from test_lead_hook import OPUS, SONNET, LeadBase, line
from tokentier_dash.pricing import Pricing
from tokentier_dash.store import Store

M = 1000000


def lead(day, hms, sid="s1", model=OPUS, tokens=None, turns=3, project="proj", **kw):
    t = tokens or toks(M, 100000, 0, 0)
    return ev("lead_usage", at(day, hms), session_id=sid, project=project, model=model, tier="x", tokens=t,
              tokens_total=sum(t.values()), turns=turns, from_ts=kw.get("from_ts"), to_ts=kw.get("to_ts"))


class LeadStoreTest(Base):
    def test_aggregates_and_cost(self):
        write(self.home, "2026-10-05", [
            start("a", at("2026-10-05", "09:00")), end("a", at("2026-10-05", "09:01")),
            lead("2026-10-05", "10:00"),                                          # opus: 4 + 2 = 6.00
            lead("2026-10-05", "11:00", tokens=toks(0, 0, 0, M), turns=2),        # opus cache read: 0.20
            lead("2026-10-05", "12:00", sid="s2", model=SONNET, tokens=toks(M, 0, 0, 0), turns=1)])  # sonnet: 2.00
        write(self.home, "2026-10-04", [lead("2026-10-04", "10:00", tokens=toks(0, M, 0, 0))])  # opus: 20.00
        s = self.store()
        o = s.overview("7d")
        ld = o["totals"]["lead"]
        self.assertEqual(ld["tokens"], {"input": 2 * M, "output": 100000 + M, "cache_creation": 0,
                                        "cache_read": M, "total": 4100000})
        self.assertAlmostEqual(ld["cost"], 6.0 + 0.2 + 2.0 + 20.0, places=6)
        self.assertEqual((ld["turns"], ld["sessions"], ld["unknown_cost"]), (9, 2, 0))
        self.assertAlmostEqual(ld["by_model"][OPUS]["cost"], 26.2, places=6)
        self.assertEqual(ld["by_model"][SONNET]["turns"], 1)
        self.assertAlmostEqual(o["totals"]["total_spend"], o["totals"]["cost"] + ld["cost"], places=6)
        d = {x["date"]: x for x in o["daily"]}
        self.assertAlmostEqual(d["2026-10-05"]["lead_cost"], 8.2, places=6)
        self.assertAlmostEqual(d["2026-10-04"]["lead_cost"], 20.0, places=6)
        self.assertEqual(d["2026-10-03"]["lead_cost"], 0.0)
        # range filter: today only
        t = s.overview("today")["totals"]["lead"]
        self.assertAlmostEqual(t["cost"], 8.2, places=6)
        # sessions rows carry lead usage; s2 has no tasks and no session_start but still shows up
        rows = {r["session_id"]: r for r in s.sessions(frm="7d")}
        self.assertAlmostEqual(rows["s1"]["lead_cost"], 26.2, places=6)
        self.assertEqual(rows["s1"]["lead_tokens"], ld["tokens"]["total"] - 1 * M)
        self.assertAlmostEqual(rows["s2"]["lead_cost"], 2.0, places=6)
        self.assertEqual(rows["s2"]["tasks"], 0)
        # tasks are untouched: no pseudo tasks
        self.assertEqual(s.tasks({})["total"], 1)

    def test_savings_math_unchanged_by_lead_usage(self):
        tasks = [start("a", at("2026-10-05", "09:00")), end("a", at("2026-10-05", "09:01"), tokens=toks(1000, 2000, 300, 400000)),
                 start("b", at("2026-10-05", "09:05"), tier="sonnet"),
                 end("b", at("2026-10-05", "09:06"), tier="sonnet", model="claude-sonnet-5-5", tokens=toks(10, 20, 30, 40))]
        write(self.home, "2026-10-05", tasks)
        before = self.store().overview("today")["totals"]
        write(self.home, "2026-10-05", [lead("2026-10-05", "10:00", tokens=toks(5 * M, 5 * M, 5 * M, 5 * M))])
        after = self.store().overview("today")["totals"]
        self.assertGreater(after["lead"]["cost"], 0)
        for k in ("cost", "baseline_cost", "saved", "saved_pct", "tokens", "tasks"):
            self.assertEqual(after[k], before[k], k)
        self.assertEqual(after["total_spend"], round(before["cost"] + after["lead"]["cost"], 6))

    def test_unknown_model_cost_null_but_tokens_counted(self):
        write(self.home, "2026-10-05", [
            lead("2026-10-05", "10:00", model="gpt-9", tokens=toks(100, 100, 0, 0)),
            lead("2026-10-05", "11:00", tokens=toks(M, 0, 0, 0))])
        o = self.store().overview("today")
        ld = o["totals"]["lead"]
        self.assertEqual(ld["unknown_cost"], 1)
        self.assertEqual(ld["tokens"]["total"], M + 200)
        self.assertAlmostEqual(ld["cost"], 4.0, places=6)
        self.assertIsNone(ld["by_model"]["gpt-9"]["pricing_key"])

    def test_no_lead_data(self):
        write(self.home, "2026-10-05", [start("a", at("2026-10-05", "09:00")), end("a", at("2026-10-05", "09:01"))])
        o = self.store().overview("today")
        ld = o["totals"]["lead"]
        self.assertEqual((ld["tokens"]["total"], ld["cost"], ld["turns"], ld["sessions"], ld["by_model"]), (0, 0.0, 0, 0, {}))
        self.assertEqual(o["totals"]["total_spend"], o["totals"]["cost"])
        self.assertEqual(o["daily"][0]["lead_cost"], 0.0)

    def test_project_filter_and_bad_lead_events(self):
        bad = lead("2026-10-05", "10:30")
        bad["tokens"] = "nope"
        write(self.home, "2026-10-05", [lead("2026-10-05", "10:00", project="a"),
                                        lead("2026-10-05", "11:00", project="b"), bad])
        s = self.store()
        self.assertEqual(s.bad_lines, 1)
        self.assertAlmostEqual(s.overview("today", project="a")["totals"]["lead"]["cost"], 6.0, places=6)
        self.assertAlmostEqual(s.overview("today")["totals"]["lead"]["cost"], 12.0, places=6)

    def test_incremental_refresh_picks_up_new_lead_events(self):
        write(self.home, "2026-10-05", [lead("2026-10-05", "10:00")])
        s = self.store()
        self.assertAlmostEqual(s.overview("today")["totals"]["lead"]["cost"], 6.0, places=6)
        write(self.home, "2026-10-05", [lead("2026-10-05", "11:00")])
        s.refresh()
        self.assertAlmostEqual(s.overview("today")["totals"]["lead"]["cost"], 12.0, places=6)


class LeadHookToStoreTest(LeadBase):
    """Real hook output (a message streamed across two flushes) read back by the store."""

    def test_split_message_totals_match_transcript(self):
        self.start()
        self.append(line("m1", OPUS, 3, 10, 1000, 500000), line("m1", OPUS, 3, 40, 1000, 500000))
        self.hook("Stop")
        self.append(line("m1", OPUS, 3, 120, 1000, 500000), line("m2", SONNET, 2, 7, 0, 100))
        self.hook("Stop")
        self.append(line("m3", OPUS, 1, 1, 1, 1))
        self.hook("SessionEnd")
        store = Store(self.home, Pricing(PRICING))
        store.refresh()
        o = store.overview("all")
        ld = o["totals"]["lead"]
        # truth: last line per id summed: m1 (3,120,1000,500000) + m3 (1,1,1,1) opus; m2 sonnet
        self.assertEqual(ld["tokens"], {"input": 3 + 1 + 2, "output": 120 + 1 + 7, "cache_creation": 1001,
                                        "cache_read": 500001 + 100, "total": 6 + 128 + 1001 + 500101})
        self.assertEqual(ld["turns"], 3)
        self.assertEqual(ld["sessions"], 1)
        want = Pricing(PRICING)
        opus = want.cost({"input": 4, "output": 121, "cache_creation": 1001, "cache_read": 500001}, "opus")["total"]
        sonnet = want.cost({"input": 2, "output": 7, "cache_creation": 0, "cache_read": 100}, "sonnet")["total"]
        self.assertAlmostEqual(ld["cost"], opus + sonnet, places=5)
        row = store.sessions()[0]
        self.assertEqual((row["session_id"], row["lead_tokens"]), ("sess-lead-1", ld["tokens"]["total"]))


if __name__ == "__main__":
    unittest.main()
