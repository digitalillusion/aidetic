"""python3 -m unittest discover -s tests -v   (from the repository root; writes only to a temporary HOME)"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent   # RULE.md and the docs
SRC = ROOT / "src"   # the modules, and hooks.json
os.environ["HOME"] = tempfile.mkdtemp()   # before ledger computes its paths
sys.path.insert(0, str(SRC))
import evalset       # noqa: E402
import judge         # noqa: E402
import ledger        # noqa: E402
import questions     # noqa: E402
import scans         # noqa: E402
import transcripts   # noqa: E402

CASE = "The treasurer sees the gate red on Monday (read: ci.log:12); objection: noise. "


def option(label, cost="Hard to undo: nothing.", preview=CASE, rec=False):
    o = {"label": label + (" (Recommended)" if rec else ""), "description": f"Do it. {cost}"}
    if preview is not None:
        o["preview"] = preview + ("Recommended: cheapest; wrong if the gate matters." if rec else "")
    return o


OTHER = {"label": "Other (describe in the note)", "description": "Something else",
         "preview": "Your note replaces the options above."}


def question(*opts, multi=False):
    return {"question": "Which way?", "header": "Way", "multiSelect": multi, "options": list(opts)}


def hook(cmd, payload, home, **env):
    r = subprocess.run([sys.executable, str(SRC / "choice_gate.py"), cmd], input=json.dumps(payload),
                       capture_output=True, text=True, env={**os.environ, "HOME": home, **env})
    return r.stdout.strip()


def events(home):
    path = Path(home) / ".claude/aidetic/events.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def failing(q):
    return {check.__name__: hits for check, hits in questions.findings([q])}


class Checks(unittest.TestCase):
    def test_compliant_question_passes(self):
        self.assertEqual(questions.findings([question(option("A", rec=True), option("B"), OTHER)]), [])

    def test_plain_and_multiselect_questions_are_exempt(self):
        plain = question(option("A", cost="", preview=None), option("B", cost="", preview=None))
        multi = question(option("A", cost="", rec=True), option("B", cost=""), multi=True)
        self.assertEqual(questions.findings([plain, multi]), [])

    def test_each_rule_names_what_is_missing(self):
        q = question(option("A", rec=True), option("B", cost=""), OTHER)
        self.assertEqual(failing(q), {"cost_hidden": {"Which way?": ["B"]}})
        q = question(option("A", rec=True), option("B", cost="", preview=CASE + "Hard to undo: nothing."), OTHER)
        self.assertEqual(failing(q), {})   # nothing to undo: said in the preview, kept out of the description
        q = question(option("A", rec=True), option("B", cost="", preview=CASE + "Hard to undo: the push."), OTHER)
        self.assertEqual(failing(q), {"cost_hidden": {"Which way?": ["B"]}})
        q = question(option("A", rec=True), option("B"))
        self.assertEqual(failing(q), {"no_other": {"Which way?": ["Other"]}})
        q = question(option("A", rec=True), option("B"), OTHER)
        q["options"][0]["preview"] = CASE * 2   # a case, and no word on why it is the pick
        self.assertEqual(failing(q), {"recommendation_unsaid": {"Which way?": ["A (Recommended)"]}})
        q = question(option("A", rec=True, preview=None), option("B", preview=None))
        self.assertEqual(set(failing(q)), {"without_case", "no_other"})


class Length(unittest.TestCase):
    def test_a_question_over_the_cap_is_denied(self):
        q = question(option("A", rec=True), option("B"), OTHER)
        self.assertNotIn("too_long", failing(q))
        long = question(option("A", rec=True, preview=CASE * 6), option("B", preview=CASE * 6), OTHER)
        self.assertIn("too_long", failing(long))
        self.assertEqual(questions.on_screen(q), questions.answer_rows([q], {"Which way?": "B"}, {}).__next__()[1]["chars"])


class Truth(unittest.TestCase):
    def test_a_read_tag_must_name_a_file_the_session_opened(self):
        q = question(option("A", rec=True, preview=CASE.replace("ci.log", "src/api.rs")), option("B"), OTHER)
        self.assertEqual(questions.cited(q["options"][0]), {"api.rs"})
        self.assertEqual(failing(q), {})
        self.assertEqual(questions.findings([q], unread={"api.rs"})[0][1], {"Which way?": ["A (Recommended) (api.rs)"]})

    def test_files_in_context_need_no_tool_call(self):
        self.assertEqual(questions.cited(option("A", preview="Seen (read: MEMORY.md, the index).")), set())

    def test_opened_counts_tool_calls_and_not_the_questions(self):
        fn = Path(tempfile.mkdtemp()) / "s.jsonl"
        read = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/repo/src/api.rs"}}]}}
        ask = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "AskUserQuestion", "input": {"q": "read: ci.log"}}]}}
        fn.write_text(json.dumps(read) + "\n" + json.dumps(ask) + "\n")
        self.assertEqual(transcripts.opened(fn, {"api.rs", "ci.log"}), {"api.rs"})
        self.assertEqual(transcripts.opened(fn.with_name("gone.jsonl"), {"ci.log"}), {"ci.log"})


class Answers(unittest.TestCase):
    def rows(self, answer, note=None):
        q = question(option("A", rec=True), option("B"), OTHER)
        ann = {"Which way?": {"notes": note}} if note else {}
        return [r for _, r in questions.answer_rows([q], {"Which way?": answer}, ann)][0]

    def test_pick_against_recommendation(self):
        self.assertTrue(self.rows("A (Recommended)")["follow"])
        self.assertFalse(self.rows("B")["follow"])
        self.assertTrue(self.rows("B", note="but smaller")["notes"])
        self.assertTrue(self.rows("my own way")["other"])
        self.assertTrue(self.rows("Other (describe in the note)")["other"])
        self.assertGreater(self.rows("B")["chars"], len(CASE) * 2)

    def test_reading_pace_counts_the_whole_call(self):
        import report
        two = [dict(id="a", tuid="t1", chars=1500, secs=100, follow=True, ans="A"),
               dict(id="b", tuid="t1", chars=1500, secs=100, follow=False, ans="B")]
        one = [dict(id="c", tuid="t2", chars=1500, secs=100, follow=True, ans="A")]
        self.assertEqual(report.on_screen(two + one), {"a": 3000, "b": 3000, "c": 1500})
        rows = dict(report.column(two + one, set(), {}, []))
        self.assertEqual(rows[f"  faster than {report.PACE} characters a second"], "1 (50%)")


class Reversals(unittest.TestCase):
    def transcript(self, *later):
        """An ask at 10:00 picking `vendor_dir`, then (minutes, type, text) lines."""
        ask = {"type": "tool_use", "name": "AskUserQuestion", "id": "tu1",
               "input": {"questions": [question(option("Keep `vendor_dir`"), option("B"))]}}
        lines = [{"type": "assistant", "timestamp": "2026-10-26T10:00:00Z", "message": {"content": [ask]}}]
        for minutes, kind, text in later:
            ts = f"2026-10-26T{10 + minutes // 60:02d}:{minutes % 60:02d}:00Z"
            content = text if kind == "user" else [{"type": "text", "text": text}]
            lines.append({"type": kind, "timestamp": ts, "message": {"content": content}})
        fn = Path(tempfile.mkdtemp()) / "s.jsonl"
        fn.write_text("".join(json.dumps(x) + "\n" for x in lines))
        answer = {"id": "a1", "tuid": "tu1", "q": "Which way?", "ans": "Keep `vendor_dir`"}
        return [how for _, how, _ in scans.reversals_in(transcripts.read_session(fn), [answer])]

    def test_user_undo_of_the_pick(self):
        self.assertEqual(self.transcript((5, "user", "remove the `vendor_dir`, it's horrible")), ["user undid"])

    def test_self_correction(self):
        self.assertEqual(self.transcript((9, "assistant", "I need to correct my own recommendation.")),
                         ["self-correction"])

    def test_recommendations_through_the_tool_or_in_prose(self):
        rec_ask = {"id": "x", "input": {"questions": [question(option("A", rec=True), option("B"))]}}
        session = [(1, "self", "Here are the numbers."), (2, "user", "ok"),
                   (3, "self", "My pick is B, since it keeps the field."), (4, "user", "ok"),
                   (5, "ask", rec_ask), (6, "user", "A"),
                   (7, "self", "I'd go with A."), (8, "ask", {"id": "y", "input": {"questions": []}}), (9, "user", "B")]
        self.assertEqual(list(scans.recommendations_in(session, after=0)), [(4, "prose"), (6, "tool")])
        self.assertEqual(list(scans.recommendations_in(session, after=4)), [(6, "tool")])

    def test_not_typed_by_the_user_or_too_late(self):
        handback = "Another Claude session sent a message: remove `vendor_dir`"
        self.assertEqual(self.transcript((5, "user", handback)), [])
        self.assertEqual(self.transcript((300, "user", "remove the `vendor_dir`")), [])
        self.assertEqual(self.transcript((5, "user", "keep `vendor_dir` as it is")), [])


class Judge(unittest.TestCase):
    def whole(self, q):
        """A reading that finds the rule's form whole."""
        return {"options": [dict(label=o["label"], free_text=o is OTHER, case=True, undo=True) for o in q["options"]],
                "recommended": "A (Recommended)", "reasoned": True, "read_files": []}

    def test_the_checks_decided_from_a_reading(self):
        q = question(option("A", rec=True), option("B"), OTHER)
        r = self.whole(q)
        self.assertEqual(judge.question_checks(q, r), {})
        r["options"][1]["undo"], r["reasoned"] = False, False
        self.assertEqual(judge.question_checks(q, r), {"cost_hidden": ["B"], "recommendation_unsaid": ["A (Recommended)"]})
        r = self.whole(q)
        r["options"][2]["free_text"], r["options"][0]["case"] = False, False
        self.assertEqual(judge.question_checks(q, r), {"no_other": ["Other"], "without_case": ["A (Recommended)"]})
        r["options"].pop()
        self.assertRaises(ValueError, judge.question_checks, q, r)

    def test_no_pick_and_no_previews_owes_nothing(self):
        q = question(option("A", cost="", preview=None), option("B", cost="", preview=None))
        r = self.whole(q)
        r.update(recommended=None, reasoned=False)
        r["options"][0]["undo"] = False
        self.assertEqual(judge.question_checks(q, r), {})
        self.assertTrue(judge.verdict("reply", {}, {"pick": True, "quote": "I'd go with B"}))

    def test_api_backends_ask_for_the_schema_and_return_the_reading(self):
        import types
        from unittest import mock
        seen = {}

        def create(**kw):
            seen.update(kw)
            usage = types.SimpleNamespace(input_tokens=10, output_tokens=2, prompt_tokens=10, completion_tokens=2)
            if "output_config" in kw:   # anthropic
                return types.SimpleNamespace(stop_reason="end_turn", model=kw["model"], usage=usage, content=[
                    types.SimpleNamespace(type="text", text='{"pick": true, "quote": "I\'d go with B"}')])
            msg = types.SimpleNamespace(content='{"pick": false, "quote": ""}', refusal=None)
            return types.SimpleNamespace(model=kw["model"], usage=usage,
                                         choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

        client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create),
                                       chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        sdks = {"anthropic": types.SimpleNamespace(Anthropic=lambda **kw: client),
                "openai": types.SimpleNamespace(OpenAI=lambda **kw: client)}
        case = {"input": {"reply": "I'd go with B."}}
        with mock.patch.dict(sys.modules, sdks):
            reading, meta = judge.read("reply", case, backend="anthropic")
            self.assertEqual(reading["pick"], True)
            self.assertEqual(seen["output_config"]["format"]["schema"], judge.SCHEMAS["reply"])
            self.assertEqual(seen["system"], judge.REPLY)
            reading, meta = judge.read("reply", case, backend="openai", model="local", base_url="http://x/v1")
            self.assertEqual(reading["pick"], False)
            self.assertEqual(seen["response_format"]["json_schema"]["schema"], judge.SCHEMAS["reply"])
            self.assertEqual(meta["tokens"], [10, 2])

    def test_without_a_config_the_judge_spends_no_key(self):
        self.assertEqual(judge.configured()["backend"], "cli")
        judge.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        try:
            judge.CONFIG.write_text('{"backend": "nope"}')
            self.assertRaises(ValueError, judge.configured)
            judge.CONFIG.write_text('{"backend": "anthropic", "model": "claude-haiku-5-5"}')
            self.assertEqual(judge.configured()["model"], "claude-haiku-5-5")
        finally:
            judge.CONFIG.unlink()

    def test_starter_cases_are_whole(self):
        cases = json.loads(evalset.STARTER.read_text())
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        for c in cases:
            self.assertIn(c["task"], judge.TASKS)
            self.assertIsInstance(c["fails"], bool)
            self.assertTrue(judge.render(c["task"], c))


