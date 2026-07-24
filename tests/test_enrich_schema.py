"""Schema-flexible MDC-sheet ingestion: column renames, brand-new flag columns, and missing
columns must all work WITHOUT editing enrich_repo_tags.py — the whole point of the codex seam.

These build real (stdlib-only) XLSX fixtures with arbitrary headers so the tests prove the
loader adapts to a sheet whose exact columns/values we were never told in advance."""
import os
import tempfile
import unittest
import zipfile
from xml.sax.saxutils import escape

import enrich_repo_tags


def build_xlsx(path, table, sheet_name="full Repository List"):
    """Write `table` (row 0 = headers) as a minimal XLSX with shared strings and sparse cells."""
    strings = []
    for row in table:
        for value in row:
            if value and value not in strings:
                strings.append(value)

    def cells(row_number, values):
        out = []
        for index, value in enumerate(values):
            if not value:  # blank cells are omitted in real XLSX; parser must keep column identity
                continue
            column = chr(ord("A") + index)
            out.append(f'<c r="{column}{row_number}" t="s"><v>{strings.index(value)}</v></c>')
        return f'<row r="{row_number}">{"".join(out)}</row>'

    shared = "".join(f"<si><t>{escape(value)}</t></si>" for value in strings)
    body = "".join(cells(i + 1, row) for i, row in enumerate(table))
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f'<sheetData>{body}</sheetData></worksheet>')
    workbook = ('<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/sharedStrings.xml",
                         f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{shared}</sst>')
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def _custom_schema(**overrides):
    schema = {key: value for key, value in enrich_repo_tags.DEFAULT_SCHEMA.items()}
    schema.update(overrides)
    return schema


class SchemaFlexibilityTests(unittest.TestCase):
    def _parse(self, table, schema):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sheet.xlsx")
            build_xlsx(path, table)
            return enrich_repo_tags.parse_sheet(path, schema)

    def test_default_schema_reads_v02_layout(self):
        table = [
            ["Repository", "MDC Common", "SMS", "EMAIL", "Others", "Remark",
             "Batch/Realtime(B/R)", "Maraketing/Servicing(M/S)", "TimeCritcal(Y/N)", "CMB/WPB"],
            ["mc-hk-hase-x", "Y", "Y", "", "", "secret note", "R", "S", "Y", "CMB"],
        ]
        mdc, rows = self._parse(table, enrich_repo_tags.DEFAULT_SCHEMA)
        entry = mdc["mc-hk-hase-x"]
        self.assertTrue(entry["mdc_common"])
        self.assertEqual(entry["channel_declared"], ["sms"])
        self.assertEqual(entry["mode_declared"], "realtime")
        self.assertEqual(entry["marketing_servicing"], "servicing")
        self.assertTrue(entry["time_critical"])
        self.assertEqual(entry["business_line"], "cmb")
        # Remark is sensitive -> never captured into flags/attrs.
        self.assertNotIn("remark", entry["attrs"])
        self.assertNotIn("remark", entry["flags"])
        # Generic flags still carry every yes/no column.
        self.assertTrue(entry["flags"]["sms"])
        self.assertFalse(entry["flags"]["email"])

    def test_renamed_columns_bind_via_schema_only(self):
        # The sheet renamed its columns; we adapt by editing the schema, NOT the code.
        table = [
            ["Repo Name", "Shared?", "Text Msg", "e-mail"],
            ["mc-hk-hase-y", "yes", "1", "n"],
        ]
        schema = _custom_schema(
            repository={"aliases": ["Repo Name"], "needles": ["repo"]},
            named_flags={"mdc_common": "Shared?"},
            channel_flags={"Text Msg": "sms", "e-mail": "email"},
        )
        mdc, _ = self._parse(table, schema)
        entry = mdc["mc-hk-hase-y"]
        self.assertTrue(entry["mdc_common"])
        self.assertEqual(entry["channel_declared"], ["sms"])  # "1" truthy, "n" falsey

    def test_unknown_flag_column_auto_captured_not_crashed(self):
        # A brand-new yes/no column the schema knows nothing about is still captured (as a flag),
        # and does NOT leak into channel_declared.
        table = [
            ["Repository", "SMS", "Urgent Escalation"],
            ["mc-hk-hase-z", "Y", "Y"],
        ]
        mdc, _ = self._parse(table, enrich_repo_tags.DEFAULT_SCHEMA)
        entry = mdc["mc-hk-hase-z"]
        self.assertTrue(entry["flags"]["urgentescalation"])
        self.assertEqual(entry["channel_declared"], ["sms"])
        self.assertNotIn("urgentescalation", entry["channel_declared"])

    def test_non_boolean_unknown_column_goes_to_attrs(self):
        table = [
            ["Repository", "SMS", "Priority Tier"],
            ["mc-hk-hase-p", "Y", "P1-High"],
        ]
        mdc, _ = self._parse(table, enrich_repo_tags.DEFAULT_SCHEMA)
        entry = mdc["mc-hk-hase-p"]
        self.assertEqual(entry["attrs"]["prioritytier"], "P1-High")
        self.assertNotIn("prioritytier", entry["flags"])

    def test_missing_channel_column_is_not_fatal(self):
        # No EMAIL column at all — the field is simply absent, and resolve_bindings reports it.
        table = [["Repository", "SMS"], ["mc-hk-hase-q", "Y"]]
        mdc, _ = self._parse(table, enrich_repo_tags.DEFAULT_SCHEMA)
        self.assertEqual(mdc["mc-hk-hase-q"]["channel_declared"], ["sms"])
        norm_to_col = {"repository": "A", "sms": "B"}
        unbound = enrich_repo_tags.resolve_bindings(norm_to_col, enrich_repo_tags.DEFAULT_SCHEMA)["unbound"]
        self.assertIn("channel:EMAIL", unbound)

    def test_repository_resolved_by_needle_when_alias_absent(self):
        table = [["Repository Identifier", "SMS"], ["mc-hk-hase-r", "Y"]]
        mdc, _ = self._parse(table, enrich_repo_tags.DEFAULT_SCHEMA)  # needles=["repo"] catches it
        self.assertIn("mc-hk-hase-r", mdc)

    def test_missing_repository_column_raises(self):
        table = [["Application", "SMS"], ["whatever", "Y"]]
        with self.assertRaises(ValueError):
            self._parse(table, enrich_repo_tags.DEFAULT_SCHEMA)

    def test_roster_lists_every_repo_the_sheet_names(self):
        table = [
            ["Repository", "SMS"],
            ["mc-hk-hase-b", "Y"],
            ["mc-hk-hase-a", ""],
            ["", "Y"],  # blank repo row is skipped
        ]
        mdc, _ = self._parse(table, enrich_repo_tags.DEFAULT_SCHEMA)
        roster = enrich_repo_tags.build_roster(mdc, "MDC_Repo_List_Analysis_v0.3.xlsx")
        self.assertEqual(roster["count"], 2)
        self.assertEqual(roster["repos"], ["mc-hk-hase-a", "mc-hk-hase-b"])
        self.assertEqual(roster["source"], "MDC_Repo_List_Analysis_v0.3.xlsx")

    def test_load_schema_missing_file_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = enrich_repo_tags.load_schema(os.path.join(tmp, "does-not-exist.json"))
            self.assertEqual(schema["channel_flags"], enrich_repo_tags.DEFAULT_SCHEMA["channel_flags"])

    def test_renamed_worksheet_tab_still_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sheet.xlsx")
            build_xlsx(path, [["Repository", "SMS"], ["mc-hk-hase-t", "Y"]], sheet_name="Renamed Tab")
            mdc, _ = enrich_repo_tags.parse_sheet(path, enrich_repo_tags.DEFAULT_SCHEMA)
            self.assertIn("mc-hk-hase-t", mdc)


if __name__ == "__main__":
    unittest.main()
