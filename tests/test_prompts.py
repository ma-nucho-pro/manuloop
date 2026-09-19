import unittest

from manuloop.prompts import portable_prompt


class PromptTests(unittest.TestCase):
    def test_portable_prompt_preserves_context_and_gates(self):
        prompt = portable_prompt(
            "Construye el juego",
            "arranque, controles y FPS medidos",
            "game",
        )
        self.assertIn("Construye el juego", prompt)
        self.assertIn("arranque, controles y FPS medidos", prompt)
        self.assertIn("fan-out", prompt)
        self.assertIn("juez independiente", prompt)
        self.assertIn("BLOCKED", prompt)


if __name__ == "__main__":
    unittest.main()