class EvalSet(unittest.TestCase):
    def test_a_reply_case_at_each_stop_of_a_turn_that_asked_nothing(self):
        ask = {"id": "tu1", "input": {"questions": []}}
        session = [(1, "self", "I'd go with B."), (2, "stop", None), (3, "user", "ok"),
                   (4, "self", "My pick is A."), (5, "ask", ask), (6, "self", "I'd go with A."), (7, "user", "A")]
        cases = list(evalset.reply_cases("s", session))
        self.assertEqual([c["input"]["reply"] for c in cases], ["I'd go with B."])
        self.assertTrue(cases[0]["src"]["patterns"])

    def test_an_undone_case_holds_the_four_hours_after_the_answer(self):
        q = question(option("Keep `vendor_dir`"), option("B"))
        ask = {"id": "tu1", "input": {"questions": [q]}}
        answer = {"tool_use_id": "tu1", "answers": {"Which way?": "Keep `vendor_dir`"}, "annotations": {}}
        later = {"tool_use_id": "tu2", "questions": [question(option("Drop it"), option("Keep it"))],
                 "answers": {"Which way?": "Drop it"}}
        t0 = 1000.0
        session = [(t0 - 60, "ask", ask), (t0, "answer", answer), (t0 + 300, "user", "remove `vendor_dir`"),
                   (t0 + 600, "answer", later), (t0 + scans.WINDOW + 1, "self", "too late")]
        [case] = evalset.undone_cases("s", session, {})
        self.assertEqual(case["id"], "u" + ledger.event_id("tu1", "Which way?"))
        self.assertEqual([(e["min"], e["who"]) for e in case["input"]["after"]], [(5, "user"), (10, "asked")])
        self.assertEqual(case["input"]["after"][1]["picked"], "Drop it")
        self.assertIn("THE USER PICKED: Keep `vendor_dir`", judge.render("undone", case))

    def test_the_bars_are_the_sizes_of_the_design(self):
        self.assertGreater(evalset.bound(59, 59, upper=False), 0.95)   # 59 failures, none missed
        self.assertLess(evalset.bound(58, 59, upper=False), 0.95)
        self.assertLess(evalset.bound(0, 149, upper=True), 0.02)      # 149 passes, none flagged
        self.assertGreater(evalset.bound(0, 148, upper=True), 0.02)


    def test_measure_reports_each_bar_against_the_labels(self):
        import contextlib, io
        v = judge.version("reply")
        cases = [dict(id=f"r{i}", task="reply", lang="orig", split="measure", input=dict(reply="x")) for i in range(3)]
        labels = {"r0": True, "r1": False, "r2": False}
        readings = {c: [dict(fails=f), dict(fails=f)] for c, f in labels.items()}
        draws = {"r0": [True, True, True], "r1": [False, False, False], "r2": [True, False, False]}
        evalset.append(evalset.JUDGED, [dict(id=c, task="reply", draw=d, v=v, judge="cli:haiku:low", fails=f, secs=7.0)
                                        for c, fs in draws.items() for d, f in enumerate(fs)])
        ledger.emit(kind="judged", task="reply", case="r1", judge="cli:haiku:low", v=v, fails=True, secs=8.0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            evalset.report(cases, readings, {}, "cli:haiku:low", ["reply"])
        text = out.getvalue()
        self.assertIn("3 cases (1 failing, 2 passing)", text)
        self.assertIn("failures caught  3/3 draws", text)
        self.assertIn("passes flagged   1/6 draws", text)   # r2 flagged once in three
        self.assertIn("on 1/2 cases", text)               # a case flagged in any draw counts against
        self.assertIn("changed between draws  1/3", text)
        self.assertIn("1 verdicts, 1 on labelled measuring cases, 0 agree", text)
        evalset.JUDGED.unlink()


class Hook(unittest.TestCase):
    def pre(self, q, home):
        return hook("pre", {"session_id": "s", "tool_use_id": "t", "tool_input": {"questions": [q]}}, home)

    def test_denies_then_lets_the_third_attempt_through(self):
        home, q = tempfile.mkdtemp(), question(option("A", rec=True), option("B", cost=""), OTHER)
        self.assertIn("Hard to undo", self.pre(q, home))
        self.assertIn("Hard to undo", self.pre(q, home))
        self.assertEqual(self.pre(q, home), "")
        events = [json.loads(l) for l in (Path(home) / ".claude/aidetic/events.jsonl").read_text().splitlines()]
        self.assertEqual([e["kind"] for e in events], ["ask_denied", "ask_denied", "ask_escaped"])
        self.assertEqual(events[0]["checks"], ["cost_hidden"])

    def test_rule_reaches_a_session_older_than_it_once(self):
        home = tempfile.mkdtemp()
        transcript = Path(home) / "s.jsonl"
        transcript.write_text(json.dumps({"timestamp": "2000-01-01T00:00:00Z"}) + "\n")
        payload = json.dumps({"session_id": "s", "transcript_path": str(transcript)})
        run = lambda: subprocess.run([sys.executable, str(SRC / "choice_gate.py"), "prompt"], input=payload,
                                     capture_output=True, text=True, env={**os.environ, "HOME": home}).stdout
        context = json.loads(run())["hookSpecificOutput"]["additionalContext"]
        self.assertIn((ROOT / "RULE.md").read_text(), context)
        self.assertEqual(run(), "")
        transcript.write_text("")   # a new session's first prompt: it read the rule at start
        payload = json.dumps({"session_id": "new", "transcript_path": str(transcript)})
        self.assertEqual(run(), "")

    def test_a_prose_pick_goes_back_once_unless_the_turn_asked(self):
        home, pick = tempfile.mkdtemp(), "My pick is B: it keeps the field."
        stop = lambda text, active=False: hook("stop", {"session_id": "s", "last_assistant_message": text,
                                                        "stop_hook_active": active}, home)
        self.assertEqual(stop(pick), "")   # a turn the prompt hook never saw
        hook("prompt", {"session_id": "s"}, home)
        self.assertIn("AskUserQuestion", json.loads(stop(pick))["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(stop(pick, active=True), "")   # once, not again
        self.assertEqual(stop("Done: the tests pass."), "")
        self.pre(question(option("A", rec=True), option("B"), OTHER), home)
        self.assertEqual(stop(pick), "")   # the turn asked through the tool

    def test_a_correction_names_the_event_the_gate_named(self):
        home = tempfile.mkdtemp()
        reason = json.loads(self.pre(question(option("A", rec=True), option("B", cost=""), OTHER), home))
        eid = reason["hookSpecificOutput"]["permissionDecisionReason"].rsplit("gate event ", 1)[1].rstrip(")")
        run = lambda *args: subprocess.run([sys.executable, str(SRC / "choice_gate.py"), "correct", *args],
                                           capture_output=True, text=True, env={**os.environ, "HOME": home})
        self.assertEqual(run(eid, "B", "needs", "no", "cost").returncode, 0)
        self.assertNotEqual(run("zzzzzz", "wrong").returncode, 0)   # no such event: nothing recorded
        self.assertEqual(run("missed", "that was a pick", "--quote", "I'd go with B").returncode, 0)
        events = [json.loads(l) for l in (Path(home) / ".claude/aidetic/events.jsonl").read_text().splitlines()]
        fixes = [(e["of"], e["way"], e["words"], e["quote"]) for e in events if e["kind"] == "correction"]
        self.assertEqual(fixes, [(eid, "wrong", "B needs no cost", None),
                                 (None, "missed", "that was a pick", "I'd go with B")])

    def test_the_shadow_judges_each_turn_end_and_decides_nothing(self):
        home, bin_ = tempfile.mkdtemp(), Path(tempfile.mkdtemp())
        fake = bin_ / "claude"   # `claude -p` answering every reading with a pick
        fake.write_text("#!/bin/sh\ncat >/dev/null\necho '{\"structured_output\": {\"pick\": true, \"quote\": \"B\"}, "
                        "\"total_cost_usd\": 0, \"modelUsage\": {\"haiku\": {}}}'\n")
        fake.chmod(0o755)
        path = f"{bin_}:{os.environ['PATH']}"
        config = Path(home) / ".claude/aidetic/judge.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"backend": "cli", "model": "haiku", "shadow": true}')
        hook("prompt", {"session_id": "s"}, home)
        self.assertEqual(hook("stop", {"session_id": "s", "last_assistant_message": "Done."}, home, PATH=path), "")
        for _ in range(50):   # the judge runs detached: the hook has already returned
            judged = [e for e in events(home) if e["kind"] in ("judged", "judge_failed")]
            if judged:
                break
            time.sleep(0.1)
        self.assertEqual([(e["kind"], e["task"], e["fails"], e["patterns"]) for e in judged],
                         [("judged", "reply", True, False)])
        self.assertEqual(judged[0]["case"], judge.reply_id("s", "Done."))
        config.write_text('{"backend": "nope", "shadow": true}')   # a config it cannot read: the gate stands
        self.assertIn("AskUserQuestion", hook("stop", {"session_id": "s", "last_assistant_message": "My pick is B."}, home))

    def test_reply_installs_in_the_background_in_place_of_the_prompt_hook(self):
        home = tempfile.mkdtemp()
        settings = Path(home) / ".claude/settings.json"
        settings.parent.mkdir(parents=True)
        other = {"type": "command", "command": "python3 x.py stop"}
        gate = {"type": "command", "command": "python3 /g/choice_gate.py stop", "timeout": 5}
        old = {"type": "prompt", "prompt": "… then \"(gate event reply)\"; otherwise ok true.", "model": "m"}
        settings.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [other, gate]}, {"hooks": [old]}]}}))
        run = lambda *a: subprocess.run([sys.executable, str(SRC / "choice_gate.py"), *a], capture_output=True,
                                        text=True, env={**os.environ, "HOME": home}).stdout
        self.assertIn("installed", run("hook"))
        self.assertIn("no change", run("hook"))
        stops = json.loads(settings.read_text())["hooks"]["Stop"]
        self.assertEqual(stops[0], {"hooks": [other, gate]})   # the installer's own hooks stay
        self.assertEqual(len(stops), 2)   # the prompt hook is gone
        mine = stops[1]["hooks"][0]
        self.assertEqual((mine["command"], mine["asyncRewake"]), ("python3 /g/choice_gate.py reply", True))
        self.assertEqual(json.loads(settings.read_text())["model"], "opus")
        hook("prompt", {"session_id": "s"}, home)   # the patterns no longer send a pick back
        self.assertEqual(hook("stop", {"session_id": "s", "last_assistant_message": "My pick is B."}, home), "")
        self.assertIn("removed", run("hook", "--off"))
        self.assertEqual(json.loads(settings.read_text())["hooks"]["Stop"], [{"hooks": [other, gate]}])

    def test_the_judge_sends_a_prose_pick_back_once_by_waking_the_session(self):
        home, bin_ = tempfile.mkdtemp(), Path(tempfile.mkdtemp())
        fake = bin_ / "claude"
        answer = lambda pick, first="": fake.write_text(
            "#!/bin/sh\ncat >/dev/null\n" + first + "echo '" + json.dumps({"structured_output": {
                "pick": pick, "quote": "I would go with B" if pick else ""}, "total_cost_usd": 0,
                "modelUsage": {"sonnet": {}}}) + "'\n")
        answer(True)
        fake.chmod(0o755)
        config = Path(home) / ".claude/aidetic/judge.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"backend": "cli", "model": "opus", "decide": {"reply": {"model": "sonnet"}}}')

        def reply(sid="s", text="I'd go with B.", active=False):
            r = subprocess.run([sys.executable, str(SRC / "choice_gate.py"), "reply"], capture_output=True, text=True,
                               input=json.dumps({"session_id": sid, "last_assistant_message": text,
                                                 "stop_hook_active": active}),
                               env={**os.environ, "HOME": home, "PATH": f"{bin_}:{os.environ['PATH']}"})
            return r.returncode, r.stderr

        self.assertEqual(reply(), (0, ""))   # a turn the prompt hook never saw
        hook("prompt", {"session_id": "s"}, home)
        code, why = reply()
        self.assertEqual(code, 2)
        self.assertIn("“I would go with B”", why)
        sent = [e for e in events(home) if e["kind"] == "judge_sent_back"]
        self.assertIn(f"(gate event {sent[0]['id']})", why)
        judged = [e for e in events(home) if e["kind"] == "judged"]
        self.assertEqual([(e["task"], e["fails"], e["judge"]) for e in judged], [("reply", True, "cli:sonnet:None")])
        self.assertEqual(reply(active=True), (0, ""))   # the turn the wake started goes through
        subprocess.run([sys.executable, str(SRC / "choice_gate.py"), "correct", "reply", "that", "was", "fine"],
                       capture_output=True, text=True, env={**os.environ, "HOME": home})
        self.assertEqual([e["of"] for e in events(home) if e["kind"] == "correction"], [sent[0]["id"]])
        self.pre(question(option("A", rec=True), option("B"), OTHER), home)
        self.assertEqual(reply(), (0, ""))   # the turn asked through the tool
        hook("prompt", {"session_id": "s2"}, home)   # the user writes again while the judge reads
        answer(True, first=f"echo '{{\"session_id\": \"s2\"}}' | HOME={home} {sys.executable} {SRC / 'choice_gate.py'} prompt\n")
        self.assertEqual(reply("s2"), (0, ""))
        self.assertEqual([e["kind"] for e in events(home)][-2:], ["judged", "judge_late"])
        answer(False)
        hook("prompt", {"session_id": "s3"}, home)
        self.assertEqual(reply("s3", "Done: the tests pass."), (0, ""))
        self.assertEqual(len([e for e in events(home) if e["kind"] == "judge_sent_back"]), 1)

    def test_the_off_arm_runs_without_the_rule_or_the_gate(self):
        home = tempfile.mkdtemp()
        config = Path(home) / ".claude/aidetic/judge.json"
        config.parent.mkdir(parents=True)
        rule = (ROOT / "RULE.md").read_text()

        def start(sid, cwd="/home/u/p", source="startup"):   # what the session is handed
            out = hook("start", {"session_id": sid, "cwd": cwd, "source": source}, home)
            return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""

        config.write_text('{"off_arm": 1.0}')
        self.assertEqual(start("off"), "")
        self.assertEqual(start("off"), "")   # put in an arm once
        self.assertIn(rule, start("headless", cwd="/tmp/x"))   # outside the arms: the rule, no row
        self.assertIn(rule, start("older", source="resume"))   # a session from before the arms
        transcript = Path(home) / "old.jsonl"
        transcript.write_text(json.dumps({"timestamp": "2000-01-01T00:00:00Z"}) + "\n")
        self.assertEqual(hook("prompt", {"session_id": "off", "transcript_path": str(transcript)}, home), "")
        self.assertEqual(hook("pre", {"session_id": "off", "tool_use_id": "t", "tool_input": {
            "questions": [question(option("A", rec=True), option("B", cost=""))]}}, home), "")
        self.assertEqual(hook("stop", {"session_id": "off", "last_assistant_message": "My pick is B."}, home), "")
        r = subprocess.run([sys.executable, str(SRC / "choice_gate.py"), "reply"], capture_output=True, text=True,
                           input=json.dumps({"session_id": "off", "last_assistant_message": "My pick is B."}),
                           env={**os.environ, "HOME": home, "PATH": "/nowhere"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(hook("pre", {"session_id": "older", "tool_use_id": "t2", "tool_input": {
            "questions": [question(option("A", rec=True), option("B", cost=""))]}}, home), "")   # at 1, all are off
        config.write_text('{"off_arm": 1e-12}')
        self.assertIn(rule, start("on"))
        self.assertIn("Hard to undo", hook("pre", {"session_id": "older", "tool_use_id": "t3", "tool_input": {
            "questions": [question(option("A", rec=True), option("B", cost=""))]}}, home))
        config.write_text('{}')   # no arms: every session gets the rule, and none is recorded
        self.assertIn(rule, start("plain"))
        self.assertEqual([(e["kind"], e.get("arm"), e.get("sid")) for e in events(home)],
                         [("arm", "off", "off"), ("ask", "off", "off"), ("ask", "off", "older"), ("arm", "on", "on"),
                          ("ask_denied", None, "older")])

    def test_the_arms_are_set_side_by_side(self):
        import report
        self.assertEqual(report.fisher_lower(5, 5, 0, 10), 1.0)
        self.assertLess(report.fisher_lower(0, 10, 5, 5), 0.05)
        self.assertAlmostEqual(report.fisher_lower(2, 8, 2, 8), 3435 / 4845, places=6)

    def test_the_judge_decides_questions_and_the_patterns_stand_in_when_it_cannot(self):
        home, bin_ = tempfile.mkdtemp(), Path(tempfile.mkdtemp())
        q = question(option("A", rec=True), option("B"), OTHER)   # whole by the patterns' markers
        reading = {"options": [dict(label=o["label"], free_text=o is OTHER, case=o["label"] != "B", undo=True)
                               for o in q["options"]],
                   "recommended": "A (Recommended)", "reasoned": True, "read_files": ["ci.log"]}
        fake = bin_ / "claude"
        fake.write_text("#!/bin/sh\ncat >/dev/null\necho '" + json.dumps(
            {"structured_output": reading, "total_cost_usd": 0, "modelUsage": {"opus": {}}}) + "'\n")
        fake.chmod(0o755)
        path = f"{bin_}:{os.environ['PATH']}"
        config = Path(home) / ".claude/aidetic/judge.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"backend": "cli", "model": "opus", "decide": ["question"]}')
        pre = lambda: hook("pre", {"session_id": "s", "tool_use_id": "t", "tool_input": {"questions": [q]}}, home, PATH=path)
        reason = json.loads(pre())["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Missing previews", reason)   # the judge read B's preview as no case
        self.assertIn("“Which way?”: B", reason)
        kinds = [(e["kind"], e.get("by"), e.get("src")) for e in events(home)]
        self.assertEqual(kinds, [("judged", None, "decide"), ("ask_denied", "judge", "hook")])
        fake.write_text("#!/bin/sh\nexit 1\n")   # a judge that cannot answer: the patterns decide
        self.assertEqual(pre(), "")
        self.assertEqual(events(home)[-1]["by"], "patterns")

    def test_the_judge_reads_reversals_once_and_past_answers_keep_their_labels(self):
        home, bin_ = tempfile.mkdtemp(), Path(tempfile.mkdtemp())
        fake = bin_ / "claude"
        fake.write_text("#!/bin/sh\ncat >/dev/null\necho '" + json.dumps({"structured_output": {
            "undone": True, "by": "user", "evidence": "remove vendor_dir"}, "total_cost_usd": 0,
            "modelUsage": {"sonnet": {}}}) + "'\n")
        fake.chmod(0o755)
        base = Path(home) / ".claude"
        (base / "aidetic/eval").mkdir(parents=True)
        (base / "aidetic/judge.json").write_text('{"backend": "cli", "model": "opus", "decide": {"undone": {"model": "sonnet"}}}')
        q = question(option("Keep `vendor_dir`"), option("B"))
        lines = []
        for tu, minute in (("tu1", 0), ("tu2", 30)):   # two answered questions in one session
            lines += [{"type": "assistant", "timestamp": f"2026-10-26T10:{minute:02d}:00Z",
                       "message": {"content": [{"type": "tool_use", "name": "AskUserQuestion", "id": tu,
                                                "input": {"questions": [q]}}]}},
                      {"type": "user", "timestamp": f"2026-10-26T10:{minute + 1:02d}:00Z",
                       "toolUseResult": {"questions": [q], "answers": {"Which way?": "Keep `vendor_dir`"}},
                       "message": {"content": [{"type": "tool_result", "tool_use_id": tu, "content": "ok"}]}}]
        lines.append({"type": "user", "timestamp": "2026-10-26T11:00:00Z", "message": {"content": "remove `vendor_dir`"}})
        (base / "projects/-p").mkdir(parents=True)
        (base / "projects/-p/s1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        ids = [ledger.event_id(tu, "Which way?") for tu in ("tu1", "tu2")]
        (base / "aidetic/events.jsonl").write_text("".join(json.dumps(dict(
            t=f"2026-10-26T10:{m:02d}:30+00:00", kind="answer", phase="gate", sid="s1", tuid=tu, id=i,
            q="Which way?", ans="Keep `vendor_dir`")) + "\n" for tu, m, i in (("tu1", 1, ids[0]), ("tu2", 31, ids[1]))))
        v = judge.version("undone")   # the first answer is labelled undone already: no draw for it
        (base / "aidetic/eval/readings.jsonl").write_text("".join(json.dumps(dict(
            id="u" + ids[0], task="undone", draw=d, v=v, fails=True)) + "\n" for d in (1, 2)))
        run = lambda: subprocess.run([sys.executable, str(SRC / "choice_gate.py"), "reversals"], capture_output=True,
                                     text=True, env={**os.environ, "HOME": home, "PATH": f"{bin_}:{os.environ['PATH']}"})
        run()
        rev = [(e["id"], e["how"]) for e in events(home) if e["kind"] == "reversal"]
        self.assertEqual(sorted(rev), sorted([(ids[0], "label"), (ids[1], "judge")]))
        self.assertEqual([e["task"] for e in events(home) if e["kind"] == "judged"], ["undone"])
        run()   # each answer is read once
        self.assertEqual(len([e for e in events(home) if e["kind"] == "reversal"]), 2)

    def test_bad_input_fails_open(self):
        r = subprocess.run([sys.executable, str(SRC / "choice_gate.py"), "pre"], input="not json",
                           capture_output=True, text=True, env={**os.environ, "HOME": tempfile.mkdtemp()})
        self.assertEqual((r.returncode, r.stdout), (0, ""))


class Carrier(unittest.TestCase):
    """One carrier of the gate at a time: the settings hooks while settings.json names it, else the plugin."""
    MATCHER = "Edit|Write|NotebookEdit|MultiEdit|Bash"

    def write_watch(self, home, *acts):
        code = "import sys; sys.path.insert(0, sys.argv[1]); import choice_gate; print(choice_gate.write_watch(sys.argv[2:]))"
        return subprocess.run([sys.executable, "-c", code, str(SRC), *acts], capture_output=True, text=True,
                              env={**os.environ, "HOME": home}).stdout.strip()

    def settings(self, home, hooks=None):
        path = Path(home) / ".claude/settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if hooks is not None:
            path.write_text(json.dumps({"hooks": hooks}))
        return path

    def test_with_the_plugin_the_watched_points_go_to_a_file_and_settings_stay_untouched(self):
        home = tempfile.mkdtemp()
        settings = self.settings(home, {"Stop": [{"hooks": [{"type": "command", "command": "other.sh"}]}]})
        before = settings.read_text()
        watched = Path(home) / ".claude/aidetic/watched.json"
        self.assertEqual(self.write_watch(home, "change", "question", "command"), "True")
        self.assertEqual(json.loads(watched.read_text()), [self.MATCHER, "AskUserQuestion", "Bash"])
        self.assertEqual(self.write_watch(home, "change", "question", "command"), "False")   # nothing to change
        self.assertEqual(self.write_watch(home), "True")   # no habit watched: none
        self.assertEqual(json.loads(watched.read_text()), [])
        self.assertEqual(settings.read_text(), before)

    def test_with_the_settings_hooks_the_entries_stay_in_settings(self):
        home = tempfile.mkdtemp()
        stop = {"type": "command", "command": "python3 /g/choice_gate.py stop || true", "timeout": 5}
        settings = self.settings(home, {"Stop": [{"hooks": [stop]}]})
        self.write_watch(home, "command")
        pre = json.loads(settings.read_text())["hooks"]["PreToolUse"]
        self.assertEqual([(e["matcher"], e["hooks"][0]["command"]) for e in pre],
                         [("Bash", "python3 /g/choice_gate.py watch || true")])
        self.assertFalse((Path(home) / ".claude/aidetic/watched.json").exists())

    def test_the_reply_hook_is_the_plugins_when_settings_do_not_name_the_gate(self):
        home = tempfile.mkdtemp()
        r = subprocess.run([sys.executable, str(SRC / "choice_gate.py"), "hook"], capture_output=True, text=True,
                           env={**os.environ, "HOME": home})
        self.assertEqual(r.returncode, 1)
        self.assertIn("the plugin carries `reply`", r.stderr)
        self.assertFalse((Path(home) / ".claude/settings.json").exists())


class Install(unittest.TestCase):
    def run_install(self, home, *args):
        return subprocess.run([sys.executable, str(SRC / "choice_gate.py"), "install", *args], input="", capture_output=True,
                              text=True, env={**os.environ, "HOME": home}).stdout

    def test_one_command_puts_every_hook_in_once_and_keeps_the_installers_own(self):
        home = tempfile.mkdtemp()
        settings = Path(home) / ".claude/settings.json"
        settings.parent.mkdir(parents=True)
        other = {"type": "command", "command": "python3 x.py stop"}
        settings.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [other]}]}}))
        out = self.run_install(home)
        s = json.loads(settings.read_text())
        self.assertEqual((s["model"], s["hooks"]["Stop"][0]), ("opus", {"hooks": [other]}))
        gate = f"python3 {SRC / 'choice_gate.py'}"
        want = json.loads((SRC / "hooks.json").read_text().replace("python3 REPO/choice_gate.py", gate))["hooks"]
        for event, entries in want.items():
            for entry in entries:
                self.assertIn(entry, s["hooks"][event])
        self.assertEqual(sum(map(len, s["hooks"].values())), sum(map(len, want.values())) + 1)
        self.assertEqual((Path(home) / ".local/bin/aidetic").resolve(), SRC / "choice_gate.py")
        config = json.loads((Path(home) / ".claude/aidetic/judge.json").read_text())
        self.assertEqual((config["model"], config["decide"]["learn"]["model"]), ("opus", "sonnet"))
        self.assertIn("run `aidetic habits` once", out)   # no terminal: the first reading is left to the user
        before = settings.read_text()
        self.assertIn("hooks: all in place", self.run_install(home))
        self.assertEqual(settings.read_text(), before)

    def test_plugin_mode_leaves_settings_alone_and_does_the_rest(self):
        home = tempfile.mkdtemp()
        out = self.run_install(home, "--plugin")
        self.assertFalse((Path(home) / ".claude/settings.json").exists())
        self.assertEqual((Path(home) / ".local/bin/aidetic").resolve(), SRC / "choice_gate.py")
        self.assertEqual(json.loads((Path(home) / ".claude/aidetic/judge.json").read_text())["model"], "opus")
        self.assertIn("the plugin's own", out)

    def test_plugin_mode_says_when_settings_still_name_the_gate(self):
        home = tempfile.mkdtemp()
        settings = Path(home) / ".claude/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 /g/choice_gate.py stop"}]}]}}))
        before = settings.read_text()
        out = self.run_install(home, "--plugin")
        self.assertEqual(settings.read_text(), before)
        self.assertIn("the plugin stands down", out)

    def test_an_existing_setup_keeps_how_it_runs_the_gate_its_link_and_its_judge(self):
        home = tempfile.mkdtemp()
        base = Path(home) / ".claude"
        (base / "aidetic").mkdir(parents=True)
        stop = {"type": "command", "command": "python3 ~/shim/choice_gate.py stop || true", "timeout": 5}
        pre = {"type": "command", "command": "python3 ~/shim/choice_gate.py pre || true", "timeout": 5}   # an old timeout
        mine = {"type": "command", "command": "lint.sh"}
        old = {"type": "prompt", "prompt": "… then \"(gate event reply)\"; otherwise ok true."}
        (base / "settings.json").write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [old]}, {"hooks": [stop]}],
            "PreToolUse": [{"matcher": "AskUserQuestion", "hooks": [mine, pre]}]}}))
        (base / "aidetic/judge.json").write_text('{"backend": "anthropic"}')
        link = Path(home) / ".local/bin/aidetic"
        link.parent.mkdir(parents=True)
        link.symlink_to("/elsewhere/choice_gate.py")
        out = self.run_install(home)
        hooks = json.loads((base / "settings.json").read_text())["hooks"]
        self.assertEqual(hooks["Stop"][1], {"hooks": [stop]})   # in place, untouched
        self.assertEqual([h.get("command") for e in hooks["Stop"] for h in e["hooks"]],   # the prompt hook `reply`
                         ["python3 ~/shim/choice_gate.py reply", stop["command"],         # replaced, replaced where it was
                          "python3 ~/shim/choice_gate.py learn"])
        self.assertEqual(hooks["PreToolUse"], [{"matcher": "AskUserQuestion", "hooks": [mine, {**pre, "timeout": 90}]}])
        self.assertEqual(os.readlink(link), "/elsewhere/choice_gate.py")
        self.assertIn("is there already, to /elsewhere/choice_gate.py", out)
        self.assertEqual((base / "aidetic/judge.json").read_text(), '{"backend": "anthropic"}')


