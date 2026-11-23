"""Genesis 3.4 - Business Automation. Real files are written to a temp folder and re-opened; the AI model, the web and
Gmail are scripted fakes (no network)."""
import hashlib
import os
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from openpyxl import load_workbook

from genesis.v3 import business as B
from genesis.v3.biz import common, docs, excel, extract, mail, research
from genesis.v3.biz import table as T
from genesis.v3.biz import workflows as W
from genesis.v3.biz.common import BizError, Run

SALES = """Order ID,Date,Product,Region,Qty,Unit Price
1001,2026-01-05,Widget, North ,2,"1,200"
1002,2026-01-17,Gadget,South,1,850
1002,2026-01-17,Gadget,South,1,850
1003,05/02/2026,widget,South,5,1200
1004,2026-02-20,Gizmo,North,,400
,,,,,
1005,2026-03-02,Gadget,North,3,850
1006,2026-03-09,=cmd|'/c calc'!A1,East,1,99
"""


class FakeLLM:
    is_generative = True
    name = "fake"

    def __init__(self, reply="ok"):
        self.reply, self.calls = reply, []

    def chat(self, messages, **kw):
        self.calls.append(messages[-1]["content"])
        return self.reply(messages[-1]["content"]) if callable(self.reply) else self.reply


class Backend:
    def __init__(self):
        self.opened = []

    def open_path(self, p):
        self.opened.append(p)


class Gmail:
    def __init__(self, msgs):
        self.msgs, self.drafts, self.sent = msgs, [], []

    def search(self, query="", max_results=5):
        return [dict(m) for m in self.msgs][:max_results]

    def get_message(self, mid):
        m = next(x for x in self.msgs if x["id"] == mid)
        return {**m, "body": m.get("body", m["snippet"]), "message_id_header": "<x@y>"}

    def create_draft(self, to, subject, body, **kw):
        self.drafts.append({"to": to, "subject": subject, "body": body, **kw})
        return {"draft_id": f"d{len(self.drafts)}", "url": "https://mail.google.com/#drafts"}

    def send_draft(self, draft_id):  # must never be called by Business Automation
        self.sent.append(draft_id)

    def _request(self, method, path, **kw):
        if path == "/profile":
            return {"emailAddress": "me@example.com"}
        if path.startswith("/threads/"):
            tid = path.split("/")[-1]
            return {"messages": [{"id": m["id"], "payload": {"headers": [{"name": "From", "value": m["from"]}]}}
                                 for m in self.msgs if m["thread_id"] == tid] +
                                ([{"id": "mine", "payload": {"headers": [{"name": "From", "value": "Me <me@example.com>"}]}}]
                                 if tid == "t-answered" else [])}
        raise AssertionError(path)


class Ctx:
    def __init__(self, tmp, llm=None, gmail=None):
        self.llm, self.backend, self.gmail, self.tmp = llm, Backend(), gmail, Path(tmp)

        class Paths:
            data_dir = Path(tmp) / "data"

        class Cfg:
            paths = Paths()

        self.config = Cfg()

    def allowed_roots(self):
        return [self.tmp]


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def make_pdf(path, lines):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Courier", 10)
    y = 800
    for ln in lines:
        c.drawString(40, y, ln)
        y -= 16
    c.save()


INV_OK = ["Sharma Traders Pvt Ltd", "TAX INVOICE", "Invoice No: ST/2026/0412      Date: 14-Nov-2026", "Bill To: Genesis Labs", "",
          "Description            Qty    Rate      Amount", "Steel bolts M8          200    4.50      900.00",
          "Hex nuts M8             200    2.25      450.00", "Labour charges            3  500.00     1500.00", "",
          "Subtotal                               2,850.00", "GST @ 18%                                513.00",
          "Total                                  3,363.00", "Due date: 30 Nov 2026"]
INV_BAD = ["Invoice # 7781", "From: Delta Supplies", "Date: 2026-11-02", "Paper A4    10   250.00   2500.00",
           "Toner        2  1800.00   3700.00", "Total   6,200.00"]
