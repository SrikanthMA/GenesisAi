# Genesis 3.4 - Business Automation

Genesis can now do office work for you: Excel, Word, e-mail, data extraction, research and ready-made workflows.
Everything runs through the existing Genesis tool system, router and permissions. Nothing is sent anywhere silently.

## How to use it
* **Left menu -> "Business automation"**: drop files on the page (or click to add), pick a tab, press the button.
  Progress shows UNDERSTAND -> PLAN -> EXECUTE -> VERIFY -> DELIVER, with a log. Press *Open result* / *Open folder*.
* **Or just say it** (voice, chat or CLI), e.g.:
  * "analyse sales.xlsx", "clean customers.csv", "sort sales.csv by amount", "merge these two sheets"
  * "make a sales report from sales.xlsx", "inventory report", "employee report", "convert report.xlsx to pdf"
  * "create an invoice for Acme: 3 chairs at 1200", "write a proposal for ...", "summarise this document"
  * "read these invoices", "write the minutes of this meeting", "extract the data from this pdf"
  * "triage my inbox", "which e-mails are unanswered", "draft replies to my urgent mail"
  * "research <topic> with sources", "compare X and Y"
  * "what did you do today" (task history), "open the business folder"
* Results are saved in `Documents\Genesis Business` (change with `GENESIS_BUSINESS_DIR`).

## What it does
| Area | Tools |
|---|---|
| Excel / CSV | analyse (quality, columns, stats), clean, edit (sort/filter/formula/pivot-style summaries), merge (append/join), convert (xlsx/csv/pdf), sheet management, ready reports (sales, inventory, employee, financial) with live formulas and charts |
| Word | create (report, proposal, invoice, quotation, minutes, letter, resume), headers/footers/page numbers/TOC, rewrite one section, summarise, compare two documents, export to PDF |
| E-mail | triage (category and priority), summaries, unanswered mail, deadlines, reply **drafts**, attachment saving |
| Extraction | PDF / Word / Excel / CSV / text / images (OCR if Tesseract is installed) -> fields, tables, invoices, meeting decisions and actions, with source reference |
| Research | search, read, summarise with citations; claims and figures not found in the sources are flagged |
| Workflows | invoice processing, employee management, sales analysis, meeting automation, research automation, inventory management |
| History | every job is logged (SQLite `business.db`): steps, files, warnings, model usage |

## Safety rules built in
* **Originals are never modified.** Output goes to a new file; existing files are never overwritten (numbered copies).
* **E-mail is read and draft-only.** There is no send or delete tool. Drafts go to your Drafts folder for you to review.
* **No invented values.** Totals are computed in Python, never by the model. Model answers are kept only if the value is
  in the source (or the operation passes column validation); otherwise they are dropped and marked
  "[not provided]" / unverified.
* **Document text is data, not instructions.** Instructions found inside files or e-mails are ignored.
* **Spreadsheet formula injection** is neutralised when external text is written into cells.
* **Paths** are restricted to your allowed folders; executables are never saved from e-mail.
* **Model use** goes through the normal Genesis router: local first; any external provider still needs your explicit
  permission (nothing changes there). Answers are cached 24 h; token usage is logged; one retry, then a clear error.
* No API keys in code; the Gmail connection keeps using your `.env`.

## Honest limits
* The Business page was compile-checked and exercised against stubs only; it has not been run inside a real Qt
  session by the developer. Report any layout problem.
* OCR needs Tesseract installed (not tested here). Gmail and web search were tested with fakes only.
* AI-written content (documents, research, reply drafts, comparisons) is only as good as your local model.
* Not included: Microsoft Graph, a FastAPI service, Excel-native pivot tables (pivots are summary sheets),
  scheduled workflows.

## Install
Run `Update_Genesis_3.4.bat`. It backs up, applies the hooks, offers optional packages
(reportlab, pdfplumber, openpyxl, python-docx, pypdf) and runs the tests.
Tests: `python -m unittest tests.test_v3_business`.
