import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from disklens import i18n, probes  # noqa: E402
import i18n_keys  # noqa: E402


class CatalogTest(unittest.TestCase):
    def test_catalog_complete(self):
        keys, missing, unused, bad, tags = i18n_keys.check()
        self.assertEqual(missing, [], "untranslated strings")
        self.assertEqual(bad, [], "placeholder mismatches")
        self.assertEqual(tags, [], "html tag mismatches")


class TrTest(unittest.TestCase):
    def tearDown(self):
        i18n.set_forced(None)

    def test_languages(self):
        i18n.set_forced("en")
        self.assertEqual(i18n.tr("Outros"), "Other")
        i18n.set_forced("es")
        self.assertEqual(i18n.tr("Outros"), "Otros")
        i18n.set_forced("pt")
        self.assertEqual(i18n.tr("Outros"), "Outros")

    def test_template_and_unknown(self):
        i18n.set_forced("en")
        self.assertEqual(i18n.tr("texto que não existe {x}", x=1), "texto que não existe 1")
        self.assertEqual(i18n.tr(""), "")

    def test_numbers(self):
        i18n.set_forced("en")
        self.assertEqual(i18n.num(1234.5), "1,234.5")
        i18n.set_forced("pt")
        self.assertEqual(i18n.num(1234.5), "1.234,5")

    def test_probe_texts_follow_language(self):
        item = probes._item("pagefile", ("Arquivo de paginação (memória virtual)", {}), 1 << 30, True,
                            ("Criado após uma tela azul. Pode ter o tamanho da RAM. Seguro de apagar.", {}))
        i18n.set_forced("en")
        out = probes.localize({"items": [item]})["items"][0]
        self.assertEqual(out["label"], "Page file (virtual memory)")
        i18n.set_forced("es")
        out = probes.localize({"items": [item]})["items"][0]
        self.assertEqual(out["label"], "Archivo de paginación (memoria virtual)")


if __name__ == "__main__":
    unittest.main()
