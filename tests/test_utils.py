import json
import unittest

from manuloop.utils import extract_json, flatten_content


class UtilsTests(unittest.TestCase):
    def test_extract_json_from_markdown_and_jsonl(self):
        fence = chr(96) * 3
        fenced = (
            "respuesta:\n"
            + fence
            + "json\n"
            + '{"verdict": "PASS", "checks": []}'
            + "\n"
            + fence
        )
        self.assertEqual(extract_json(fenced)["verdict"], "PASS")

        jsonl = "\n".join(
            [
                json.dumps({"type": "progress", "value": 1}),
                json.dumps({"type": "result", "result": {"verdict": "FAIL"}}),
            ]
        )
        self.assertEqual(extract_json(jsonl)["type"], "result")

    def test_flatten_content_supports_provider_shapes(self):
        value = {"response": [{"text": "uno"}, {"content": "dos"}]}
        self.assertEqual(flatten_content(value), "uno\ndos")


if __name__ == "__main__":
    unittest.main()
