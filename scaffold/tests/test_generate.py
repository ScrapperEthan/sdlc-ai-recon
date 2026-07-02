import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from scaffold.generate import generate_service
from scaffold.reference import DEFAULTS, load_template


FIXTURES = Path(__file__).parent / "fixtures" / "mirror"


class ReferenceTemplateTests(unittest.TestCase):
    def test_load_template_from_fixture_mirror(self):
        template, found = load_template(str(FIXTURES))

        self.assertTrue(found)
        self.assertEqual(
            template["parent"],
            {
                "groupId": "com.hsbc.hase",
                "artifactId": "mc-hk-hase-api-parent",
                "version": "9.8.7",
            },
        )
        self.assertEqual(
            template["starter"],
            {
                "groupId": "com.hsbc.hase",
                "artifactId": "mc-hk-hase-api-starter",
                "version": "1.2.3",
            },
        )
        self.assertEqual(template["base_package"], "com.hsbc.hase")

    def test_load_template_falls_back_without_mirror(self):
        missing = FIXTURES.parent / "does-not-exist"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            template, found = load_template(str(missing))

        self.assertFalse(found)
        self.assertEqual(template, DEFAULTS)
        self.assertIn("NOTE:", out.getvalue())


class GenerateServiceTests(unittest.TestCase):
    def test_generated_pom_inherits_parent_and_omits_java_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_service(
                "payments",
                "com.hsbc.hase.payments",
                out_dir=os.path.join(tmp, "scratch"),
                mirror=str(FIXTURES),
            )
            target = Path(result["path"])
            pom = (target / "pom.xml").read_text(encoding="utf-8")
            review = (target / "REVIEW_DIFF.md").read_text(encoding="utf-8")

            self.assertIn("<parent>", pom)
            self.assertIn("<artifactId>mc-hk-hase-api-parent</artifactId>", pom)
            self.assertIn("<version>9.8.7</version>", pom)
            self.assertIn("<artifactId>mc-hk-hase-api-starter</artifactId>", pom)
            self.assertIn("<artifactId>spring-boot-maven-plugin</artifactId>", pom)
            self.assertNotIn("java.version", pom)
            self.assertNotIn("spring-boot.version", pom)
            self.assertIn("mc-hk-hase-api-parent/pom.xml:", review)
            self.assertIn("mc-hk-hase-api-starter/pom.xml:", review)
            self.assertIn("REVIEW_DIFF.md", review)

    def test_rejects_dot_dot_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                generate_service(
                    "payments",
                    "com.hsbc..hase",
                    out_dir=os.path.join(tmp, "scratch"),
                    mirror=str(FIXTURES),
                )
            self.assertFalse((Path(tmp) / "scratch" / "payments").exists())

    def test_generated_files_stay_inside_out_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "scratch"
            result = generate_service(
                "payments",
                "com.hsbc.hase.payments",
                out_dir=str(out_dir),
                mirror=str(FIXTURES),
            )

            self.assertEqual(
                os.path.commonpath([str(out_dir.resolve()), result["path"]]),
                str(out_dir.resolve()),
            )
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["scratch"])

    def test_rejects_path_like_service_name_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                generate_service(
                    "../escape",
                    "com.hsbc.hase.payments",
                    out_dir=os.path.join(tmp, "scratch"),
                    mirror=str(FIXTURES),
                )
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_rejects_path_like_reference_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                generate_service(
                    "payments",
                    "com.hsbc.hase.payments",
                    out_dir=os.path.join(tmp, "scratch"),
                    mirror=str(FIXTURES),
                    reference="../hase-mc-service",
                )
            self.assertFalse((Path(tmp) / "scratch" / "payments").exists())

    def test_force_does_not_delete_before_reference_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "scratch"
            result = generate_service(
                "payments",
                "com.hsbc.hase.payments",
                out_dir=str(out_dir),
                mirror=str(FIXTURES),
            )
            marker = Path(result["path"]) / "README.md"
            before = marker.read_text(encoding="utf-8")

            with self.assertRaises(ValueError):
                generate_service(
                    "payments",
                    "com.hsbc.hase.payments",
                    out_dir=str(out_dir),
                    force=True,
                    mirror=str(FIXTURES),
                    reference="../hase-mc-service",
                )

            self.assertEqual(marker.read_text(encoding="utf-8"), before)

    def test_rejects_output_inside_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = Path(tmp) / "mirror"
            mirror.mkdir()
            with self.assertRaises(ValueError):
                generate_service(
                    "payments",
                    "com.hsbc.hase.payments",
                    out_dir=str(mirror / "scratch"),
                    mirror=str(mirror),
                )
            self.assertFalse((mirror / "scratch").exists())


if __name__ == "__main__":
    unittest.main()
