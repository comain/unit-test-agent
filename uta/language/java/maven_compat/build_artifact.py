"""Build the reviewed Java source and package its reproducible artifact digest."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parent
subprocess.run([sys.argv[1] if len(sys.argv) > 1 else "mvn", "-B", "-ntp", "-f", str(root / "pom.xml"), "clean", "package"], check=True)
target = root / "pit-runtime-compat.jar"
shutil.copyfile(root / "target/pit-runtime-compat-1.0.0.jar", target)
(root / "manifest.json").write_text(json.dumps({"version": "1.0.0", "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}, indent=2) + "\n")
