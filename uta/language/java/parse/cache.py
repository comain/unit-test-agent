import json
import hashlib
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import asdict
from uta.language.java.parse.models import ParseResult, ParsedSymbol, ExtractedCall, ExtractedHeritage, Annotation, Param

PARSE_CACHE_VERSION = "2026-04-23-v2"


class CacheManager:
    def __init__(self, cache_dir: str):
        self.cache_dir = Path(cache_dir)
        self.parsed_dir = self.cache_dir / "parsed"
        self.index_file = self.cache_dir / "parse_index.json"
        
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.parsed_dir.mkdir(parents=True, exist_ok=True)
        
        self.index = self._load_index()

    def _load_index(self) -> Dict[str, Dict[str, str]]:
        if self.index_file.exists():
            try:
                with open(self.index_file, "r") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except (OSError, json.JSONDecodeError):
                self._quarantine_corrupt_file(self.index_file)
        return {}

    def _save_index(self):
        tmp_file = self.index_file.with_suffix(self.index_file.suffix + ".tmp")
        with open(tmp_file, "w") as f:
            json.dump(self.index, f, indent=2)
        os.replace(tmp_file, self.index_file)

    def _quarantine_corrupt_file(self, path: Path):
        if not path.exists():
            return
        suffix = f".corrupt.{int(time.time() * 1000)}"
        try:
            path.rename(path.with_name(path.name + suffix))
        except OSError:
            try:
                path.unlink()
            except OSError:
                pass

    def get_hash(self, file_path: str) -> str:
        with open(file_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def get_parsed(self, file_path: str) -> Optional[ParseResult]:
        rel_path = str(file_path)
        content_hash = self.get_hash(file_path)
        
        entry = self.index.get(rel_path)
        if (
            entry
            and entry["content_hash"] == content_hash
            and entry.get("cache_version") == PARSE_CACHE_VERSION
        ):
            cache_file = self.parsed_dir / f"{content_hash}.json"
            if cache_file.exists():
                try:
                    with open(cache_file, "r") as f:
                        data = json.load(f)
                    return self._deserialize_parse_result(data)
                except (OSError, json.JSONDecodeError, KeyError, TypeError):
                    self._quarantine_corrupt_file(cache_file)
                    self.index.pop(rel_path, None)
                    self._save_index()
        return None

    def save_parsed(self, file_path: str, result: ParseResult):
        rel_path = str(file_path)
        content_hash = self.get_hash(file_path)
        
        cache_file = self.parsed_dir / f"{content_hash}.json"
        tmp_file = cache_file.with_suffix(cache_file.suffix + ".tmp")
        with open(tmp_file, "w") as f:
            json.dump(asdict(result), f, indent=2)
        os.replace(tmp_file, cache_file)
            
        self.index[rel_path] = {
            "content_hash": content_hash,
            "cache_version": PARSE_CACHE_VERSION,
            "parsed_at": "TODO",
            "cache_file": str(cache_file)
        }
        self._save_index()

    def _deserialize_parse_result(self, data: Dict[str, Any]) -> ParseResult:
        # Complex deserialization because asdict doesn't handle custom types automatically back
        symbols = []
        for s in data.get("symbols", []):
            annos = [Annotation(**a) for a in s.get("annotations", [])]
            params = [Param(**p) for p in s.get("params", [])]
            symbols.append(ParsedSymbol(
                kind=s["kind"],
                name=s["name"],
                fqn=s["fqn"],
                line=s["line"],
                modifiers=s.get("modifiers", []),
                annotations=annos,
                params=params,
                return_type=s.get("return_type"),
                parent_fqn=s.get("parent_fqn"),
                complexity=s.get("complexity"),
            ))
            
        calls = [ExtractedCall(**c) for c in data.get("calls", [])]
        heritage = [ExtractedHeritage(**h) for h in data.get("heritage", [])]
        
        return ParseResult(
            path=data["path"],
            package=data["package"],
            imports=data.get("imports", []),
            symbols=symbols,
            calls=calls,
            heritage=heritage,
            field_bindings=data.get("field_bindings", {})
        )
