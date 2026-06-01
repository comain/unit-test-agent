from __future__ import annotations

class PythonVerificationRunner:
    language = "python"

    def __init__(self, verify_func=None):
        if verify_func is None:
            from uta.language.python.verification.runner import verify_python_target

            verify_func = verify_python_target
        self.verify_func = verify_func

    def verify(self, repo_path, target, **kwargs):
        return self.verify_func(repo_path, target, **kwargs)