class Habits(unittest.TestCase):
    def test_each_tool_call_has_its_point_of_action(self):
        import habits
        calls = {"Edit": {}, "AskUserQuestion": {}, "mcp__claude_ai_Slack__slack_send_message": {},
                 "mcp__claude_ai_Slack__slack_read_channel": {}}
        self.assertEqual([habits.kind(dict(name=n, input=i)) for n, i in calls.items()],
                         ["change", "question", "message", "other"])
        shell = ["git push origin x", "cd w && rm -rf out", "gh pr comment 12 --body hi", "ls -la", "git log -3"]
        self.assertEqual([habits.kind(dict(name="Bash", input=dict(command=c))) for c in shell],
                         ["change", "change", "message", "command", "command"])

    def test_objections_are_grouped_and_priced_against_their_points_of_action(self):
        home, bin_ = tempfile.mkdtemp(), Path(tempfile.mkdtemp())
        calls = bin_ / "calls"
        items = [dict(i=1, kind="none", habit="", act="other", quote=""),
                 dict(i=2, kind="objection", habit="commits before the user says go", act="change",
                      quote="I did not ask you to commit"),
                 dict(i=3, kind="instruction", habit="commits before the user says go", act="change",
                      quote="do not commit anything until I say so")]   # no apostrophes: the fake echoes in quotes
        out = lambda reading: "'" + json.dumps({"structured_output": reading, "total_cost_usd": 0,
                                                 "modelUsage": {"haiku": {}}}) + "'"
        fake = bin_ / "claude"   # the grouping leaves every item out, so items group by their own names
        fake.write_text(f"#!/bin/sh\ncat >/dev/null\necho x >> {calls}\ncase \"$*\" in\n"
                        f"  *'objected to or forestalled'*) echo {out({'groups': []})};;\n  *) echo {out({'items': items})};;\nesac\n")
        fake.chmod(0o755)
        t0 = time.time() - 7200
        at = lambda m: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 + 60 * m))
        use = lambda name, **inp: {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "id": name + str(len(inp)), "input": inp}]}}
        lines = [{"type": "user", "message": {"content": "rename the helper in a.rs"}},
                 use("Edit", file_path="a.rs"), use("Bash", command="git commit -m rename"),
                 {"type": "assistant", "message": {"content": [{"type": "text", "text": "Renamed and committed."}]}},
                 {"type": "user", "message": {"content": "I didn't ask you to commit, undo it"}},
                 {"type": "user", "message": {"content": "Stop hook feedback: [x]: y"}},
                 use("mcp__claude_ai_Slack__slack_send_message", channel_id="C1", message="hi"),
                 {"type": "user", "message": {"content": "don't commit anything until I say so"}}]
        folder = Path(home) / ".claude/projects/-p"
        folder.mkdir(parents=True)
        (folder / "s1.jsonl").write_text("".join(json.dumps({**x, "timestamp": at(m)}) + "\n" for m, x in enumerate(lines)))
        run = lambda *a: subprocess.run([sys.executable, str(SRC / "choice_gate.py"), "habits", *a], capture_output=True,
                                        text=True, env={**os.environ, "HOME": home, "PATH": f"{bin_}:{os.environ['PATH']}"})
        r = run("--days", "1", "-j", "1")
        self.assertIn("3 messages typed (0 unread), 1 objections, 1 instructions", r.stdout)
        row = next(line for line in r.stdout.splitlines() if "commits before the user says go" in line)
        self.assertEqual(row.split()[-7:], ["change", "1", "1", "1.0", "100.0%", "worth", "measuring"])   # the one turn that changed
        self.assertIn("change 1, message 1, question 0, command 0, reply 3", r.stdout)   # in turns
        self.assertEqual(len(calls.read_text().split()), 2)   # one reading of the batch, one grouping
        run("--days", "1", "-j", "1")
        self.assertEqual(len(calls.read_text().split()), 2)   # nothing new: nothing drawn again
        shown = run("show", "1").stdout
        self.assertIn("I did not ask you to commit", shown)
        self.assertIn("instruction", shown)


