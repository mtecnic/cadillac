"""Tests for PyTorch / GPU-ML language support (Phase 2)."""

import unittest

from cadillac.languages import (
    detect_language,
    pytorch_language,
    python_language,
)


class TestPytorchLanguage(unittest.TestCase):
    def test_shape(self):
        lang = pytorch_language()
        self.assertEqual(lang.name, "pytorch")
        # Family is python — same toolchain (pip, pytest, py_compile).
        # The richer guidance lives in quality.py, not in a new family.
        self.assertEqual(lang.family, "python")
        self.assertEqual(lang.entry_point, "train.py")
        # On-topic content
        self.assertIn(".to(device)", lang.coding_standards)
        self.assertIn("model.train()", lang.coding_standards)
        self.assertIn("overfit", lang.functional_test_guidance.lower())

    def test_explicit_hard_keyword(self):
        # PyTorch-specific keywords route directly.
        self.assertEqual(detect_language("Build a PyTorch image classifier").name,
                         "pytorch")
        self.assertEqual(detect_language("Fine-tune a transformer model with LoRA").name,
                         "pytorch")
        self.assertEqual(detect_language("a deep-learning training loop").name,
                         "pytorch")

    def test_torch_plus_ml_signal(self):
        # "torch" + ML soft signal = PyTorch.
        self.assertEqual(detect_language("Run inference on torch tensors").name,
                         "pytorch")
        self.assertEqual(detect_language("CUDA training script with gradient logging").name,
                         "pytorch")

    def test_torch_alone_is_not_pytorch(self):
        # A "torch app" or "flashlight" mention shouldn't route to pytorch.
        # The LLM would route it to python_language() through other rules,
        # so we just check it doesn't claim pytorch.
        self.assertNotEqual(
            detect_language("a torch app for the flashlight UI").name, "pytorch"
        )

    def test_python_still_routes_for_non_ml_python(self):
        self.assertEqual(detect_language("Build a Flask REST API").name, "python")
        self.assertEqual(detect_language("a CLI utility that parses logs").name, "python")


if __name__ == "__main__":
    unittest.main()
