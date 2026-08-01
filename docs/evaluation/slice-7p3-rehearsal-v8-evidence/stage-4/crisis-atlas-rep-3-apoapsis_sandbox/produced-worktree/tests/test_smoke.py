import unittest

import crisis_atlas


class PackageSmokeTests(unittest.TestCase):
    def test_package_imports(self) -> None:
        self.assertEqual(crisis_atlas.__version__, "0.1.0")


if __name__ == "__main__":
    unittest.main()
