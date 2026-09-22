"""Generated import hooks used by UTA's mutmut compatibility shim.

These snippets execute inside the generated ``sitecustomize.py`` process, not
inside UTA itself.  They intentionally depend on helper names established by
the surrounding compatibility source.
"""

TARGET_IMPORT_FINDER_SOURCE = r'''class _UtaTargetImportFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not CANONICAL_MODULE or fullname != CANONICAL_MODULE:
            return None
        redirected_location = _mutant_path_for(TARGET_REL)
        if not _is_target_path(redirected_location):
            return None
        try:
            if not Path(redirected_location).exists():
                return None
        except Exception:
            return None
        spec = _orig_spec_from_file_location(fullname, redirected_location)
        if spec is not None and spec.loader is not None:
            spec.loader._uta_canonical_name = fullname
        return spec


sys.meta_path.insert(0, _UtaTargetImportFinder())'''


MUTANTS_PACKAGE_ALIAS_FINDER_SOURCE = r'''class _UtaMutantsPackageAliasFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        repo = REPO_ROOT
        package_name = repo.name
        if not package_name.isidentifier():
            return None
        if fullname == package_name:
            init_path = repo / "__init__.py"
            search = [repo.as_posix()]
        elif fullname == package_name + ".mutants":
            init_path = repo / "mutants" / "__init__.py"
            search = [(repo / "mutants").as_posix()]
        else:
            return None
        if not init_path.exists():
            return None
        spec = importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        spec.origin = init_path.as_posix()
        spec.submodule_search_locations = search
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        origin = getattr(module.__spec__, "origin", None)
        module.__file__ = origin
        module.__path__ = list(module.__spec__.submodule_search_locations or [])
        _extend_mutant_package_to_real_siblings(module)


sys.meta_path.insert(0, _UtaMutantsPackageAliasFinder())'''


__all__ = ["MUTANTS_PACKAGE_ALIAS_FINDER_SOURCE", "TARGET_IMPORT_FINDER_SOURCE"]