NOTES = """Weekly sync - 12 Nov 2026
Attendees: Priya, Ravi Kumar, Anil
- We decided to move the launch to 5 December.
- Ravi will prepare the test report by Friday.
- Discussed the vendor delays at length and their impact on cost.
Action: update the budget sheet (owner: Anil) by 20 Nov 2026
Action: book the venue
Priya: I'll send the customer email tomorrow.
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gbiz"))
        self.out = self.tmp / "out"
        self.env = mock.patch.dict(os.environ, {"GENESIS_BUSINESS_DIR": str(self.out)})
        self.env.start()
        common.clear_attached()
        common._HIST.clear()
        self.ctx = Ctx(self.tmp)
        self.sales = self.tmp / "sales.csv"
        self.sales.write_text(SALES, encoding="utf-8")

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tool(self, name, **args):
        t = next(t for t in B.TOOLS if t.name == name)
        return t.run(args, self.ctx)


class TableEngine(Base):
    def test_read_profile_finds_real_problems(self):
        t = T.read_table(self.sales)
        self.assertEqual(t.headers, ["Order ID", "Date", "Product", "Region", "Qty", "Unit Price"])
        p = T.profile(t)
        self.assertEqual(p["duplicate_rows"], 1)
        self.assertEqual(p["empty_rows"], 1)
        issues = {(i["column"], i["issue"]) for i in p["issues"]}
        self.assertIn(("Product", "same value written differently"), issues)
        self.assertIn(("Region", "extra spaces"), issues)
        self.assertIn(("Qty", "missing values"), issues)

    def test_clean_never_invents_values(self):
        t, rep = T.clean(T.read_table(self.sales))
        self.assertEqual((rep["duplicates_removed"], rep["empty_rows_removed"]), (1, 1))
        self.assertEqual(len(t.rows), 6)
        gizmo = next(r for r in t.rows if r[2] == "Gizmo")
        self.assertIsNone(gizmo[4])  # the missing quantity stays missing
        self.assertEqual(t.rows[0][1], date(2026, 1, 5))
        self.assertEqual(t.rows[2][1], date(2026, 2, 5))  # 05/02/2026 read day-first
        self.assertEqual({r[2] for r in t.rows if str(r[2]).lower() == "widget"}, {"Widget"})

    def test_sort_filter_pivot_stats(self):
        t, _ = T.clean(T.read_table(self.sales))
        self.assertEqual(T.sort(t, "Unit Price", True).rows[0][5], 1200)
        self.assertEqual(len(T.filter_rows(t, "Qty > 1").rows), 3)
        self.assertEqual(len(T.filter_rows(t, "Region is north").rows), 3)
        self.assertEqual(len(T.filter_rows(t, "Date after 2026-02-01").rows), 4)
        self.assertEqual(len(T.filter_rows(t, "Qty is empty").rows), 1)
        pv = {r[0]: r[1] for r in T.pivot(t, "Region", "Qty").rows}
        self.assertEqual(pv, {"East": 1, "North": 5, "South": 6})
        self.assertEqual([r[0] for r in T.pivot(t, "Date", "Qty", by_month=True).rows], ["2026-01", "2026-02", "2026-03"])
        self.assertEqual(next(s for s in T.stats(t) if s["column"] == "Qty")["sum"], 12)
        with self.assertRaises(BizError):
            t.col("Profit")
        with self.assertRaises(BizError):
            T.pivot(t, "Region", "Qty", "magic")

    def test_formulas_are_live_and_checked(self):
        t, _ = T.clean(T.read_table(self.sales))
        t = T.add_formula_column(t, "Line Total", "Qty * Unit Price")
        out = self.tmp / "f.xlsx"
        T.save_workbook(out, [T.Sheet(t, "Data", totals=True)])
        ws = load_workbook(out)["Data"]
        self.assertEqual(ws["G2"].value, "=E2*F2")
        self.assertEqual(ws["G8"].value, "=SUM(G2:G7)")
        self.assertIsNone(ws["F8"].value)  # unit prices are not summed
        self.assertTrue(T.validate_workbook(out)["ok"])
        self.assertEqual(T.evaluate(t, "Qty * Unit Price", t.rows[0]), 2400)
        with self.assertRaises(BizError):
            T.add_formula_column(t, "X", "Qty * Profit")
        with self.assertRaises(BizError):
            T.formula_for(t, 'Qty * HYPERLINK("http://x")', 2)

    def test_data_cannot_become_a_formula(self):
        t = T.read_table(self.sales)
        out = self.tmp / "inj.xlsx"
        T.save_workbook(out, [T.Sheet(t, "Data")])
        vals = [c.value for row in load_workbook(out)["Data"].iter_rows() for c in row if isinstance(c.value, str)]
        self.assertTrue(any(v.startswith("'=cmd") for v in vals))
        self.assertFalse(any(v.startswith("=cmd") for v in vals))

    def test_validate_catches_broken_workbook(self):
        from openpyxl import Workbook

        wb = Workbook()
        wb.active["A1"], wb.active["A2"], wb.active["A3"] = 1, "=SUM(A1:A99)", "=SUM(A1:A3)"
        p = self.tmp / "bad.xlsx"
        wb.save(p)
        v = T.validate_workbook(p)
        self.assertFalse(v["ok"])
        self.assertTrue(any("outside" in x for x in v["problems"]) and any("itself" in x for x in v["problems"]))

    def test_merge_append_and_join(self):
        a = T.Table(["ID", "Name"], [[1, "A"], [2, "B"]], source="a.xlsx")
        b = T.Table(["id", "City"], [[2, "Pune"], [3, "Goa"]], source="b.xlsx")
        m = T.merge([a, b])
        self.assertEqual(m.headers, ["ID", "Name", "City", "Source"])
        self.assertEqual(len(m.rows), 4)
        j = T.merge([a, b], "join")
        self.assertEqual(j.rows, [[1, "A", None], [2, "B", "Pune"], [3, None, "Goa"]])


class ExcelJobs(Base):
    def test_analyse_tool_reports_duplicates_and_keeps_original(self):
        before = sha(self.sales)
        r = self.tool("business_excel_analyse", file=str(self.sales))
        self.assertTrue(r.success, r.message)
        self.assertIn("1 duplicate row", r.message)
        self.assertTrue(Path(r.data["path"]).is_file())
        self.assertEqual(sha(self.sales), before)
        self.assertEqual(self.ctx.backend.opened, [r.data["path"]])

    def test_clean_tool(self):
        r = self.tool("business_excel_clean", file="sales.csv") if False else self.tool("business_excel_clean", file=str(self.sales))
        self.assertTrue(r.success, r.message)
        wb = load_workbook(r.data["path"])
        self.assertEqual(wb.sheetnames, ["Cleaned", "Changes", "Removed duplicates"])
        self.assertEqual(wb["Cleaned"].max_row, 3 + 6)  # note + blank + header + 6 rows
        self.assertEqual(wb["Removed duplicates"].max_row, 2)
        self.assertIn("don't invent", r.message)

    def test_edit_from_plain_request(self):
        r = self.tool("business_excel_edit", file=str(self.sales),
                      request="remove duplicates then add a column Total = Qty * Unit Price then sort by Unit Price descending "
                              "then chart Qty by Region")
        self.assertTrue(r.success, r.message)
        self.assertEqual([o["op"] for o in r.data["ops"]], ["clean", "formula", "sort", "chart"])
        wb = load_workbook(r.data["path"])
        ws = wb.worksheets[0]
        self.assertEqual(ws["G1"].value, "Total")
        self.assertEqual(ws["F2"].value, 1200)
        self.assertEqual(ws["G2"].value, "=E2*F2")
        self.assertEqual(len(wb.worksheets[1]._charts), 1)

    def test_edit_refuses_what_it_does_not_understand(self):
        r = self.tool("business_excel_edit", file=str(self.sales), request="make it nicer")
        self.assertFalse(r.success)
        self.assertIn("Columns:", r.message)
        r = self.tool("business_excel_edit", file=str(self.sales), request="sort by Profit")
        self.assertFalse(r.success)
        self.assertIn("Profit", r.message)

    def test_model_plan_is_validated_against_real_columns(self):
        self.ctx.llm = FakeLLM('[{"op":"sort","by":"Profit"},{"op":"filter","condition":"Qty > 1"},{"op":"formula","name":"X","expression":"Qty*Nope"}]')
        ops = W.plan_excel(self.ctx, T.read_table(self.sales), "show me the big orders please")
        self.assertEqual(ops, [{"op": "filter", "condition": "Qty > 1"}])

    def test_merge_convert_sheets(self):
        other = self.tmp / "more.csv"
        other.write_text("Order ID,Product,Qty\n2001,Bolt,4\n", encoding="utf-8")
        r = self.tool("business_excel_merge", files=[str(self.sales), str(other)])
        self.assertTrue(r.success, r.message)
        self.assertEqual(r.data["rows"], 8)
        self.assertEqual(load_workbook(r.data["path"]).sheetnames[0], "Combined")
        r = self.tool("business_excel_convert", file=str(self.sales), to="xlsx")
        xlsx = Path(r.data["path"])
        self.assertEqual(load_workbook(xlsx).active["A2"].value, 1001)
        self.assertTrue(self.tool("business_excel_convert", file=str(xlsx), to="csv").data["path"].endswith(".csv"))
        pdf = self.tool("business_excel_convert", file=str(xlsx), to="pdf")
        self.assertTrue(Path(pdf.data["path"]).read_bytes().startswith(b"%PDF"))
        before = sha(xlsx)
        r = self.tool("business_excel_sheets", file=str(xlsx), add="Notes", rename_from="sales", rename_to="Orders")
        self.assertEqual(r.data["sheets"], ["Orders", "Notes"])
        self.assertEqual(sha(xlsx), before)
        r = self.tool("business_excel_sheets", file=str(xlsx), delete="sales")
        self.assertFalse(r.success)  # a workbook must keep one sheet
        self.assertFalse(self.tool("business_excel_merge", files=[str(self.sales)]).success)

    def test_sales_report_figures_match_the_data(self):
        r = self.tool("business_report", file=str(self.sales), kind="sales")
        self.assertTrue(r.success, r.message)
        # 2*1200 + 850 + 5*1200 + 3*850 + 99; the Gizmo row has no quantity and is excluded, and said so
        self.assertIn("11,899.00", r.message)
        self.assertIn("left out of totals", r.message)
        wb = load_workbook(r.data["path"])
        self.assertEqual(wb.sheetnames[:2], ["Summary", "Data"])
        self.assertTrue(any(ws._charts for ws in wb.worksheets))
        self.assertTrue(Path(r.data["doc"]).is_file())
        self.assertIn("Widget", docs.read(r.data["doc"])["text"])

    def test_commentary_with_an_invented_number_is_dropped(self):
        self.ctx.llm = FakeLLM("Sales were strong at 11,899.00 and will grow 37% next year.")
        self.assertIsNone(W._commentary(self.ctx, "sales", ["Total amount: 11,899.00"]))
        self.ctx.llm = FakeLLM("Total sales came to 11,899.00.")
        self.assertIn("11,899.00", W._commentary(self.ctx, "sales", ["Total amount: 11,899.00", "Records: 6"]))

    def test_inventory_and_employee_reports(self):
        inv = self.tmp / "stock.csv"
        inv.write_text("Item,Stock,Reorder Level,Unit Cost\nBolt,5,10,2.5\nNut,0,20,1\nGear,50,10,\nCog,-2,5,3\n", encoding="utf-8")
        r = self.tool("business_report", file=str(inv))
        self.assertTrue(r.success, r.message)
        self.assertEqual(r.data["kind"], "inventory")
        wb = load_workbook(r.data["path"])
        self.assertIn("Reorder list", wb.sheetnames)
        self.assertEqual([c.value for c in wb["Reorder list"][4]][:4], ["Bolt", 5, 10, 15])
        self.assertIn("negative stock", r.message)
        emp = self.tmp / "staff.csv"
        emp.write_text("Name,Department,Salary,Email\nA,HR,100,a@x.com\nB,HR,200,\nC,IT,300,c@x.com\n", encoding="utf-8")
        r = self.tool("business_report", file=str(emp))
        self.assertEqual(r.data["kind"], "employee")
        self.assertIn("B (Email)", r.message)
        self.assertIn("Missing information", load_workbook(r.data["path"]).sheetnames)


class Documents(Base):
    def test_invoice_totals_are_computed(self):
        r = self.tool("business_create_document", kind="invoice",
                      details="invoice no INV-7 for Beta Ltd: 3 widgets at 1200, 1 install at 500.50, 18% GST")
        self.assertTrue(r.success, r.message)
        self.assertIn("total 4,838.59", r.message)
        text = docs.read(r.data["path"])["text"]
        self.assertIn("4,838.59", text)
        self.assertIn("Beta Ltd", text)
        self.assertIn("seller not provided", r.message)  # said, not invented
        self.assertFalse(self.tool("business_create_document", kind="invoice", details="for someone").success)

    def test_written_documents_need_a_model_and_mark_gaps(self):
        r = self.tool("business_create_document", kind="proposal", topic="Website redesign for Acme")
        self.assertFalse(r.success)
        self.assertIn("AI model", r.message)
        self.ctx.llm = FakeLLM("We will deliver the work.\n- Price: [to be confirmed]")
        r = self.tool("business_create_document", kind="proposal", topic="Website redesign for Acme", pdf=True)
        self.assertTrue(r.success, r.message)
        d = docs.read(r.data["path"])
        self.assertIn("Pricing", [h for h, _ in d["sections"]])
        self.assertIn("[to be confirmed]", r.message)
        self.assertTrue(Path(r.data["pdf"]).read_bytes().startswith(b"%PDF"))

    def test_rewrite_and_compare_work_on_copies(self):
        a = self.tmp / "plan.docx"
        docs.build(a, {"title": "Plan", "sections": [{"heading": "Scope", "paragraphs": ["Budget is 5,000 for 3 sites."]},
                                                     {"heading": "Cost", "paragraphs": ["It costs 5."]}]})
        before = sha(a)
        self.ctx.llm = FakeLLM("The budget covers three sites.")
        r = self.tool("business_rewrite_section", file=str(a), section="scope", instruction="shorter")
        self.assertTrue(r.success, r.message)
        self.assertIn("5,000", r.message)  # the dropped figure is flagged
        self.assertEqual(sha(a), before)
        new = docs.read(r.data["path"])
        self.assertEqual(dict(new["sections"])["Scope"], "The budget covers three sites.")
        self.assertEqual(dict(new["sections"])["Cost"], "It costs 5.")
        c = self.tool("business_compare_documents", files=[str(a), r.data["path"]])
        self.assertTrue(c.success, c.message)
        self.assertEqual((c.data["changed"], c.data["added"], c.data["removed"]), (1, 0, 0))
        self.assertFalse(self.tool("business_rewrite_section", file=str(a), section="Nope").success)

    def test_summarise_without_a_model_says_so(self):
        p = self.tmp / "notes.txt"
        p.write_text("Genesis is a local assistant. " * 3 + "It processes invoices and spreadsheets on the user's own computer. "
                     "Nothing is sent to the cloud unless the user allows it. " * 2, encoding="utf-8")
        r = self.tool("business_summarise", files=[str(p)])
        self.assertTrue(r.success, r.message)
        self.assertIn("No AI model was available", r.message)
        self.ctx.llm = FakeLLM("- point one is here\n- point two is here")
        r = self.tool("business_summarise", files=[str(p)])
        self.assertEqual(r.data["method"], "AI model")


class Extraction(Base):
    def test_invoice_fields_items_and_arithmetic(self):
        p = self.tmp / "inv1.pdf"
        make_pdf(p, INV_OK)
        r = extract.invoice(extract.pages(p))
        self.assertEqual((r["vendor"], r["invoice_number"], r["invoice_date"], r["due_date"]),
                         ("Sharma Traders Pvt Ltd", "ST/2026/0412", "2026-11-14", "2026-11-30"))
        self.assertEqual((r["subtotal"], r["tax"], r["total"], len(r["items"])), (2850.0, 513.0, 3363.0, 3))
        self.assertTrue(r["valid"] and not r["missing"])
        self.assertTrue(all(i["source"] == "inv1.pdf p.1" for i in r["items"]))

    def test_invoice_workflow_flags_bad_arithmetic_and_unreadable_files(self):
        make_pdf(self.tmp / "inv1.pdf", INV_OK)
        make_pdf(self.tmp / "inv2.pdf", INV_BAD)
        (self.tmp / "inv3.pdf").write_bytes(b"not a pdf")
        r = self.tool("business_process_invoices", files=[str(self.tmp)])
        self.assertTrue(r.success, r.message)
        self.assertEqual((r.data["invoices"], r.data["ok"]), (3, 1))
        self.assertEqual(sorted(r.data["need_checking"]), ["inv2.pdf", "inv3.pdf"])
        self.assertEqual(r.data["total"], 9563.0)
        self.assertIn("Problems:", r.message)
        wb = load_workbook(r.data["path"])
        checks = [[c.value for c in row] for row in wb["Checks"].iter_rows(min_row=2)]
        self.assertTrue(any(c[2] == "MISMATCH" and "Toner" in c[1] for c in checks))
        self.assertEqual(wb["Line items"].max_row, 1 + 5)

    def test_model_values_not_in_the_document_are_dropped(self):
        text = "Contact Asha Rao at asha@example.com about order 55."
        self.ctx.llm = FakeLLM('{"vendor": "Asha Rao", "invoice_number": "INV-999"}')
        got = extract.llm_fields(self.ctx, text, {"vendor": "x", "invoice_number": "y"})
        self.assertEqual(got, {"vendor": "Asha Rao"})
        self.ctx.llm = FakeLLM('[{"Name": "Asha Rao", "City": "Mumbai"}, {"Name": "Made Up", "City": null}]')
        t, dropped = extract.records(self.ctx, text, ["Name", "City"], "a.txt")
        self.assertEqual(t.rows, [["Asha Rao", None, "a.txt"]])
        self.assertEqual(dropped, 2)

    def test_extract_tool_keeps_sources(self):
        make_pdf(self.tmp / "inv1.pdf", INV_OK)
        r = self.tool("business_extract", files=[str(self.tmp / "inv1.pdf")])
        self.assertTrue(r.success, r.message)
        self.assertEqual(r.data["counts"]["date"], 2)
        ws = load_workbook(r.data["path"])["Details found"]
        self.assertEqual(ws["D4"].value, "inv1.pdf p.1")

    def test_meeting_minutes(self):
        m = extract.meeting(NOTES)
        self.assertEqual(m["attendees"], ["Priya", "Ravi Kumar", "Anil"])
        self.assertEqual(m["decisions"], ["We decided to move the launch to 5 December."])
        self.assertEqual([(a["task"], a["owner"], a["due"]) for a in m["actions"]],
                         [("prepare the test report", "Ravi", "Friday"), ("update the budget sheet", "Anil", "20 Nov 2026"),
                          ("book the venue", None, None), ("send the customer email tomorrow", "Priya", "tomorrow")])
        r = self.tool("business_meeting_minutes", text=NOTES)
        self.assertTrue(r.success, r.message)
        text = docs.read(r.data["path"])["text"]
        self.assertIn("Not assigned", text)
        self.assertIn("No date given", text)
        self.assertIn("no owner", r.message)
        self.assertIn("owner to be confirmed", Path(r.data["email_draft"]).read_text(encoding="utf-8"))


SOURCES = [{"title": "Alpha report", "url": "https://a.example/1", "snippet": "s",
            "page_text": "The market grew 12% in 2025 to 500 million dollars according to the Alpha survey of 300 firms. " * 2},
           {"title": "Beta news", "url": "https://b.example/2", "snippet": "s",
            "page_text": "Beta says growth was only 9% and that the largest vendor holds 40% share of the market today. " * 2}]


class ResearchAndMail(Base):
    def research(self, report):
        self.ctx.llm = FakeLLM(lambda p: report if p.startswith("Write a") else "- a fact")
        with mock.patch("genesis.services.web.fetch_results", return_value=[dict(s) for s in SOURCES]), \
                mock.patch("genesis.services.web.read_pages", side_effect=lambda r, **kw: r):
            return self.tool("business_research", question="How big is the widget market?")

    def test_report_is_cited_and_checked(self):
        r = self.research("Executive summary\nThe market grew 12% in 2025 [1].\nFindings\nBeta reports 9% growth [2]. It will hit 750 "
                          "million soon [7].\nConflicting information\nSources disagree on growth: 12% [1] versus 9% [2].\n"
                          "Conclusion\nGrowth is positive [1, 2].")
        self.assertTrue(r.success, r.message)
        self.assertEqual(r.data["sources"], ["https://a.example/1", "https://b.example/2"])
        self.assertEqual(r.data["check"]["bad_citations"], [7])
        self.assertIn("750", r.data["check"]["unverified_numbers"])
        text = docs.read(r.data["path"])["text"]
        self.assertIn("[citation removed]", text)
        self.assertIn("https://b.example/2", text)
        self.assertIn("Conflicting information", text)
        self.assertIn("Unverified figures flagged: 750", r.message)

    def test_no_sources_means_no_report(self):
        self.ctx.llm = FakeLLM("anything")
        with mock.patch("genesis.services.web.fetch_results", return_value=[]):
            r = self.tool("business_research", question="How big is the widget market?")
        self.assertFalse(r.success)
        self.assertIn("won't make one up", r.message)
        self.assertEqual(list(self.out.glob("*.docx")), [])

    def test_comparison_cells_must_be_in_the_sources(self):
        self.ctx.llm = FakeLLM('{"Growth": "12%", "Founder": "Jane Roe"}')
        with mock.patch("genesis.services.web.fetch_results", return_value=[dict(SOURCES[0])]), \
                mock.patch("genesis.services.web.read_pages", side_effect=lambda r, **kw: r):
            res = research.comparison(self.ctx, ["Alpha", "Beta"], ["Growth", "Founder"])
        self.assertEqual([r[1:3] for r in res["rows"]], [["12%", "not found"]] * 2)

    MSGS = [{"id": "1", "thread_id": "t1", "subject": "URGENT: invoice overdue", "snippet": "Please confirm payment by Friday.",
             "from": "Asha <asha@client.com>", "from_name": "Asha", "from_email": "asha@client.com", "unread": True, "date": "d"},
            {"id": "2", "thread_id": "t2", "subject": "50% off sale ends today", "snippet": "unsubscribe here",
             "from": "Shop <news@shop.com>", "from_name": "Shop", "from_email": "news@shop.com", "unread": True, "date": "d"},
            {"id": "3", "thread_id": "t-answered", "subject": "Lunch?", "snippet": "Can you join on Tuesday?",
             "from": "Ravi <ravi@x.com>", "from_name": "Ravi", "from_email": "ravi@x.com", "unread": False, "date": "d"}]

    def test_triage_orders_by_priority(self):
        self.ctx.gmail = Gmail(self.MSGS)
        r = self.tool("business_email_triage")
        self.assertTrue(r.success, r.message)
        self.assertTrue(r.untrusted_content)
        self.assertTrue(r.message.splitlines()[1].startswith("1. [Urgent] URGENT: invoice overdue"))
        self.assertIn("[Low] 50% off", r.message)
        self.assertEqual([m["id"] for m in mail.unanswered(self.ctx)], ["1"])

    def test_drafts_are_saved_never_sent(self):
        self.ctx.gmail = Gmail(self.MSGS)
        self.ctx.llm = FakeLLM("Thank you, I will confirm the payment shortly.")
        r = self.tool("business_email_drafts")
        self.assertTrue(r.success, r.message)
        self.assertIn("nothing has been sent", r.message)
        self.assertEqual([d["to"] for d in self.ctx.gmail.drafts], [["asha@client.com"], ["ravi@x.com"]])
        self.assertTrue(self.ctx.gmail.drafts[0]["subject"].startswith("Re: "))
        self.assertEqual(self.ctx.gmail.drafts[0]["thread_id"], "t1")
        self.assertEqual(self.ctx.gmail.sent, [])
        self.assertFalse(any("send" in t.name or "delete" in t.name for t in B.TOOLS))

    def test_not_connected_is_reported(self):
        class Off(Gmail):
            def search(self, *a, **k):
                raise type("GoogleAuthError", (Exception,), {})("not connected")

        self.ctx.gmail = Off([])
        r = self.tool("business_email_triage")
        self.assertFalse(r.success)
        self.assertIn("isn't connected", r.message)


class Plumbing(Base):
    def test_history_log_and_usage(self):
        self.tool("business_excel_analyse", file=str(self.sales))
        self.tool("business_excel_edit", file=str(self.sales), request="make it nicer")
        runs = common.history(self.ctx).recent()
        self.assertEqual([r["status"] for r in runs], ["failed", "done"])
        self.assertEqual({s["stage"] for s in runs[1]["steps"]}, {"UNDERSTAND", "EXECUTE", "VERIFY", "DELIVER"})
        self.assertEqual(len(common.history(self.ctx).files()), 1)
        self.assertIn("1. excel edit", self.tool("business_history").message)
        self.ctx.llm = FakeLLM("hello")
        self.assertEqual(common.ask(self.ctx, "q"), "hello")
        self.assertEqual(common.ask(self.ctx, "q"), "hello")
        self.assertEqual(len(self.ctx.llm.calls), 1)  # second answer came from the cache
        self.assertEqual(common.history(self.ctx).usage()["cached"], 1)

    def test_model_failure_is_retried_then_reported(self):
        n = []

        def flaky(_p):
            n.append(1)
            if len(n) < 2:
                raise RuntimeError("timeout")
            return "fine"

        self.ctx.llm = FakeLLM(flaky)
        self.assertEqual(common.ask(self.ctx, "x", cache=False), "fine")
        self.ctx.llm = FakeLLM(lambda p: (_ for _ in ()).throw(RuntimeError("down")))
        with self.assertRaises(BizError):
            common.ask(self.ctx, "y", cache=False)

    def test_files_are_found_or_asked_about(self):
        roots = [self.tmp]
        self.assertEqual(common.resolve_files("sales.csv", "excel", roots=roots), [self.sales])
        self.assertEqual(common.resolve_files("the sales workbook", "excel", roots=roots), [self.sales])
        (self.tmp / "sales 2025.csv").write_text("a,b\n1,2\n")
        with self.assertRaises(BizError) as e:
            common.resolve_files("sales data", "excel", roots=roots)
        self.assertIn("More than one file", str(e.exception))
        with self.assertRaises(BizError):
            common.resolve_files("budget.xlsx", "excel", roots=roots)
        common.attach([str(self.sales)])
        self.assertEqual(common.resolve_files("this spreadsheet", "excel", roots=[]), [self.sales])

    def test_files_outside_allowed_folders_are_refused(self):
        other = Path(tempfile.mkdtemp(prefix="gother"))
        try:
            (other / "x.csv").write_text("a,b\n1,2\n")
            r = self.tool("business_excel_analyse", file=str(other / "x.csv"))
            self.assertFalse(r.success)
            self.assertIn("not allowed", r.message)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_results_never_overwrite(self):
        a = self.tool("business_excel_clean", file=str(self.sales)).data["path"]
        b = self.tool("business_excel_clean", file=str(self.sales)).data["path"]
        self.assertNotEqual(a, b)
        self.assertTrue(Path(a).is_file() and Path(b).is_file())

    def test_tools_are_registered_and_low_risk(self):
        from genesis.security.permissions import RiskLevel
        from genesis.tools.registry import build_default_registry

        reg = build_default_registry()
        for t in B.TOOLS:
            self.assertIsNotNone(reg.get(t.name), t.name)
            self.assertEqual(t.risk, RiskLevel.LOW)
            self.assertTrue(t.description and t.parameters["type"] == "object")


class Commands(unittest.TestCase):
    CASES = {
        "Genesis, analyse this Excel workbook and find duplicate entries.": ("business_excel_analyse", {}),
        "Create a professional financial report using this spreadsheet.": ("business_report", {"kind": "financial"}),
        "Combine these three Excel files into one workbook.": ("business_excel_merge", {"files": [], "mode": "append"}),
        "Read these invoices and extract the information into Excel.": ("business_process_invoices", {"files": []}),
        "Summarise these 20 PDF documents.": ("business_summarise", {"files": []}),
        "Prepare meeting minutes from these notes.": ("business_meeting_minutes", {}),
        "Research the top competitors in this industry and create a comparison report.":
            ("business_research", {"question": "the top competitors in this industry", "kind": "comparison report"}),
        "Find important emails from today and prepare response drafts.": ("business_email_drafts", {"query": "newer_than:1d"}),
        "Create a monthly sales dashboard.": ("business_report", {"kind": "sales"}),
        "sort sales.csv by Unit Price descending": ("business_excel_edit", {"request": "sort by Unit Price descending", "file": "sales.csv"}),
        "remove duplicates from sales.csv": ("business_excel_clean", {"file": "sales.csv"}),
        "export report.docx to pdf": ("business_to_pdf", {"file": "report.docx"}),
        "convert sales.csv to excel": ("business_excel_convert", {"file": "sales.csv", "to": "xlsx"}),
        "compare contract_v1.docx and contract_v2.docx": ("business_compare_documents", {"files": ["contract_v1.docx", "contract_v2.docx"]}),
        "find unanswered emails": ("business_email_triage", {"unanswered": True}),
        "rename sheet Sheet1 to Data in sales.xlsx": ("business_excel_sheets", {"file": "sales.xlsx", "rename_from": "Sheet1", "rename_to": "Data"}),
    }

    def test_the_prompt_examples_reach_the_right_tool(self):
        from genesis.nlp import rules

        for text, (tool, args) in self.CASES.items():
            res = rules.parse(text)
            self.assertIsNotNone(res, text)
            self.assertEqual((res.slots.get("tool"), res.slots.get("args")), (tool, args), text)

    def test_existing_commands_are_untouched(self):
        from genesis.nlp import rules

        for text in ("open chrome", "what's the weather", "create an excel sheet for my expenses", "summarise this",
                     "research quantum computing", "read my emails", "compare python and java", "set volume to 40"):
            res = rules.parse(text)
            self.assertFalse(res is not None and str(res.slots.get("tool", "")).startswith("business_"), text)


if __name__ == "__main__":
    unittest.main()
