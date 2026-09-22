import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "tools" / "python-enforcement"
sys.path.insert(0, str(PACKAGE_ROOT))

from uta_py_enforce import mutation as lightweight_mutation  # noqa: E402


def test_read_mutmut_meta_skips_copied_tensorflow_checkpoint_files(tmp_path):
    mutants = tmp_path / "mutants"
    py_meta = mutants / "pkg" / "service.py.meta"
    py_meta.parent.mkdir(parents=True)
    py_meta.write_text(
        json.dumps({"exit_code_by_key": {"pkg.service.x_run__mutmut_1": 1}}),
        encoding="utf-8",
    )
    # Copied from aistore/gaia MTCNN models. Production CI
    # 0a2f342d872c489190c9660370a60243 crashed on this exact prefix:
    # UnicodeDecodeError: utf-8 can't decode byte 0xba in position 1.
    checkpoint = (
        mutants / "mtcnn" / "data" / "MTCNN_model" / "ONet_landmark" / "ONet-16.meta"
    )
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(bytes.fromhex("0aba451297450a2c0a03416273"))
    other_json = mutants / "notes.meta"
    other_json.write_text(
        json.dumps({"exit_code_by_key": {"should_not_count": 0}}),
        encoding="utf-8",
    )
    binary_py_meta = mutants / "pkg" / "broken.py.meta"
    binary_py_meta.write_bytes(bytes.fromhex("0aba4512"))
    (mutants / "pkg" / "list.py.meta").write_text("[]", encoding="utf-8")
    (mutants / "pkg" / "bad.py.meta").write_text("{not json", encoding="utf-8")

    counts, keys = lightweight_mutation.read_mutmut_meta(mutants)

    assert counts["killed"] == 1
    assert counts["survived"] == 0
    assert keys == ["pkg.service.x_run__mutmut_1"]