def fake_judge(bin_, **readings):
    """A `claude` that answers each task of the judge with a fixed reading, known by its definition's words."""
    keys = {"correction": "You read messages a user typed", "match": "habits already known",
            "draft": "You write one rule", "watch": "about to take or has just taken",
            "habits": "Group them into habits"}
    out = lambda r: "'" + json.dumps({"structured_output": r, "total_cost_usd": 0, "modelUsage": {"sonnet": {}}}) + "'"
    cases = "".join(f"  *'{keys[t]}'*) echo {out(r)};;\n" for t, r in readings.items())
    fake = Path(bin_) / "claude"
    fake.write_text(f"#!/bin/sh\ncat >/dev/null\ncase \"$*\" in\n{cases}  *) exit 1;;\nesac\n")
    fake.chmod(0o755)
    return f"{bin_}:{os.environ['PATH']}"


class Loop(unittest.TestCase):
    NAME = "commits before the user says go"

    def setUp(self):
        self.home, self.bin = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.base = Path(self.home) / ".claude"
        (self.base / "aidetic/habits").mkdir(parents=True)
        (self.base / "aidetic/judge.json").write_text('{"backend": "cli", "model": "opus"}')
        stop = {"type": "command", "command": "python3 /g/choice_gate.py stop || true", "timeout": 5}
        (self.base / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [stop]}]}}))   # a settings install
        import loop
        h = loop.blank(self.NAME, "change")
        h["signals"] = [["old1", "objection", time.time() - 86400, "s0"]]
        self.k = loop.hid(self.NAME)
        (self.base / "aidetic/habits/live.json").write_text(json.dumps({"habits": {self.k: h}}))

    def run_gate(self, *args, payload=None, path=None):
        return subprocess.run([sys.executable, str(SRC / "choice_gate.py"), *args], capture_output=True, text=True,
                              input=json.dumps(payload) if payload is not None else None,
                              env={**os.environ, "HOME": self.home, "PATH": path or os.environ["PATH"]})

    def live(self):
        return json.loads((self.base / "aidetic/habits/live.json").read_text())

    def test_proposals_come_on_the_2nd_4th_8th_signal(self):
        import loop
        h = loop.blank("x", "change")
        due = []
        for n in range(1, 18):
            h["signals"].append([str(n), "objection", n, "s"])
            if loop.due(h):
                due.append(n)
                h["proposed"].append(n)
        self.assertEqual(due, [2, 4, 8, 16])
        h = loop.blank("x", "change")
        h["signals"] = [[str(n), "instruction", n, "s"] for n in range(31)]   # seeded past its 2nd
        self.assertTrue(loop.due(h))
        h["proposed"] = [31]
        h["signals"].append(["32", "objection", 32, "s"])
        self.assertFalse(loop.due(h))   # a catch-up at 31 is not asked again at 32
        h["signals"] += [[str(n), "objection", n, "s"] for n in range(33, 65)]
        self.assertTrue(loop.due(h))

    def test_a_rule_is_proposed_at_the_turn_end_kept_on_accept_and_handed_to_sessions(self):
        path = fake_judge(self.bin,
                          correction={"items": [dict(i=1, kind="instruction", habit=self.NAME, act="change",
                                                     quote="do not commit until I say so")]},
                          match={"items": []}, draft={"rule": "I commit only once the user has said to."})
        folder = self.base / "projects/-p"
        folder.mkdir(parents=True)
        t0 = time.time() - 60
        at = lambda s: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 + s))
        lines = [{"type": "assistant", "timestamp": at(0), "message": {"content": [
                     {"type": "tool_use", "name": "Bash", "id": "b1", "input": {"command": "git commit -m x"}}]}},
                 {"type": "user", "timestamp": at(10), "message": {"content": "do not commit until I say so"}}]
        transcript = folder / "s1.jsonl"
        transcript.write_text("".join(json.dumps(x) + "\n" for x in lines))
        (self.base / "aidetic/state.json").write_text(json.dumps({"turns": {"s1": t0 + 9}}))
        stop = dict(session_id="s1", transcript_path=str(transcript), cwd="/home/u/p", last_assistant_message="Ok.")
        r = self.run_gate("learn", payload=stop, path=path)
        self.assertEqual(r.returncode, 2)
        self.assertIn("I commit only once the user has said to.", r.stderr)
        self.assertIn(f"aidetic rule {self.k} accept", r.stderr)
        h = self.live()["habits"][self.k]
        self.assertEqual((len(h["signals"]), h["proposed"]), (2, [2]))
        self.assertEqual(self.run_gate("learn", payload=stop, path=path).returncode, 0)   # nothing new: quiet
        self.assertIn("kept", self.run_gate("rule", self.k, "accept").stdout)
        kept = (self.base / "aidetic/rules.md").read_text()
        self.assertIn("I commit only once the user has said to.", kept)
        settings = json.loads((self.base / "settings.json").read_text())
        watch = [e["matcher"] for e in settings["hooks"]["PreToolUse"]
                 if any(h["command"].endswith("watch || true") for h in e["hooks"])]
        self.assertEqual(watch, ["Edit|Write|NotebookEdit|MultiEdit|Bash"])
        (self.base / "aidetic/judge.json").write_text('{"backend": "cli", "model": "opus", "off_arm": 1}')
        handed = json.loads(self.run_gate("start", payload={"session_id": "n1", "cwd": "/home/u"}).stdout)
        context = handed["hookSpecificOutput"]["additionalContext"]
        self.assertIn("I commit only once the user has said to.", context)   # kept rules outlive the retired one
        self.assertNotIn("When the user must choose", context)
        self.run_gate("rule", self.k, "drop")
        self.assertFalse((self.base / "aidetic/rules.md").exists())
        self.assertEqual(json.loads((self.base / "settings.json").read_text())["hooks"]["PreToolUse"], [])

    def test_a_message_aidetic_habits_read_mid_turn_is_not_read_again(self):
        import habits
        path = fake_judge(self.bin, correction={"items": [dict(i=1, kind="instruction", habit=self.NAME, act="change",
                                                               quote="do not commit until I say so")]},
                          match={"items": []}, draft={"rule": "I commit only once the user has said to."})
        folder = self.base / "projects/-p"
        folder.mkdir(parents=True)
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        transcript = folder / "s1.jsonl"
        transcript.write_text(json.dumps({"type": "user", "timestamp": at,
                                          "message": {"content": "do not commit until I say so"}}) + "\n")
        m = habits.session(str(transcript), 0)[0]
        row = dict(id=m["id"], v=judge.version("correction"), judge="cli:sonnet:high", at=m["t"], sid="s1",
                   project="-p", kind="instruction", habit=self.NAME, act="change", quote=m["text"])
        readings = self.base / "aidetic/habits/readings.jsonl"
        readings.write_text(json.dumps(row) + "\n")
        live = self.live()
        live["habits"][self.k]["signals"].append([m["id"], "instruction", m["t"], "s1"])   # as `aidetic habits` puts it
        (self.base / "aidetic/habits/live.json").write_text(json.dumps(live))
        stop = dict(session_id="s1", transcript_path=str(transcript), cwd="/home/u/p", last_assistant_message="Ok.")
        r = self.run_gate("learn", payload=stop, path=path)
        self.assertEqual(r.returncode, 0, r.stderr)   # its proposal waits for the habit's next signal
        self.assertEqual(len(readings.read_text().splitlines()), 1)
        self.assertEqual(self.live()["read"]["s1"], m["t"])

    def test_the_guard_switches_on_its_bars_and_denies_once_on(self):
        import loop
        h = loop.blank(self.NAME, "change")
        h["rule"], h["guard"] = "I commit only once the user has said to.", "watch"
        events = [dict(kind="watched", habit=self.k, sid="s", turn=float(n), breaks=n < 3) for n in range(40)]
        h["signals"] = [[f"o{n}", "objection", n + 0.5, "s"] for n in range(3)]   # each right after a flagged turn
        self.assertTrue(loop.score(self.k, h, events)["pays"])
        events += [dict(kind="watched", habit=self.k, sid="s", turn=40.0 + n, breaks=True) for n in range(5)]
        s = loop.score(self.k, h, events)   # 5 wrong flags in 42: far over the bar
        self.assertEqual((s["turns"], s["objected"], s["pays"]), (45, 3, False))
        h["guard"] = "on"
        (self.base / "aidetic/habits/live.json").write_text(json.dumps({"habits": {self.k: h}}))
        path = fake_judge(self.bin, watch={"breaks": True, "why": "nobody said commit"})
        r = self.run_gate("watch", payload=dict(session_id="s", tool_name="Bash", tool_input={"command": "git commit -m x"}),
                          path=path)
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("nobody said commit", reason)
        e = reason.rsplit("gate event ", 1)[1].rstrip(")")
        self.assertEqual(self.run_gate("correct", e, "that", "commit", "was", "asked").returncode, 0)
        self.assertEqual(self.run_gate("watch", payload=dict(session_id="s", tool_name="Read",
                                                             tool_input={"file_path": "a"}), path=path).stdout, "")


if __name__ == "__main__":
    unittest.main()
